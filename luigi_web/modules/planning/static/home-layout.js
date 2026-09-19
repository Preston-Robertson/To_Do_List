(function () {
  "use strict";

  const KEY = "luigi.home.layout";
  const LEGACY_KEY = "luigi.home.hiddenWidgets";
  const LABELS = {
    "open-tasks": "Tasks", "disc-pending": "Habits", "task-week": "Today's progress",
    upcoming: "Coming up", "gnw-playing": "Continue", overdue: "Overdue",
    "gnw-watching": "Currently watching", "disc-streaks": "Discipline streaks",
    "follow-ups": "Follow-ups", "recent-done": "Recent completions",
    "weekly-review": "Weekly review", "disc-week": "Discipline this week",
    activity: "Recent activity",
  };
  const IDS = Object.keys(LABELS);
  const copy = (value) => JSON.parse(JSON.stringify(value));
  const defaults = () => ({ version: 1, order: [...IDS], hidden: [], pinned: [] });

  function validate(value) {
    if (!value || value.version !== 1 || Object.keys(value).sort().join() !== "hidden,order,pinned,version") {
      throw new Error("Invalid layout");
    }
    for (const field of ["order", "hidden", "pinned"]) {
      const items = value[field];
      if (!Array.isArray(items) || items.length > IDS.length || new Set(items).size !== items.length
          || items.some((id) => !IDS.includes(id))) throw new Error("Invalid layout");
    }
    return { version: 1, order: [...value.order, ...IDS.filter((id) => !value.order.includes(id))],
      hidden: [...value.hidden], pinned: [...value.pinned] };
  }

  function store(state) {
    const previous = localStorage.getItem(KEY);
    const encoded = JSON.stringify(state);
    try {
      localStorage.setItem(KEY, encoded);
      if (localStorage.getItem(KEY) !== encoded) throw new Error("Storage verification failed");
    } catch (error) {
      try {
        if (previous === null) localStorage.removeItem(KEY);
        else localStorage.setItem(KEY, previous);
      } catch (_) {}
      throw error;
    }
  }

  function icon(name) {
    const element = document.createElement("span");
    element.className = `home-layout-icon home-layout-icon--${name}`;
    element.setAttribute("aria-hidden", "true");
    return element;
  }

  function verifyStorageAccess() {
    const previous = localStorage.getItem(KEY);
    const probe = previous === null ? "" : previous;
    try {
      localStorage.setItem(KEY, probe);
      if (localStorage.getItem(KEY) !== probe) throw new Error("Storage verification failed");
    } finally {
      if (previous === null) localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, previous);
    }
    if (localStorage.getItem(KEY) !== previous) throw new Error("Storage verification failed");
  }

  function initialize() {
    const grid = document.querySelector("[data-home-layout]");
    const dialog = document.getElementById("home-layout-dialog");
    const trigger = document.querySelector("[data-home-layout-open]");
    if (!grid || !dialog || !trigger || grid.dataset.homeLayoutReady) return;
    grid.dataset.homeLayoutReady = "true";
    const widgets = new Map(Array.from(grid.querySelectorAll(".widget[data-widget]"))
      .filter((widget) => IDS.includes(widget.dataset.widget))
      .map((widget) => [widget.dataset.widget, widget]));
    const parents = new Map([...widgets].map(([id, widget]) => [id, widget.parentElement]));
    const list = dialog.querySelector("[data-home-layout-list]");
    const form = dialog.querySelector("form");
    const pageStatus = document.querySelector("[data-home-layout-status]");
    const errorStatus = dialog.querySelector("[data-home-layout-error]");
    const announcement = dialog.querySelector("[data-home-layout-announcement]");
    const saveLabel = dialog.querySelector("[data-home-layout-save-label]");
    let committed = { version: 1, acrossDevices: false, layout: defaults() };
    let draft = null;
    let busy = false;

    function message(element, text, error = false) {
      element.textContent = text;
      element.hidden = !text;
      element.classList.toggle("is-error", error);
    }

    function ordered(layout) {
      return [...layout.order.filter((id) => layout.pinned.includes(id)),
        ...layout.order.filter((id) => !layout.pinned.includes(id))];
    }

    for (const widget of widgets.values()) {
      const pin = document.createElement("span");
      pin.className = "home-layout-pin";
      pin.title = "Pinned";
      pin.setAttribute("role", "img");
      pin.setAttribute("aria-label", "Pinned");
      pin.append(icon("pin"));
      pin.hidden = true;
      widget.querySelector(".widget-header")?.append(pin);
    }

    function apply(layout) {
      for (const id of ordered(layout)) {
        const widget = widgets.get(id);
        if (!widget) continue;
        widget.hidden = layout.hidden.includes(id);
        widget.classList.remove("is-hidden");
        const pin = widget.querySelector(".home-layout-pin");
        if (pin) pin.hidden = !layout.pinned.includes(id);
        parents.get(id).append(widget);
      }
      const empty = document.querySelector("[data-home-layout-empty]");
      if (empty) empty.hidden = [...widgets.keys()].some((id) => !layout.hidden.includes(id));
    }

    function groupFor(id) {
      return draft.layout.order.filter((candidate) => widgets.has(candidate)
        && parents.get(candidate) === parents.get(id)
        && draft.layout.pinned.includes(candidate) === draft.layout.pinned.includes(id));
    }

    function render(focusKey) {
      list.replaceChildren();
      const lanes = [...new Set(parents.values())];
      const displayed = lanes.flatMap((parent) => ordered(draft.layout)
        .filter((id) => widgets.has(id) && parents.get(id) === parent));
      let previousParent = null;
      for (const id of displayed) {
        const parent = parents.get(id);
        if (parent !== previousParent && parent.dataset.homeZone) {
          const heading = document.createElement("li");
          heading.className = "home-layout-zone";
          heading.textContent = parent.dataset.homeZone === "main" ? "Main" : "Day overview";
          list.append(heading);
        }
        previousParent = parent;
        const row = document.createElement("li");
        row.className = "home-layout-row";
        const label = document.createElement("label");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = !draft.layout.hidden.includes(id);
        checkbox.dataset.layoutId = id;
        checkbox.dataset.layoutAction = "visible";
        checkbox.dataset.layoutFocus = `${id}:visible`;
        checkbox.setAttribute("aria-label", `Show ${LABELS[id]}`);
        const title = document.createElement("span");
        title.textContent = LABELS[id];
        label.append(checkbox, title);
        row.append(label);
        const group = groupFor(id);
        for (const action of ["pin", "up", "down"]) {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "home-layout-tool";
          button.dataset.layoutId = id;
          button.dataset.layoutAction = action;
          button.dataset.layoutFocus = `${id}:${action}`;
          const pinned = draft.layout.pinned.includes(id);
          const caption = action === "pin" ? `${pinned ? "Unpin" : "Pin"} ${LABELS[id]}`
            : `Move ${LABELS[id]} ${action}`;
          button.title = caption;
          button.setAttribute("aria-label", caption);
          if (action === "pin") button.setAttribute("aria-pressed", String(pinned));
          if (action === "up") button.disabled = group.indexOf(id) === 0;
          if (action === "down") button.disabled = group.indexOf(id) === group.length - 1;
          button.append(icon(action));
          row.append(button);
        }
        list.append(row);
      }
      for (const radio of dialog.querySelectorAll('[name="home-layout-scope"]')) {
        radio.checked = (radio.value === "shared") === draft.acrossDevices;
      }
      if (focusKey) {
        const control = list.querySelector(`[data-layout-focus="${focusKey}"]`);
        const target = control?.disabled ? control.closest("li").querySelector("input") : control;
        target?.focus({ preventScroll: true });
      }
    }

    function setBusy(value, saving = false) {
      busy = value;
      form.setAttribute("aria-busy", String(value));
      trigger.disabled = value;
      for (const control of form.querySelectorAll("button, input")) control.disabled = value;
      saveLabel.textContent = value && saving ? "Saving..." : "Save";
      if (!value && draft) render();
    }

    async function shared(method, layout) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 10000);
      try {
        const response = await window.fetch(grid.dataset.homePreferences, {
          method, credentials: "same-origin", cache: "no-store", redirect: "error",
          headers: { Accept: "application/json", ...(method === "PUT" ? { "Content-Type": "application/json" } : {}) },
          ...(method === "PUT" ? { body: JSON.stringify(layout) } : {}),
          signal: controller.signal,
        });
        if (!response.ok) throw new Error("Shared layout unavailable");
        const raw = await response.text();
        if (raw.length > 4096) throw new Error("Invalid layout");
        const result = JSON.parse(raw);
        if (typeof result.saved !== "boolean" || (!result.saved && result.layout !== null)) {
          throw new Error("Invalid layout");
        }
        return result.saved ? validate(result.layout) : null;
      } finally {
        clearTimeout(timer);
      }
    }

    try {
      const raw = localStorage.getItem(KEY);
      if (raw !== null) {
        if (raw.length > 8192) throw new Error("Invalid layout");
        const stored = JSON.parse(raw);
        if (stored?.version !== 1 || typeof stored.acrossDevices !== "boolean"
            || Object.keys(stored).sort().join() !== "acrossDevices,layout,version") throw new Error("Invalid layout");
        committed = { version: 1, acrossDevices: stored.acrossDevices, layout: validate(stored.layout) };
        if (!committed.acrossDevices) message(pageStatus, "Saved on this browser");
      } else {
        const legacy = localStorage.getItem(LEGACY_KEY);
        if (legacy !== null) {
          if (legacy.length > 4096) throw new Error("Invalid layout");
          const hidden = JSON.parse(legacy);
          if (!Array.isArray(hidden)) throw new Error("Invalid layout");
          committed.layout.hidden = IDS.filter((id) => hidden.includes(id));
          store(committed);
          message(pageStatus, "Saved on this browser");
        }
      }
    } catch (_) {
      message(pageStatus, "Could not load browser layout.", true);
    }
    apply(committed.layout);
    trigger.hidden = false;

    trigger.addEventListener("click", () => {
      if (busy || dialog.open) return;
      if (document.body.classList.contains("command-open")) window.closeCommandPalette?.();
      if (document.body.classList.contains("modal-open")) window.closeModal?.();
      document.querySelectorAll("dialog[open]").forEach((other) => other.close());
      draft = copy(committed);
      message(errorStatus, "");
      render();
      dialog.showModal();
    });
    dialog.addEventListener("cancel", (event) => {
      if (busy) event.preventDefault();
    });
    dialog.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        if (!busy) dialog.close();
      } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        if (busy) event.stopPropagation();
        else dialog.close();
      }
    });
    dialog.addEventListener("close", () => {
      if (dialog.open) return;
      apply(committed.layout);
      draft = null;
      if (!document.querySelector("dialog[open]") && !document.body.classList.contains("command-open")
          && !document.body.classList.contains("modal-open")) trigger.focus({ preventScroll: true });
    });
    dialog.querySelectorAll("[data-home-layout-cancel]").forEach((button) => {
      button.addEventListener("click", () => { if (!busy) dialog.close(); });
    });
    dialog.querySelector("[data-home-layout-reset]").addEventListener("click", () => {
      if (busy || !draft) return;
      draft.layout = defaults();
      render();
      apply(draft.layout);
      message(errorStatus, "");
      announcement.textContent = "Defaults restored in draft.";
    });

    list.addEventListener("change", (event) => {
      const control = event.target;
      if (busy || !draft || control.dataset.layoutAction !== "visible") return;
      const id = control.dataset.layoutId;
      draft.layout.hidden = draft.layout.hidden.filter((candidate) => candidate !== id);
      if (!control.checked) draft.layout.hidden.push(id);
      apply(draft.layout);
      message(errorStatus, "");
    });
    list.addEventListener("click", (event) => {
      const control = event.target.closest("button[data-layout-action]");
      if (!control || busy || !draft) return;
      const id = control.dataset.layoutId;
      const action = control.dataset.layoutAction;
      if (action === "pin") {
        const wasPinned = draft.layout.pinned.includes(id);
        draft.layout.pinned = draft.layout.pinned.filter((candidate) => candidate !== id);
        if (!wasPinned) draft.layout.pinned.push(id);
        announcement.textContent = `${LABELS[id]} ${wasPinned ? "unpinned" : "pinned"}.`;
      } else {
        const group = groupFor(id);
        const neighbor = group[group.indexOf(id) + (action === "up" ? -1 : 1)];
        if (!neighbor) return;
        const currentIndex = draft.layout.order.indexOf(id);
        const neighborIndex = draft.layout.order.indexOf(neighbor);
        [draft.layout.order[currentIndex], draft.layout.order[neighborIndex]] = [neighbor, id];
        announcement.textContent = `${LABELS[id]} moved ${action}.`;
      }
      render(control.dataset.layoutFocus);
      apply(draft.layout);
      message(errorStatus, "");
    });

    form.addEventListener("change", async (event) => {
      if (event.target.name !== "home-layout-scope" || busy || !draft) return;
      const acrossDevices = event.target.value === "shared";
      if (!acrossDevices) {
        draft.acrossDevices = false;
        message(errorStatus, "");
        return;
      }
      const previous = copy(draft);
      setBusy(true);
      message(errorStatus, "");
      try {
        const layout = await shared("GET");
        draft.acrossDevices = true;
        if (layout) draft.layout = layout;
        apply(draft.layout);
      } catch (_) {
        draft = previous;
        apply(committed.layout);
        message(errorStatus, "Could not load shared layout.", true);
      } finally {
        setBusy(false);
        if (dialog.open) dialog.querySelector('[name="home-layout-scope"]:checked')?.focus();
      }
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (busy || !draft) return;
      const next = copy(draft);
      setBusy(true, true);
      message(errorStatus, "");
      let sharedSaved = false;
      try {
        if (next.acrossDevices) {
          verifyStorageAccess();
          const saved = await shared("PUT", next.layout);
          if (!saved || JSON.stringify(saved) !== JSON.stringify(validate(next.layout))) {
            throw new Error("Save verification failed");
          }
          sharedSaved = true;
          next.layout = saved;
        }
        store(next);
        committed = next;
        apply(committed.layout);
        message(pageStatus, next.acrossDevices ? "Saved across devices" : "Saved on this browser");
        dialog.close();
      } catch (_) {
        apply(committed.layout);
        message(errorStatus, sharedSaved ? "Shared layout saved. Browser settings failed."
          : next.acrossDevices ? "Could not save shared layout." : "Could not save browser layout.", true);
      } finally {
        setBusy(false);
        if (dialog.open) dialog.querySelector("[data-home-layout-save]").focus();
      }
    });

    if (committed.acrossDevices) {
      setBusy(true);
      shared("GET").then((layout) => {
        if (!layout) {
          message(pageStatus, "No shared layout saved.");
          return;
        }
        const next = { ...committed, layout };
        store(next);
        committed = next;
        apply(layout);
        message(pageStatus, "Saved across devices");
      }).catch(() => {
        message(pageStatus, "Could not load shared layout. Browser layout retained.", true);
      }).finally(() => setBusy(false));
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize);
  else initialize();
  document.addEventListener("htmx:afterSwap", initialize);
})();