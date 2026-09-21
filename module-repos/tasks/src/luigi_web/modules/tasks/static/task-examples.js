(() => {
  "use strict";
  const root = document.getElementById("task-examples");
  if (!root) return;
  const find = (selector) => root.querySelector(selector);
  const initial = JSON.parse(find("#te-seed").textContent);
  let state = structuredClone(initial), previous = null, sequence = 0;
  let editingTask = "", editingRule = "", deletion = null;
  const types = { completion: "Completion trigger", dependency: "Dependency", reminder: "Reminder" };
  const taskById = (id) => state.tasks.find((task) => task.id === id);
  const announce = (message) => { find("#te-status").textContent = message; };
  const blocked = (task) => !task.done && state.rules.some((rule) => rule.enabled && rule.type === "dependency" && rule.target === task.id && !taskById(rule.source)?.done);
  const summary = (rule) => {
    const source = taskById(rule.source)?.title, target = taskById(rule.target)?.title;
    if (rule.type === "completion") return `When ${source} completes, create a new copy of ${target}.`;
    if (rule.type === "dependency") return `${target} waits for ${source}.`;
    return `Remind ${source} ${rule.lead} minutes before due date.`;
  };
  document.addEventListener("htmx:beforeRequest", (event) => {
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);
  document.querySelectorAll("[data-command-open], [data-command-input], .logout button").forEach((control) => {
    control.disabled = true;
  });
  document.querySelectorAll("#undo-toast, #success-toast, #error-toast").forEach((toast) => toast.remove());
  document.addEventListener("submit", (event) => {
    if (!root.contains(event.target)) { event.preventDefault(); event.stopImmediatePropagation(); }
  }, true);
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault(); event.stopImmediatePropagation();
    }
  }, true);
  function activate(tab, focus = true) {
    tab.closest('[role="tablist"]').querySelectorAll('[role="tab"]').forEach((item) => {
      const selected = item === tab;
      item.setAttribute("aria-selected", String(selected));
      item.tabIndex = selected ? 0 : -1;
      find(`#${item.getAttribute("aria-controls")}`).hidden = !selected;
    });
    if (focus) tab.focus();
  }
  root.querySelectorAll('[role="tablist"]').forEach((tablist) => {
    tablist.addEventListener("keydown", (event) => {
      const tabs = [...tablist.querySelectorAll('[role="tab"]')];
      const index = tabs.indexOf(event.target);
      if (index < 0 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      activate(tabs[next]);
    });
  });
  function render() {
    const focused = document.activeElement;
    const rowId = focused?.closest("[data-task-id], [data-rule-id]")?.dataset;
    const selector = focused?.hasAttribute("data-te-complete") ? "[data-te-complete]" : focused?.hasAttribute("data-te-enabled") ? "[data-te-enabled]" : `[data-te-action="${focused?.dataset.teAction}"]`;
    const project = find("#te-project").value;
    const projects = [...new Set(state.tasks.map((task) => task.project))].sort();
    if (project && !projects.includes(project)) projects.push(project);
    find("#te-project").replaceChildren(new Option("All projects", ""), ...projects.map((name) => new Option(name, name)));
    find("#te-project").value = project;
    const query = find("#te-search").value.trim().toLowerCase(), mode = find("#te-filter").value;
    const tasks = state.tasks.filter((task) => (!project || task.project === project) && (mode === "all" || task.done === (mode === "done")) && `${task.title} ${task.project}`.toLowerCase().includes(query));
    const sort = find("#te-sort").value, ranks = { High: 0, Normal: 1, Low: 2 };
    if (sort !== "order") tasks.sort((left, right) => sort === "priority" ? ranks[left.priority] - ranks[right.priority] : (sort === "due" ? (left.due || "9999-99-99").localeCompare(right.due || "9999-99-99") : left.title.localeCompare(right.title)));
    find("#te-task-list").replaceChildren(...tasks.map((task) => {
      const row = find("#te-task-template").content.firstElementChild.cloneNode(true);
      row.dataset.taskId = task.id;
      row.classList.toggle("is-done", task.done);
      row.querySelector(".te-task-title").textContent = task.title;
      row.querySelector("[data-te-project]").textContent = task.project;
      row.querySelector("[data-te-priority]").textContent = `${task.priority} priority`;
      row.querySelector("[data-te-date]").textContent = task.due || "No due date";
      const completed = row.querySelector("[data-te-completed]");
      completed.hidden = !task.done || !task.completedAt;
      completed.textContent = task.completedAt ? `Completed ${new Date(task.completedAt).toLocaleString()}` : "";
      completed.dateTime = task.completedAt || "";
      row.dataset.ruleType = task.ruleType || task.id;
      row.dataset.generatedBy = task.generatedBy || "";
      row.dataset.completedAt = task.completedAt || "";
      row.querySelector("[data-te-recurring]").hidden = !task.recurring;
      row.querySelector("[data-te-blocked]").hidden = !blocked(task);
      const checkbox = row.querySelector("[data-te-complete]");
      checkbox.checked = task.done;
      checkbox.setAttribute("aria-label", `${task.done ? "Reopen" : "Complete"} ${task.title}`);
      row.querySelector('[data-te-action="edit-task"]').setAttribute("aria-label", `Edit ${task.title}`);
      row.querySelector('[data-te-action="delete-task"]').setAttribute("aria-label", `Delete ${task.title}`);
      return row;
    }));
    find("#te-task-empty").hidden = tasks.length > 0;
    find("#te-count").textContent = `${tasks.length} of ${state.tasks.length} tasks`;
    for (const kind of Object.keys(types)) {
      const rules = state.rules.filter((rule) => rule.type === kind);
      find(`#te-${kind}-list`).replaceChildren(...rules.map((rule) => {
        const row = find("#te-rule-template").content.firstElementChild.cloneNode(true);
        row.dataset.ruleId = rule.id;
        row.querySelector(".te-rule-type").textContent = types[rule.type];
        row.querySelector(".te-rule-summary").textContent = summary(rule);
        const checkbox = row.querySelector("[data-te-enabled]");
        checkbox.checked = rule.enabled;
        checkbox.setAttribute("aria-label", `Enabled: ${summary(rule)}`);
        row.querySelector('[data-te-action="edit-rule"]').setAttribute("aria-label", `Edit rule: ${summary(rule)}`);
        row.querySelector('[data-te-action="delete-rule"]').setAttribute("aria-label", `Delete rule: ${summary(rule)}`);
        return row;
      }));
      find(`#te-${kind}-empty`).hidden = rules.length > 0;
    }
    find("#te-undo").disabled = previous === null;
    if (rowId) {
      const row = rowId.taskId ? find(`[data-task-id="${rowId.taskId}"]`) : find(`[data-rule-id="${rowId.ruleId}"]`);
      (row?.querySelector(selector) || find(rowId.taskId ? "#te-capture-title" : "#te-add-rule")).focus();
    }
  }
  function change(operation, message) {
    previous = structuredClone(state);
    operation();
    render();
    announce(message);
  }
  function validText(input) {
    input.setCustomValidity(input.value.trim() ? "" : "Enter a nonempty value.");
    return input.reportValidity();
  }
  root.addEventListener("input", (event) => { event.target.setCustomValidity?.(""); });
  root.addEventListener("change", (event) => {
    event.target.setCustomValidity?.("");
    if (event.target.matches("[data-te-complete]")) {
      const task = taskById(event.target.closest("[data-task-id]").dataset.taskId);
      const completing = event.target.checked;
      if (completing === task.done) return;
      if (blocked(task)) { render(); announce("Complete the prerequisite task first."); return; }
      let created = 0;
      change(() => {
        task.done = completing;
        task.completedAt = completing ? new Date().toISOString() : null;
        if (completing) state.rules.filter((rule) => rule.enabled && rule.type === "completion" && rule.source === (task.ruleType || task.id)).forEach((rule) => {
          const template = taskById(rule.target);
          if (!template) return;
          state.tasks.push({
            ...structuredClone(template), id: `example-new-task-${++sequence}`,
            ruleType: template.ruleType || template.id, generatedBy: rule.id,
            createdAt: new Date().toISOString(), done: false, completedAt: null,
          });
          created += 1;
        });
      }, completing ? "Task completed." : "Task marked incomplete.");
      if (created) announce(`Task completed. ${created} new ${created === 1 ? "copy" : "copies"} created.`);
    }
    if (event.target.matches("[data-te-enabled]")) {
      const rule = state.rules.find((item) => item.id === event.target.closest("[data-rule-id]").dataset.ruleId);
      change(() => { rule.enabled = !rule.enabled; }, rule.enabled ? "Rule disabled." : "Rule enabled.");
    }
    if (event.target.matches("#te-rule-type")) ruleFields();
    if (event.target.matches("#te-rule-source, #te-rule-type")) find("#te-rule-target").setCustomValidity("");
  });
  ["#te-search", "#te-filter", "#te-project", "#te-sort"].forEach((selector) => find(selector).addEventListener("input", render));
  function editTask(task) {
    editingTask = task.id;
    find("#te-task-form").reset();
    for (const name of ["title", "project", "priority", "due"]) find(`#te-task-${name}`).value = task[name];
    find("#te-task-recurring").checked = task.recurring;
    find("#te-task-form").querySelectorAll("input").forEach((input) => input.setCustomValidity(""));
    find("#te-task-dialog").showModal();
  }
  function ruleFields() {
    const kind = find("#te-rule-type").value, reminder = kind === "reminder";
    find("#te-target-field").hidden = reminder;
    find("#te-rule-target").disabled = reminder;
    find("#te-lead-field").hidden = !reminder;
    find("#te-rule-lead").disabled = !reminder;
    find("#te-source-label").textContent = kind === "completion" ? "After completing" : reminder ? "Task" : "Prerequisite task";
    find("#te-target-label").textContent = kind === "completion" ? "Create a new copy of" : "Dependent task";
  }
  function editRule(rule) {
    editingRule = rule?.id || "";
    find("#te-rule-form").reset();
    find("#te-rule-heading").textContent = rule ? "Edit rule" : "Add rule";
    find("#te-rule-type").value = rule?.type || find('[data-te-kind][aria-selected="true"]').dataset.teKind;
    for (const name of ["source", "target"]) {
      const select = find(`#te-rule-${name}`);
      select.replaceChildren(new Option("Choose task type", ""), ...state.tasks.filter((task) => !task.generatedBy).map((task) => new Option(task.title, task.id)));
      select.value = rule?.[name] || "";
      select.setCustomValidity("");
    }
    find("#te-rule-lead").value = rule?.lead || 30;
    find("#te-rule-lead").setCustomValidity("");
    find("#te-rule-enabled").checked = rule?.enabled ?? true;
    ruleFields();
    find("#te-rule-dialog").showModal();
  }
  root.addEventListener("click", (event) => {
    const tab = event.target.closest('[role="tab"]');
    if (tab) { activate(tab); return; }
    const button = event.target.closest("[data-te-action]");
    if (!button) return;
    const action = button.dataset.teAction;
    const task = taskById(button.closest("[data-task-id]")?.dataset.taskId);
    const rule = state.rules.find((item) => item.id === button.closest("[data-rule-id]")?.dataset.ruleId);
    if (action === "close") button.closest("dialog").close();
    if (action === "edit-task") editTask(task);
    if (action === "edit-rule") editRule(rule);
    if (action === "add-rule") editRule(null);
    if (action.startsWith("delete-")) {
      deletion = { type: task ? "task" : "rule", id: task?.id || rule.id };
      find("#te-delete-heading").textContent = task ? "Delete task?" : "Delete rule?";
      find("#te-delete-description").textContent = task ? `${task.title}. Rules referencing this task will also be removed.` : summary(rule);
      find("#te-delete-dialog").showModal();
    }
    if (action === "undo" && previous) { state = previous; previous = null; render(); announce("Last change undone."); }
    if (action === "reset") {
      state = structuredClone(initial); previous = null;
      find("#te-search").value = ""; find("#te-project").value = "";
      find("#te-filter").value = "all"; find("#te-sort").value = "order";
      find("#te-capture").reset(); render(); announce("Examples reset.");
    }
  });
  find("#te-capture").addEventListener("submit", (event) => {
    event.preventDefault();
    const title = find("#te-capture-title");
    if (!validText(title)) return;
    change(() => state.tasks.push({ id: `example-new-task-${++sequence}`, title: title.value.trim(), project: find("#te-project").value || "Example project", priority: "Normal", due: "", done: false, recurring: false }), "Task added.");
    title.value = ""; title.focus();
  });
  find("#te-task-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!["#te-task-title", "#te-task-project"].every((selector) => validText(find(selector)))) return;
    const task = taskById(editingTask);
    find("#te-task-dialog").close();
    change(() => {
      for (const name of ["title", "project", "priority", "due"]) task[name] = find(`#te-task-${name}`).value.trim();
      task.recurring = find("#te-task-recurring").checked;
    }, "Task updated.");
  });
  find("#te-rule-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const kind = find("#te-rule-type").value, source = find("#te-rule-source"), target = find("#te-rule-target"), lead = find("#te-rule-lead");
    target.setCustomValidity(kind === "dependency" && source.value === target.value ? "A task cannot depend on itself." : "");
    lead.setCustomValidity(kind === "reminder" && (!Number.isInteger(Number(lead.value)) || Number(lead.value) <= 0) ? "Enter a positive whole number of minutes." : "");
    if (!event.target.reportValidity()) return;
    const draft = { id: editingRule || `example-new-rule-${++sequence}`, type: kind, source: source.value, target: kind === "reminder" ? "" : target.value, lead: Number(lead.value), enabled: find("#te-rule-enabled").checked };
    find("#te-rule-dialog").close();
    change(() => {
      const index = state.rules.findIndex((rule) => rule.id === editingRule);
      if (index < 0) state.rules.push(draft); else state.rules[index] = draft;
    }, editingRule ? "Rule updated." : "Rule added.");
    activate(find(`#te-${kind}-tab`), false);
  });
  find("#te-delete-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!deletion) return;
    find("#te-delete-dialog").close();
    change(() => {
      if (deletion.type === "task") {
        state.tasks = state.tasks.filter((task) => task.id !== deletion.id);
        state.rules = state.rules.filter((rule) => rule.source !== deletion.id && rule.target !== deletion.id);
      } else state.rules = state.rules.filter((rule) => rule.id !== deletion.id);
    }, deletion.type === "task" ? "Task deleted." : "Rule deleted.");
    find(deletion.type === "task" ? "#te-capture-title" : "#te-add-rule").focus();
    deletion = null;
  });
  render();
})();