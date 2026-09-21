(() => {
  "use strict";
  const root = document.getElementById("home-preview");
  if (!root) return;
  const find = (selector) => root.querySelector(selector);
  const seed = JSON.parse(find("#hp-seed").textContent);
  const initial = { tasks: seed.tasks, habits: seed.habits };
  let state = structuredClone(initial);
  let undoState = null;
  let filter = "today";
  let nextId = 1;
  const dateLabel = (value) => value ? new Date(`${value}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "No due date";
  const attention = (task) => !task.done && !task.attentionDismissed && task.due && task.due < seed.today;
  const matches = (task, mode) => mode === "all" || (!task.done && (mode === "today" ? task.today : !task.today && task.due > seed.today));
  const taskFor = (element) => state.tasks.find((task) => task.id === element.closest("[data-task-id]")?.dataset.taskId);
  const announce = (message) => { find("#hp-status").textContent = message; };

  document.addEventListener("htmx:beforeRequest", (event) => {
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);
  document.querySelectorAll("#undo-toast, #success-toast, #error-toast").forEach((toast) => toast.remove());
  document.querySelectorAll("[data-command-open], [data-command-input]").forEach((control) => {
    control.disabled = true;
    control.title = "Search is unavailable in the synthetic preview";
  });

  function render() {
    for (const mode of ["today", "upcoming", "all"]) {
      find(`[data-preview-count="${mode}"]`).textContent = state.tasks.filter((task) => matches(task, mode)).length;
      find(`input[value="${mode}"]`).checked = mode === filter;
    }
    const tasks = state.tasks.filter((task) => matches(task, filter)).sort((left, right) =>
      Number(left.done) - Number(right.done) || Number(right.priority === "High") - Number(left.priority === "High") || (left.due || "9999").localeCompare(right.due || "9999"));
    const list = find("#hp-task-list");
    list.replaceChildren();
    for (const task of tasks) {
      const row = find("#hp-task-template").content.firstElementChild.cloneNode(true);
      row.dataset.taskId = task.id;
      row.classList.toggle("is-done", task.done);
      row.querySelector(".hp-task-title").textContent = task.title;
      row.querySelector(".hp-task-open").setAttribute("aria-label", `Details: ${task.title}`);
      row.querySelector(".sr-only").textContent = `Complete ${task.title}`;
      row.querySelector("[data-preview-complete]").checked = task.done;
      const metadata = row.querySelector(".hp-task-meta");
      for (const label of [task.priority === "High" ? "High priority" : "", task.due ? `Due ${dateLabel(task.due)}` : "No due date", filter === "all" && task.today ? "In Today" : "", attention(task) ? "Needs attention" : ""]) {
        if (!label) continue;
        const item = document.createElement("span");
        item.textContent = label;
        if (label === "Needs attention") item.className = "hp-attention";
        metadata.append(item);
      }
      row.querySelector(".hp-dismiss").hidden = !attention(task);
      row.querySelector('[data-preview-action="reschedule"]').setAttribute("aria-label", `Reschedule ${task.title}`);
      list.append(row);
    }
    find("#hp-empty").hidden = tasks.length !== 0;
    const attentionCount = tasks.filter(attention).length;
    find("#hp-attention").hidden = attentionCount === 0;
    find("#hp-attention > span:last-child").textContent = `${attentionCount} needs attention`;
    const todayTasks = state.tasks.filter((task) => task.today);
    const completed = todayTasks.filter((task) => task.done).length;
    find("#hp-progress-label").textContent = `${completed} of ${todayTasks.length} tasks complete`;
    find("#hp-progress").max = todayTasks.length || 1;
    find("#hp-progress").value = completed;
    const habits = find("#hp-habit-list");
    habits.replaceChildren();
    for (const habit of state.habits) {
      const row = find("#hp-habit-template").content.firstElementChild.cloneNode(true);
      row.querySelector("span").textContent = habit.title;
      const checkbox = row.querySelector("input");
      checkbox.dataset.previewHabit = habit.id;
      checkbox.checked = habit.done;
      habits.append(row);
    }
    find("#hp-habit-count").textContent = `${state.habits.filter((habit) => habit.done).length} / ${state.habits.length}`;
    const dates = [...new Set([...seed.slots.map((slot) => slot.date), ...state.tasks.filter((task) => !task.done && task.due > seed.today).map((task) => task.due)])].sort();
    find("#hp-agenda").replaceChildren();
    for (const date of dates) {
      const row = document.createElement("li");
      const heading = document.createElement("strong");
      heading.textContent = dateLabel(date);
      const count = state.tasks.filter((task) => !task.done && task.due === date).length;
      const summary = document.createElement("span");
      summary.textContent = `${count} ${count === 1 ? "task" : "tasks"} due`;
      row.append(heading, summary);
      for (const slot of seed.slots.filter((slot) => slot.date === date)) {
        const label = document.createElement("span");
        label.textContent = `${slot.time} ${slot.label}`;
        row.append(label);
      }
      find("#hp-agenda").append(row);
    }
    find('[data-preview-action="undo"]').disabled = !undoState;
  }

  function focusAfterRender(active) {
    if (active?.isConnected && !active.disabled) return active.focus();
    const taskId = active?.closest("[data-task-id]")?.dataset.taskId;
    const selector = active?.matches("[data-preview-complete]") ? "[data-preview-complete]" : ".hp-task-open";
    const row = Array.from(find("#hp-task-list").children).find((item) => item.dataset.taskId === taskId);
    const habit = Array.from(root.querySelectorAll("[data-preview-habit]")).find((item) => item.dataset.previewHabit === active?.dataset.previewHabit);
    (row?.querySelector(selector) || habit || find(".hp-task-open") || find('[data-preview-action="add"]')).focus();
  }

  function change(message, action) {
    const active = document.activeElement;
    undoState = structuredClone(state);
    action();
    render();
    announce(message);
    focusAfterRender(active);
  }

  function updateDetails(task) {
    find("#hp-detail-title").value = task.title;
    find("#hp-detail-notes").value = task.notes;
    find("#hp-detail-priority").value = task.priority;
    find("#hp-detail-due").textContent = task.due ? `Due ${dateLabel(task.due)}` : "No due date";
    find("#hp-detail-today").textContent = task.today ? "In Today" : "Not in Today";
    find('#hp-details [data-preview-action="today"]').textContent = task.today ? "Remove from today" : "Add to Today";
    find("#hp-detail-done").checked = task.done;
  }

  function openDialog(dialog, trigger, task) {
    dialog.previewOpener = trigger;
    if (task) dialog.dataset.taskId = task.id;
    dialog.showModal();
  }

  root.querySelectorAll("dialog").forEach((dialog) => {
    dialog.addEventListener("close", () => focusAfterRender(dialog.previewOpener));
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      const bounds = dialog.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
    });
  });

  root.addEventListener("click", (event) => {
    const button = event.target.closest("[data-preview-action]");
    if (!button) return;
    const action = button.dataset.previewAction;
    const task = taskFor(button);
    if (action === "close") button.closest("dialog").close();
    if (action === "details" && task) {
      updateDetails(task);
      openDialog(find("#hp-details"), button, task);
    }
    if (action === "reschedule" && task) {
      find("#hp-due-input").value = task.due;
      find("#hp-reschedule-title").textContent = task.title;
      openDialog(find("#hp-reschedule"), button, task);
    }
    if (action === "clear-date") { find("#hp-due-input").value = ""; find("#hp-due-input").focus(); }
    if (action === "add") { find("#hp-add-form").reset(); openDialog(find("#hp-add"), button); }
    if (action === "dismiss" && task) change("Attention dismissed. Due date unchanged.", () => { task.attentionDismissed = true; });
    if (action === "today" && task) {
      change(task.today ? "Removed from Today. Due date unchanged." : "Added to Today. Due date unchanged.", () => { task.today = !task.today; });
      find("#hp-detail-today").textContent = task.today ? "In Today" : "Not in Today";
      button.textContent = task.today ? "Remove from today" : "Add to Today";
    }
    if (action === "undo" && undoState) {
      state = undoState;
      undoState = null;
      render();
      announce("Last change undone.");
      focusAfterRender(button);
    }
    if (action === "reset") {
      state = structuredClone(initial);
      undoState = null;
      filter = "today";
      nextId = 1;
      render();
      announce("Demo reset.");
    }
  });

  root.addEventListener("change", (event) => {
    const input = event.target;
    if (input.matches('[name="preview-filter"]')) {
      filter = input.value;
      render();
      announce(`${find(`[data-preview-count="${filter}"]`).textContent} tasks in ${filter === "all" ? "All" : filter === "today" ? "Today" : "Upcoming"}.`);
    }
    if (input.matches("[data-preview-complete]")) {
      const task = taskFor(input);
      if (task) change(input.checked ? "Task completed." : "Task reopened.", () => { task.done = input.checked; });
    }
    if (input.matches("[data-preview-habit]")) {
      const habit = state.habits.find((item) => item.id === input.dataset.previewHabit);
      if (habit) change(input.checked ? "Habit completed." : "Habit reopened.", () => { habit.done = input.checked; });
    }
  });

  root.addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.target;
    if (!form.reportValidity()) return;
    const task = taskFor(form);
    if (form.id === "hp-details-form" && task) {
      const title = find("#hp-detail-title").value.trim();
      if (!title) { find("#hp-detail-title").focus(); return; }
      change("Task updated.", () => {
        task.title = title;
        task.notes = find("#hp-detail-notes").value.trim();
        task.priority = find("#hp-detail-priority").value;
      });
    }
    if (form.id === "hp-reschedule-form" && task) {
      change("Due date updated. Today selection unchanged.", () => {
        task.due = find("#hp-due-input").value;
        task.attentionDismissed = false;
      });
      if (find("#hp-details").open) find("#hp-detail-due").textContent = task.due ? `Due ${dateLabel(task.due)}` : "No due date";
    }
    if (form.id === "hp-add-form") {
      const title = find("#hp-add-title").value.trim();
      if (!title) { find("#hp-add-title").focus(); return; }
      change("Task added.", () => {
        const inToday = find("#hp-add-today").checked;
        const due = find("#hp-add-due").value;
        state.tasks.push({ id: `local-example-${nextId++}`, title, notes: "", due, today: inToday, priority: "Normal", done: false, attentionDismissed: false });
        filter = inToday ? "today" : due > seed.today ? "upcoming" : "all";
      });
    }
    form.closest("dialog").close();
  });
  render();
})();