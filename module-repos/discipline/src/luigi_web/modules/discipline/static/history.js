(() => {
  "use strict";
  const root = document.getElementById("dh-live");
  if (!root) return;
  const find = (selector) => root.querySelector(selector);
  const dayLength = 86400000;
  const dateKey = (value) => value.toISOString().slice(0, 10);
  const dateValue = (value) => {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
    const parsed = new Date(`${value}T00:00:00Z`);
    return Number.isFinite(parsed.getTime()) && dateKey(parsed) === value ? parsed : null;
  };
  const addDays = (value, amount) => dateKey(new Date(dateValue(value).getTime() + amount * dayLength));
  const formatDay = (value) => dateValue(value).toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
  const shortDay = (value) => dateValue(value).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
  const formatStamp = (value) => value === null ? "Not recorded" : value.replace("T", " ").replace(/Z$/, "+00:00");
  const versionPattern = /^[a-f0-9]{64}$/i;
  function validState(candidate, uuid, year) {
    if (!candidate || typeof candidate.uuid !== "string" || !candidate.uuid ||
        (uuid !== undefined && candidate.uuid !== uuid) || (year !== undefined && candidate.year !== year) ||
        typeof candidate.name !== "string" || !(candidate.category === null || typeof candidate.category === "string") ||
        ![true, false, 0, 1].includes(candidate.active) || !dateValue(candidate.today) ||
        !Number.isInteger(candidate.year) || candidate.year < 1900 || candidate.year > Number(candidate.today.slice(0, 4)) + 1 ||
        typeof candidate.timezone !== "string" || !candidate.timezone ||
        !Number.isInteger(candidate.weeklyTarget) || candidate.weeklyTarget < 1 || candidate.weeklyTarget > 7 ||
        typeof candidate.emptyVersion !== "string" || !versionPattern.test(candidate.emptyVersion) || !Array.isArray(candidate.completions)) return false;
    const weekly = candidate.weekly;
    if (!weekly || !Number.isInteger(weekly.count) || weekly.count < 0 || weekly.count > 7 ||
        weekly.target !== candidate.weeklyTarget || !Number.isInteger(weekly.remaining) || weekly.remaining < 0 ||
        typeof weekly.target_met !== "boolean" || !dateValue(weekly.week_start) || !dateValue(weekly.week_end)) return false;
    const dates = new Set();
    return candidate.completions.every((record) => {
      if (!record || !dateValue(record.date) || Number(record.date.slice(0, 4)) !== candidate.year || dates.has(record.date) ||
          typeof record.version !== "string" || !versionPattern.test(record.version) ||
          !(record.loggedAt === null || (typeof record.loggedAt === "string" &&
            /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(record.loggedAt) && Number.isFinite(Date.parse(record.loggedAt))))) return false;
      dates.add(record.date);
      return true;
    });
  }
  let state;
  try {
    state = JSON.parse(find("#dh-seed").textContent);
    if (!validState(state)) throw new Error("Invalid history state");
  } catch {
    find("#dh-error").textContent = "History could not be loaded. Reload the page.";
    find("#dh-load-error").hidden = false;
    root.querySelectorAll("button, input").forEach((control) => { control.disabled = true; });
    find("#dh-reload").disabled = false;
    find("#dh-reload").addEventListener("click", () => window.location.reload());
    return;
  }
  const endpoint = `/discipline/${encodeURIComponent(state.uuid)}/history`;
  const dialog = find("#dh-day-dialog");
  let records = new Map(state.completions.map((record) => [record.date, record]));
  let month = state.year === Number(state.today.slice(0, 4)) ? Number(state.today.slice(5, 7)) - 1 : 0;
  let selectedDay = "", origin = null, pending = false, needsRefresh = false, errorMessage = "";
  let retryView = null, undo = null, undoTimer = null;
  const editable = (value) => Boolean(dateValue(value)) && value <= state.today && (Boolean(state.active) || value < state.today);
  const maxYear = () => Number(state.today.slice(0, 4)) + 1;
  const make = (tag, className, text) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  };
  const announce = (message) => { find("#dh-status").textContent = message; };
  function activate(tab, focus = true) {
    if (pending) return;
    root.querySelectorAll('[role="tab"]').forEach((item) => {
      const selected = item === tab;
      item.setAttribute("aria-selected", String(selected));
      item.tabIndex = selected ? 0 : -1;
      find(`#${item.getAttribute("aria-controls")}`).hidden = !selected;
    });
    if (focus) tab.focus();
  }
  function syncControls() {
    root.setAttribute("aria-busy", String(pending));
    root.querySelectorAll("button, input").forEach((control) => { control.disabled = pending; });
    find("#dh-prev").disabled = pending || (state.year === 1900 && month === 0);
    find("#dh-next").disabled = pending || (state.year === maxYear() && month === 11);
    find("#dh-year-prev").disabled = pending || state.year === 1900;
    find("#dh-year-next").disabled = pending || state.year === maxYear();
    find("#dh-mark").disabled = pending || needsRefresh || !editable(selectedDay) || records.has(selectedDay);
    find("#dh-remove").disabled = pending || needsRefresh || !editable(selectedDay) || !records.has(selectedDay);
    find("#dh-undo").disabled = pending || needsRefresh || !undo || performance.now() >= undo.expires;
    find("#dh-load-error").hidden = !errorMessage;
    find("#dh-error").textContent = errorMessage;
    find("#dh-day-error").textContent = errorMessage;
    find("#dh-day-retry").hidden = !errorMessage;
    if (dialog.open && !pending && (!dialog.contains(document.activeElement) || document.activeElement.hidden || document.activeElement.disabled)) {
      find("#dh-cancel").focus();
    }
  }
  function clearUndo() {
    undo = null;
    clearTimeout(undoTimer);
    undoTimer = null;
  }
  function rememberUndo(payload, started) {
    clearUndo();
    if (!payload.undo_token) return;
    const expires = started + Math.min(12000, payload.undo_ttl_ms);
    if (expires <= performance.now()) return;
    undo = { token: payload.undo_token, expires };
    undoTimer = setTimeout(() => {
      clearUndo(); syncControls(); announce("Undo expired.");
    }, expires - performance.now());
  }
  function dayButton(value, compact = false) {
    const completed = records.has(value);
    const button = make("button", compact ? "dh-year-day" : "dh-day");
    button.type = "button";
    button.dataset.dhDay = value;
    button.classList.toggle("is-complete", completed);
    button.classList.toggle("is-current", value === state.today);
    button.classList.toggle("is-readonly", !editable(value));
    const label = `${formatDay(value)}: ${completed ? "Completed" : "No completion"}${!editable(value) ? "; read only" : ""}`;
    button.setAttribute("aria-label", label);
    button.setAttribute("aria-haspopup", "dialog");
    button.title = label;
    if (value === state.today) button.setAttribute("aria-current", "date");
    if (compact) button.tabIndex = -1;
    else {
      button.append(make("span", "", String(dateValue(value).getUTCDate())));
      if (completed) button.append(find("#dh-check-icon").content.cloneNode(true));
    }
    return button;
  }
  function renderMonth() {
    const first = new Date(Date.UTC(state.year, month, 1));
    const last = new Date(Date.UTC(state.year, month + 1, 0));
    const offset = (first.getUTCDay() + 6) % 7;
    find("#dh-month-heading").textContent = first.toLocaleDateString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" });
    find("#dh-month-days").replaceChildren(...Array.from({ length: 42 }, (_, index) => {
      if (index < offset || index >= offset + last.getUTCDate()) {
        const blank = make("span"); blank.setAttribute("aria-hidden", "true"); return blank;
      }
      return dayButton(dateKey(new Date(Date.UTC(state.year, month, index - offset + 1))));
    }));
    const firstKey = dateKey(first), lastKey = dateKey(last);
    const countBetween = (start, end) => [...records.keys()].filter((value) => value >= start && value <= end).length;
    const total = countBetween(firstKey, lastKey);
    find("#dh-month-total").textContent = `${total} ${total === 1 ? "completion" : "completions"}`;
    const startOfGrid = addDays(firstKey, -offset);
    const weeks = [];
    for (let index = 0; index < 6; index += 1) {
      const start = addDays(startOfGrid, index * 7), end = addDays(start, 6);
      if (start > lastKey) continue;
      const boundedStart = start < firstKey ? firstKey : start;
      const boundedEnd = end > lastKey ? lastKey : end;
      const row = make("li"); row.dataset.dhWeek = start;
      if (start === state.weekly.week_start) row.setAttribute("aria-current", "date");
      row.append(make("span", "dh-week-label", `${shortDay(boundedStart)} - ${shortDay(boundedEnd)}`),
        make("span", "dh-week-total", String(countBetween(boundedStart, boundedEnd))));
      weeks.push(row);
    }
    find("#dh-week-list").replaceChildren(...weeks);
  }
  function renderYear() {
    const first = new Date(Date.UTC(state.year, 0, 1));
    const firstKey = dateKey(first);
    const count = (Date.UTC(state.year + 1, 0, 1) - first.getTime()) / dayLength;
    const offset = (first.getUTCDay() + 6) % 7;
    const columns = Math.ceil((offset + count) / 7);
    find(".dh-year-layout").style.setProperty("--dh-year-columns", String(columns));
    find("#dh-year-months").replaceChildren(...Array.from({ length: 12 }, (_, index) => {
      const start = new Date(Date.UTC(state.year, index, 1));
      const column = Math.floor(((start.getTime() - first.getTime()) / dayLength + offset) / 7) + 1;
      const label = make("span", "", start.toLocaleDateString("en-GB", { month: "short", timeZone: "UTC" }));
      label.style.gridColumn = `${column} / span 4`;
      return label;
    }));
    find("#dh-year-days").replaceChildren(...Array.from({ length: columns * 7 }, (_, index) => {
      if (index < offset || index >= offset + count) {
        const blank = make("span"); blank.setAttribute("aria-hidden", "true"); return blank;
      }
      return dayButton(addDays(firstKey, index - offset), true);
    }));
    const entry = find(`#dh-year-days [data-dh-day="${state.today}"]`) || find("#dh-year-days button");
    entry.tabIndex = 0;
    find("#dh-year-heading").textContent = String(state.year);
    find("#dh-year-count").textContent = `${records.size} completions`;
    find(".dh-year-scroll").setAttribute("aria-label", `${state.year} completion calendar`);
  }
  function renderSummary() {
    find("#dh-name").textContent = state.name;
    find("#dh-category").textContent = state.category || "";
    find("#dh-active").textContent = state.active ? "Active" : "Paused";
    find("#dh-today").textContent = formatDay(state.today);
    find("#dh-today").dateTime = state.today;
    find("#dh-timezone").textContent = state.timezone;
    const weekly = state.weekly;
    find("#dh-week-range").textContent = `${formatDay(weekly.week_start)} - ${formatDay(weekly.week_end)}`;
    find("#dh-week-count").textContent = `${weekly.count} / ${weekly.target}`;
    find("#dh-week-progress").max = weekly.target;
    find("#dh-week-progress").value = Math.min(weekly.count, weekly.target);
    find("#dh-week-progress").setAttribute("aria-valuetext", `${weekly.count} completions; current weekly target ${weekly.target}`);
    find("#dh-current-target").textContent = `Current weekly target: ${state.weeklyTarget}. ${weekly.target_met ? "Target met" : `${weekly.remaining} remaining`}`;
    find("#dh-total-label").textContent = `Completions in ${state.year}`;
    find("#dh-last-label").textContent = `Latest completion in ${state.year}`;
    find("#dh-total").textContent = String(records.size);
    const latest = [...records.keys()].sort().at(-1);
    find("#dh-last").textContent = latest ? formatDay(latest) : "None";
    find("#dh-year-input").value = String(state.year);
    find("#dh-year-input").max = String(maxYear());
  }
  function renderLog() {
    const sorted = [...records.values()].sort((left, right) => right.date.localeCompare(left.date));
    find("#dh-log").replaceChildren(...sorted.map((record) => {
      const row = make("li"); row.dataset.dhRecord = record.date;
      const button = make("button", "dh-log-entry");
      button.type = "button"; button.dataset.dhDay = record.date;
      button.setAttribute("aria-haspopup", "dialog");
      button.setAttribute("aria-label", `Inspect ${formatDay(record.date)}`);
      [["Completion date", record.date, formatDay(record.date)], [`Logged at (${state.timezone})`, record.loggedAt, formatStamp(record.loggedAt)]].forEach(([label, stamp, text]) => {
        const column = make("span"), time = make("time", "", text);
        if (stamp !== null) time.dateTime = stamp;
        column.append(make("small", "", label), time); button.append(column);
      });
      row.append(button); return row;
    }));
    find("#dh-log-heading").textContent = `Completion log in ${state.year}`;
    find("#dh-log-empty").hidden = records.size > 0;
    find("#dh-log-count").textContent = `${records.size} ${records.size === 1 ? "entry" : "entries"}`;
  }
  function renderDialog() {
    if (!selectedDay) return;
    const record = records.get(selectedDay);
    find("#dh-day-heading").textContent = formatDay(selectedDay);
    const restriction = selectedDay > state.today ? "Future date; read only." : !state.active && selectedDay === state.today ? "Paused today; read only." : "";
    find("#dh-day-status").textContent = `${record ? "Completed." : "No completion."} ${restriction}`.trim();
    find("#dh-timestamps").hidden = !record;
    find("#dh-logged-label").textContent = `Logged at (${state.timezone})`;
    const time = find("#dh-logged-at");
    time.textContent = record ? formatStamp(record.loggedAt) : "";
    if (record?.loggedAt) time.dateTime = record.loggedAt;
    else time.removeAttribute("datetime");
    find("#dh-mark").hidden = Boolean(record);
    find("#dh-remove").hidden = !record;
  }
  function render() {
    renderMonth(); renderYear(); renderSummary(); renderLog(); renderDialog(); syncControls();
  }
  function closeDay() {
    if (pending) return;
    dialog.close();
    const replacement = origin && find(`#${origin.panel} [data-dh-day="${origin.date}"]`);
    (replacement || find('[role="tab"][aria-selected="true"]')).focus();
  }
  function openDay(value, control) {
    if (pending || !dateValue(value) || dialog.open) return;
    selectedDay = value;
    origin = { date: value, panel: control.closest('[role="tabpanel"]').id };
    renderDialog(); syncControls(); dialog.showModal(); find("#dh-cancel").focus();
  }
  async function request(url, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, { ...options, credentials: "same-origin", cache: "no-store", redirect: "error", headers: { Accept: "application/json" }, signal: controller.signal });
      if (!response.ok) throw new Error("History request failed");
      return await response.json();
    } finally { clearTimeout(timeout); }
  }
  function applyState(next, nextMonth) {
    state = next; month = nextMonth;
    records = new Map(state.completions.map((record) => [record.date, record]));
    needsRefresh = false; errorMessage = ""; retryView = null;
    render();
  }
  async function loadYear(year = state.year, nextMonth = month) {
    if (pending || !Number.isInteger(year) || year < 1900 || year > maxYear()) return;
    pending = true; errorMessage = ""; clearUndo(); syncControls(); announce("Loading history...");
    try {
      const next = await request(`${endpoint}/data?year=${year}`);
      if (!validState(next, state.uuid, year)) throw new Error("Invalid history state");
      applyState(next, nextMonth); announce("History reloaded.");
    } catch {
      needsRefresh = true;
      retryView = { year, month: nextMonth };
      errorMessage = "History could not be loaded. Last confirmed values are unchanged. Retry to reload.";
      find("#dh-year-input").value = String(state.year);
      announce("");
    } finally { pending = false; syncControls(); }
  }
  async function write(action) {
    if (pending || needsRefresh) return;
    const isUndo = action === "undo";
    if (isUndo ? !undo || performance.now() >= undo.expires : !dialog.open || !editable(selectedDay) || (action === "mark") === records.has(selectedDay)) return;
    const body = new FormData();
    body.set("year", String(state.year));
    if (isUndo) body.set("token", undo.token);
    else {
      body.set("day", selectedDay); body.set("action", action);
      body.set("expected_version", records.get(selectedDay)?.version || state.emptyVersion);
    }
    const started = performance.now();
    pending = true; errorMessage = ""; clearUndo(); syncControls(); announce("Saving...");
    try {
      const payload = await request(isUndo ? `${endpoint}/undo` : endpoint, { method: "POST", body });
      if (payload?.ok !== true || !validState(payload.state, state.uuid, state.year) ||
          (!isUndo && !(payload.undo_token === null || (typeof payload.undo_token === "string" && payload.undo_token.length > 0 && Number.isFinite(payload.undo_ttl_ms) && payload.undo_ttl_ms > 0)))) throw new Error("Unconfirmed history write");
      applyState(payload.state, month);
      if (!isUndo) rememberUndo(payload, started);
      pending = false; syncControls();
      if (dialog.open) closeDay();
      announce(isUndo ? "Change undone." : action === "mark" ? "Completion confirmed." : "Removal confirmed.");
    } catch {
      needsRefresh = true; retryView = { year: state.year, month };
      errorMessage = "The save result could not be confirmed. Reload before another change. Last confirmed values are shown.";
      announce("");
    } finally { pending = false; syncControls(); }
  }
  function moveMonth(amount) {
    if (pending) return;
    const next = new Date(Date.UTC(state.year, month + amount, 1));
    const year = next.getUTCFullYear();
    if (year < 1900 || year > maxYear()) return;
    if (year !== state.year) { loadYear(year, next.getUTCMonth()); return; }
    month = next.getUTCMonth(); renderMonth(); syncControls();
  }
  const retry = () => loadYear(retryView?.year ?? state.year, retryView?.month ?? month);
  find("#dh-reload").addEventListener("click", () => loadYear());
  find("#dh-retry").addEventListener("click", retry);
  find("#dh-day-retry").addEventListener("click", retry);
  find("#dh-prev").addEventListener("click", () => moveMonth(-1));
  find("#dh-next").addEventListener("click", () => moveMonth(1));
  find("#dh-year-prev").addEventListener("click", () => loadYear(state.year - 1));
  find("#dh-year-next").addEventListener("click", () => loadYear(state.year + 1));
  find("#dh-year-form").addEventListener("submit", (event) => { event.preventDefault(); loadYear(find("#dh-year-input").valueAsNumber); });
  find("#dh-day-form").addEventListener("submit", (event) => { event.preventDefault(); write("mark"); });
  find("#dh-remove").addEventListener("click", () => write("unmark"));
  find("#dh-undo").addEventListener("click", () => write("undo"));
  find("#dh-close").addEventListener("click", closeDay);
  find("#dh-cancel").addEventListener("click", closeDay);
  dialog.addEventListener("cancel", (event) => { event.preventDefault(); closeDay(); });
  root.addEventListener("click", (event) => {
    const tab = event.target.closest('[role="tab"]');
    if (tab) activate(tab);
    const control = event.target.closest("[data-dh-day]");
    if (control) openDay(control.dataset.dhDay, control);
  });
  find('[role="tablist"]').addEventListener("keydown", (event) => {
    const tabs = [...root.querySelectorAll('[role="tab"]')], index = tabs.indexOf(event.target);
    if (pending || index < 0 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    activate(tabs[next]);
  });
  find("#dh-year-days").addEventListener("keydown", (event) => {
    const control = event.target.closest("[data-dh-day]");
    const movements = { ArrowRight: 7, ArrowLeft: -7, ArrowDown: 1, ArrowUp: -1 };
    if (pending || !control || ![...Object.keys(movements), "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const value = event.key === "Home" ? `${state.year}-01-01` : event.key === "End" ? `${state.year}-12-31` : addDays(control.dataset.dhDay, movements[event.key]);
    const next = find(`#dh-year-days [data-dh-day="${value}"]`);
    if (next) {
      find("#dh-year-days").querySelectorAll("button").forEach((button) => { button.tabIndex = button === next ? 0 : -1; });
      next.focus();
    }
  });
  render();
})();