(() => {
  "use strict";
  const root = document.getElementById("home-live");
  if (!root) return;
  const find = (selector) => root.querySelector(selector);
  const FILTER_KEY = "luigi.home.filter";
  const HISTORY_MESSAGE = "This occurrence has a successor and its history cannot be changed.";
  const modes = ["today", "upcoming", "all"];
  const ranks = new Map();
  const handledFormResponses = new WeakSet();
  let state;
  let filter = "today";
  let showCompleted = false;
  let busy = false;
  let stale = false;
  let uncertain = false;
  let dateTask = null;
  let dateOpener = null;
  const dateDialog = find("#home-reschedule");
  const dateInput = find("#home-due-input");

  function normalize(value) {
    if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value.today)
        || !Array.isArray(value.tasks) || !Array.isArray(value.habits)
        || !Array.isArray(value.shortcuts) || typeof value.selection_available !== "boolean") {
      throw new Error("Invalid Home state");
    }
    const tasks = new Map();
    for (const task of value.tasks) {
      const endpoint = task.source === "task" ? "/tasks" : task.source === "recurring" ? "/recurring" : null;
      if (!endpoint || task.endpoint !== endpoint || typeof task.uuid !== "string" || !task.uuid
          || task.id !== `${task.source}:${task.uuid}` || typeof task.title !== "string"
          || typeof task.today !== "boolean" || typeof task.done !== "boolean"
          || (task.history_locked !== undefined && typeof task.history_locked !== "boolean")
          || typeof task.due !== "string" || !Array.isArray(task.blockers)) throw new Error("Invalid task");
      if (!tasks.has(task.id)) tasks.set(task.id, { ...task, due: task.due.slice(0, 10),
        history_locked: task.source === "recurring" && task.history_locked === true,
        completed_at: typeof task.completed_at === "string" ? task.completed_at : "" });
      if (!ranks.has(task.id)) ranks.set(task.id, ranks.size);
    }
    const habits = new Map();
    for (const habit of value.habits) {
      if (typeof habit.id !== "string" || !habit.id || typeof habit.title !== "string"
          || typeof habit.done !== "boolean") throw new Error("Invalid habit");
      if (!habits.has(habit.id)) habits.set(habit.id, habit);
    }
    return { ...value, tasks: [...tasks.values()].sort((left, right) => ranks.get(left.id) - ranks.get(right.id)),
      habits: [...habits.values()] };
  }

  function icon(name) {
    const element = document.createElement("span");
    element.className = "shell-icon";
    element.setAttribute("aria-hidden", "true");
    element.style.setProperty("--shell-icon", `url('/static/icons/lucide/${/^[a-z0-9-]+$/.test(name) ? name : "chevron-right"}.svg')`);
    return element;
  }

  const dateLabel = (value) => new Date(`${value}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const needsAttention = (task) => !task.done && ((task.due && task.due < state.today) || task.blockers.length > 0);
  const matches = (task, mode) => mode === "all" ? (!task.done || showCompleted)
    : !task.done && (mode === "today" ? task.today : !task.today && task.due > state.today);
  const taskFor = (element) => state.tasks.find((task) => task.id === element.closest("[data-task-id]")?.dataset.taskId);
  const taskPath = (task, action) => `${task.endpoint}/${encodeURIComponent(task.uuid)}/${action}`;

  function announce(text, error = false) {
    find("#home-status").textContent = text;
    find("#home-status").classList.toggle("home-error", error);
    find('[data-home-action="reload"]').hidden = !error;
    if (dateDialog.open) {
      find("#home-date-error").textContent = error ? text : "";
      find("#home-date-error").hidden = !error;
    }
  }

  function syncControls() {
    root.setAttribute("aria-busy", String(busy));
    for (const control of root.querySelectorAll("[data-home-write]")) {
      control.disabled = busy || stale || uncertain || control.dataset.homeBlocked === "true"
        || (control.dataset.homeAction === "today" && !state.selection_available);
    }
    find(".home-filters").disabled = busy;
    find("#home-show-completed").disabled = busy;
    find('[data-home-action="reload"]').disabled = busy;
    const modalSubmit = document.querySelector('[data-home-task-form] button[type="submit"]');
    if (modalSubmit) modalSubmit.disabled = busy || stale || uncertain;
    for (const control of dateDialog.querySelectorAll("button:not([data-home-write]), input")) control.disabled = busy;
  }

  function captureFocus(element = document.activeElement) {
    return { element, taskId: element?.closest("[data-task-id]")?.dataset.taskId,
      habitId: element?.dataset.homeHabit,
      action: element?.hasAttribute("data-home-complete") ? "complete" : element?.dataset.homeAction || "editor" };
  }

  function restoreFocus(saved) {
    if (!saved || (document.activeElement !== document.body && document.activeElement !== saved.element
        && document.activeElement?.isConnected)) return;
    let target = saved.element?.isConnected && !saved.element.disabled ? saved.element : null;
    if (!target && saved.taskId) {
      const row = [...find("#home-task-list").children].find((item) => item.dataset.taskId === saved.taskId);
      const selector = saved.action === "complete" ? "[data-home-complete]"
        : saved.action === "editor" ? "[data-home-editor]" : `[data-home-action="${saved.action}"]`;
      target = row?.querySelector(selector);
    }
    if (!target && saved.habitId) target = [...root.querySelectorAll("[data-home-habit]")].find((item) => item.dataset.homeHabit === saved.habitId);
    if (!target || target.disabled) target = find(`input[name="home-filter"][value="${filter}"]`);
    target?.focus({ preventScroll: true });
  }

  function render() {
    const focused = captureFocus();
    for (const mode of modes) {
      find(`[data-home-count="${mode}"]`).textContent = state.tasks.filter((task) => matches(task, mode)).length;
      find(`input[name="home-filter"][value="${mode}"]`).checked = mode === filter;
    }
    find(".home-completed").hidden = filter !== "all";
    find("#home-selection-error").hidden = state.selection_available;
    const tasks = state.tasks.filter((task) => matches(task, filter));
    const list = find("#home-task-list");
    list.replaceChildren();
    for (const task of tasks) {
      const row = find("#home-task-template").content.firstElementChild.cloneNode(true);
      row.dataset.taskId = task.id;
      row.classList.toggle("is-done", task.done);
      row.querySelector(".home-task-title").textContent = task.title;
      const editor = row.querySelector("[data-home-editor]");
      editor.setAttribute("hx-get", taskPath(task, "edit"));
      editor.setAttribute("aria-label", `Details: ${task.title}`);
      const complete = row.querySelector("[data-home-complete]");
      complete.checked = task.done;
      complete.dataset.homeBlocked = String(task.history_locked || (!task.done && task.blockers.length > 0));
      if (task.history_locked) complete.title = HISTORY_MESSAGE;
      row.querySelector(".sr-only").textContent = `${task.history_locked ? "Completed" : task.done ? "Reopen" : "Complete"} ${task.title}`;
      const metadata = row.querySelector(".home-task-meta");
      const completedDay = task.done && /^\d{4}-\d{2}-\d{2}T/.test(task.completed_at) ? task.completed_at.slice(0, 10) : "";
      const labels = [task.priority > 0 ? `P${task.priority}` : "", task.due ? `Due ${dateLabel(task.due)}` : "No due date",
        task.category, task.source === "recurring" ? "Recurring" : "", filter !== "today" && task.today ? "In Today" : "",
        completedDay ? `Completed ${dateLabel(completedDay)}` : "", task.history_locked ? "Next instance created" : "",
        !task.done && task.due && task.due < state.today ? "Overdue" : "",
        !task.done && task.blockers.length ? `Blocked by ${task.blockers.join(", ")}` : ""];
      for (const label of labels.filter(Boolean)) {
        const item = document.createElement("span");
        item.textContent = label;
        if (label.startsWith("Completed ")) item.title = task.completed_at;
        if (label === "Next instance created") { item.className = "chip chip-recurring"; item.title = HISTORY_MESSAGE; }
        if (label === "Overdue" || label.startsWith("Blocked by ")) item.className = "home-attention";
        metadata.append(item);
      }
      const today = row.querySelector('[data-home-action="today"]');
      const caption = `${task.today ? "Remove from Today" : "Add to Today"}: ${task.title}`;
      today.title = caption;
      today.setAttribute("aria-label", caption);
      today.setAttribute("aria-pressed", String(task.today));
      today.hidden = task.history_locked;
      today.dataset.homeBlocked = String(task.history_locked);
      row.querySelector("[data-home-today-icon]").replaceChildren(icon(task.today ? "x" : "plus"));
      const reschedule = row.querySelector('[data-home-action="reschedule"]');
      reschedule.setAttribute("aria-label", `Reschedule ${task.title}`);
      reschedule.hidden = task.history_locked;
      reschedule.dataset.homeBlocked = String(task.history_locked);
      list.append(row);
    }
    window.htmx?.process(list);
    find("#home-task-empty").hidden = tasks.length > 0;
    find("#home-task-empty").textContent = filter === "today"
      ? (state.tasks.some((task) => task.today) ? "Today's selected tasks are complete." : "No tasks selected for today.")
      : filter === "upcoming" ? "Nothing upcoming." : "No tasks in this view.";
    const attention = tasks.filter(needsAttention).length;
    find("#home-attention").hidden = attention === 0;
    find("#home-attention > span:last-child").textContent = `${attention} needs attention`;
    const todayTasks = state.tasks.filter((task) => task.today);
    const completed = todayTasks.filter((task) => task.done).length;
    find("#home-progress-label").textContent = `${completed} of ${todayTasks.length} tasks complete`;
    find("#home-progress").max = todayTasks.length || 1;
    find("#home-progress").value = completed;
    const habits = find("#home-habit-list");
    habits.replaceChildren();
    for (const habit of state.habits) {
      const row = find("#home-habit-template").content.firstElementChild.cloneNode(true);
      row.querySelector(".home-habit-name").textContent = habit.title;
      row.querySelector("small").textContent = `${habit.frequency_per_week}/wk`;
      row.title = `${habit.streak} day streak`;
      const checkbox = row.querySelector("input");
      checkbox.dataset.homeHabit = habit.id;
      checkbox.checked = habit.done;
      checkbox.setAttribute("aria-label", `${habit.done ? "Unmark" : "Complete"} ${habit.title}`);
      habits.append(row);
    }
    if (!state.habits.length) empty(habits, "No habits today.");
    find("#home-habit-count").textContent = `${state.habits.filter((habit) => habit.done).length} / ${state.habits.length}`;
    const agenda = find("#home-agenda");
    agenda.replaceChildren();
    const upcoming = state.tasks.filter((task) => matches(task, "upcoming"));
    for (const day of [...new Set(upcoming.map((task) => task.due))].sort().slice(0, 5)) {
      const row = document.createElement("li");
      const heading = document.createElement("strong");
      heading.textContent = dateLabel(day);
      const summary = document.createElement("span");
      const count = upcoming.filter((task) => task.due === day).length;
      summary.textContent = `${count} ${count === 1 ? "task" : "tasks"} due`;
      row.append(heading, summary);
      agenda.append(row);
    }
    if (!upcoming.length) empty(agenda, "Nothing coming up.", "li");
    const shortcuts = find("#home-shortcuts");
    shortcuts.replaceChildren();
    for (const link of state.shortcuts) {
      if (typeof link.href !== "string" || !/^\/(?!\/)[a-z0-9/_-]*$/i.test(link.href)) continue;
      const anchor = document.createElement("a");
      anchor.className = "home-shortcut";
      anchor.href = link.href;
      const label = document.createElement("span");
      label.textContent = link.label;
      anchor.append(icon(link.icon), label, icon("chevron-right"));
      shortcuts.append(anchor);
    }
    if (!shortcuts.children.length) empty(shortcuts, "No shortcuts available.");
    syncControls();
    restoreFocus(focused);
  }

  function empty(parent, text, tag = "p") {
    const element = document.createElement(tag);
    element.className = "home-empty";
    element.textContent = text;
    parent.append(element);
  }

  async function request(url, body, json = true) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await window.fetch(url, { method: body ? "POST" : "GET", credentials: "same-origin",
        cache: "no-store", redirect: "error", headers: { Accept: json ? "application/json" : "text/html" },
        ...(body ? { body: new URLSearchParams(body) } : {}), signal: controller.signal });
      if (!response.ok) {
        const failure = new Error("Request failed");
        failure.status = response.status;
        throw failure;
      }
      let triggers = {};
      try { triggers = JSON.parse(response.headers.get("HX-Trigger") || "{}"); } catch (_) {}
      if (triggers?.flashError) throw new Error("Write not verified");
      const data = json ? await response.json() : null;
      return { data, triggers };
    } finally {
      clearTimeout(timer);
    }
  }

  function dispatchUndo(triggers) {
    const undo = triggers?.showUndo;
    if (!undo || typeof undo.op_id !== "string" || !/^[a-zA-Z0-9_-]{1,128}$/.test(undo.op_id)) return;
    document.body.dispatchEvent(new CustomEvent("showUndo", { bubbles: true,
      detail: { op_id: undo.op_id, label: "Home change saved", ttl_ms: Math.min(60000, Math.max(1000, Number(undo.ttl_ms) || 12000)) } }));
  }

  async function loadState() {
    const next = normalize((await request(root.dataset.homeData)).data);
    const changedDay = state.today !== next.today;
    state = next;
    stale = false;
    if (changedDay) find("#home-date").textContent = new Date(`${state.today}T12:00:00`).toLocaleDateString(undefined,
      { weekday: "long", month: "long", day: "numeric", year: "numeric" });
    render();
  }

  async function refresh() {
    if (busy) return;
    const focused = captureFocus();
    busy = true;
    syncControls();
    try {
      await loadState();
      uncertain = false;
      announce("");
    } catch (_) {
      stale = true;
      announce("Home could not refresh. Showing previous data; reload before making changes.", true);
    } finally {
      busy = false;
      syncControls();
      restoreFocus(focused);
    }
  }

  async function write(url, body, { json = true, verify = () => true, message = "Change saved.", closeDate = false } = {}) {
    if (busy || stale || uncertain) return;
    const focused = captureFocus();
    busy = true;
    syncControls();
    announce("");
    let confirmed = false;
    try {
      const result = await request(url, body, json);
      if (!verify(result.data)) throw new Error("Write not verified");
      confirmed = true;
      dispatchUndo(result.triggers);
      if (closeDate) dateDialog.close();
      await loadState();
      announce(message);
    } catch (error) {
      if (confirmed) {
        stale = true;
        announce("Change saved, but Home could not refresh. Showing stale data; reload before making more changes.", true);
      } else if (error.status === 409 && url === "/home/today") {
        try { await loadState(); } catch (_) { stale = true; }
        announce(stale ? "Today selection conflicted, and Home could not refresh. Reload before retrying."
          : "The day changed or Today selection conflicted. Review the refreshed day before choosing again.", true);
      } else if (error.status >= 400 && error.status < 500) {
        announce(error.status === 401 || error.status === 403 ? "Session expired or access denied. Reload before retrying."
          : "Could not save this change. Review the task and try again.", true);
      } else {
        uncertain = true;
        announce("The write could not be confirmed. Reload Home before retrying; it may already have saved.", true);
      }
    } finally {
      busy = false;
      syncControls();
      restoreFocus(confirmed && closeDate ? dateOpener : focused);
    }
  }

  root.addEventListener("htmx:beforeRequest", (event) => {
    const trigger = event.detail.elt;
    if (!trigger?.matches("[data-home-editor]")) return;
    if (busy || stale || uncertain) { event.preventDefault(); return; }
    busy = true;
    syncControls();
    event.detail.xhr.timeout = 12000;
    window.openModal?.();
  });

  root.addEventListener("htmx:afterRequest", (event) => {
    if (!event.detail.elt?.matches("[data-home-editor]")) return;
    busy = false;
    syncControls();
  });

  document.body.addEventListener("htmx:afterSwap", (event) => {
    if (event.target.id !== "modal-body") return;
    const form = event.target.querySelector("form.entity-form[hx-post]");
    if (!form || !/^\/(tasks|recurring)(\/[^/]+)?$/.test(form.getAttribute("hx-post"))) return;
    form.setAttribute("hx-target", "#modal-body");
    form.setAttribute("hx-swap", "none");
    form.setAttribute("hx-sync", "this:drop");
    form.setAttribute("hx-disabled-elt", "find button[type='submit']");
    form.setAttribute("hx-request", '{"timeout":12000}');
    form.dataset.homeTaskForm = "true";
    window.htmx?.process(form);
  });

  document.body.addEventListener("htmx:beforeRequest", (event) => {
    const form = event.detail.elt;
    if (!form?.matches("[data-home-task-form]")) return;
    if (busy || stale || uncertain) { event.preventDefault(); return; }
    busy = true;
    syncControls();
  });

  document.body.addEventListener("htmx:beforeOnLoad", (event) => {
    const form = event.detail.elt;
    const response = event.detail.xhr;
    if (!form?.matches("[data-home-task-form]") || response.status < 200 || response.status >= 300) return;
    handledFormResponses.add(response);
    event.preventDefault();
    window.closeModal?.();
    loadState().then(() => {
      announce("Task saved.");
    }).catch(() => {
      stale = true;
      announce("Task saved, but Home could not refresh. Showing stale data; reload before making more changes.", true);
    }).finally(() => {
      busy = false;
      syncControls();
    });
  });

  document.body.addEventListener("htmx:afterRequest", (event) => {
    if (handledFormResponses.has(event.detail.xhr)) return;
    const form = event.detail.elt;
    if (!form?.matches("[data-home-task-form]")) return;
    if (event.detail.successful) return;
    busy = false;
    if (!event.detail.successful) {
      const status = event.detail.xhr.status;
      uncertain = status === 0 || status >= 500;
      const text = uncertain ? "The write could not be confirmed. Reload Home before retrying; it may already have saved."
        : "Could not save this change. Your draft is unchanged.";
      let error = form.querySelector("[data-home-editor-error]");
      if (!error) {
        error = document.createElement("p");
        error.dataset.homeEditorError = "true";
        error.setAttribute("role", "alert");
        form.append(error);
      }
      error.textContent = text;
      announce(text, true);
    }
    syncControls();
  });

  document.body.addEventListener("reloadBoard", () => {
    busy = false;
    refresh();
  });

  root.addEventListener("click", (event) => {
    const control = event.target.closest("[data-home-action]");
    if (!control || control.disabled) return;
    const action = control.dataset.homeAction;
    if (action === "reload") { refresh(); return; }
    if (action === "cancel-date" && !busy) { dateDialog.close(); return; }
    if (action === "clear-date" && !busy) { dateInput.value = ""; dateInput.focus(); return; }
    if (busy || stale || uncertain) return;
    const task = taskFor(control);
    if (!task || task.history_locked) return;
    if (action === "today" && state.selection_available) {
      const selected = !task.today;
      const day = state.today;
      write("/home/today", { source: task.source, uuid: task.uuid, selected: String(selected), day },
        { verify: (data) => data?.selected === selected && data.day === day,
          message: selected ? "Added to Today. Due date unchanged." : "Removed from Today. Due date unchanged." });
    }
    if (action === "reschedule") {
      dateTask = task.id;
      dateOpener = captureFocus(control);
      dateInput.value = task.due;
      find("#home-reschedule-title").textContent = task.title;
      find("#home-date-error").hidden = true;
      dateDialog.showModal();
    }
  });

  root.addEventListener("change", (event) => {
    const input = event.target;
    if (input.matches('[name="home-filter"]')) {
      filter = input.value;
      try { sessionStorage.setItem(FILTER_KEY, filter); } catch (_) {}
      render();
    }
    if (input.id === "home-show-completed") { showCompleted = input.checked; render(); }
    if (input.matches("[data-home-complete]")) {
      const task = taskFor(input);
      if (!task) return;
      input.checked = task.done;
      if (task.history_locked || (!task.done && task.blockers.length)) return;
      write(taskPath(task, "complete"), {}, { json: false, message: task.done ? "Task reopened." : "Task completed." });
    }
    if (input.matches("[data-home-habit]")) {
      const habit = state.habits.find((item) => item.id === input.dataset.homeHabit);
      if (!habit) return;
      const marked = !habit.done;
      input.checked = habit.done;
      write(`/discipline/${encodeURIComponent(habit.id)}/today`, { action: marked ? "mark" : "unmark" },
        { verify: (data) => data?.ok === true && data.discipline_uuid === habit.id && data.marked === marked,
          message: marked ? "Habit completed." : "Habit cleared for today." });
    }
  });

  find("#home-reschedule-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const task = state.tasks.find((item) => item.id === dateTask);
    if (!task || task.history_locked || !event.target.reportValidity()) return;
    const due = dateInput.value;
    write("/home/reschedule", { source: task.source, uuid: task.uuid, due_date: due },
      { verify: (data) => typeof data?.due === "string" && data.due.slice(0, 10) === due,
        message: "Due date saved. Today selection unchanged.", closeDate: true });
  });
  dateDialog.addEventListener("cancel", (event) => { if (busy) event.preventDefault(); });
  dateDialog.addEventListener("close", () => requestAnimationFrame(() => restoreFocus(dateOpener)));

  try {
    const saved = sessionStorage.getItem(FILTER_KEY);
    if (modes.includes(saved)) filter = saved;
  } catch (_) {}
  try {
    state = normalize(JSON.parse(find("#home-state").textContent));
  } catch (_) {
    state = { today: "", tasks: [], habits: [], shortcuts: [], selection_available: false };
    stale = true;
  }
  render();
  refresh();
})();