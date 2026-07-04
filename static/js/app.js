/* luigi-web frontend glue: modal open/close, HTMX-triggered close, and
   Sortable.js wiring for the kanban board (drag = POST new status). */

(function () {
  "use strict";

  // ------------------- Modal -------------------
  const modal = () => document.getElementById("modal");
  const modalBody = () => document.getElementById("modal-body");

  window.openModal = function () {
    const m = modal();
    if (!m) return;
    m.classList.remove("hidden");
    // Focus the first input in the loaded form when it arrives.
    setTimeout(() => {
      const first = modalBody().querySelector("input, select, textarea, button");
      if (first) first.focus();
    }, 30);
  };

  window.closeModal = function () {
    const m = modal();
    if (!m) return;
    m.classList.add("hidden");
    modalBody().innerHTML = "";
  };

  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-close-modal]")) {
      e.preventDefault();
      window.closeModal();
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") window.closeModal();
  });

  // HTMX custom event: server sends HX-Trigger: closeModal on save/create/delete.
  document.body.addEventListener("closeModal", () => window.closeModal());

  // Refresh the kanban board when the server asks (after edit/complete).
  document.body.addEventListener("reloadBoard", () => {
    // Simplest reliable refresh: reload the page. Small board, fine cost.
    // Only reload if we're on a kanban page.
    if (document.getElementById("kanban-board")) {
      window.location.reload();
    }
  });

  // ------------------- Kanban drag-and-drop -------------------
  function initKanban() {
    if (typeof Sortable === "undefined") return;
    document.querySelectorAll(".kanban-column-body.sortable").forEach((col) => {
      new Sortable(col, {
        group: "kanban",
        animation: 150,
        ghostClass: "sortable-ghost",
        dragClass: "sortable-drag",
        onEnd: async (evt) => {
          const card = evt.item;
          const uuid = card.dataset.uuid;
          const targetCol = evt.to.dataset.status;
          const endpoint = evt.to.dataset.endpoint;
          if (!uuid || !targetCol || !endpoint) return;
          // Fire-and-forget; if it fails, the visual state and DB will diverge,
          // but a page refresh will restore truth.
          const body = new URLSearchParams({ status: targetCol });
          const resp = await fetch(`${endpoint}/${uuid}/status`, {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded" },
            body,
            credentials: "same-origin",
          });
          if (!resp.ok) {
            console.error("status update failed", resp.status);
            window.location.reload();
          }
        },
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initKanban);
  } else {
    initKanban();
  }

  // HTMX swaps new cards in; re-init Sortable on the whole board after swaps
  // targeting kanban children.
  document.body.addEventListener("htmx:afterSwap", (e) => {
    if (e.target.closest && e.target.closest(".kanban-column-body")) {
      // No-op: Sortable already covers the column since it was initialized
      // on the container, not the children.
    }
  });

  // ------------------- Home page widget visibility (localStorage) -------------------
  const HIDDEN_KEY = "luigi.home.hiddenWidgets";
  function loadHidden() {
    try { return new Set(JSON.parse(localStorage.getItem(HIDDEN_KEY) || "[]")); }
    catch { return new Set(); }
  }
  function saveHidden(s) {
    localStorage.setItem(HIDDEN_KEY, JSON.stringify([...s]));
  }
  function initHomeWidgets() {
    const toggles = document.querySelectorAll(".widget-toggle");
    if (!toggles.length) return;
    const hidden = loadHidden();
    document.querySelectorAll(".widget[data-widget]").forEach((w) => {
      if (hidden.has(w.dataset.widget)) w.classList.add("is-hidden");
    });
    toggles.forEach((cb) => {
      const id = cb.dataset.widget;
      cb.checked = !hidden.has(id);
      cb.addEventListener("change", () => {
        const target = document.querySelector(`.widget[data-widget="${id}"]`);
        if (!target) return;
        if (cb.checked) {
          target.classList.remove("is-hidden");
          hidden.delete(id);
        } else {
          target.classList.add("is-hidden");
          hidden.add(id);
        }
        saveHidden(hidden);
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initHomeWidgets);
  } else {
    initHomeWidgets();
  }

  // ------------------- Chat mic (Web Speech API, feature-detected) -------------------
  // Kept behind a runtime check so browsers without SpeechRecognition just see
  // a greyed-out button. When available AND the chat panel is enabled, one
  // click starts dictation; the recognized text is inserted into the textarea
  // and the form is submitted. No permissions are requested until the user
  // clicks the button.
  function initChatMic() {
    const btn = document.querySelector("[data-chat-mic]");
    if (!btn) return;
    const panel = document.getElementById("chat-panel");
    if (!panel || panel.classList.contains("chat-disabled")) return;

    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      btn.title = "Voice input not supported in this browser";
      return;
    }
    btn.disabled = false;
    btn.title = "Click to dictate (Web Speech API)";

    let recognition = null;
    let listening = false;

    btn.addEventListener("click", () => {
      const textarea = document.querySelector(".chat-composer textarea");
      if (!textarea) return;
      if (listening && recognition) { recognition.stop(); return; }
      recognition = new SpeechRecognition();
      recognition.lang = navigator.language || "en-US";
      recognition.interimResults = false;
      recognition.maxAlternatives = 1;
      recognition.onstart = () => { listening = true; btn.classList.add("is-listening"); };
      recognition.onend = () => { listening = false; btn.classList.remove("is-listening"); };
      recognition.onerror = () => { listening = false; btn.classList.remove("is-listening"); };
      recognition.onresult = (event) => {
        const transcript = Array.from(event.results)
          .map((r) => r[0].transcript).join(" ").trim();
        if (!transcript) return;
        textarea.value = textarea.value
          ? textarea.value.trim() + " " + transcript
          : transcript;
        // Auto-send when dictation completes — matches how voice assistants feel.
        const form = textarea.closest("form");
        if (form) form.requestSubmit();
      };
      try { recognition.start(); } catch (e) { /* already started */ }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initChatMic);
  } else {
    initChatMic();
  }

  // ------------------- Admin env editor: secret reveal -------------------
  // Toggle a password field to a text field and back. Purely client-side —
  // the value never leaves the DOM until the form is submitted normally.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-env-toggle]");
    if (!btn) return;
    const wrap = btn.closest("[data-env-secret]");
    if (!wrap) return;
    const input = wrap.querySelector("input");
    if (!input) return;
    input.type = input.type === "password" ? "text" : "password";
    btn.textContent = input.type === "password" ? "👁" : "🙈";
  });

  // ------------------- Snooze menu (close on outside click) -------------------
  // The <details data-snooze-menu> element handles open/close natively; we
  // just close any open menus when the user clicks outside of them so the
  // page never has multiple menus hanging open. Also close after any hx-post
  // resolves so the newly-swapped card doesn't inherit an "open" state.
  document.addEventListener("click", (e) => {
    document.querySelectorAll("details[data-snooze-menu][open]").forEach((d) => {
      if (!d.contains(e.target)) d.removeAttribute("open");
    });
  });
  document.body.addEventListener("htmx:afterSwap", () => {
    document.querySelectorAll("details[data-snooze-menu][open]")
      .forEach((d) => d.removeAttribute("open"));
  });

  // ------------------- Tasks filter bar + saved filters -------------------
  // All filtering is client-side: cards carry data-* attrs and we toggle a
  // `.card-filtered-out` class. Saved filters live in localStorage under
  //   luigi.tasks.savedFilters                → array of {name, filter}
  //   luigi.tasks.activeFilter.<endpoint>     → most-recent filter state
  // Scoped per endpoint so /tasks and /recurring have independent memory.
  const SAVED_KEY = "luigi.tasks.savedFilters";
  const activeKey = (scope) => `luigi.tasks.activeFilter.${scope}`;

  function loadSaved() {
    try { return JSON.parse(localStorage.getItem(SAVED_KEY) || "[]"); }
    catch { return []; }
  }
  function writeSaved(list) {
    localStorage.setItem(SAVED_KEY, JSON.stringify(list));
  }

  function readFilterState(bar) {
    return {
      q:         bar.querySelector("[data-filter-search]").value.trim().toLowerCase(),
      smart:     bar.querySelector("[data-filter-smartlist]").value,
      minPrio:   parseInt(bar.querySelector("[data-filter-priority]").value, 10) || 0,
      catagory:  bar.querySelector("[data-filter-catagory]").value,
    };
  }
  function writeFilterState(bar, state) {
    bar.querySelector("[data-filter-search]").value    = state.q || "";
    bar.querySelector("[data-filter-smartlist]").value = state.smart || "";
    bar.querySelector("[data-filter-priority]").value  = String(state.minPrio || 0);
    bar.querySelector("[data-filter-catagory]").value  = state.catagory || "";
  }

  function isoAddDays(base, days) {
    const d = new Date(base.getTime());
    d.setDate(d.getDate() + days);
    return d.toISOString().slice(0, 10);
  }
  function weekBounds() {
    // Monday..Sunday for the current local week.
    const today = new Date();
    const dow = (today.getDay() + 6) % 7;   // Mon=0..Sun=6
    const mon = new Date(today);
    mon.setDate(mon.getDate() - dow);
    return { mon: mon.toISOString().slice(0, 10),
             sun: isoAddDays(mon, 6),
             today: today.toISOString().slice(0, 10) };
  }

  function cardMatches(card, state, wk) {
    // Text search hits title + category + groups.
    if (state.q) {
      const hay = [
        card.dataset.title,
        card.dataset.catagory,
        card.dataset.taskGroup,
        card.dataset.subGroup,
      ].join(" ");
      if (!hay.includes(state.q)) return false;
    }
    if (state.minPrio > 0) {
      const p = parseInt(card.dataset.priority, 10) || 0;
      if (p < state.minPrio) return false;
    }
    if (state.catagory && card.dataset.catagory !== state.catagory) {
      return false;
    }
    const due = card.dataset.dueDate ? card.dataset.dueDate.slice(0, 10) : "";
    const completed = card.dataset.completed === "1";
    const completedTime = card.dataset.completedTime
      ? card.dataset.completedTime.slice(0, 10) : "";
    switch (state.smart) {
      case "open":
        if (completed) return false; break;
      case "overdue":
        if (completed || !due || due >= wk.today) return false; break;
      case "due-week":
        if (completed || !due || due < wk.mon || due > wk.sun) return false; break;
      case "no-due":
        if (completed || due) return false; break;
      case "high-priority":
        if (completed) return false;
        if ((parseInt(card.dataset.priority, 10) || 0) < 5) return false;
        break;
      case "completed-week":
        if (!completed) return false;
        if (!completedTime || completedTime < wk.mon || completedTime > wk.sun) return false;
        break;
      default: break;
    }
    return true;
  }

  function applyFilter(bar) {
    const state = readFilterState(bar);
    const scope = bar.closest("[data-tasks-scope]");
    if (!scope) return;
    const wk = weekBounds();
    let shown = 0, total = 0;
    scope.querySelectorAll(".kanban-column").forEach((col) => {
      let colCount = 0;
      col.querySelectorAll(".card").forEach((card) => {
        total += 1;
        const ok = cardMatches(card, state, wk);
        card.classList.toggle("card-filtered-out", !ok);
        if (ok) { colCount += 1; shown += 1; }
      });
      const badge = col.querySelector("[data-column-count]");
      if (badge) badge.textContent = String(colCount);
    });
    const summary = bar.querySelector("[data-filter-summary]");
    if (summary) {
      const isFiltering = state.q || state.smart || state.minPrio > 0 || state.catagory;
      summary.textContent = isFiltering ? `${shown} of ${total}` : "";
    }
    // Persist most-recent state per endpoint so a page reload keeps context.
    const scopeName = scope.dataset.tasksScope || "default";
    localStorage.setItem(activeKey(scopeName), JSON.stringify(state));
  }

  function populateCategoryOptions(bar) {
    const scope = bar.closest("[data-tasks-scope]");
    if (!scope) return;
    const select = bar.querySelector("[data-filter-catagory]");
    const seen = new Set();
    scope.querySelectorAll(".card").forEach((c) => {
      const v = c.dataset.catagory;
      if (v) seen.add(v);
    });
    const current = select.value;
    // Preserve the "All categories" placeholder as the first option.
    select.querySelectorAll("option:not(:first-child)").forEach((o) => o.remove());
    [...seen].sort().forEach((v) => {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v;
      select.appendChild(opt);
    });
    if (current && seen.has(current)) select.value = current;
  }

  function renderSavedList(bar) {
    const list = bar.querySelector("[data-saved-filters-list]");
    if (!list) return;
    const saved = loadSaved();
    list.innerHTML = "";
    if (!saved.length) {
      const empty = document.createElement("li");
      empty.className = "saved-filters-empty";
      empty.textContent = "No saved filters yet.";
      list.appendChild(empty);
      return;
    }
    saved.forEach((entry, idx) => {
      const li = document.createElement("li");
      li.className = "saved-filter";
      const load = document.createElement("button");
      load.type = "button";
      load.className = "btn btn-tiny btn-ghost saved-filter-name";
      load.textContent = entry.name;
      load.addEventListener("click", () => {
        writeFilterState(bar, entry.filter);
        applyFilter(bar);
        bar.querySelector("[data-saved-filters]").removeAttribute("open");
      });
      const del = document.createElement("button");
      del.type = "button";
      del.className = "btn btn-tiny btn-danger";
      del.title = "Delete";
      del.textContent = "×";
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        const next = loadSaved();
        next.splice(idx, 1);
        writeSaved(next);
        renderSavedList(bar);
      });
      li.appendChild(load);
      li.appendChild(del);
      list.appendChild(li);
    });
  }

  function initTasksFilter() {
    const bar = document.querySelector("[data-filter-bar]");
    if (!bar) return;
    populateCategoryOptions(bar);
    renderSavedList(bar);

    // Restore last-active state for this scope.
    const scope = bar.closest("[data-tasks-scope]");
    const scopeName = scope ? (scope.dataset.tasksScope || "default") : "default";
    try {
      const saved = localStorage.getItem(activeKey(scopeName));
      if (saved) writeFilterState(bar, JSON.parse(saved));
    } catch { /* ignore corrupted json */ }

    ["input", "change"].forEach((evt) => {
      bar.addEventListener(evt, (e) => {
        if (e.target.closest("[data-saved-filter-name]")) return; // name field only
        applyFilter(bar);
      });
    });

    bar.querySelector("[data-filter-clear]").addEventListener("click", () => {
      writeFilterState(bar, { q: "", smart: "", minPrio: 0, catagory: "" });
      applyFilter(bar);
    });

    bar.querySelector("[data-saved-filter-save]").addEventListener("click", () => {
      const nameInput = bar.querySelector("[data-saved-filter-name]");
      const name = (nameInput.value || "").trim();
      if (!name) { nameInput.focus(); return; }
      const list = loadSaved();
      const filter = readFilterState(bar);
      // Replace an existing entry with the same name so re-saving updates it.
      const existing = list.findIndex((e) => e.name === name);
      if (existing >= 0) list[existing] = { name, filter };
      else list.push({ name, filter });
      writeSaved(list);
      nameInput.value = "";
      renderSavedList(bar);
    });

    // Cards get replaced by HTMX after edit/snooze/complete — re-apply the
    // filter and refresh the category options so a newly-added category
    // shows up in the dropdown.
    document.body.addEventListener("htmx:afterSwap", (e) => {
      if (e.target.closest && e.target.closest(".kanban-column-body")) {
        populateCategoryOptions(bar);
        applyFilter(bar);
      }
    });

    applyFilter(bar);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initTasksFilter);
  } else {
    initTasksFilter();
  }
})();
