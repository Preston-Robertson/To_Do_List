/* Character sheet tabs and context-aware drawer fields. */
(function () {
  "use strict";

  const TAB_NAMES = new Set(["overview", "actions", "spells", "inventory", "features", "notes"]);

  function selectedTab(workspace) {
    const fromHash = window.location.hash.replace(/^#/, "");
    if (TAB_NAMES.has(fromHash)) return fromHash;
    const characterId = workspace.querySelector("[data-character-id]")?.dataset.characterId;
    if (!characterId) return "overview";
    try {
      const stored = sessionStorage.getItem(`luigi.rpg.tab.${characterId}`);
      return TAB_NAMES.has(stored) ? stored : "overview";
    } catch {
      return "overview";
    }
  }

  function activateTab(workspace, name, moveFocus = false) {
    if (!TAB_NAMES.has(name)) name = "overview";
    const buttons = Array.from(workspace.querySelectorAll("[data-rpg-tab]"));
    buttons.forEach((button) => {
      const selected = button.dataset.rpgTab === name;
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
      if (selected && moveFocus) button.focus();
    });
    workspace.querySelectorAll("[data-rpg-panel]").forEach((panel) => {
      panel.hidden = panel.dataset.rpgPanel !== name;
    });
    const characterId = workspace.querySelector("[data-character-id]")?.dataset.characterId;
    if (characterId) {
      try { sessionStorage.setItem(`luigi.rpg.tab.${characterId}`, name); } catch {}
    }
    if (window.location.hash && window.location.hash !== `#${name}`) {
      history.replaceState(null, "", `${window.location.pathname}${window.location.search}#${name}`);
    }
  }

  function initTabs(root = document) {
    root.querySelectorAll("[data-rpg-workspace]").forEach((workspace) => {
      if (workspace.dataset.rpgTabsReady === "true") return;
      workspace.dataset.rpgTabsReady = "true";
      const buttons = Array.from(workspace.querySelectorAll("[data-rpg-tab]"));
      if (!buttons.length) return;
      buttons.forEach((button, index) => {
        button.addEventListener("click", () => activateTab(workspace, button.dataset.rpgTab));
        button.addEventListener("keydown", (event) => {
          let nextIndex = index;
          if (event.key === "ArrowRight") nextIndex = (index + 1) % buttons.length;
          else if (event.key === "ArrowLeft") nextIndex = (index - 1 + buttons.length) % buttons.length;
          else if (event.key === "Home") nextIndex = 0;
          else if (event.key === "End") nextIndex = buttons.length - 1;
          else return;
          event.preventDefault();
          activateTab(workspace, buttons[nextIndex].dataset.rpgTab, true);
        });
      });
      activateTab(workspace, selectedTab(workspace));
    });
  }

  function syncEntryFields(form) {
    const kind = form.querySelector("[data-rpg-entry-kind]")?.value;
    if (!kind) return;
    form.querySelectorAll("[data-entry-for]").forEach((field) => {
      field.hidden = !field.dataset.entryFor.split(" ").includes(kind);
    });
  }

  function initEntryForms(root = document) {
    root.querySelectorAll("[data-rpg-entry-form]").forEach((form) => {
      if (form.dataset.rpgEntryReady === "true") return;
      form.dataset.rpgEntryReady = "true";
      const select = form.querySelector("[data-rpg-entry-kind]");
      select?.addEventListener("change", () => syncEntryFields(form));
      syncEntryFields(form);
    });
  }

  function init(root = document) {
    initTabs(root);
    initEntryForms(root);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => init());
  } else {
    init();
  }

  document.body.addEventListener("htmx:afterSwap", (event) => init(event.detail.target));
})();