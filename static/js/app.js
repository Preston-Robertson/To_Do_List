/* luigi-web frontend glue: modal open/close, HTMX-triggered close, and
   Sortable.js wiring for the kanban board (drag = POST new status). */

(function () {
  "use strict";

  // ------------------- Same-origin CSRF -------------------
  function readCookie(name) {
    const prefix = `${encodeURIComponent(name)}=`;
    for (const part of document.cookie.split(";")) {
      const value = part.trim();
      if (value.startsWith(prefix)) return decodeURIComponent(value.slice(prefix.length));
    }
    return "";
  }

  function csrfToken() { return readCookie("luigi_csrf"); }

  document.body.addEventListener("htmx:configRequest", (event) => {
    const token = csrfToken();
    if (token) event.detail.headers["X-CSRF-Token"] = token;
  });

  const nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init = {}) {
    const requestUrl = new URL(
      typeof input === "string" ? input : input.url,
      window.location.href,
    );
    const method = String(init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    if (requestUrl.origin === window.location.origin && !["GET", "HEAD", "OPTIONS"].includes(method)) {
      const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
      const token = csrfToken();
      if (token) headers.set("X-CSRF-Token", token);
      init = { ...init, headers };
    }
    return nativeFetch(input, init);
  };

  // ------------------- Application sidebar -------------------
  // Desktop collapse state persists per browser. On narrow screens the same
  // controls open a temporary drawer and never overwrite the desktop choice.
  const SIDEBAR_KEY = "luigi.sidebar.collapsed";
  const sidebarMedia = window.matchMedia("(max-width: 900px)");

  function syncSidebarButtonLabels() {
    const mobile = sidebarMedia.matches;
    const open = document.body.classList.contains("sidebar-open");
    const collapsed = document.body.classList.contains("sidebar-collapsed");
    document.querySelectorAll("[data-sidebar-toggle]").forEach((button) => {
      const label = mobile
        ? (open ? "Close navigation" : "Open navigation")
        : (collapsed ? "Expand navigation" : "Collapse navigation");
      button.setAttribute("aria-label", label);
      button.setAttribute("title", label);
      button.setAttribute("aria-expanded", mobile ? String(open) : String(!collapsed));
    });
  }

  function closeMobileSidebar() {
    document.body.classList.remove("sidebar-open");
    syncSidebarButtonLabels();
  }

  function initSidebar() {
    if (!sidebarMedia.matches) {
      try {
        document.body.classList.toggle(
          "sidebar-collapsed",
          localStorage.getItem(SIDEBAR_KEY) === "1",
        );
      } catch {}
    }
    syncSidebarButtonLabels();
  }

  document.addEventListener("click", (e) => {
    const toggle = e.target.closest("[data-sidebar-toggle]");
    if (toggle) {
      if (sidebarMedia.matches) {
        document.body.classList.toggle("sidebar-open");
      } else {
        const collapsed = document.body.classList.toggle("sidebar-collapsed");
        try { localStorage.setItem(SIDEBAR_KEY, collapsed ? "1" : "0"); } catch {}
      }
      syncSidebarButtonLabels();
      return;
    }
    if (e.target.closest("[data-sidebar-dismiss]")) {
      closeMobileSidebar();
      return;
    }
    if (sidebarMedia.matches && e.target.closest(".sidebar .nav-item")) {
      closeMobileSidebar();
    }
  });

  sidebarMedia.addEventListener?.("change", () => {
    document.body.classList.remove("sidebar-open");
    initSidebar();
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initSidebar);
  } else {
    initSidebar();
  }

  // ------------------- Global command palette -------------------
  const commandPalette = () => document.getElementById("command-palette");
  const commandInput = () => document.querySelector("[data-command-input]");
  let commandIndex = -1;

  function commandOptions() {
    const palette = commandPalette();
    return palette ? Array.from(palette.querySelectorAll("[data-command-option]")) : [];
  }

  function selectCommandOption(nextIndex) {
    const options = commandOptions();
    options.forEach((option) => option.classList.remove("is-selected"));
    if (!options.length) { commandIndex = -1; return; }
    commandIndex = (nextIndex + options.length) % options.length;
    options[commandIndex].classList.add("is-selected");
    options[commandIndex].scrollIntoView({ block: "nearest" });
  }

  window.openCommandPalette = function () {
    const palette = commandPalette();
    const input = commandInput();
    if (!palette || !input) return;
    closeMobileSidebar();
    palette.classList.remove("hidden");
    document.body.classList.add("command-open");
    commandIndex = -1;
    input.value = "";
    input.focus();
    if (window.htmx) window.htmx.trigger(input, "search");
  };

  window.closeCommandPalette = function () {
    const palette = commandPalette();
    if (!palette) return;
    palette.classList.add("hidden");
    document.body.classList.remove("command-open");
    commandIndex = -1;
  };

  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-command-open]")) {
      e.preventDefault();
      window.openCommandPalette();
      return;
    }
    if (e.target.closest("[data-command-close]")) {
      window.closeCommandPalette();
    }
  });

  // ------------------- Modal -------------------
  const modal = () => document.getElementById("modal");
  const modalBody = () => document.getElementById("modal-body");

  window.openModal = function () {
    const m = modal();
    if (!m) return;
    const body = modalBody();
    if (body && !body.children.length && !body.textContent.trim()) {
      body.innerHTML = `
        <div class="drawer-skeleton" aria-label="Loading">
          <div class="skeleton-line is-title"></div>
          <div class="skeleton-line is-short"></div>
          <div class="skeleton-block"></div>
          <div class="skeleton-line"></div>
          <div class="skeleton-block"></div>
        </div>`;
    }
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
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      const palette = commandPalette();
      if (palette && !palette.classList.contains("hidden")) window.closeCommandPalette();
      else window.openCommandPalette();
      return;
    }
    const palette = commandPalette();
    if (palette && !palette.classList.contains("hidden")) {
      if (e.key === "Escape") {
        e.preventDefault();
        window.closeCommandPalette();
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        selectCommandOption(commandIndex + 1);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        selectCommandOption(commandIndex - 1);
      } else if (e.key === "Enter" && commandIndex >= 0) {
        e.preventDefault();
        commandOptions()[commandIndex]?.click();
      }
      return;
    }
    if (e.key === "Escape") {
      if (document.body.classList.contains("sidebar-open")) closeMobileSidebar();
      else window.closeModal();
    }
  });

  // HTMX custom event: server sends HX-Trigger: closeModal on save/create/delete.
  document.body.addEventListener("closeModal", () => window.closeModal());

  document.body.addEventListener("htmx:afterSwap", (e) => {
    if (!e.target || e.target.id !== "modal-body") return;
    const first = e.target.querySelector("input, select, textarea, button, a[href]");
    if (first) first.focus();
  });

  document.body.addEventListener("htmx:afterSwap", (e) => {
    if (!e.target || e.target.id !== "command-results") return;
    commandIndex = -1;
  });

  // ------------------- Global HTMX progress -------------------
  let pendingRequests = 0;
  const progress = () => document.getElementById("route-progress");
  function beginProgress() {
    pendingRequests += 1;
    progress()?.classList.add("is-active");
  }
  function endProgress() {
    pendingRequests = Math.max(0, pendingRequests - 1);
    if (pendingRequests === 0) progress()?.classList.remove("is-active");
  }
  document.body.addEventListener("htmx:beforeRequest", beginProgress);
  document.body.addEventListener("htmx:afterRequest", endProgress);

  // ------------------- Success toast -------------------
  const SUCCESS_KEY = "luigi.pendingSuccess";
  let successTimer = null;
  function hideSuccess() {
    const toast = document.getElementById("success-toast");
    if (toast) toast.classList.add("hidden");
    if (successTimer) { clearTimeout(successTimer); successTimer = null; }
    try { sessionStorage.removeItem(SUCCESS_KEY); } catch {}
  }
  window.showSuccess = function (message) {
    const toast = document.getElementById("success-toast");
    if (!toast) return;
    const text = message || "Changes saved.";
    const label = toast.querySelector(".success-toast-label");
    if (label) label.textContent = text;
    toast.classList.remove("hidden");
    try { sessionStorage.setItem(SUCCESS_KEY, text); } catch {}
    if (successTimer) clearTimeout(successTimer);
    successTimer = setTimeout(hideSuccess, 3500);
  };
  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-success-dismiss]")) hideSuccess();
  });
  document.body.addEventListener("flashSuccess", (e) => {
    window.showSuccess((e.detail || {}).message || "Changes saved.");
  });
  function restoreSuccess() {
    try {
      const message = sessionStorage.getItem(SUCCESS_KEY);
      if (message) {
        sessionStorage.removeItem(SUCCESS_KEY);
        window.showSuccess(message);
      }
    } catch {}
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", restoreSuccess);
  } else {
    restoreSuccess();
  }

  // Refresh the kanban board when the server asks (after edit/complete).
  document.body.addEventListener("reloadBoard", () => {
    // Simplest reliable refresh: reload the page. Small board, fine cost.
    // Only reload if we're on a kanban page.
    if (document.getElementById("kanban-board")) {
      window.location.reload();
    }
  });

  // ------------------- Undo toast -------------------
  // Server emits `showUndo` with { op_id, label, ttl_ms }. We persist to
  // localStorage FIRST so any following `reloadBoard` (which navigates the
  // page) can still restore the toast after the fresh load. The Undo button
  // POSTs to /undo/{op_id}; server reply fires `reloadBoard` + `undoCleared`
  // which pulls the entry from storage.
  const UNDO_KEY = "luigi.pendingUndo";
  const UNDO_FALLBACK_TTL_MS = 12000;

  function loadPendingUndo() {
    try {
      const raw = localStorage.getItem(UNDO_KEY);
      if (!raw) return null;
      const entry = JSON.parse(raw);
      if (!entry || !entry.op_id || !entry.expires_at) return null;
      if (Date.now() >= entry.expires_at) {
        localStorage.removeItem(UNDO_KEY);
        return null;
      }
      return entry;
    } catch { return null; }
  }
  function clearPendingUndo() {
    try { localStorage.removeItem(UNDO_KEY); } catch {}
    hideUndoToast();
  }

  let undoTimer = null;
  function hideUndoToast() {
    const toast = document.getElementById("undo-toast");
    if (!toast) return;
    toast.classList.add("hidden");
    if (undoTimer) { clearTimeout(undoTimer); undoTimer = null; }
  }
  function renderUndoToast(entry) {
    const toast = document.getElementById("undo-toast");
    if (!toast) return;
    const label = toast.querySelector(".undo-toast-label");
    const progress = toast.querySelector(".undo-toast-progress");
    if (label) label.textContent = entry.label || "Action complete";
    toast.dataset.opId = entry.op_id;
    toast.classList.remove("hidden");
    // Restart CSS progress animation with the exact remaining time.
    const remaining = Math.max(200, entry.expires_at - Date.now());
    if (progress) {
      progress.style.animation = "none";
      // Force reflow so the animation can restart cleanly.
      // eslint-disable-next-line no-unused-expressions
      progress.offsetWidth;
      progress.style.animation = `undo-progress ${remaining}ms linear forwards`;
    }
    if (undoTimer) clearTimeout(undoTimer);
    undoTimer = setTimeout(clearPendingUndo, remaining);
  }

  document.body.addEventListener("showUndo", (e) => {
    const d = e.detail || {};
    if (!d.op_id) return;
    const ttl = Number(d.ttl_ms) || UNDO_FALLBACK_TTL_MS;
    const entry = {
      op_id: d.op_id,
      label: d.label || "Action complete",
      expires_at: Date.now() + ttl,
    };
    // Persist BEFORE reloadBoard (fired in the same HX-Trigger batch after
    // this handler) navigates away. Storage survives the reload; the DOM
    // toast doesn't.
    try { localStorage.setItem(UNDO_KEY, JSON.stringify(entry)); } catch {}
    renderUndoToast(entry);
  });

  document.body.addEventListener("undoCleared", clearPendingUndo);

  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-undo-dismiss]")) {
      clearPendingUndo();
      return;
    }
    const btn = e.target.closest("[data-undo-btn]");
    if (!btn) return;
    const toast = document.getElementById("undo-toast");
    const opId = toast ? toast.dataset.opId : null;
    if (!opId) return;
    // Use htmx.ajax when available so the response's HX-Trigger fires
    // through the normal event pipeline (giving us reloadBoard).
    if (window.htmx && typeof window.htmx.ajax === "function") {
      window.htmx.ajax("POST", `/undo/${encodeURIComponent(opId)}`, { target: "body", swap: "none" });
    } else {
      fetch(`/undo/${encodeURIComponent(opId)}`, { method: "POST", credentials: "same-origin" })
        .then(() => window.location.reload());
    }
    clearPendingUndo();
  });

  // Re-render the toast on every page load if a fresh entry is still in
  // storage — this is how the toast survives the reloadBoard that follows
  // the mutation.
  function restoreUndoToast() {
    const entry = loadPendingUndo();
    if (entry) renderUndoToast(entry);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", restoreUndoToast);
  } else {
    restoreUndoToast();
  }

  // ------------------- Error toast -------------------
  // A single shared error toast for failed mutations. Driven by:
  //   - the `flashError` HX-Trigger event (server-sent, e.g. a discipline
  //     completion that didn't save),
  //   - htmx `responseError` / `sendError` events (non-2xx or network drop),
  //   - direct calls to window.showError(msg) from our own fetch() handlers.
  let errorToastTimer = null;
  window.showError = function (message) {
    const toast = document.getElementById("error-toast");
    if (!toast) {
      // Last-resort fallback so a failure is never fully silent.
      window.alert(message || "Something went wrong.");
      return;
    }
    const label = toast.querySelector(".error-toast-label");
    if (label) label.textContent = message || "Something went wrong. Please try again.";
    toast.classList.remove("hidden");
    if (errorToastTimer) clearTimeout(errorToastTimer);
    // Errors linger longer than the undo toast so they can be read.
    errorToastTimer = setTimeout(hideErrorToast, 9000);
  };
  function hideErrorToast() {
    const toast = document.getElementById("error-toast");
    if (toast) toast.classList.add("hidden");
    if (errorToastTimer) { clearTimeout(errorToastTimer); errorToastTimer = null; }
  }

  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-error-dismiss]")) hideErrorToast();
  });

  // Server-sent explicit error (HX-Trigger: flashError { message }).
  document.body.addEventListener("flashError", (e) => {
    const d = e.detail || {};
    window.showError(d.message || "The server reported an error.");
  });

  // Any HTMX request that comes back non-2xx. Prefer the server's plain-text
  // body (our /discipline/toggle sends a human message), else a generic note.
  document.body.addEventListener("htmx:responseError", (e) => {
    const xhr = (e.detail && e.detail.xhr) || null;
    let msg = "";
    if (xhr) {
      // Skip HTML error pages; only surface short plain-text bodies.
      const ct = xhr.getResponseHeader("Content-Type") || "";
      if (ct.indexOf("text/plain") !== -1 && xhr.responseText) {
        msg = xhr.responseText.trim();
      }
    }
    // If the response already fired a flashError trigger, that handler covers
    // it — avoid a duplicate generic toast.
    const hasTrigger = xhr && (xhr.getResponseHeader("HX-Trigger") || "").indexOf("flashError") !== -1;
    if (!hasTrigger) {
      window.showError(msg || `Request failed (${xhr ? xhr.status : "network"}). Nothing was saved.`);
    }
  });

  // Network-level failure (server unreachable, connection dropped).
  document.body.addEventListener("htmx:sendError", () => {
    window.showError("Couldn't reach the server — check the connection. Nothing was saved.");
  });

  // ------------------- At-risk discipline banner -------------------
  // Dismiss for the day: signature = "YYYY-MM-DD:<count>" so a new day OR a
  // changed count re-shows the banner even if the user dismissed yesterday's.
  const AT_RISK_DISMISS_KEY = "luigi.atRiskDismiss";

  function initAtRiskBanner() {
    const banner = document.querySelector("[data-at-risk]");
    if (!banner) return;
    const sig = banner.dataset.atRiskSignature || "";
    if (sig && localStorage.getItem(AT_RISK_DISMISS_KEY) === sig) {
      banner.remove();
      return;
    }
    banner.hidden = false;
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initAtRiskBanner);
  } else {
    initAtRiskBanner();
  }

  document.addEventListener("click", (e) => {
    const dismiss = e.target.closest("[data-at-risk-dismiss]");
    if (dismiss) {
      const banner = dismiss.closest("[data-at-risk]");
      if (banner) {
        const sig = banner.dataset.atRiskSignature || "";
        if (sig) localStorage.setItem(AT_RISK_DISMISS_KEY, sig);
        banner.remove();
      }
      return;
    }
    const done = e.target.closest("[data-at-risk-done]");
    if (done) {
      e.preventDefault();
      const fd = new FormData();
      fd.set("discipline_uuid", done.dataset.disciplineUuid || "");
      fd.set("task", done.dataset.task || "");
      fd.set("day", done.dataset.day || "");
      if (done.dataset.catagory) fd.set("catagory", done.dataset.catagory);
      fd.set("action", "mark");
      const originalText = done.textContent;
      done.disabled = true;
      done.textContent = "…";
      fetch("/discipline/toggle", {
        method: "POST",
        body: fd,
        credentials: "same-origin",
      })
        .then(async (resp) => {
          if (resp.ok) {
            window.location.reload();
            return;
          }
          // Surface the server's reason (plain text) instead of a silent
          // reload that would make it look saved when it wasn't.
          let msg = "";
          try { msg = (await resp.text()).trim(); } catch {}
          window.showError(msg || `Couldn't save (${resp.status}). Try again.`);
          done.disabled = false;
          done.textContent = originalText;
        })
        .catch(() => {
          window.showError("Couldn't reach the server — nothing was saved.");
          done.disabled = false;
          done.textContent = originalText;
        });
    }
  });

  // ------------------- Discipline "Done" (home widget) -------------------
  // Explicit fetch (not htmx) so we get deterministic feedback: the row is
  // removed ONLY after the server confirms the completion persisted; a failure
  // shows the error toast with the server's reason instead of silently
  // reverting or reloading the page.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-discipline-done]");
    if (!btn) return;
    e.preventDefault();
    const fd = new FormData();
    fd.set("discipline_uuid", btn.dataset.disciplineUuid || "");
    fd.set("task", btn.dataset.task || "");
    fd.set("day", btn.dataset.day || "");
    if (btn.dataset.catagory) fd.set("catagory", btn.dataset.catagory);
    fd.set("action", "mark");
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "…";
    fetch("/discipline/toggle", {
      method: "POST",
      body: fd,
      credentials: "same-origin",
    })
      .then(async (resp) => {
        if (!resp.ok) {
          let msg = "";
          try { msg = (await resp.text()).trim(); } catch {}
          window.showError(msg || `Couldn't save (${resp.status}). Try again.`);
          btn.disabled = false;
          btn.textContent = originalText;
          return;
        }
        // Confirmed persisted: reload so Pending, weekly totals, heatmap-derived
        // state, streaks, and at-risk widgets all reflect the same DB truth.
        window.location.reload();
      })
      .catch(() => {
        window.showError("Couldn't reach the server — nothing was saved.");
        btn.disabled = false;
        btn.textContent = originalText;
      });
  });

  // ------------------- Discipline page "Done today" -------------------
  // Unlike the legacy heatmap swap, this endpoint returns the state read from
  // a fresh DB connection after COMMIT. Paint exactly that response so a toast
  // can never claim success while the card still shows the old state.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-discipline-today]");
    if (!btn) return;
    e.preventDefault();
    const endpoint = btn.dataset.endpoint;
    const action = btn.dataset.action || "mark";
    if (!endpoint) return;
    const card = btn.closest("[data-discipline-card]");
    const originalText = btn.textContent;
    const originalTitle = btn.title;
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    btn.textContent = action === "mark" ? "Saving…" : "Clearing…";
    const form = new FormData();
    form.set("action", action);

    fetch(endpoint, { method: "POST", body: form, credentials: "same-origin" })
      .then(async (resp) => {
        if (!resp.ok) {
          let message = "";
          try { message = (await resp.text()).trim(); } catch {}
          throw new Error(message || `Couldn't save (${resp.status}).`);
        }
        const state = await resp.json();
        const marked = state.marked === true;
        btn.dataset.action = marked ? "unmark" : "mark";
        btn.textContent = marked ? "✓ Done today" : "Done today";
        btn.title = marked
          ? "Remove today’s completion"
          : "Mark this discipline complete today";
        btn.setAttribute("aria-pressed", String(marked));
        btn.classList.toggle("btn-primary", !marked);

        if (card) {
          const streak = card.querySelector("[data-discipline-streak]");
          if (streak) streak.textContent = `🔥 ${Number(state.streak) || 0}`;
          const todayCell = card.querySelector(`[data-day="${state.day || ""}"]`);
          if (todayCell) {
            todayCell.classList.toggle("is-marked", marked);
            todayCell.setAttribute("aria-label", `${state.day} — ${marked ? "done" : "not done"}`);
            todayCell.setAttribute("hx-vals", JSON.stringify({
              discipline_uuid: state.discipline_uuid,
              task: state.task,
              catagory: todayCell.getAttribute("data-catagory") || "",
              day: state.day,
              action: marked ? "unmark" : "mark",
            }));
          }
        }
        window.showSuccess(state.message || "Discipline updated.");
      })
      .catch((error) => {
        btn.textContent = originalText;
        btn.title = originalTitle;
        window.showError(error.message || "Couldn't save the discipline completion.");
      })
      .finally(() => {
        btn.disabled = false;
        btn.removeAttribute("aria-busy");
      });
  });

  // ------------------- Tasks Board/List view -------------------
  const TASK_VIEW_KEY = "luigi.tasks.view";

  function setTaskView(view) {
    const scope = document.querySelector("[data-tasks-scope]");
    if (!scope) return;
    const next = view === "list" ? "list" : "board";
    scope.querySelectorAll("[data-view-panel]").forEach((panel) => {
      panel.hidden = panel.dataset.viewPanel !== next;
    });
    scope.querySelectorAll("[data-task-view]").forEach((button) => {
      const active = button.dataset.taskView === next;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    scope.dataset.activeView = next;
    try { localStorage.setItem(TASK_VIEW_KEY, next); } catch {}
  }

  function initTaskView() {
    if (!document.querySelector("[data-tasks-scope]")) return;
    let saved = "board";
    try { saved = localStorage.getItem(TASK_VIEW_KEY) || "board"; } catch {}
    setTaskView(saved);
  }

  document.addEventListener("click", (e) => {
    const button = e.target.closest("[data-task-view]");
    if (button) setTaskView(button.dataset.taskView);
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initTaskView);
  } else {
    initTaskView();
  }

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
          const endpoint = card.dataset.endpoint || evt.to.dataset.endpoint;
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
          // Drag may have emptied the source column or filled the target;
          // reorder so empty columns fall to the end.
          if (typeof reorderEmptyLast === "function") reorderEmptyLast();
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

  // ------------------- Push-empty-to-end (home widgets + kanban columns) ---
  // Server-rendered widgets and kanban columns keep their natural
  // (semantic) order in the HTML. On the client we shove any container that
  // renders as "empty" to the end of its grid so the most important stuff
  // stays near the top of the viewport. This is DOM re-append, so CSS Grid
  // just reflows — no `order:` needed. Applied on load and after any HTMX
  // swap or drag-drop.
  function pushEmptyChildrenToEnd(container, isEmpty) {
    if (!container) return;
    Array.from(container.children)
      .filter((c) => isEmpty(c))
      .forEach((el) => {
        el.setAttribute("data-empty", "1");
        container.appendChild(el);
      });
    // Reset the flag on children that came back to being non-empty.
    Array.from(container.children)
      .filter((c) => !isEmpty(c))
      .forEach((el) => el.removeAttribute("data-empty"));
  }
  function widgetIsEmpty(widgetEl) {
    return !!widgetEl.querySelector(":scope > .widget-body > .empty");
  }
  function kanbanColumnIsEmpty(colEl) {
    const body = colEl.querySelector(".kanban-column-body");
    return !body || body.querySelectorAll(".card").length === 0;
  }
  function reorderEmptyLast() {
    pushEmptyChildrenToEnd(document.querySelector(".home-grid"), widgetIsEmpty);
    pushEmptyChildrenToEnd(document.getElementById("kanban-board"), kanbanColumnIsEmpty);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", reorderEmptyLast);
  } else {
    reorderEmptyLast();
  }
  document.body.addEventListener("htmx:afterSwap", reorderEmptyLast);

  // ------------------- Chat mic (Web Speech API, feature-detected) -------------------
  // Kept behind a runtime check so browsers without SpeechRecognition just see
  // a greyed-out button. When available AND the chat panel is enabled, one
  // ------------------- Chat: text-to-speech confirmations -------------------
  // Client-only. Uses window.speechSynthesis. Prefs persist in localStorage.
  //   luigi.tts.enabled  -> "1" | "0"
  //   luigi.tts.voice    -> voiceURI string
  const TTS_ENABLED_KEY = "luigi.tts.enabled";
  const TTS_VOICE_KEY = "luigi.tts.voice";

  function ttsSupported() {
    return typeof window !== "undefined" && "speechSynthesis" in window;
  }
  function ttsEnabled() {
    return localStorage.getItem(TTS_ENABLED_KEY) === "1";
  }
  function ttsGetVoice() {
    if (!ttsSupported()) return null;
    const uri = localStorage.getItem(TTS_VOICE_KEY);
    if (!uri) return null;
    return window.speechSynthesis.getVoices().find((v) => v.voiceURI === uri) || null;
  }
  function ttsSpeak(text) {
    if (!ttsSupported() || !ttsEnabled()) return;
    const clean = (text || "").trim();
    if (!clean) return;
    // Cancel any in-flight utterance so rapid replies don't queue up.
    window.speechSynthesis.cancel();
    const utter = new SpeechSynthesisUtterance(clean);
    const voice = ttsGetVoice();
    if (voice) { utter.voice = voice; utter.lang = voice.lang; }
    window.speechSynthesis.speak(utter);
  }

  function populateVoiceOptions(select) {
    if (!ttsSupported()) return;
    const voices = window.speechSynthesis.getVoices();
    const current = localStorage.getItem(TTS_VOICE_KEY) || "";
    // Preserve the default option, then rebuild the rest.
    select.querySelectorAll("option:not([value=''])").forEach((o) => o.remove());
    voices
      .slice()
      .sort((a, b) => (a.lang + a.name).localeCompare(b.lang + b.name))
      .forEach((v) => {
        const opt = document.createElement("option");
        opt.value = v.voiceURI;
        opt.textContent = `${v.name} (${v.lang})${v.default ? " — default" : ""}`;
        if (v.voiceURI === current) opt.selected = true;
        select.appendChild(opt);
      });
  }

  function initTtsSettings() {
    const wrap = document.querySelector("[data-tts-menu]");
    if (!wrap) return;
    const enabledEl = wrap.querySelector("[data-tts-enabled]");
    const voiceEl = wrap.querySelector("[data-tts-voice]");
    const testEl = wrap.querySelector("[data-tts-test]");

    if (!ttsSupported()) {
      enabledEl.disabled = true;
      voiceEl.disabled = true;
      testEl.disabled = true;
      wrap.title = "Text-to-speech is not supported in this browser";
      return;
    }

    enabledEl.checked = ttsEnabled();
    populateVoiceOptions(voiceEl);
    // Voices load async on Chrome — repopulate when the list changes.
    window.speechSynthesis.addEventListener?.("voiceschanged", () => populateVoiceOptions(voiceEl));

    enabledEl.addEventListener("change", () => {
      localStorage.setItem(TTS_ENABLED_KEY, enabledEl.checked ? "1" : "0");
      if (!enabledEl.checked) window.speechSynthesis.cancel();
    });
    voiceEl.addEventListener("change", () => {
      localStorage.setItem(TTS_VOICE_KEY, voiceEl.value || "");
    });
    testEl.addEventListener("click", () => {
      // Force-speak for the test even if the enabled toggle is off, so the
      // user can preview a voice before committing.
      window.speechSynthesis.cancel();
      const utter = new SpeechSynthesisUtterance("This is Luigi speaking.");
      const voice = ttsGetVoice();
      if (voice) { utter.voice = voice; utter.lang = voice.lang; }
      window.speechSynthesis.speak(utter);
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initTtsSettings);
  } else {
    initTtsSettings();
  }

  // Speak each new assistant reply after HTMX appends it to #chat-log.
  document.body.addEventListener("htmx:afterSwap", (e) => {
    if (!e.target || e.target.id !== "chat-log") return;
    // Grab only the just-appended assistant bubble text (skip tool-call log).
    const bubbles = e.target.querySelectorAll(".chat-msg-assistant .chat-bubble");
    const last = bubbles[bubbles.length - 1];
    if (!last) return;
    const clone = last.cloneNode(true);
    clone.querySelectorAll(".chat-tool-log").forEach((el) => el.remove());
    ttsSpeak(clone.textContent);
  });

  // ------------------- Chat voice-input mic (Web Speech Recognition) -------------------
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

  // ------------------- Card action menus (close on outside click) ----------
  // Native <details> handles open/close; this keeps only the active menu open
  // and clears menus after an HTMX action resolves.
  document.addEventListener("click", (e) => {
    document.querySelectorAll("details[data-action-menu][open], details[data-snooze-menu][open]").forEach((d) => {
      if (!d.contains(e.target)) d.removeAttribute("open");
    });
  });
  document.body.addEventListener("htmx:afterSwap", () => {
    document.querySelectorAll("details[data-action-menu][open], details[data-snooze-menu][open]")
      .forEach((d) => d.removeAttribute("open"));
  });

  // ------------------- Date picker (task form "Due date") -------------------
  // Native <input type="date"> keeps the OS calendar; keyboard typing is
  // already blocked by onkeydown on the element. Here we just wire the
  // quick-preset chips (Today / Tomorrow / +1w / +2w / Clear) and keep the
  // active chip highlighted so the user always sees what got picked.
  function localIsoDate(d) {
    const year = d.getFullYear();
    const month = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function initDatePickers(root) {
    const scope = root && root.querySelectorAll ? root : document;
    scope.querySelectorAll("[data-datepicker]").forEach((dp) => {
      if (dp.dataset.dpInit === "1") return;
      dp.dataset.dpInit = "1";
      const input = dp.querySelector("[data-datepicker-input]");
      if (!input) return;

      const paint = () => {
        const v = input.value || "";
        const today = new Date();
        today.setHours(0, 0, 0, 0);
        dp.querySelectorAll("[data-date-preset]").forEach((btn) => {
          const p = btn.getAttribute("data-date-preset");
          let match = false;
          if (p === "clear") {
            match = v === "";
          } else {
            const days = parseInt(p, 10);
            if (!Number.isNaN(days)) {
              const d = new Date(today);
              d.setDate(d.getDate() + days);
              match = v === localIsoDate(d);
            }
          }
          btn.classList.toggle("is-active", match);
        });
      };

      dp.querySelectorAll("[data-date-preset]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const p = btn.getAttribute("data-date-preset");
          if (p === "clear") {
            input.value = "";
          } else {
            const days = parseInt(p, 10) || 0;
            const d = new Date();
            d.setHours(0, 0, 0, 0);
            d.setDate(d.getDate() + days);
            input.value = localIsoDate(d);
          }
          paint();
        });
      });
      input.addEventListener("change", paint);
      paint();
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => initDatePickers());
  } else {
    initDatePickers();
  }
  document.body.addEventListener("htmx:afterSwap", (e) => {
    initDatePickers(e.target);
  });

  // ------------------- Recurring toggle (task form) -------------------
  // Show/hide the "Repeat every (days)" input based on the Recurring checkbox
  // and wire preset chips (Daily / Weekly / Bi-weekly / Monthly). Unchecking
  // clears the interval so the server stores NULL.
  function initRecurringToggle(root) {
    const scope = root && root.querySelectorAll ? root : document;
    scope.querySelectorAll("[data-recurring-toggle]").forEach((cb) => {
      if (cb.dataset.recInit === "1") return;
      cb.dataset.recInit = "1";
      const form = cb.closest("form") || cb.closest("[data-recurring-row]").parentElement;
      const row = cb.closest("[data-recurring-row]") || cb.parentElement;
      const fields = row.querySelector("[data-recurring-fields]");
      const input = row.querySelector("[data-recurring-interval]");
      if (!fields || !input) return;

      // Sibling weekday row lives outside the interval row so the fieldset
      // can span the full width. It's optional — /tasks doesn't render it.
      const daysRow = form ? form.querySelector("[data-recurring-days-row]") : null;
      const dayInputs = daysRow
        ? daysRow.querySelectorAll('input[name="recurring_days"]')
        : [];

      const validateSchedule = () => {
        const hasWeekday = Array.from(dayInputs).some((el) => el.checked);
        const missing = cb.checked && !hasWeekday && !String(input.value || "").trim();
        input.setCustomValidity(missing
          ? "Choose at least one weekday or enter a repeat interval."
          : "");
      };

      const paintChips = () => {
        const v = String(input.value || "").trim();
        row.querySelectorAll("[data-recurring-preset]").forEach((btn) => {
          btn.classList.toggle("is-active", btn.getAttribute("data-recurring-preset") === v);
        });
      };

      const sync = () => {
        if (cb.checked) {
          fields.removeAttribute("hidden");
          if (daysRow) daysRow.removeAttribute("hidden");
        } else {
          fields.setAttribute("hidden", "");
          input.value = "";
          if (daysRow) {
            daysRow.setAttribute("hidden", "");
            dayInputs.forEach((el) => { el.checked = false; });
          }
          paintChips();
        }
        validateSchedule();
      };

      row.querySelectorAll("[data-recurring-preset]").forEach((btn) => {
        btn.addEventListener("click", () => {
          input.value = btn.getAttribute("data-recurring-preset") || "";
          if (!cb.checked) { cb.checked = true; sync(); }
          paintChips();
          validateSchedule();
        });
      });
      input.addEventListener("input", () => { paintChips(); validateSchedule(); });
      dayInputs.forEach((el) => el.addEventListener("change", validateSchedule));
      cb.addEventListener("change", sync);
      sync();
      paintChips();
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => initRecurringToggle());
  } else {
    initRecurringToggle();
  }
  document.body.addEventListener("htmx:afterSwap", (e) => {
    initRecurringToggle(e.target);
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
    return localIsoDate(d);
  }
  function weekBounds() {
    // Monday..Sunday for the current local week.
    const today = new Date();
    const dow = (today.getDay() + 6) % 7;   // Mon=0..Sun=6
    const mon = new Date(today);
    mon.setDate(mon.getDate() - dow);
    return { mon: localIsoDate(mon),
             sun: isoAddDays(mon, 6),
         today: localIsoDate(today) };
  }

  function cardMatches(card, state, wk) {
    // Text search hits title + category + groups.
    if (state.q) {
      const hay = [
        card.dataset.title,
        card.dataset.project,
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
      case "awaiting-reactivation":
        // Completed recurring rows only; the server precomputed the next
        // reactivation date into data-reactivation-date (see task_card.html
        // and db.reactivation_date). Rows without one — non-recurring, or
        // missing an interval — are hidden.
        if (!completed) return false;
        if (!card.dataset.reactivationDate) return false;
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
    scope.querySelectorAll("[data-task-list-row]").forEach((row) => {
      row.classList.toggle("card-filtered-out", !cardMatches(row, state, wk));
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
