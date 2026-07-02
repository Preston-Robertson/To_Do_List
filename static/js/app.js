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
})();
