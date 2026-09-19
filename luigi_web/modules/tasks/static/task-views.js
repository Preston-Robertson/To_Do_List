(() => {
  "use strict";

  const PREFIX = "luigi.tasks.views.v1.";
  const SAVED_KEY = "luigi.tasks.savedFilters";
  const MAX_VIEWS = 30;
  const SMART = ["", "open", "overdue", "upcoming", "due-week", "no-due", "high-priority", "completed-week", "awaiting-reactivation", "recurring", "completed"];
  const copy = (value) => JSON.parse(JSON.stringify(value));
  const sourceKey = (record) => `${record.taskSource}:${record.uuid}`;
  const exact = (value, keys) => value !== null && typeof value === "object"
    && !Array.isArray(value) && Object.keys(value).length === keys.length
    && keys.every((key) => Object.hasOwn(value, key));
  const boundedText = (value) => typeof value === "string" && value.length <= 240;
  const validName = (value) => typeof value === "string" && value === value.trim()
    && value.length > 0 && value.length <= 40 && !/[\u0000-\u001f\u007f]/.test(value);

  function defaults(statuses, mode = "board") {
    return {
      filters: { text: "", project: "", status: "", source: "any", category: "", minPriority: 0, smart: "" },
      sort: "default", groupList: "none", mode, density: "comfortable",
      shownColumns: [...statuses], collapsedColumns: [],
    };
  }

  function validView(view, statuses) {
    if (!exact(view, ["filters", "sort", "groupList", "mode", "density", "shownColumns", "collapsedColumns"])) return false;
    const filters = view.filters;
    if (!exact(filters, ["text", "project", "status", "source", "category", "minPriority", "smart"])) return false;
    if (![filters.text, filters.project, filters.category].every(boundedText)) return false;
    if (!["", ...statuses].includes(filters.status) || !["any", "task", "recurring"].includes(filters.source)) return false;
    if (!Number.isInteger(filters.minPriority) || filters.minPriority < 0 || filters.minPriority > 10 || !SMART.includes(filters.smart)) return false;
    if (!["default", "due", "priority", "title"].includes(view.sort) || !["none", "project", "status"].includes(view.groupList)) return false;
    if (!["board", "list"].includes(view.mode) || !["comfortable", "compact"].includes(view.density)) return false;
    const validColumns = (columns) => Array.isArray(columns) && columns.length <= statuses.length
      && columns.every((status) => typeof status === "string" && statuses.includes(status))
      && new Set(columns).size === columns.length;
    return validColumns(view.shownColumns) && view.shownColumns.length > 0 && validColumns(view.collapsedColumns);
  }

  function validStore(store, statuses) {
    if (!exact(store, ["version", "current", "views", "active"]) || store.version !== 1 || !validView(store.current, statuses)) return false;
    if (!Array.isArray(store.views) || store.views.length > MAX_VIEWS || typeof store.active !== "string") return false;
    if (!store.views.every((entry) => exact(entry, ["name", "view"]) && validName(entry.name) && validView(entry.view, statuses))) return false;
    if (new Set(store.views.map((entry) => entry.name)).size !== store.views.length) return false;
    return store.active === "" || store.views.some((entry) => entry.name === store.active);
  }

  function legacyFilter(filter, statuses, mode) {
    if (!exact(filter, ["q", "smart", "minPrio", "catagory"])) return null;
    const view = defaults(statuses, mode);
    Object.assign(view.filters, { text: filter.q, smart: filter.smart, minPriority: filter.minPrio, category: filter.catagory });
    return validView(view, statuses) ? view : null;
  }

  function migrateLegacy(saved, active, statuses, mode = "board") {
    const store = { version: 1, current: legacyFilter(active, statuses, mode) || defaults(statuses, mode), views: [], active: "" };
    if (!Array.isArray(saved)) return store;
    for (const entry of saved.slice(0, 300)) {
      if (!exact(entry, ["name", "filter"]) || !validName(entry.name)) continue;
      const view = legacyFilter(entry.filter, statuses, mode);
      if (!view || store.views.some((existing) => existing.name === entry.name)) continue;
      store.views.push({ name: entry.name, view });
      if (store.views.length === MAX_VIEWS) break;
    }
    return store;
  }

  function parse(raw) {
    if (typeof raw !== "string" || raw.length > 131072) return null;
    try { return JSON.parse(raw); } catch { return null; }
  }

  function writeStore(storage, scope, store, statuses) {
    if (!validStore(store, statuses)) return false;
    try {
      const raw = JSON.stringify(store);
      storage.setItem(PREFIX + scope, raw);
      return storage.getItem(PREFIX + scope) === raw;
    } catch { return false; }
  }

  function readStore(storage, scope, statuses, mobile) {
    const fresh = () => ({ version: 1, current: defaults(statuses), views: [], active: "" });
    try {
      const raw = storage.getItem(PREFIX + scope);
      if (raw !== null) {
        const store = parse(raw);
        if (validStore(store, statuses)) return { store, issue: "" };
        return { store: fresh(), issue: "Saved view settings could not be read. Defaults are active; stored settings have not been replaced." };
      }
      const remembered = storage.getItem(`luigi.tasks.view.${mobile ? "mobile" : "desktop"}`)
        || (mobile ? "board" : storage.getItem("luigi.tasks.view"));
      const mode = remembered === "list" ? "list" : "board";
      const saved = parse(storage.getItem(SAVED_KEY));
      const active = parse(storage.getItem(`luigi.tasks.activeFilter.${scope}`));
      const store = migrateLegacy(saved, active, statuses, mode);
      const confirmed = writeStore(storage, scope, store, statuses);
      return { store, issue: confirmed ? "" : "Browser storage is unavailable. Changes apply to this page only; existing saved filters are unchanged." };
    } catch {
      return { store: fresh(), issue: "Browser storage is unavailable. Changes apply to this page only." };
    }
  }

  function localDate(date) {
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  }

  function weekBounds(now = new Date()) {
    if (typeof now === "string" && /^\d{4}-\d{2}-\d{2}$/.test(now)) now = new Date(`${now}T12:00:00`);
    const monday = new Date(now);
    monday.setDate(monday.getDate() - ((monday.getDay() + 6) % 7));
    const sunday = new Date(monday);
    sunday.setDate(sunday.getDate() + 6);
    return { today: localDate(now), mon: localDate(monday), sun: localDate(sunday) };
  }

  function dateOnly(value) {
    const date = String(value || "").slice(0, 10);
    return /^\d{4}-\d{2}-\d{2}$/.test(date) ? date : "";
  }

  function matches(record, filters, week) {
    const query = filters.text.trim().toLowerCase();
    const haystack = [record.title, record.project, record.catagory, record.taskGroup, record.subGroup].join(" ").toLowerCase();
    if (query && !haystack.includes(query)) return false;
    if (filters.project && (record.project || "") !== filters.project) return false;
    if (filters.category && (record.catagory || "") !== filters.category) return false;
    if (filters.status && record.status !== filters.status) return false;
    if (filters.source !== "any" && record.taskSource !== filters.source) return false;
    const priority = Number(record.priority) || 0;
    if (priority < filters.minPriority) return false;
    const due = dateOnly(record.dueDate);
    const completedTime = dateOnly(record.completedTime);
    const completed = record.completed === "1" || (record.status || "").toLowerCase() === "completed";
    switch (filters.smart) {
      case "open": return !completed;
      case "overdue": return !completed && !!due && due < week.today;
      case "upcoming": return !completed && !!due && due >= week.today;
      case "due-week": return !completed && !!due && due >= week.mon && due <= week.sun;
      case "no-due": return !completed && !due;
      case "high-priority": return !completed && priority >= 5;
      case "completed-week": return completed && !!completedTime && completedTime >= week.mon && completedTime <= week.sun;
      case "awaiting-reactivation": return completed && !!record.reactivationDate;
      case "recurring": return record.taskSource === "recurring";
      case "completed": return completed;
      default: return true;
    }
  }

  function compareRecords(left, right, sort) {
    if (sort === "due") return (dateOnly(left.dueDate) || "9999-12-31").localeCompare(dateOnly(right.dueDate) || "9999-12-31");
    if (sort === "priority") return (Number(right.priority) || 0) - (Number(left.priority) || 0);
    if (sort === "title") return (left.title || "").localeCompare(right.title || "");
    return 0;
  }

  function mount(scope) {
    if (scope.taskViewsController) return scope.taskViewsController;
    const columns = [...scope.querySelectorAll("[data-task-column]")];
    const statuses = columns.map((column) => column.dataset.status);
    if (!statuses.length) return null;
    const scopeName = scope.dataset.tasksScope || "default";
    const find = (selector) => scope.querySelector(selector);
    const notice = (message, error = false) => {
      const target = find("[data-task-views-notice]");
      target.textContent = message;
      target.hidden = !message;
      target.dataset.error = String(error);
    };
    let storage;
    try { storage = window.localStorage; } catch {}
    const loaded = readStore(storage, scopeName, statuses, window.matchMedia("(max-width: 620px)").matches);
    let store = loaded.store;
    const boardOrder = new Map();
    const listOrder = new Map();
    const wiredSortables = new WeakSet();
    const bindings = {
      "[data-filter-search]": ["filters", "text"],
      "[data-filter-smartlist]": ["filters", "smart"],
      "[data-filter-project]": ["filters", "project"],
      "[data-filter-status]": ["filters", "status"],
      "[data-filter-source]": ["filters", "source"],
      "[data-filter-catagory]": ["filters", "category"],
      "[data-filter-priority]": ["filters", "minPriority"],
      "[data-task-sort]": ["sort"], "[data-task-group-list]": ["groupList"], "[data-task-density]": ["density"],
    };

    function rememberOrder(nodes, order) {
      nodes.forEach((node) => {
        const key = sourceKey(node.dataset);
        if (!order.has(key)) order.set(key, order.size);
      });
    }

    function choices(selector, records, field, current) {
      const select = find(selector);
      const values = new Set([...records.values()].map((record) => record[field]).filter(Boolean));
      if (current) values.add(current);
      select.querySelectorAll("option:not(:first-child)").forEach((option) => option.remove());
      [...values].sort().forEach((value) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        select.append(option);
      });
    }

    function wireSortables() {
      if (typeof window.Sortable?.get !== "function") return;
      columns.forEach((column) => {
        const instance = window.Sortable.get(column.querySelector(".sortable"));
        if (!instance || wiredSortables.has(instance)) return;
        const original = instance.option("onEnd");
        if (typeof original !== "function") return;
        wiredSortables.add(instance);
        instance.option("onEnd", async function (event) {
          await original.call(this, event);
          if (event.from !== event.to && event.item.parentElement === event.to
              && event.item.dataset.uuid && event.item.dataset.endpoint && statuses.includes(event.to.dataset.status)) {
            window.location.reload();
          } else {
            render();
          }
        });
      });
    }

    function render() {
      const view = store.current;
      const cards = [...scope.querySelectorAll("[data-task-column] .card[data-task-source]")];
      const rows = [...scope.querySelectorAll("[data-task-list-row]")];
      rememberOrder(cards, boardOrder);
      rememberOrder(rows, listOrder);
      const records = new Map();
      [...cards, ...rows].forEach((node) => {
        const key = sourceKey(node.dataset);
        if (!records.has(key)) records.set(key, node.dataset);
      });
      const week = weekBounds(scope.dataset.taskCalendarDate || new Date());
      const matching = new Set([...records].filter(([, record]) => matches(record, view.filters, week)).map(([key]) => key));
      choices("[data-filter-project]", records, "project", view.filters.project);
      choices("[data-filter-catagory]", records, "catagory", view.filters.category);
      Object.entries(bindings).forEach(([selector, path]) => {
        find(selector).value = path.length === 2 ? view[path[0]][path[1]] : view[path[0]];
      });
      scope.dataset.activeView = view.mode;
      scope.dataset.density = view.density;
      scope.querySelectorAll("[data-view-panel]").forEach((panel) => { panel.hidden = panel.dataset.viewPanel !== view.mode; });
      scope.querySelectorAll("[data-task-view]").forEach((button) => {
        const active = button.dataset.taskView === view.mode;
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", String(active));
      });
      find("[data-board-controls]").hidden = view.mode !== "board";
      const sorted = (nodes, order) => nodes.sort((left, right) => compareRecords(records.get(sourceKey(left.dataset)), records.get(sourceKey(right.dataset)), view.sort)
        || order.get(sourceKey(left.dataset)) - order.get(sourceKey(right.dataset)));
      let hiddenCount = 0;
      let collapsedCount = 0;
      let exposedCount = 0;
      columns.forEach((column) => {
        const status = column.dataset.status;
        const hidden = !view.shownColumns.includes(status);
        const collapsed = view.collapsedColumns.includes(status);
        column.hidden = hidden;
        column.dataset.collapsed = String(collapsed);
        const body = column.querySelector(".kanban-column-body");
        body.hidden = collapsed;
        const button = column.querySelector("[data-column-collapse]");
        button.setAttribute("aria-expanded", String(!collapsed));
        button.setAttribute("aria-label", `${collapsed ? "Expand" : "Collapse"} ${status}`);
        button.title = button.getAttribute("aria-label");
        const columnCards = [...body.querySelectorAll(".card[data-task-source]")];
        const count = new Set(columnCards.filter((card) => matching.has(sourceKey(card.dataset))).map((card) => sourceKey(card.dataset))).size;
        column.querySelector("[data-column-count]").textContent = String(count);
        columnCards.forEach((card) => card.classList.toggle("card-filtered-out", !matching.has(sourceKey(card.dataset)) || hidden || collapsed));
        sorted(columnCards, boardOrder).forEach((card) => body.append(card));
        if (hidden) hiddenCount += count;
        else if (collapsed) collapsedCount += count;
        else exposedCount += count;
        scope.querySelectorAll("[data-shown-column]").forEach((input) => {
          if (input.dataset.shownColumn === status) input.checked = !hidden;
        });
        scope.querySelectorAll("[data-column-option-count]").forEach((badge) => {
          if (badge.dataset.columnOptionCount === status) badge.textContent = String(count);
        });
      });
      const completed = statuses.find((status) => status.toLowerCase() === "completed");
      find("[data-collapse-completed]").checked = view.collapsedColumns.includes(completed);
      find("[data-collapse-completed]").disabled = !completed;
      scope.querySelectorAll("[data-task-list-group]").forEach((group) => group.remove());
      const table = find(".task-list-table");
      const groupKey = (row) => view.groupList === "project" ? (records.get(sourceKey(row.dataset)).project || "") : records.get(sourceKey(row.dataset)).status;
      const orderedRows = sorted(rows, listOrder);
      if (view.groupList !== "none") {
        orderedRows.sort((left, right) => view.groupList === "status"
          ? statuses.indexOf(groupKey(left)) - statuses.indexOf(groupKey(right))
          : groupKey(left).localeCompare(groupKey(right)));
      }
      let previousGroup = null;
      orderedRows.forEach((row) => {
        const matched = matching.has(sourceKey(row.dataset));
        row.classList.toggle("card-filtered-out", !matched);
        const key = groupKey(row);
        if (view.groupList !== "none" && matched && key !== previousGroup) {
          const heading = document.createElement("div");
          heading.className = "task-list-group";
          heading.dataset.taskListGroup = "";
          heading.setAttribute("role", "row");
          const cell = document.createElement("span");
          cell.setAttribute("role", "rowheader");
          cell.setAttribute("aria-colspan", "6");
          cell.textContent = key || "No project";
          heading.append(cell);
          table.append(heading);
          previousGroup = key;
        }
        table.append(row);
      });
      scope.querySelectorAll("[data-task-list-empty]").forEach((empty) => { empty.hidden = true; });
      let summary = `${matching.size} of ${records.size} tasks`;
      if (view.mode === "board") {
        const hiddenColumns = statuses.length - view.shownColumns.length;
        if (hiddenColumns) summary += `; ${hiddenCount} in ${hiddenColumns} hidden column${hiddenColumns === 1 ? "" : "s"}`;
        if (collapsedCount) summary += `; ${collapsedCount} in collapsed columns`;
      }
      find("[data-filter-summary]").textContent = summary;
      const empty = find("[data-task-view-empty]");
      empty.hidden = view.mode === "board" ? exposedCount > 0 : matching.size > 0;
      empty.textContent = !records.size ? "No tasks yet." : !matching.size ? "No tasks match these filters."
        : "Matching tasks are in hidden or collapsed columns.";
      renderSaved();
      wireSortables();
    }

    function changeView(next) {
      if (!validView(next, statuses)) { notice("These view settings are not valid.", true); return; }
      store.current = next;
      const confirmed = writeStore(storage, scopeName, store, statuses);
      notice(confirmed ? "" : "View changed for this page only. Browser storage could not confirm the save.", !confirmed);
      render();
    }

    function saveStore(next, message) {
      if (!writeStore(storage, scopeName, next, statuses)) {
        notice("Could not confirm the saved view. Existing saved views are unchanged in this page.", true);
        return false;
      }
      store = next;
      notice(message);
      render();
      return true;
    }

    function iconButton(icon, label, action) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn-tiny btn-ghost task-view-icon-button";
      button.title = label;
      button.setAttribute("aria-label", label);
      const image = document.createElement("span");
      image.className = "shell-icon";
      image.setAttribute("aria-hidden", "true");
      image.style.setProperty("--shell-icon", `url('/static/icons/lucide/${icon}.svg')`);
      button.append(image);
      button.addEventListener("click", action);
      return button;
    }

    function renderSaved() {
      const list = find("[data-saved-views-list]");
      list.replaceChildren();
      if (!store.views.length) {
        const empty = document.createElement("li");
        empty.textContent = "No saved views yet.";
        list.append(empty);
      }
      store.views.forEach((entry, index) => {
        const item = document.createElement("li");
        const apply = document.createElement("button");
        apply.type = "button";
        apply.className = "btn btn-tiny btn-ghost task-saved-view-name";
        apply.textContent = entry.name;
        apply.setAttribute("aria-pressed", String(store.active === entry.name && JSON.stringify(entry.view) === JSON.stringify(store.current)));
        apply.addEventListener("click", () => {
          store.active = entry.name;
          changeView(copy(entry.view));
          find("[data-saved-views]").open = false;
        });
        item.append(apply);
        item.append(iconButton("save", `Update ${entry.name}`, () => {
          const next = copy(store);
          next.views[index].view = copy(store.current);
          next.active = entry.name;
          saveStore(next, "View updated.");
        }));
        item.append(iconButton("pencil", `Rename ${entry.name}`, () => {
          const answer = window.prompt("View name", entry.name);
          if (answer === null) return;
          const name = answer.trim();
          if (!validName(name)) { notice("Use a view name of 1 to 40 characters.", true); return; }
          if (store.views.some((other, position) => position !== index && other.name === name)) { notice("A view already has that name.", true); return; }
          const next = copy(store);
          next.views[index].name = name;
          if (next.active === entry.name) next.active = name;
          saveStore(next, "View renamed.");
        }));
        item.append(iconButton("trash-2", `Delete ${entry.name}`, () => {
          if (!window.confirm(`Delete saved view "${entry.name}"?`)) return;
          const next = copy(store);
          next.views.splice(index, 1);
          if (next.active === entry.name) next.active = "";
          saveStore(next, "Saved view deleted.");
        }));
        list.append(item);
      });
    }

    Object.entries(bindings).forEach(([selector, path]) => {
      const input = find(selector);
      input.addEventListener(selector === "[data-filter-search]" ? "input" : "change", () => {
        const next = copy(store.current);
        if (path.length === 2) next[path[0]][path[1]] = path[1] === "minPriority" ? Number(input.value) : input.value;
        else next[path[0]] = input.value;
        changeView(next);
      });
    });
    scope.querySelectorAll("[data-task-view]").forEach((button) => button.addEventListener("click", () => {
      changeView({ ...copy(store.current), mode: button.dataset.taskView });
    }));
    find("[data-filter-clear]").addEventListener("click", () => changeView({ ...copy(store.current), filters: defaults(statuses).filters }));
    find("[data-view-reset]").addEventListener("click", () => {
      store.active = "";
      changeView(defaults(statuses));
    });
    scope.querySelectorAll("[data-shown-column]").forEach((input) => input.addEventListener("change", () => {
      const next = copy(store.current);
      next.shownColumns = statuses.filter((status) => status === input.dataset.shownColumn ? input.checked : next.shownColumns.includes(status));
      if (!next.shownColumns.length) { input.checked = true; notice("Keep at least one column visible.", true); return; }
      changeView(next);
    }));
    function collapse(status, collapsed) {
      const next = copy(store.current);
      next.collapsedColumns = statuses.filter((candidate) => candidate === status ? collapsed : next.collapsedColumns.includes(candidate));
      changeView(next);
    }
    scope.querySelectorAll("[data-column-collapse]").forEach((button) => button.addEventListener("click", () => {
      collapse(button.dataset.columnCollapse, button.getAttribute("aria-expanded") === "true");
    }));
    find("[data-collapse-completed]").addEventListener("change", (event) => {
      collapse(statuses.find((status) => status.toLowerCase() === "completed"), event.target.checked);
    });
    find("[data-saved-view-save]").addEventListener("click", () => {
      const input = find("[data-saved-view-name]");
      const name = input.value.trim();
      if (!validName(name)) { notice("Use a view name of 1 to 40 characters.", true); input.focus(); return; }
      const next = copy(store);
      const existing = next.views.findIndex((entry) => entry.name === name);
      if (existing < 0 && next.views.length >= MAX_VIEWS) { notice("Keep up to 30 saved views. Update or delete an existing view first.", true); return; }
      const entry = { name, view: copy(store.current) };
      if (existing < 0) next.views.push(entry); else next.views[existing] = entry;
      next.active = name;
      if (saveStore(next, existing < 0 ? "View saved." : "View updated.")) input.value = "";
    });
    scope.querySelectorAll(".task-view-popover").forEach((popover) => popover.addEventListener("toggle", () => {
      if (popover.open) scope.querySelectorAll(".task-view-popover[open]").forEach((other) => { if (other !== popover) other.open = false; });
    }));
    scope.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      const popover = event.target.closest(".task-view-popover[open]");
      if (popover) { popover.open = false; popover.querySelector("summary").focus(); }
    });
    document.addEventListener("click", (event) => {
      if (scope.isConnected && !event.target.closest(".task-view-popover")) scope.querySelectorAll(".task-view-popover[open]").forEach((popover) => { popover.open = false; });
    });
    document.body.addEventListener("htmx:afterSwap", (event) => {
      if (scope.isConnected && (scope.contains(event.target) || scope.contains(event.detail?.requestConfig?.elt))) render();
    });
    document.body.addEventListener("htmx:afterRequest", (event) => {
      const element = event.detail?.elt || event.detail?.requestConfig?.elt || event.target;
      if (!scope.isConnected || !scope.contains(element)) return;
      const path = event.detail?.requestConfig?.path || element.getAttribute?.("hx-post") || "";
      if (!/^\/(tasks|recurring)\/[^/?]+\/status$/.test(path)) return;
      if (event.detail.successful) window.location.reload();
      else if (element.matches(".task-list-status")) element.value = element.closest("[data-task-list-row]").dataset.status;
    });
    scope.taskViewsController = { render, state: () => copy(store) };
    render();
    if (loaded.issue) notice(loaded.issue, true);
    return scope.taskViewsController;
  }

  window.LuigiTaskViews = Object.freeze({ defaults, validView, validStore, validName, migrateLegacy, readStore, writeStore, weekBounds, matches, compareRecords, sourceKey, mount });
  const boot = () => document.querySelectorAll("[data-task-views]").forEach(mount);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
  document.addEventListener("htmx:afterSwap", boot);
})();