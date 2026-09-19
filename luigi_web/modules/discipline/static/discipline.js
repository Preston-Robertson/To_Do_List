(() => {
  "use strict";

  const root = document.querySelector("[data-discipline-workspace]");
  if (!root) return;

  const STORAGE_KEY = "luigi.discipline.layout";
  const VERSION = 1;
  const MAX_IDS = 500;
  const MAX_ID_LENGTH = 128;
  const list = root.querySelector("[data-discipline-list]");
  const cards = Array.from(list.querySelectorAll("[data-discipline-card]"));
  const byId = new Map(cards.map((card) => [card.dataset.disciplineUuid, card]));
  const naturalOrder = cards.map((card) => card.dataset.disciplineUuid);
  const search = root.querySelector("[data-discipline-search]");
  const category = root.querySelector("[data-discipline-category-filter]");
  const targetFilter = root.querySelector("[data-discipline-target-filter]");
  const organize = root.querySelector("[data-discipline-organize]");
  const layoutStatus = root.querySelector("[data-discipline-layout-status]");
  const progressError = root.querySelector("[data-discipline-progress-error]");
  const progressMessage = root.querySelector("[data-discipline-progress-message]");
  const refreshButton = root.querySelector("[data-discipline-refresh-progress]");
  let statusFilter = "active";
  let progressController = null;
  let progressSequence = 0;
  let refreshQueued = false;

  function validId(value) {
    return typeof value === "string" && value.length > 0 && value.length <= MAX_ID_LENGTH
      && !/[\u0000-\u001f\u007f]/.test(value);
  }

  function validIds(values) {
    return Array.isArray(values) && values.length <= MAX_IDS
      && values.every(validId) && new Set(values).size === values.length;
  }

  function validLayout(value) {
    return value !== null && typeof value === "object" && value.version === VERSION
      && Object.keys(value).every((key) => ["version", "order", "pinned"].includes(key))
      && validIds(value.order) && validIds(value.pinned)
      && value.pinned.every((uuid) => value.order.includes(uuid));
  }

  function announceLayout(message, error = false) {
    layoutStatus.textContent = message;
    layoutStatus.dataset.layoutError = String(error);
    layoutStatus.hidden = false;
  }

  function defaultLayout() {
    return { version: VERSION, order: naturalOrder.slice(), pinned: [] };
  }

  function loadLayout() {
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      if (raw === null) return defaultLayout();
      if (raw.length > 150000) throw new Error("Invalid layout");
      const saved = JSON.parse(raw);
      if (!validLayout(saved)) throw new Error("Invalid layout");
      const missing = naturalOrder.filter((uuid) => !saved.order.includes(uuid));
      return { version: VERSION, order: saved.order.concat(missing), pinned: saved.pinned.slice() };
    } catch {
      announceLayout("Saved order and pins are unavailable. Using the default order.", true);
      return defaultLayout();
    }
  }

  let layout = loadLayout();

  function saveLayout() {
    if (!validLayout(layout)) {
      announceLayout("Order and pins could not be saved. This browser supports up to 500 habit IDs.", true);
      return;
    }
    try {
      const serialized = JSON.stringify({ version: VERSION, order: layout.order, pinned: layout.pinned });
      window.localStorage.setItem(STORAGE_KEY, serialized);
      if (window.localStorage.getItem(STORAGE_KEY) !== serialized) throw new Error("Unverified layout");
      announceLayout("Order and pins saved in this browser.");
    } catch {
      announceLayout("Order and pins were not confirmed saved. Changes apply here, but may not persist.", true);
    }
  }

  function orderedCards() {
    const ranks = new Map(layout.order.map((uuid, index) => [uuid, index]));
    const pinned = new Set(layout.pinned);
    return cards.slice().sort((left, right) => {
      const leftId = left.dataset.disciplineUuid;
      const rightId = right.dataset.disciplineUuid;
      return Number(pinned.has(rightId)) - Number(pinned.has(leftId))
        || ranks.get(leftId) - ranks.get(rightId);
    });
  }

  function visiblePeers(card) {
    const isPinned = layout.pinned.includes(card.dataset.disciplineUuid);
    return orderedCards().filter((candidate) => !candidate.hidden
      && layout.pinned.includes(candidate.dataset.disciplineUuid) === isPinned);
  }

  function centerToday(visibleCards, focus = false) {
    requestAnimationFrame(() => {
      let focused = false;
      visibleCards.forEach((card) => {
        if (card.hidden) return;
        const heatmap = card.querySelector(".heatmap");
        const today = heatmap?.querySelector("[data-current-day]");
        if (!today) return;
        const heatmapBounds = heatmap.getBoundingClientRect();
        const todayBounds = today.getBoundingClientRect();
        heatmap.scrollLeft += todayBounds.left - heatmapBounds.left
          - (heatmap.clientWidth - todayBounds.width) / 2;
        if (focus && !focused) {
          today.focus({ preventScroll: true });
          focused = true;
        }
      });
    });
  }

  function applyFilters() {
    const query = search.value.trim().toLocaleLowerCase();
    const newlyVisible = [];
    let visibleCount = 0;
    cards.forEach((card) => {
      const active = card.dataset.disciplineActive === "true";
      const name = card.querySelector("[data-discipline-name]").textContent;
      const categoryName = card.dataset.disciplineCategory || "";
      const statusMatches = statusFilter === "all" || active === (statusFilter === "active");
      const targetMatches = targetFilter.value === "all"
        || (card.dataset.weeklyAvailable === "true"
          && (card.dataset.weeklyMet === "true") === (targetFilter.value === "met"));
      const visible = statusMatches && targetMatches
        && (!category.value || categoryName === category.value)
        && `${name} ${categoryName}`.toLocaleLowerCase().includes(query);
      if (visible && card.hidden) newlyVisible.push(card);
      card.hidden = !visible;
      if (visible) visibleCount += 1;
    });
    root.querySelectorAll("[data-discipline-status]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.disciplineStatus === statusFilter));
    });
    cards.forEach((card) => {
      card.querySelector("[data-discipline-reorder]").hidden = !organize.checked;
      const peers = visiblePeers(card);
      const index = peers.indexOf(card);
      card.querySelector('[data-discipline-move="up"]').disabled = index <= 0;
      card.querySelector('[data-discipline-move="down"]').disabled = index < 0 || index === peers.length - 1;
    });
    const count = root.querySelector("[data-discipline-result-count]");
    count.textContent = `${visibleCount} of ${cards.length} habits`;
    count.hidden = cards.length === 0;
    root.querySelector("[data-discipline-filter-empty]").hidden = visibleCount > 0 || cards.length === 0;
    centerToday(newlyVisible);
  }

  function renderLayout() {
    orderedCards().forEach((card) => {
      const pinned = layout.pinned.includes(card.dataset.disciplineUuid);
      card.classList.toggle("is-pinned", pinned);
      const pin = card.querySelector("[data-discipline-pin]");
      const name = card.querySelector("[data-discipline-name]").textContent;
      pin.setAttribute("aria-pressed", String(pinned));
      pin.setAttribute("aria-label", `${pinned ? "Unpin" : "Pin"} ${name}`);
      pin.title = pinned ? "Unpin habit" : "Pin habit";
      card.querySelectorAll("[data-discipline-move]").forEach((button) => {
        const direction = button.dataset.disciplineMove;
        const group = pinned ? "pinned" : "unpinned";
        button.title = `Move ${direction} within ${group} habits`;
        button.setAttribute("aria-label", `Move ${name} ${direction} within ${group} habits`);
      });
      list.append(card);
    });
    applyFilters();
  }

  function setDailyStreak(card, streak) {
    const label = card.querySelector("[data-discipline-daily-streak]");
    if (!label) return;
    label.textContent = streak === null ? "Daily streak unavailable."
      : `Daily streak: ${streak} ${streak === 1 ? "day" : "days"} (current)`;
  }

  function nonnegativeInteger(value) {
    return Number.isSafeInteger(value) && value >= 0;
  }

  function validWeekly(weekly) {
    return weekly !== null && typeof weekly === "object"
      && nonnegativeInteger(weekly.count) && Number.isInteger(weekly.target)
      && weekly.target >= 1 && weekly.target <= 7
      && weekly.remaining === Math.max(0, weekly.target - weekly.count)
      && weekly.target_met === (weekly.count >= weekly.target);
  }

  function validDate(value) {
    return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)
      && Number.isFinite(Date.parse(`${value}T00:00:00Z`));
  }

  function validateProgress(payload) {
    if (!payload || !validDate(payload.today) || !validDate(payload.week_start)
      || !validDate(payload.week_end) || !Array.isArray(payload.disciplines)) return false;
    const seen = new Set();
    for (const state of payload.disciplines) {
      if (!state || !validId(state.uuid) || seen.has(state.uuid) || typeof state.active !== "boolean"
        || typeof state.today_done !== "boolean" || !validWeekly(state.weekly)
        || !(state.daily_streak === null || nonnegativeInteger(state.daily_streak))) return false;
      seen.add(state.uuid);
    }
    return naturalOrder.every((uuid) => seen.has(uuid));
  }

  function applyProgress(payload) {
    root.dataset.today = payload.today;
    for (const [selector, value] of [
      ["[data-discipline-week-start]", payload.week_start],
      ["[data-discipline-week-end]", payload.week_end],
    ]) {
      const label = root.querySelector(selector);
      label.textContent = value;
      label.dateTime = value;
    }
    root.querySelector("[data-discipline-focus-today]").href = `/discipline?year=${payload.today.slice(0, 4)}`;
    payload.disciplines.forEach((state) => {
      const card = byId.get(state.uuid);
      if (!card) return;
      const weekly = state.weekly;
      card.dataset.disciplineActive = String(state.active);
      card.classList.toggle("discipline-inactive", !state.active);
      card.querySelector("[data-discipline-paused]").hidden = state.active;
      card.dataset.weeklyAvailable = "true";
      card.dataset.weeklyMet = String(weekly.target_met);
      card.querySelector("[data-discipline-weekly-details]").hidden = false;
      card.querySelector("[data-discipline-weekly-unavailable]").hidden = true;
      card.querySelector("[data-discipline-weekly-count]").textContent = `${weekly.count} / ${weekly.target} this week`;
      card.querySelector("[data-discipline-weekly-remaining]").textContent = weekly.target_met
        ? "Target met" : `${weekly.remaining} remaining`;
      const progress = card.querySelector("[data-discipline-weekly-bar]");
      progress.max = weekly.target;
      progress.value = Math.min(weekly.count, weekly.target);
      setDailyStreak(card, state.daily_streak);
      const today = card.querySelector("[data-discipline-today]");
      if (today) {
        today.hidden = !state.active;
        if (!today.disabled) {
          today.dataset.action = state.today_done ? "unmark" : "mark";
          today.setAttribute("aria-pressed", String(state.today_done));
          today.textContent = "Done today";
          today.title = state.today_done ? "Remove today's completion" : "Mark this discipline complete today";
          today.classList.toggle("btn-primary", !state.today_done);
        }
      }
    });
    applyFilters();
  }

  async function refreshProgress() {
    const sequence = ++progressSequence;
    progressController?.abort();
    progressController = new AbortController();
    refreshButton.disabled = true;
    try {
      const response = await fetch("/discipline/progress", {
        credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" },
        signal: progressController.signal,
      });
      if (!response.ok) throw new Error("Progress unavailable");
      const payload = await response.json();
      if (sequence !== progressSequence) return;
      if (!validateProgress(payload)) throw new Error("Progress unavailable");
      applyProgress(payload);
      progressError.hidden = true;
    } catch (error) {
      if (sequence !== progressSequence || error.name === "AbortError") return;
      progressMessage.textContent = "Current week progress unavailable. Last confirmed values are unchanged.";
      progressError.hidden = false;
    } finally {
      if (sequence === progressSequence) refreshButton.disabled = false;
    }
  }

  function queueProgressRefresh() {
    if (refreshQueued) return;
    refreshQueued = true;
    queueMicrotask(() => {
      refreshQueued = false;
      refreshProgress();
    });
  }

  root.addEventListener("input", (event) => {
    if (event.target === search) applyFilters();
  });
  root.addEventListener("change", (event) => {
    if ([category, targetFilter, organize].includes(event.target)) applyFilters();
  });
  root.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) return;
    const button = event.target.closest("button, [data-discipline-focus-today]");
    if (!button) return;
    if (button.hasAttribute("data-discipline-status")) {
      const status = button.dataset.disciplineStatus;
      if (["active", "paused", "all"].includes(status)) statusFilter = status;
      applyFilters();
    } else if (button.hasAttribute("data-discipline-clear-filters")) {
      search.value = "";
      category.value = "";
      targetFilter.value = "all";
      statusFilter = "active";
      applyFilters();
    } else if (button.hasAttribute("data-discipline-reset-layout")) {
      layout = defaultLayout();
      renderLayout();
      saveLayout();
    } else if (button.hasAttribute("data-discipline-pin")) {
      const uuid = button.closest("[data-discipline-card]").dataset.disciplineUuid;
      layout.pinned = layout.pinned.includes(uuid)
        ? layout.pinned.filter((pinnedId) => pinnedId !== uuid) : layout.pinned.concat(uuid);
      renderLayout();
      saveLayout();
      button.focus({ preventScroll: true });
    } else if (button.hasAttribute("data-discipline-move")) {
      const card = button.closest("[data-discipline-card]");
      const peers = visiblePeers(card);
      const offset = button.dataset.disciplineMove === "up" ? -1 : 1;
      const neighbor = peers[peers.indexOf(card) + offset];
      if (!neighbor) return;
      const currentIndex = layout.order.indexOf(card.dataset.disciplineUuid);
      const neighborIndex = layout.order.indexOf(neighbor.dataset.disciplineUuid);
      [layout.order[currentIndex], layout.order[neighborIndex]] = [layout.order[neighborIndex], layout.order[currentIndex]];
      renderLayout();
      saveLayout();
      if (button.disabled) {
        card.querySelector("[data-discipline-pin]").focus({ preventScroll: true });
      } else {
        button.focus({ preventScroll: true });
      }
    } else if (button.hasAttribute("data-discipline-focus-today")) {
      if (root.dataset.year === root.dataset.today.slice(0, 4)) {
        event.preventDefault();
        centerToday(orderedCards().filter((card) => !card.hidden), true);
      }
    } else if (button.hasAttribute("data-discipline-refresh-progress")) {
      queueProgressRefresh();
    }
  });

  document.addEventListener("luigi:discipline-updated", (event) => {
    const detail = event.detail;
    const card = detail && byId.get(detail.uuid);
    if (!card) return;
    if (detail.streak === null || nonnegativeInteger(detail.streak)) setDailyStreak(card, detail.streak);
    queueProgressRefresh();
  });

  document.body.addEventListener("undoCleared", () => window.location.reload());

  renderLayout();
})();