"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync(process.argv[3], "utf8");
const statuses = ["Not Started", "In Progress", "Completed"];
const plain = (value) => JSON.parse(JSON.stringify(value));
const prefix = "luigi.tasks.views.v1.";
const week = { today: "2030-01-02", mon: "2029-12-31", sun: "2030-01-06" };

function load(windowOverrides = {}, documentOverrides = {}) {
  const listeners = [];
  const document = {
    readyState: "loading",
    addEventListener(name, callback) { listeners.push({ name, callback }); },
    querySelectorAll() { throw new Error("Unexpected DOM boot in a pure JavaScript test"); },
    ...documentOverrides,
  };
  const window = { ...windowOverrides };
  const context = vm.createContext({ window, document });
  vm.runInContext(source, context, { filename: "task-views.js", timeout: 1000 });
  return { api: window.LuigiTaskViews, listeners };
}

function fresh(api) {
  return { version: 1, current: api.defaults(statuses), views: [], active: "" };
}

function storage(initial = {}) {
  const values = new Map(Object.entries(initial));
  const writes = [];
  const reads = [];
  return {
    values, writes, reads,
    getItem(key) { reads.push(key); return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { writes.push([key, value]); values.set(key, String(value)); },
  };
}

function record(overrides = {}) {
  return {
    uuid: "example-shared-id", taskSource: "task", title: "Example task", project: "example project",
    catagory: "example category", taskGroup: "example group", subGroup: "example subgroup",
    status: "Not Started", completed: "0", priority: "5", dueDate: "", completedTime: "",
    reactivationDate: "", ...overrides,
  };
}

function accepts(api, fields = {}, filters = {}) {
  return api.matches(record(fields), { ...api.defaults(statuses).filters, ...filters }, week);
}

class Element {
  constructor(tagName = "div", dataset = {}) {
    this.tagName = tagName;
    this.dataset = dataset;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.textContent = "";
    this.hidden = false;
    this.style = { setProperty() {} };
    this.classList = { toggle() {} };
  }

  set innerHTML(value) { throw new Error(`Unsafe HTML insertion: ${value}`); }
  set outerHTML(value) { throw new Error(`Unsafe HTML replacement: ${value}`); }
  insertAdjacentHTML() { throw new Error("Unsafe HTML insertion"); }
  append(...children) {
    for (const child of children) {
      this.children = this.children.filter((existing) => existing !== child);
      this.children.push(child);
    }
  }
  replaceChildren(...children) { this.children = [...children]; }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name] ?? null; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  querySelectorAll() { return []; }
}

function mountedScope(store) {
  const controls = new Map();
  const created = [];
  const scope = new Element("div", { tasksScope: "/tasks" });
  const columns = statuses.map((status) => {
    const column = new Element("div", { status });
    column.parts = {
      ".kanban-column-body": new Element(),
      "[data-column-collapse]": new Element("button", { columnCollapse: status }),
      "[data-column-count]": new Element("span"),
    };
    column.querySelector = (selector) => column.parts[selector];
    return column;
  });
  scope.children = [...columns];
  scope.querySelector = (selector) => {
    if (!controls.has(selector)) controls.set(selector, new Element());
    return controls.get(selector);
  };
  scope.querySelectorAll = (selector) => {
    if (selector === "[data-task-column]") return columns;
    if (selector === "[data-column-collapse]") return columns.map((column) => column.parts["[data-column-collapse]"]);
    return [];
  };
  const memory = storage({ [prefix + "/tasks"]: JSON.stringify(store) });
  const { api } = load({ localStorage: memory, matchMedia: () => ({ matches: false }) }, {
    body: new Element("body"),
    createElement(tag) { const element = new Element(tag); created.push(element); return element; },
  });
  const controller = api.mount(scope);
  return { api, scope, columns, controls, created, controller, memory };
}

const cases = {
  exports_and_board_defaults() {
    const { api, listeners } = load();
    assert.deepEqual(Object.keys(api).sort(), [
      "defaults", "validView", "validStore", "validName", "migrateLegacy", "readStore",
      "writeStore", "weekBounds", "matches", "compareRecords", "sourceKey", "mount",
    ].sort());
    assert.equal(Object.isFrozen(api), true);
    assert.deepEqual(listeners.map((entry) => entry.name), ["DOMContentLoaded", "htmx:afterSwap"]);
    assert.equal(listeners.every((entry) => typeof entry.callback === "function"), true);
    const view = api.defaults(statuses);
    assert.deepEqual(plain(view), {
      filters: { text: "", project: "", status: "", source: "any", category: "", minPriority: 0, smart: "" },
      sort: "default", groupList: "none", mode: "board", density: "comfortable",
      shownColumns: statuses, collapsedColumns: [],
    });
    view.shownColumns.pop();
    view.filters.text = "Synthetic change";
    assert.equal(statuses.length, 3);
    assert.deepEqual(plain(api.defaults(statuses).shownColumns), statuses);
    assert.equal(api.defaults(statuses).filters.text, "");
    assert.equal(api.defaults(statuses, "list").mode, "list");
  },

  view_schema_whitelists() {
    const { api } = load();
    for (const [field, values] of Object.entries({
      mode: ["board", "list"], density: ["comfortable", "compact"],
      groupList: ["none", "project", "status"], sort: ["default", "due", "priority", "title"],
    })) {
      for (const value of values) assert.equal(api.validView({ ...api.defaults(statuses), [field]: value }, statuses), true);
      for (const value of ["unknown", "", null, 1, true, []]) {
        assert.equal(api.validView({ ...api.defaults(statuses), [field]: value }, statuses), false, `${field}: ${value}`);
      }
    }
    for (const invalid of [null, [], {}, "board", { ...api.defaults(statuses), extra: true }]) {
      assert.equal(api.validView(invalid, statuses), false);
    }
    for (const field of Object.keys(api.defaults(statuses))) {
      const incomplete = api.defaults(statuses);
      delete incomplete[field];
      assert.equal(api.validView(incomplete, statuses), false, field);
    }
    const inherited = Object.create(api.defaults(statuses));
    assert.equal(api.validView(inherited, statuses), false);
    const view = api.defaults(statuses);
    view.filters.extra = "unapproved";
    assert.equal(api.validView(view, statuses), false);
    for (const field of Object.keys(api.defaults(statuses).filters)) {
      const incomplete = api.defaults(statuses);
      delete incomplete.filters[field];
      assert.equal(api.validView(incomplete, statuses), false, field);
    }
  },

  filter_types_and_bounds() {
    const { api } = load();
    const valid = (field, value) => {
      const view = api.defaults(statuses);
      view.filters[field] = value;
      return api.validView(view, statuses);
    };
    for (const field of ["text", "project", "category"]) {
      for (const value of ["", "x".repeat(240)]) assert.equal(valid(field, value), true);
      for (const value of ["x".repeat(241), 0, null, [], {}]) assert.equal(valid(field, value), false);
    }
    for (const value of [0, 5, 10]) assert.equal(valid("minPriority", value), true);
    for (const value of [-1, 11, 0.5, "5", true, null, NaN, Infinity]) assert.equal(valid("minPriority", value), false);
    for (const value of ["", ...statuses]) assert.equal(valid("status", value), true);
    for (const value of ["Unknown", "completed", 0, null]) assert.equal(valid("status", value), false);
    for (const value of ["any", "task", "recurring"]) assert.equal(valid("source", value), true);
    for (const value of ["tasks", "all", "", null]) assert.equal(valid("source", value), false);
    for (const value of ["", "open", "overdue", "upcoming", "due-week", "no-due", "high-priority", "completed-week", "awaiting-reactivation", "recurring", "completed"]) {
      assert.equal(valid("smart", value), true);
    }
    for (const value of ["today", "unknown", null]) assert.equal(valid("smart", value), false);
  },

  column_sets_reject_duplicates_unknown_and_zero_shown() {
    const { api } = load();
    for (const field of ["shownColumns", "collapsedColumns"]) {
      for (const value of [[statuses[0]], [...statuses].reverse()]) {
        assert.equal(api.validView({ ...api.defaults(statuses), [field]: value }, statuses), true);
      }
      for (const value of [[statuses[0], statuses[0]], ["Unknown"], [0], "Completed", null, [...statuses, "extra"]]) {
        assert.equal(api.validView({ ...api.defaults(statuses), [field]: value }, statuses), false);
      }
    }
    assert.equal(api.validView({ ...api.defaults(statuses), shownColumns: [] }, statuses), false);
    assert.equal(api.validView({ ...api.defaults(statuses), collapsedColumns: [] }, statuses), true);
    assert.equal(api.validView({ ...api.defaults(statuses), shownColumns: [statuses[0]], collapsedColumns: [statuses[2]] }, statuses), true);
  },

  names_and_store_schema_limits() {
    const { api } = load();
    for (const name of ["A", "x".repeat(40), "Example view", "<img src=x onerror=alert(1)>"]) assert.equal(api.validName(name), true);
    for (const name of ["", " ", " leading", "trailing ", "x".repeat(41), "a\nb", "a\u0000b", "a\u007fb", 1, null]) assert.equal(api.validName(name), false);
    const store = fresh(api);
    store.views = Array.from({ length: 30 }, (_, index) => ({ name: `Example ${index}`, view: api.defaults(statuses) }));
    store.active = "Example 0";
    assert.equal(api.validStore(store, statuses), true);
    assert.equal(api.validStore({ ...store, views: [...store.views, { name: "Extra", view: api.defaults(statuses) }] }, statuses), false);
    assert.equal(api.validStore({ ...store, views: [store.views[0], store.views[0]] }, statuses), false);
    for (const invalid of [
      null, [], { ...store, version: 2 }, { ...store, version: "1" }, { ...store, extra: true },
      { ...store, active: "Missing" }, { ...store, active: null }, { ...store, current: {} },
      { ...store, views: {} }, { ...store, views: [{ name: "Example", view: {}, extra: true }] },
      { ...store, views: [{ name: "Example", view: api.defaults(statuses), extra: true }] },
      { ...store, views: [{ name: " bad", view: api.defaults(statuses) }] },
    ]) assert.equal(api.validStore(invalid, statuses), false);
    for (const field of Object.keys(store)) {
      const incomplete = { ...store };
      delete incomplete[field];
      assert.equal(api.validStore(incomplete, statuses), false);
    }
  },

  legacy_migration_and_limits() {
    const { api } = load();
    const filter = { q: "Example", smart: "open", minPrio: 5, catagory: "example category" };
    const saved = [
      { name: "Example", filter }, { name: "Example", filter },
      { name: " invalid", filter }, { name: "Bad filter", filter: { ...filter, extra: true } },
      { name: "Bad priority", filter: { ...filter, minPrio: "5" } },
      { name: "Extra fields", filter, extra: true },
      ...Array.from({ length: 35 }, (_, index) => ({ name: `View ${index}`, filter })),
    ];
    const before = JSON.stringify(saved);
    const migrated = api.migrateLegacy(saved, filter, statuses, "list");
    assert.equal(api.validStore(migrated, statuses), true);
    assert.equal(migrated.views.length, 30);
    assert.equal(migrated.views[0].name, "Example");
    assert.equal(migrated.views[29].name, "View 28");
    assert.equal(migrated.active, "");
    assert.equal(migrated.current.mode, "list");
    assert.deepEqual(plain(migrated.current.filters), {
      text: "Example", smart: "open", minPriority: 5, category: "example category", project: "", status: "", source: "any",
    });
    migrated.current.filters.text = "Changed";
    assert.equal(migrated.views[0].view.filters.text, "Example");
    assert.equal(JSON.stringify(saved), before);
    assert.deepEqual(plain(api.migrateLegacy({}, { ...filter, unknown: true }, statuses)), plain(fresh(api)));
    assert.equal(api.migrateLegacy([...Array(300).fill(null), { name: "Beyond limit", filter }], null, statuses).views.length, 0);
  },

  storage_roundtrip_scope_and_legacy_precedence() {
    const { api } = load();
    const store = fresh(api);
    store.current.mode = "list";
    const memory = storage();
    assert.equal(api.writeStore(memory, "/tasks", store, statuses), true);
    const result = api.readStore(memory, "/tasks", statuses, true);
    assert.equal(result.issue, "");
    assert.deepEqual(plain(result.store), plain(store));
    assert.equal(memory.writes.length, 1);
    assert.equal(api.readStore(memory, "/recurring", statuses, true).store.current.mode, "board");
    assert.equal(JSON.parse(memory.values.get(prefix + "/tasks")).current.mode, "list");
    const filter = { q: "Example", smart: "open", minPrio: 2, catagory: "example" };
    for (const mobile of [false, true]) {
      const legacy = storage({
        "luigi.tasks.view": "list",
        "luigi.tasks.savedFilters": JSON.stringify([{ name: "Legacy example", filter }]),
        "luigi.tasks.activeFilter./tasks": JSON.stringify(filter),
      });
      const oldValues = [...legacy.values];
      const migrated = api.readStore(legacy, "/tasks", statuses, mobile);
      assert.equal(migrated.issue, "");
      assert.equal(migrated.store.current.mode, mobile ? "board" : "list");
      assert.equal(migrated.store.current.filters.text, "Example");
      assert.equal(migrated.store.views[0].name, "Legacy example");
      assert.deepEqual(legacy.writes.map(([key]) => key), [prefix + "/tasks"]);
      for (const [key, value] of oldValues) assert.equal(legacy.values.get(key), value);
      legacy.values.set(`luigi.tasks.view.${mobile ? "mobile" : "desktop"}`, "list");
      legacy.values.delete(prefix + "/tasks");
      assert.equal(api.readStore(legacy, "/tasks", statuses, mobile).store.current.mode, "list");
    }
    const preferred = storage({ [prefix + "/tasks"]: JSON.stringify(store), "luigi.tasks.savedFilters": "invalid" });
    assert.equal(api.readStore(preferred, "/tasks", statuses, false).store.current.mode, "list");
    assert.deepEqual(preferred.reads, [prefix + "/tasks"]);
    assert.equal(preferred.writes.length, 0);
  },

  corrupt_storage_falls_back_without_overwriting() {
    const { api } = load();
    const invalid = fresh(api);
    invalid.current.shownColumns = [];
    for (const raw of ["{", "null", "[]", JSON.stringify(invalid), JSON.stringify({ ...fresh(api), version: 2 }), " ".repeat(131073) + JSON.stringify(fresh(api))]) {
      const memory = storage({ [prefix + "/tasks"]: raw, "luigi.tasks.view": "list" });
      const result = api.readStore(memory, "/tasks", statuses, false);
      assert.deepEqual(plain(result.store), plain(fresh(api)));
      assert.match(result.issue, /could not be read/);
      assert.match(result.issue, /not been replaced/);
      assert.equal(memory.values.get(prefix + "/tasks"), raw);
      assert.equal(memory.writes.length, 0);
      assert.deepEqual(memory.reads, [prefix + "/tasks"]);
    }
  },

  inaccessible_storage_keeps_usable_page_state() {
    const { api } = load();
    for (const memory of [undefined, { getItem() { throw new Error("Denied"); }, setItem() { assert.fail("Unexpected write"); } }]) {
      const result = api.readStore(memory, "/tasks", statuses, false);
      assert.deepEqual(plain(result.store), plain(fresh(api)));
      assert.match(result.issue, /unavailable/);
    }
    const filter = { q: "Example", smart: "", minPrio: 0, catagory: "" };
    const memory = storage({ "luigi.tasks.activeFilter./tasks": JSON.stringify(filter) });
    memory.setItem = () => { throw new Error("Quota exceeded"); };
    const result = api.readStore(memory, "/tasks", statuses, true);
    assert.equal(result.store.current.filters.text, "Example");
    assert.match(result.issue, /page only/);
    assert.equal(memory.values.has(prefix + "/tasks"), false);
  },

  write_requires_validation_and_exact_readback() {
    const { api } = load();
    const store = fresh(api);
    const untouched = { getItem() { assert.fail("Invalid store read"); }, setItem() { assert.fail("Invalid store write"); } };
    assert.equal(api.writeStore(untouched, "/tasks", { ...store, version: 2 }, statuses), false);
    for (const memory of [
      undefined,
      { setItem() { throw new Error("Quota"); } },
      { setItem() {}, getItem() { return null; } },
      { setItem() {}, getItem() { return "stale"; } },
      { setItem() {}, getItem() { throw new Error("Read denied"); } },
      { setItem() {}, getItem() { return JSON.stringify(store, null, 2); } },
    ]) assert.equal(api.writeStore(memory, "/tasks", store, statuses), false);
    const memory = storage();
    assert.equal(api.writeStore(memory, "/tasks", store, statuses), true);
    assert.deepEqual(memory.writes, [[prefix + "/tasks", JSON.stringify(store)]]);
  },

  source_keys_prevent_uuid_collisions() {
    const { api } = load();
    const task = record();
    const recurring = record({ taskSource: "recurring" });
    assert.equal(api.sourceKey(task), "task:example-shared-id");
    assert.equal(api.sourceKey(recurring), "recurring:example-shared-id");
    const keys = new Set([task, { ...task }, recurring, { ...recurring }].map(api.sourceKey));
    assert.equal(keys.size, 2);
    assert.equal(accepts(api, recurring, { source: "task" }), false);
    assert.equal(accepts(api, task, { source: "recurring" }), false);
  },

  completion_dataset_and_status_are_not_truthiness() {
    const { api } = load();
    for (const [fields, completed] of [
      [{ completed: "1", status: "In Progress" }, true],
      [{ completed: "0", status: "Not Started" }, false],
      [{ completed: "", status: "In Progress" }, false],
      [{ completed: undefined, status: "In Progress" }, false],
      [{ completed: "0", status: "Completed" }, true],
      [{ completed: "0", status: "COMPLETED" }, true],
    ]) {
      assert.equal(accepts(api, fields, { smart: "completed" }), completed);
      assert.equal(accepts(api, fields, { smart: "open" }), !completed);
    }
  },

  filters_are_conjunctive_and_search_all_fields() {
    const { api } = load();
    for (const text of [" EXAMPLE TASK ", "EXAMPLE PROJECT", "example category", "example group", "example subgroup"]) {
      assert.equal(accepts(api, {}, { text }), true, text);
    }
    const filters = { text: "task", project: "example project", category: "example category", status: "Not Started", source: "task", minPriority: 5, smart: "open" };
    assert.equal(accepts(api, {}, filters), true);
    for (const [field, value] of Object.entries({ text: "missing", project: "Example Project", category: "missing", status: "Completed", source: "recurring", minPriority: 6 })) {
      assert.equal(accepts(api, {}, { ...filters, [field]: value }), false, field);
    }
    assert.equal(accepts(api, { project: null, catagory: null, priority: "n/a" }), true);
    assert.equal(accepts(api, { priority: "n/a" }, { minPriority: 1 }), false);
  },

  week_bounds_are_local_monday_to_sunday() {
    const { api } = load();
    for (const [date, expected] of [
      [new Date(2030, 0, 2, 0, 15), week],
      [new Date(2029, 11, 31, 23, 45), { today: "2029-12-31", mon: "2029-12-31", sun: "2030-01-06" }],
      [new Date(2030, 0, 6, 23, 45), { today: "2030-01-06", mon: "2029-12-31", sun: "2030-01-06" }],
      [new Date(2028, 1, 29, 12), { today: "2028-02-29", mon: "2028-02-28", sun: "2028-03-05" }],
      [new Date(2026, 2, 8, 12), { today: "2026-03-08", mon: "2026-03-02", sun: "2026-03-08" }],
    ]) {
      const original = date.getTime();
      assert.deepEqual(plain(api.weekBounds(date)), expected);
      assert.equal(date.getTime(), original);
    }
  },

  smart_date_filters_respect_inclusive_boundaries() {
    const { api } = load();
    for (const [dueDate, overdue, upcoming, dueWeek] of [
      ["2029-12-30", true, false, false], [week.mon, true, false, true],
      [week.today, false, true, true], [week.sun, false, true, true],
      ["2030-01-07", false, true, false], [week.today + "T23:59:59", false, true, true],
      ["", false, false, false], ["not-a-date", false, false, false],
    ]) {
      for (const [smart, expected] of [["overdue", overdue], ["upcoming", upcoming], ["due-week", dueWeek]]) {
        assert.equal(accepts(api, { dueDate }, { smart }), expected, `${smart}: ${dueDate}`);
        assert.equal(accepts(api, { dueDate, completed: "1" }, { smart }), false);
      }
    }
    for (const [completedTime, expected] of [[week.mon, true], [week.sun + "T23:59:59", true], ["2029-12-30", false], ["2030-01-07", false], ["", false]]) {
      assert.equal(accepts(api, { completed: "1", completedTime }, { smart: "completed-week" }), expected);
      assert.equal(accepts(api, { completed: "0", completedTime }, { smart: "completed-week" }), false);
    }
  },

  other_smart_filters_distinguish_source_and_completion() {
    const { api } = load();
    for (const dueDate of ["", null, "invalid"]) assert.equal(accepts(api, { dueDate }, { smart: "no-due" }), true);
    assert.equal(accepts(api, { dueDate: week.today }, { smart: "no-due" }), false);
    assert.equal(accepts(api, { completed: "1" }, { smart: "no-due" }), false);
    for (const [priority, expected] of [["4", false], ["5", true], ["10", true], ["", false]]) {
      assert.equal(accepts(api, { priority }, { smart: "high-priority" }), expected);
      assert.equal(accepts(api, { priority, completed: "1" }, { smart: "high-priority" }), false);
    }
    assert.equal(accepts(api, { completed: "1", reactivationDate: "2030-01-07" }, { smart: "awaiting-reactivation" }), true);
    assert.equal(accepts(api, { completed: "0", reactivationDate: "2030-01-07" }, { smart: "awaiting-reactivation" }), false);
    assert.equal(accepts(api, { completed: "1" }, { smart: "awaiting-reactivation" }), false);
    assert.equal(accepts(api, { taskSource: "recurring", recurring: "0" }, { smart: "recurring" }), true);
    assert.equal(accepts(api, { taskSource: "task", recurring: "1" }, { smart: "recurring" }), false);
  },

  sorting_handles_missing_dates_priorities_titles_and_ties() {
    const { api } = load();
    const rows = [
      record({ uuid: "missing", title: "zeta", priority: "", dueDate: "" }),
      record({ uuid: "late", title: "beta", priority: "5", dueDate: "2030-01-06" }),
      record({ uuid: "early", title: "alpha", priority: "10", dueDate: "2029-12-31T23:00:00" }),
      record({ uuid: "invalid", title: "gamma", priority: "invalid", dueDate: "invalid" }),
    ];
    const before = JSON.stringify(rows);
    const ordered = (sort) => [...rows].sort((left, right) => api.compareRecords(left, right, sort)).map((item) => item.uuid);
    assert.deepEqual(ordered("due"), ["early", "late", "missing", "invalid"]);
    assert.deepEqual(ordered("priority"), ["early", "late", "missing", "invalid"]);
    assert.deepEqual(ordered("title"), ["early", "late", "invalid", "missing"]);
    assert.deepEqual(ordered("default"), rows.map((item) => item.uuid));
    assert.equal(api.compareRecords(record({ title: null }), record({ title: "alpha" }), "title") < 0, true);
    for (const sort of ["default", "due", "priority", "title"]) assert.equal(api.compareRecords(rows[0], { ...rows[0] }, sort), 0);
    assert.equal(JSON.stringify(rows), before);
  },

  mount_renders_names_as_text_and_preserves_column_order() {
    const { api } = load();
    assert.equal(api.mount({ querySelectorAll: () => [] }), null);
    const store = fresh(api);
    const name = "<img src=x onerror=alert(1)>";
    store.views = [{ name, view: api.defaults(statuses) }];
    store.active = name;
    store.current.shownColumns = [statuses[2], statuses[0]];
    store.current.collapsedColumns = [statuses[2]];
    const fixture = mountedScope(store);
    const { controller, controls, columns, scope } = fixture;
    assert.equal(fixture.api.mount(scope), controller);
    assert.equal(controls.get("[data-saved-views-list]").children[0].children[0].textContent, name);
    assert.equal(fixture.created.some((element) => element.tagName === "img" || element.tagName === "script"), false);
    assert.deepEqual(scope.children.map((column) => column.dataset.status), statuses);
    assert.deepEqual(columns.map((column) => column.hidden), [false, true, false]);
    assert.equal(columns[2].parts[".kanban-column-body"].hidden, true);
    assert.equal(columns[2].parts["[data-column-collapse]"].getAttribute("aria-expanded"), "false");
    const snapshot = controller.state();
    snapshot.current.mode = "list";
    assert.equal(controller.state().current.mode, "board");
    columns[2].parts["[data-column-collapse]"].listeners.click();
    assert.equal(columns[2].parts[".kanban-column-body"].hidden, false);
    assert.equal(JSON.parse(fixture.memory.values.get(prefix + "/tasks")).current.collapsedColumns.length, 0);
    assert.deepEqual(scope.children.map((column) => column.dataset.status), statuses);
    controls.get("[data-view-reset]").listeners.click();
    assert.equal(controller.state().current.mode, "board");
    assert.deepEqual(columns.map((column) => column.hidden), [false, false, false]);
    assert.deepEqual(scope.children.map((column) => column.dataset.status), statuses);
  },
};

const selected = process.argv[2];
const names = selected ? [selected] : Object.keys(cases);
for (const name of names) {
  assert.equal(Object.hasOwn(cases, name), true, `Unknown case: ${name}`);
  cases[name]();
  console.log(`PASS ${name}`);
}