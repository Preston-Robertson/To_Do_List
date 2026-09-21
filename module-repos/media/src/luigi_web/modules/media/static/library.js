(() => {
  "use strict";
  const root = document.getElementById("media-library");
  if (!root || root.dataset.disabled === "true") return;
  const section = root.dataset.section;
  if (!["games", "shows"].includes(section)) return;
  const byId = (name) => document.getElementById(`library-${name}`);
  const filtersForm = byId("filters");
  const activeStatus = section === "games" ? "playing" : "watching";
  const pausedStatus = section === "games" ? "paused" : "on_hold";
  const finishedStatuses = new Set(["completed", "achievements", "dropped"]);
  const defaults = {query: "", status: "", platform: "", genre: "", tag: "", priority: "", rating: "", sort: "recent"};
  const labels = {status: "Status", priority: "Priority", rating: "Rating", platform: "Platform", genre: "Genre", tags: "Tags", notes: "Notes", hours_played: "Hours played", current_season: "Season", current_episode: "Episode", total_episodes: "Total episodes"};
  const editable = ["status", "priority", "rating", "platform", "genre", "tags", "notes", ...(section === "shows" ? ["current_season", "current_episode", "total_episodes"] : ["hours_played"])];
  const drafts = new Map();
  let state = null;
  let view = "continue";
  let savedViews = [];
  let pending = false;
  let loading = false;
  let uncertain = false;
  let detailKey = null;
  let detailSequence = 0;
  let undo = null;
  let undoTimer = null;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function icon(name) {
    return root.querySelector(`template[data-icon="${name}"]`).content.cloneNode(true);
  }

  function button(text, symbol, action, mutation = false, compact = false) {
    const control = element("button", `btn${compact ? " library-icon-button" : ""}`);
    control.type = "button";
    control.title = text;
    control.setAttribute("aria-label", text);
    control.append(icon(symbol));
    if (!compact) control.append(element("span", "", text));
    if (mutation) control.dataset.mutation = "true";
    control.addEventListener("click", action);
    return control;
  }

  function safeURL(value) {
    if (typeof value !== "string" || !/^https?:\/\//i.test(value.trim())) return null;
    try {
      const parsed = new URL(value);
      return ["http:", "https:"].includes(parsed.protocol) && !parsed.username && !parsed.password ? parsed.href : null;
    } catch { return null; }
  }

  function cover(item) {
    const holder = element("div", "library-cover");
    holder.append(icon(section === "games" ? "gamepad-2" : "tv-minimal"));
    const url = safeURL(item.cover_url);
    if (url) {
      const image = element("img");
      image.src = url;
      image.alt = "";
      image.loading = "lazy";
      image.referrerPolicy = "no-referrer";
      image.addEventListener("error", () => image.remove(), {once: true});
      holder.append(image);
    }
    return holder;
  }

  function message(text) {
    byId("message").textContent = text;
    root.querySelectorAll("dialog[open] [data-dialog-message]").forEach((node) => {
      node.textContent = text;
      if (uncertain) {
        const reload = button("Reload library", "rotate-ccw", () => loadState(state?.profile || "", false, true));
        reload.dataset.reload = "true";
        reload.disabled = pending || loading;
        node.append(reload);
      }
    });
  }

  function validItem(item) {
    return item && /^[a-f0-9]{64}$/i.test(item.key) && /^[a-f0-9]{64}$/i.test(item.version)
      && typeof item.title === "string" && typeof item.profile === "string" && typeof item.status === "string";
  }

  function validState(value) {
    return value && value.section === section && typeof value.profile === "string"
      && Array.isArray(value.items) && value.items.every(validItem)
      && new Set(value.items.map((item) => item.key)).size === value.items.length
      && Array.isArray(value.profiles) && Array.isArray(value.statuses) && value.status_labels;
  }

  async function request(path, data = null) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 45000);
    const options = {method: data ? "POST" : "GET", credentials: "same-origin", cache: "no-store", redirect: "error", signal: controller.signal, headers: {Accept: "application/json"}};
    if (data) {
      options.body = new FormData();
      Object.entries(data).forEach(([name, value]) => options.body.append(name, String(value)));
    }
    try {
      const response = await fetch(`/media/${section}/${path}`, options);
      if (!response.ok) {
        const error = new Error("Media request failed");
        error.status = response.status;
        throw error;
      }
      return await response.json();
    } finally { clearTimeout(timeout); }
  }

  function lockControls() {
    root.setAttribute("aria-busy", String(pending || loading));
    root.querySelectorAll("[data-mutation], #library-add, #library-pick").forEach((control) => {
      control.disabled = pending || loading || uncertain || !state || control.dataset.unavailable === "true";
    });
    root.querySelectorAll("#library-edit-form input, #library-edit-form textarea, #library-edit-form select").forEach((control) => { control.disabled = pending; });
    byId("refresh").disabled = pending || loading;
    root.querySelectorAll("[data-reload]").forEach((control) => { control.disabled = pending || loading; });
    filtersForm.elements.profile.disabled = pending || loading;
    byId("refresh").title = uncertain ? "Reload before another change" : "Refresh library";
    byId("undo-button").disabled = pending || loading || uncertain || !undo;
  }

  function filterValues() {
    return Object.fromEntries(Object.keys(defaults).map((name) => [name, filtersForm.elements[name].value]));
  }

  function cleanFilters(values) {
    return Object.fromEntries(Object.entries(defaults).map(([name, fallback]) => [name,
      typeof values?.[name] === "string" ? values[name].slice(0, 200) : fallback]));
  }

  function preferenceKey(profile) { return `luigi.media.library.v1.${section}.${encodeURIComponent(profile)}`; }

  function persistPreferences() {
    if (!state) return;
    try {
      localStorage.setItem(preferenceKey(state.profile), JSON.stringify({
        filters: filterValues(), view, excludeRecent: byId("exclude-recent").checked, saved: savedViews,
      }));
    } catch { message("Preferences could not be saved on this browser."); }
  }

  function loadPreferences(profile) {
    let preferences = {};
    try { preferences = JSON.parse(localStorage.getItem(preferenceKey(profile))) || {}; } catch { preferences = {}; }
    view = ["continue", "board", "list"].includes(preferences.view) ? preferences.view : "continue";
    savedViews = Array.isArray(preferences.saved) ? preferences.saved.slice(0, 12).filter((entry) => typeof entry?.name === "string").map((entry) => ({
      name: entry.name.slice(0, 60), filters: cleanFilters(entry.filters),
      view: ["continue", "board", "list"].includes(entry.view) ? entry.view : "continue",
      excludeRecent: entry.excludeRecent !== false,
    })) : [];
    byId("exclude-recent").checked = preferences.excludeRecent !== false;
    applyFilters(cleanFilters(preferences.filters));
    renderSavedViews();
  }

  function applyFilters(values) {
    Object.entries(values).forEach(([name, value]) => {
      const control = filtersForm.elements[name];
      if (control.tagName === "SELECT" && !Array.from(control.options).some((option) => option.value === value)) {
        control.add(new Option(value, value));
      }
      control.value = value;
    });
  }

  function options(control, values, first, selected) {
    control.replaceChildren(new Option(first, ""));
    [...new Set(values.filter((value) => typeof value === "string" && value))].sort((left, right) => left.localeCompare(right)).forEach((value) => control.add(new Option(value, value)));
    if (selected && !Array.from(control.options).some((option) => option.value === selected)) control.add(new Option(selected, selected));
    control.value = selected || "";
  }

  function populateFilters() {
    const previous = filterValues();
    options(filtersForm.elements.profile, state.profiles, "All profiles", state.profile);
    options(filtersForm.elements.status, state.statuses, "All statuses", previous.status);
    Array.from(filtersForm.elements.status.options).forEach((option) => { if (option.value) option.textContent = state.status_labels[option.value] || option.value; });
    for (const name of ["platform", "genre", "tag"]) {
      const values = state.items.flatMap((item) => name === "tag" ? item.tags || [] : [item[name]]);
      options(filtersForm.elements[name], values, `All ${name === "tag" ? "tags" : `${name}s`}`, previous[name]);
    }
    const insights = new URL("/media/insights", location.origin);
    insights.searchParams.set("section", section);
    if (state.profile) insights.searchParams.set("profile", state.profile);
    byId("insights").href = insights.pathname + insights.search;
  }

  function number(value, fallback = -1) {
    if (value === null || value === undefined || value === "") return fallback;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function dateNumber(value) { return Date.parse(value || "") || 0; }

  function filteredItems() {
    if (!state) return [];
    const filters = filterValues();
    const query = filters.query.trim().toLocaleLowerCase();
    const result = state.items.filter((item) => {
      const searchable = [item.title, item.notes, item.platform, item.genre, ...(item.tags || [])].join(" ").toLocaleLowerCase();
      return (!query || searchable.includes(query))
        && (!filters.status || item.status === filters.status)
        && (!filters.platform || item.platform === filters.platform)
        && (!filters.genre || item.genre === filters.genre)
        && (!filters.tag || (item.tags || []).includes(filters.tag))
        && (!filters.priority || String(item.priority) === filters.priority)
        && (!filters.rating || (filters.rating === "unrated" ? number(item.rating) < 0 : number(item.rating) >= Number(filters.rating)));
    });
    const sorters = {
      title: (left, right) => left.title.localeCompare(right.title),
      priority: (left, right) => number(right.priority) - number(left.priority),
      rating: (left, right) => number(right.rating) - number(left.rating),
      added: (left, right) => dateNumber(right.date_added) - dateNumber(left.date_added),
      oldest: (left, right) => dateNumber(left.date_added) - dateNumber(right.date_added),
      recent: (left, right) => dateNumber(right.last_played || right.date_started || right.date_added) - dateNumber(left.last_played || left.date_started || left.date_added),
    };
    return result.sort((left, right) => (sorters[filters.sort] || sorters.recent)(left, right) || left.title.localeCompare(right.title));
  }

  function progressText(item) {
    if (section === "shows") return `Season ${item.current_season ?? "?"} / Episode ${item.current_episode ?? "?"}${item.total_episodes != null ? ` / ${item.total_episodes} total` : ""}`;
    return number(item.hours_played) >= 0 ? `${item.hours_played} hours played` : "Hours not recorded";
  }

  function itemCard(item, list = false) {
    const card = element("article", `library-item${list ? " library-list-item" : ""}`);
    card.dataset.key = item.key;
    card.dataset.status = item.status;
    card.append(cover(item));
    const copy = element("div", "library-item-copy");
    const heading = element("h3");
    const title = element("button", "library-title", item.title);
    title.type = "button";
    title.addEventListener("click", () => openDetail(item.key));
    heading.append(title);
    copy.append(heading, element("p", "library-muted", [item.profile, item.platform].filter(Boolean).join(" / ")));
    copy.append(element("p", "library-progress", progressText(item)));
    const meta = element("div", "library-item-meta");
    meta.append(element("span", "library-status", state.status_labels[item.status] || item.status));
    meta.append(element("span", "", `Priority ${item.priority ?? "?"}`));
    if (number(item.rating) >= 0) meta.append(element("span", "", `${item.rating}/10`));
    (item.tags || []).slice(0, 3).forEach((tag) => meta.append(element("span", "library-tag", tag)));
    copy.append(meta);
    const actions = element("div", "library-item-actions");
    if (section === "shows" && !finishedStatuses.has(item.status)) {
      const advance = button("+1 episode", "plus", () => mutate("episode", item.key), true);
      advance.dataset.action = "episode";
      actions.append(advance);
    }
    if (["backlog", pausedStatus].includes(item.status)) actions.append(button(item.status === "backlog" ? "Start" : "Resume", "chevron-right", () => mutate("change", item.key, {fields: JSON.stringify({status: activeStatus})}), true));
    actions.append(button(section === "shows" ? "Edit season and episode" : "Edit details", "pencil", () => openDetail(item.key, section === "shows" ? "current_season" : "status"), false, true));
    card.append(copy, actions);
    return card;
  }

  function empty(parent, text) { parent.append(element("p", "library-empty", text)); }

  function render() {
    if (!state) return;
    const items = filteredItems();
    const values = filterValues();
    const filterCount = ["status", "platform", "genre", "tag", "priority", "rating"].filter((name) => values[name]).length;
    byId("filter-label").textContent = filterCount ? `Filters (${filterCount})` : "Filters";
    for (const name of ["continue", "board", "list"]) {
      byId(name).hidden = name !== view;
      byId(name).replaceChildren();
      const tab = byId(`tab-${name}`);
      tab.setAttribute("aria-selected", String(view === name));
      tab.tabIndex = view === name ? 0 : -1;
    }
    const continuing = items.filter((item) => [activeStatus, pausedStatus].includes(item.status));
    const continueGrid = element("div", "library-continue-grid");
    continuing.forEach((item) => continueGrid.append(itemCard(item)));
    byId("continue").append(continueGrid);
    if (!continuing.length) empty(byId("continue"), "No in-progress titles match these filters.");
    const board = element("div", "library-board-grid");
    for (const status of state.statuses) {
      const column = element("section", "library-column");
      column.dataset.status = status;
      const matches = items.filter((item) => item.status === status);
      column.append(element("h2", "", `${state.status_labels[status] || status} (${matches.length})`));
      matches.forEach((item) => column.append(itemCard(item)));
      if (!matches.length) empty(column, "No titles");
      board.append(column);
    }
    byId("board").append(board);
    items.forEach((item) => byId("list").append(itemCard(item, true)));
    if (!items.length) empty(byId("list"), "No titles match these filters.");
    byId("count").textContent = `${view === "continue" ? continuing.length : items.length} shown / ${items.length} matching / ${state.items.length} total`;
    const updated = new Date(state.updated_at);
    byId("freshness").textContent = `${state.cached ? "Cached" : "Updated"}${Number.isNaN(updated.getTime()) ? "" : ` ${updated.toLocaleString()}`}`;
    lockControls();
  }

  function renderSavedViews() {
    byId("saved").replaceChildren(new Option("Current filters", ""));
    savedViews.forEach((entry, index) => byId("saved").add(new Option(entry.name, String(index))));
    byId("delete-view").disabled = true;
  }

  function showDialog(dialog) {
    if (dialog.open) return;
    dialog.returnFocus = document.activeElement;
    dialog.querySelector("[data-dialog-message]").textContent = "";
    dialog.showModal();
    if (uncertain) message("Reload the library before another change. Your draft is kept.");
  }

  function rememberDraft() {
    const form = byId("edit-form");
    if (detailKey && form?.dataset.dirty === "true") {
      const changed = drafts.get(detailKey) || {};
      Object.keys(changed).forEach((name) => { changed[name] = form.elements[name].value; });
      drafts.set(detailKey, changed);
    }
  }

  root.querySelectorAll("dialog").forEach((dialog) => {
    dialog.querySelector("[data-dialog-close]").addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => {
      if (dialog === byId("detail")) { rememberDraft(); detailSequence += 1; }
      if (dialog.returnFocus?.isConnected) dialog.returnFocus.focus();
      else byId(`tab-${view}`).focus();
    });
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const dialog = Array.from(root.querySelectorAll("dialog[open]")).at(-1);
    if (!dialog) return;
    event.preventDefault();
    event.stopPropagation();
    dialog.close();
  }, true);

  async function loadState(profile = state?.profile || "", refresh = false, recovery = false) {
    if (pending || loading) return false;
    loading = true;
    lockControls();
    try {
      const next = await request(refresh || recovery ? "refresh" : `data?${new URLSearchParams({profile})}`, refresh || recovery ? {profile} : null);
      if (!validState(next) || next.profile !== profile || (recovery && next.cached)) throw new Error("Invalid state");
      const changedProfile = state?.profile !== next.profile;
      state = next;
      if (recovery) uncertain = false;
      populateFilters();
      if (changedProfile) loadPreferences(profile);
      render();
      if (detailKey && byId("detail").open) {
        const item = state.items.find((entry) => entry.key === detailKey);
        if (item) {
          byId("detail").dataset.version = item.version;
          const form = byId("edit-form");
          const changed = drafts.get(detailKey) || {};
          Object.entries(itemFields(item)).forEach(([name, value]) => {
            if (!form) return;
            form.querySelector(`[data-conflict="${name}"]`)?.remove();
            if (!(name in changed)) form.elements[name].value = value;
            else if (changed[name] !== value) {
              const conflict = element("small", "library-muted", `Saved value: ${value || "Not set"}. Your draft is retained.`);
              conflict.dataset.conflict = name;
              form.elements[name].parentElement.append(conflict);
            }
          });
        }
      }
      message(next.history_warning || (recovery ? "Library reloaded. Review your values before saving again." : ""));
      return true;
    } catch {
      filtersForm.elements.profile.value = state?.profile || "";
      message(uncertain ? "The change could not be verified. Refresh the library before making another change. Your draft is kept." : "The library could not be loaded. Your current data and drafts are kept.");
      return false;
    } finally { loading = false; lockControls(); }
  }

  function replaceConfirmed(item, expectedKey) {
    if (!validItem(item) || item.key !== expectedKey) throw new Error("Invalid confirmed item");
    const index = state.items.findIndex((entry) => entry.key === expectedKey);
    if (index < 0 || state.items[index].title !== item.title || state.items[index].profile !== item.profile) throw new Error("Item identity changed");
    state.items[index] = item;
    if (detailKey === item.key) byId("detail").dataset.version = item.version;
  }

  function clearUndo() {
    undo = null;
    clearTimeout(undoTimer);
    byId("undo").hidden = true;
    byId("detail").querySelector("[data-detail-undo]")?.remove();
  }

  function offerUndo(reply, key) {
    clearUndo();
    if (!reply.undo_token || !Number.isFinite(reply.undo_ttl_ms) || reply.undo_ttl_ms <= 0) return;
    undo = {token: reply.undo_token, key, expires: Date.now() + reply.undo_ttl_ms};
    byId("undo").hidden = false;
    if (byId("detail").open) {
      const control = button("Undo", "rotate-ccw", performUndo, true);
      control.dataset.detailUndo = "true";
      byId("detail").querySelector("[data-dialog-message]").after(control);
    }
    undoTimer = setTimeout(clearUndo, reply.undo_ttl_ms);
  }

  async function mutate(endpoint, key, extra = {}) {
    if (pending || loading || uncertain || !state) return false;
    const item = state.items.find((entry) => entry.key === key);
    if (!item) return false;
    pending = true;
    lockControls();
    message("Saving...");
    let recovery = false;
    try {
      const payload = endpoint === "undo" ? extra : {profile: item.profile, title: item.title, expected_version: item.version, ...extra};
      const reply = await request(endpoint, payload);
      if (reply.ok !== true) throw new Error("Unconfirmed change");
      replaceConfirmed(reply.item, key);
      if (endpoint === "undo") clearUndo();
      else offerUndo(reply, key);
      populateFilters();
      render();
      uncertain = Boolean(reply.history_warning);
      message(reply.history_warning || "Change saved.");
      return true;
    } catch (error) {
      recovery = ![400, 422].includes(error.status);
      uncertain = recovery;
      message(recovery ? "The change could not be verified. Reloading the library before another change." : "The change was not saved. Check your values and try again. Your draft is kept.");
      return false;
    } finally {
      pending = false;
      lockControls();
      if (recovery) { clearUndo(); await loadState(state.profile, false, true); }
    }
  }

  async function performUndo() {
    if (!undo) return;
    if (Date.now() >= undo.expires) { clearUndo(); message("Undo has expired."); return; }
    const key = undo.key;
    if (await mutate("undo", key, {token: undo.token})) {
      if (detailKey === key && byId("detail").open) await refreshDetail(key, false);
    }
  }

  function readEditForm(form) {
    return Object.fromEntries(editable.map((name) => [name, form.elements[name].value]));
  }

  function itemFields(item) {
    return Object.fromEntries(editable.map((name) => [name, name === "tags" ? (item.tags || []).join(", ") : String(item[name] ?? "")]));
  }

  function detailForm(item, focusField) {
    const form = element("form", "library-edit-form");
    form.id = "library-edit-form";
    const fields = {...itemFields(item), ...drafts.get(item.key)};
    form.dataset.dirty = String(drafts.has(item.key));
    for (const name of editable) {
      const label = element("label", name === "notes" || name === "tags" ? "library-wide" : "", labels[name]);
      let control;
      if (name === "status") {
        control = element("select");
        state.statuses.forEach((status) => control.add(new Option(state.status_labels[status] || status, status)));
      } else if (name === "notes") {
        control = element("textarea");
        control.rows = 4;
      } else {
        control = element("input");
        control.type = ["priority", "rating", "hours_played", "current_season", "current_episode", "total_episodes"].includes(name) ? "number" : "text";
        if (control.type === "number") {
          control.min = name === "priority" ? "1" : "0";
          control.step = name === "hours_played" ? "any" : "1";
          if (name === "priority") control.max = "5";
          if (name === "rating") control.max = "10";
        }
      }
      control.name = name;
      control.value = fields[name];
      label.append(control);
      form.append(label);
    }
    const actions = element("div", "library-actions library-wide");
    const save = button("Save changes", "save", () => {}, true);
    save.type = "submit";
    actions.append(save);
    form.append(actions);
    form.addEventListener("input", (event) => {
      if (!editable.includes(event.target.name)) return;
      form.dataset.dirty = "true";
      drafts.set(item.key, {...drafts.get(item.key), [event.target.name]: event.target.value});
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const values = readEditForm(form);
      const original = itemFields(state.items.find((entry) => entry.key === item.key) || item);
      const changes = Object.fromEntries(Object.keys(drafts.get(item.key) || {}).filter((name) => values[name] !== original[name]).map((name) => [name, values[name]]));
      if (!Object.keys(changes).length) { message("No changes to save."); return; }
      if (await mutate("change", item.key, {fields: JSON.stringify(changes)})) {
        drafts.delete(item.key);
        form.dataset.dirty = "false";
        await refreshDetail(item.key, false);
      }
    });
    if (focusField) setTimeout(() => { if (form.isConnected) form.elements[focusField]?.focus(); }, 0);
    return form;
  }

  function metadata(item) {
    const details = element("dl", "library-metadata");
    const values = {"Profile": item.profile, "Catalog source": item.source || "Manual", "Catalog ID": item.external_id,
      "Added": item.date_added, "Started": item.date_started, "Completed": item.date_completed,
      ...(section === "games" ? {"Last played": item.last_played, "Release date": item.release_date, "Developers": item.developers} : {"Premiere date": item.premiere_date, "Runtime": item.runtime})};
    Object.entries(values).forEach(([label, value]) => {
      if (!value) return;
      details.append(element("dt", "", label), element("dd", "", value));
    });
    return details;
  }

  function renderHistory(data) {
    const host = byId("history");
    if (!host) return;
    host.replaceChildren(element("h3", "", "Recorded web activity"));
    if (data.history_warning) host.append(element("p", "library-muted", "Recorded web activity is temporarily unavailable."));
    const events = Array.isArray(data.activity) ? data.activity : [];
    if (!events.length && !data.history_warning) empty(host, "No recorded web activity.");
    const list = element("ol", "library-history");
    for (const event of events) {
      const row = element("li");
      row.append(element("strong", "", String(event.kind || "Change").replaceAll("_", " ")), element("time", "library-muted", event.occurred_at || ""));
      Object.entries(event.changes || {}).forEach(([field, change]) => {
        const display = (value) => value == null || value === "" ? "Not set" : Array.isArray(value) ? value.join(", ") : String(value);
        row.append(element("p", "", `${labels[field] || field}: ${display(change.before)} -> ${display(change.after)}`));
      });
      list.append(row);
    }
    host.append(list, element("h3", "", "Runs"));
    const runs = Array.isArray(data.runs) ? data.runs : [];
    if (!runs.length) empty(host, "No recorded runs.");
    runs.forEach((run) => host.append(element("p", "library-run", `Run ${run.number} / ${run.status} / ${run.started_at || "Start not recorded"}${run.completed_at ? ` / ${run.completed_at}` : ""}${run.origin ? ` / ${run.origin}` : ""}`)));
  }

  function renderDetail(item, focusField) {
    const body = byId("detail-body");
    body.replaceChildren();
    byId("detail-title").textContent = item.title;
    byId("detail").dataset.version = item.version;
    const overview = element("div", "library-detail-overview");
    overview.append(cover(item), metadata(item));
    const url = safeURL(item.link);
    if (url) {
      const link = element("a", "btn", "Open catalog source");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      overview.append(link);
    }
    body.append(overview, detailForm(item, focusField));
    if (finishedStatuses.has(item.status)) body.append(button(section === "games" ? "Start replay" : "Start rewatch", "rotate-ccw", () => confirmRun(item.key), true));
    if (section === "games" && String(item.source).toLowerCase() === "steam") {
      const steam = element("section", "library-detail-section");
      steam.id = "library-steam";
      steam.append(element("h3", "", "Steam"), element("p", "library-muted", "Loading cached snapshot..."));
      body.append(steam);
    }
    const history = element("section", "library-detail-section");
    history.id = "library-history";
    history.append(element("h3", "", "Recorded web activity"), element("p", "library-muted", "Loading..."));
    body.append(history);
    lockControls();
  }

  async function refreshDetail(key, preserveDraft = true, focusField = null) {
    const item = state.items.find((entry) => entry.key === key);
    if (!item || !byId("detail").open || detailKey !== key) return;
    if (preserveDraft) rememberDraft();
    const sequence = ++detailSequence;
    try {
      const result = await request(`detail?${new URLSearchParams({profile: item.profile, title: item.title})}`);
      if (sequence !== detailSequence || detailKey !== key || !byId("detail").open || pending) return;
      if (state.items.find((entry) => entry.key === key)?.version !== item.version) return;
      replaceConfirmed(result.item, key);
      render();
      renderDetail(result.item, focusField);
      renderHistory(result);
    } catch {
      if (sequence === detailSequence && detailKey === key) {
        const current = state.items.find((entry) => entry.key === key);
        if (!preserveDraft && current) renderDetail(current, focusField);
        byId("history")?.replaceChildren(element("h3", "", "Recorded web activity"), element("p", "library-muted", "Activity could not be loaded. Your draft is kept."));
      }
    }
    if (sequence === detailSequence && byId("steam")) await loadSteam(key, false);
  }

  async function openDetail(key, focusField = null) {
    if (!state || pending || loading) return;
    const item = state.items.find((entry) => entry.key === key);
    if (!item) return;
    rememberDraft();
    detailKey = key;
    renderDetail(item, focusField);
    showDialog(byId("detail"));
    await refreshDetail(key, true, focusField);
  }

  function confirmRun(key) {
    const item = state.items.find((entry) => entry.key === key);
    if (!item || !finishedStatuses.has(item.status)) return;
    const dialog = byId("confirm");
    byId("confirm-title").textContent = section === "games" ? "Start replay?" : "Start rewatch?";
    const body = byId("confirm-body");
    body.replaceChildren(element("p", "", `${item.title}${section === "shows" ? " will restart at season 1, episode 0." : " will return to Playing."} Earlier history and original completion dates will be kept. Unsaved edits will be discarded.`));
    body.append(button("Cancel", "x", () => dialog.close()), button("Start new run", "rotate-ccw", async () => {
      if (await mutate("runs", key)) {
        drafts.delete(key);
        const form = byId("edit-form");
        if (form) form.dataset.dirty = "false";
        dialog.close();
        await refreshDetail(key, false);
      }
    }, true));
    showDialog(dialog);
    lockControls();
  }

  function renderSteam(key, data) {
    const host = byId("steam");
    if (!host || detailKey !== key) return;
    host.replaceChildren(element("h3", "", "Steam"));
    const snapshot = data.snapshot;
    host.append(element("p", "library-muted", snapshot ? `${data.stale ? "Stale snapshot" : "Cached snapshot"}${data.fetched_at ? ` / ${data.fetched_at}` : ""}` : "No cached snapshot."));
    if (snapshot) {
      const metrics = element("dl", "library-metadata");
      for (const [label, field] of [["Hours played", "hours_played"], ["Recent hours", "hours_recent"], ["Achievements unlocked", "achievements_unlocked"], ["Achievements total", "achievements_total"], ["Achievement percent", "achievement_percent"]]) {
        metrics.append(element("dt", "", label), element("dd", "", number(snapshot[field]) >= 0 ? snapshot[field] : "Unknown"));
      }
      host.append(metrics);
      if (snapshot.next_achievements?.length) {
        host.append(element("h4", "", "Next achievements"));
        const achievements = element("ul");
        snapshot.next_achievements.forEach((entry) => achievements.append(element("li", "", `${entry.name}${entry.description ? ` / ${entry.description}` : ""}`)));
        host.append(achievements);
      }
    }
    const actions = element("div", "library-actions");
    actions.append(button("Refresh Steam", "rotate-ccw", () => loadSteam(key, true), true));
    if (snapshot && number(snapshot.hours_played) >= 0 && typeof data.snapshot_id === "string" && data.snapshot_id) {
      actions.append(button("Save Steam hours", "save", async () => {
        if (await mutate("steam/save", key, {snapshot_id: data.snapshot_id})) await refreshDetail(key, true);
      }, true));
    }
    host.append(actions);
    lockControls();
  }

  async function loadSteam(key, refresh) {
    if (refresh && (pending || loading || uncertain)) return;
    const item = state.items.find((entry) => entry.key === key);
    if (!item) return;
    if (refresh) { pending = true; lockControls(); }
    const sequence = detailSequence;
    try {
      const data = await request(refresh ? "steam/refresh" : `steam?${new URLSearchParams({profile: item.profile, title: item.title})}`, refresh ? {profile: item.profile, title: item.title} : null);
      if (sequence === detailSequence && detailKey === key) renderSteam(key, data);
    } catch {
      if (sequence === detailSequence && detailKey === key) {
        message("Steam could not be loaded. Previously displayed values are unchanged.");
        if (byId("steam") && !byId("steam").querySelector("button")) renderSteam(key, {snapshot: null});
      }
    } finally { if (refresh) pending = false; lockControls(); }
  }

  async function pick() {
    if (pending || loading || uncertain || !state) return;
    const keys = filteredItems().map((item) => item.key);
    if (!keys.length) { message("No titles match these filters."); return; }
    pending = true;
    lockControls();
    try {
      const result = await request("pick", {profile: state.profile, keys: JSON.stringify(keys), exclude_recent: byId("exclude-recent").checked ? "1" : "0"});
      const body = byId("choice-body");
      byId("choice-title").textContent = "Surprise me";
      body.replaceChildren();
      if (!result.item) empty(body, "No eligible titles. You can include recent picks and try again.");
      else {
        if (!validItem(result.item) || !keys.includes(result.item.key)) throw new Error("Invalid pick");
        const item = result.item;
        replaceConfirmed(item, item.key);
        render();
        body.append(cover(item), element("h3", "", item.title), element("p", "library-muted", `${item.profile} / ${state.status_labels[item.status] || item.status}`));
        body.append(button("Open details", "pencil", () => { byId("choice").close(); openDetail(item.key); }));
        if (["backlog", pausedStatus].includes(item.status)) body.append(button("Start", "chevron-right", async () => {
          if (await mutate("change", item.key, {fields: JSON.stringify({status: activeStatus})})) byId("choice").close();
        }, true));
      }
      showDialog(byId("choice"));
    } catch { message("A title could not be picked. Your filters are unchanged."); }
    finally { pending = false; lockControls(); }
  }

  byId("save-view").addEventListener("click", () => {
    const body = byId("confirm-body");
    byId("confirm-title").textContent = "Save view";
    const form = element("form", "library-edit-form");
    const label = element("label", "library-wide", "View name");
    const name = element("input");
    name.name = "name";
    name.required = true;
    name.maxLength = 60;
    label.append(name);
    const save = button("Save view", "save", () => {});
    save.type = "submit";
    form.append(label, save);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const title = name.value.trim();
      if (!title) return;
      const existing = savedViews.findIndex((entry) => entry.name === title);
      const saved = {name: title, filters: filterValues(), view, excludeRecent: byId("exclude-recent").checked};
      if (existing >= 0) savedViews[existing] = saved;
      else if (savedViews.length < 12) savedViews.push(saved);
      else { message("Remove a saved view before adding another."); return; }
      renderSavedViews();
      persistPreferences();
      byId("confirm").close();
    });
    body.replaceChildren(form);
    showDialog(byId("confirm"));
    name.focus();
  });
  byId("saved").addEventListener("change", () => {
    const selection = byId("saved").value;
    const saved = selection === "" ? null : savedViews[Number(selection)];
    byId("delete-view").disabled = !saved;
    if (!saved) return;
    applyFilters(saved.filters);
    view = saved.view;
    byId("exclude-recent").checked = saved.excludeRecent;
    persistPreferences();
    render();
  });
  byId("delete-view").addEventListener("click", () => {
    if (byId("saved").value === "") return;
    savedViews.splice(Number(byId("saved").value), 1);
    renderSavedViews();
    persistPreferences();
  });
  filtersForm.addEventListener("submit", (event) => event.preventDefault());
  filtersForm.addEventListener("input", (event) => {
    if (event.target.name === "profile") return;
    byId("saved").value = "";
    byId("delete-view").disabled = true;
    persistPreferences();
    render();
  });
  filtersForm.elements.profile.addEventListener("change", async () => {
    const profile = filtersForm.elements.profile.value;
    persistPreferences();
    if (await loadState(profile)) { clearUndo(); byId("detail").close(); }
  });
  filtersForm.addEventListener("reset", (event) => {
    event.preventDefault();
    applyFilters(defaults);
    byId("saved").value = "";
    byId("delete-view").disabled = true;
    persistPreferences();
    render();
  });
  byId("tabs").addEventListener("click", (event) => {
    const tab = event.target.closest("[data-view]");
    if (!tab) return;
    view = tab.dataset.view;
    persistPreferences();
    render();
  });
  byId("tabs").addEventListener("keydown", (event) => {
    const tabs = Array.from(byId("tabs").querySelectorAll("[role=tab]"));
    const index = tabs.indexOf(document.activeElement);
    if (index < 0 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const target = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    tabs[target].click();
    tabs[target].focus();
  });
  byId("exclude-recent").addEventListener("change", persistPreferences);
  byId("pick").addEventListener("click", pick);
  byId("refresh").addEventListener("click", () => loadState(state?.profile || "", true, uncertain));
  byId("undo-button").addEventListener("click", performUndo);
  try {
    const seed = JSON.parse(byId("seed").textContent);
    if (validState(seed)) {
      state = seed;
      populateFilters();
      loadPreferences(state.profile);
      render();
    }
  } catch { message("The library could not be loaded."); }
  if (!state) loadState(new URLSearchParams(location.search).get("profile") || "");
})();