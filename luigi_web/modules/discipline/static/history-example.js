(() => {
  "use strict";
  const root = document.getElementById("dh-example");
  if (!root) return;
  const find = (selector) => root.querySelector(selector);
  const seed = JSON.parse(find("#dh-seed").textContent);
  const dayLength = 86400000;
  const dateValue = (value) => {
    if (typeof value !== "string" || !/^2030-\d{2}-\d{2}$/.test(value)) return null;
    const parsed = new Date(`${value}T00:00:00Z`);
    return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value ? parsed : null;
  };
  const dateKey = (value) => value.toISOString().slice(0, 10);
  const addDays = (value, amount) => dateKey(new Date(dateValue(value).getTime() + amount * dayLength));
  const formatDay = (value) => dateValue(value).toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
  const shortDay = (value) => dateValue(value).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
  const formatStamp = (value) => `${value.slice(0, 10)} ${value.slice(11, 19)} UTC`;
  const editable = (value) => Boolean(dateValue(value)) && value <= seed.sampleToday;
  let records = new Map(seed.completions.map((record) => [record.date, { ...record }]));
  let previous = null, month = 3, selectedDay = "", origin = null;
  const dialog = find("#dh-day-dialog");
  const make = (tag, className, text) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  };
  const announce = (message) => { find("#dh-status").textContent = message; };
  document.addEventListener("htmx:beforeRequest", (event) => {
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);
  document.querySelectorAll("[data-command-open], [data-command-input], [data-assistant-open], .logout button").forEach((control) => { control.disabled = true; });
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
    root.querySelectorAll('[role="tab"]').forEach((item) => {
      const selected = item === tab;
      item.setAttribute("aria-selected", String(selected));
      item.tabIndex = selected ? 0 : -1;
      find(`#${item.getAttribute("aria-controls")}`).hidden = !selected;
    });
    if (focus) tab.focus();
  }
  find('[role="tablist"]').addEventListener("keydown", (event) => {
    const tabs = [...root.querySelectorAll('[role="tab"]')];
    const index = tabs.indexOf(event.target);
    if (index < 0 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    activate(tabs[next]);
  });
  function dayButton(value, compact = false) {
    const completed = records.has(value);
    const button = make("button", compact ? "dh-year-day" : "dh-day");
    button.type = "button";
    button.dataset.dhDay = value;
    button.classList.toggle("is-complete", completed);
    button.classList.toggle("is-current", value === seed.sampleToday);
    button.disabled = !editable(value);
    const label = `${formatDay(value)}: ${!editable(value) ? "Future date" : completed ? "Completed" : "No completion"}`;
    button.setAttribute("aria-label", label);
    button.setAttribute("aria-haspopup", "dialog");
    button.title = label;
    if (value === seed.sampleToday) button.setAttribute("aria-current", "date");
    if (compact) button.tabIndex = value === seed.sampleToday ? 0 : -1;
    else {
      button.append(make("span", "", String(dateValue(value).getUTCDate())));
      if (completed) button.append(find("#dh-check-icon").content.cloneNode(true));
    }
    return button;
  }
  function renderMonth() {
    const first = new Date(Date.UTC(2030, month, 1));
    const offset = (first.getUTCDay() + 6) % 7;
    const count = new Date(Date.UTC(2030, month + 1, 0)).getUTCDate();
    find("#dh-month-heading").textContent = first.toLocaleDateString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" });
    const cells = Array.from({ length: 42 }, (_, index) => {
      if (index < offset || index >= offset + count) {
        const blank = make("span");
        blank.setAttribute("aria-hidden", "true");
        return blank;
      }
      return dayButton(dateKey(new Date(Date.UTC(2030, month, index - offset + 1))));
    });
    find("#dh-month-days").replaceChildren(...cells);
    find("#dh-prev").disabled = month === 0;
    find("#dh-next").disabled = month === 11;
    const prefix = dateKey(first).slice(0, 7);
    const total = [...records.keys()].filter((value) => value.startsWith(prefix)).length;
    find("#dh-month-total").textContent = `${total} ${total === 1 ? "completion" : "completions"}`;
  }
  function renderYear() {
    const first = dateValue("2030-01-01");
    const offset = (first.getUTCDay() + 6) % 7;
    find("#dh-year-months").replaceChildren(...Array.from({ length: 12 }, (_, index) => {
      const start = new Date(Date.UTC(2030, index, 1));
      const column = Math.floor(((start - first) / dayLength + offset) / 7) + 1;
      const label = make("span", "", start.toLocaleDateString("en-GB", { month: "short", timeZone: "UTC" }));
      label.style.gridColumn = `${column} / span 4`;
      label.dataset.dhMonth = dateKey(start).slice(0, 7);
      label.title = start.toLocaleDateString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" });
      return label;
    }));
    find("#dh-year-days").replaceChildren(...Array.from({ length: 371 }, (_, index) => {
      if (index < offset || index >= offset + 365) {
        const blank = make("span");
        blank.setAttribute("aria-hidden", "true");
        return blank;
      }
      return dayButton(addDays("2030-01-01", index - offset), true);
    }));
    find("#dh-year-count").textContent = `${records.size} completions`;
  }
  function renderSummary() {
    const startOfWeek = addDays(seed.sampleToday, -((dateValue(seed.sampleToday).getUTCDay() + 6) % 7));
    const countWeek = (start) => [...records.keys()].filter((value) => value >= start && value < addDays(start, 7)).length;
    const current = countWeek(startOfWeek);
    find("#dh-week-count").textContent = `${current} / ${seed.weeklyTarget}`;
    find("#dh-week-progress").max = seed.weeklyTarget;
    find("#dh-week-progress").value = Math.min(current, seed.weeklyTarget);
    find("#dh-week-progress").setAttribute("aria-valuetext", `${current} completions; weekly target ${seed.weeklyTarget}`);
    find("#dh-total").textContent = String(records.size);
    const latest = [...records.keys()].sort().at(-1);
    find("#dh-last").textContent = latest ? formatDay(latest) : "None";
    find("#dh-week-list").replaceChildren(...seed.weekStarts.map((start) => {
      const row = make("li");
      row.dataset.dhWeek = start;
      const label = make("span", "dh-week-label", `${shortDay(start)} - ${shortDay(addDays(start, 6))}`);
      if (start === startOfWeek) { row.setAttribute("aria-current", "date"); label.append(make("small", "", "This week")); }
      row.append(label, make("span", "dh-week-total", `${countWeek(start)} / ${seed.weeklyTarget}`));
      return row;
    }));
  }
  function renderLog() {
    const sorted = [...records.values()].sort((left, right) => right.date.localeCompare(left.date));
    find("#dh-log").replaceChildren(...sorted.map((record) => {
      const row = make("li");
      row.dataset.dhRecord = record.date;
      const button = make("button", "dh-log-entry");
      button.type = "button";
      button.dataset.dhDay = record.date;
      button.setAttribute("aria-haspopup", "dialog");
      button.setAttribute("aria-label", `Inspect ${formatDay(record.date)}`);
      [["Date", record.date], ["Completed at (UTC)", record.completedAt], ["Logged at (UTC)", record.loggedAt]].forEach(([label, value]) => {
        const column = make("span");
        const time = make("time", "", value.length === 10 ? formatDay(value) : formatStamp(value));
        time.dateTime = value;
        column.append(make("small", "", label), time);
        button.append(column);
      });
      row.append(button);
      return row;
    }));
    find("#dh-log-empty").hidden = records.size > 0;
    find("#dh-log-count").textContent = `${records.size} ${records.size === 1 ? "entry" : "entries"}`;
  }
  function render() { renderMonth(); renderYear(); renderSummary(); renderLog(); find("#dh-undo").disabled = previous === null; }
  function closeDay() {
    dialog.close();
    const replacement = origin && find(`#${origin.panel} [data-dh-day="${origin.date}"]`);
    (replacement || find('[role="tab"][aria-selected="true"]')).focus();
  }
  function openDay(value, control) {
    if (!editable(value) || dialog.open) return;
    selectedDay = value;
    origin = { date: value, panel: control.closest('[role="tabpanel"]').id };
    const record = records.get(value);
    find("#dh-day-heading").textContent = formatDay(value);
    find("#dh-day-status").textContent = record ? "Completed" : "No completion";
    find("#dh-timestamps").hidden = !record;
    [["#dh-completed-at", record?.completedAt], ["#dh-logged-at", record?.loggedAt]].forEach(([selector, stamp]) => {
      find(selector).textContent = stamp ? formatStamp(stamp) : "";
      find(selector).dateTime = stamp || "";
    });
    find("#dh-time-field").hidden = Boolean(record);
    find("#dh-time").disabled = Boolean(record);
    find("#dh-time").value = "18:30";
    find("#dh-time").max = value === seed.sampleToday ? seed.sampleNow.slice(11, 16) : "23:59";
    find("#dh-mark").hidden = Boolean(record);
    find("#dh-remove").hidden = !record;
    find("#dh-day-error").textContent = "";
    dialog.showModal();
    find(record ? "#dh-cancel" : "#dh-time").focus();
  }
  function remember() { previous = new Map([...records].map(([value, record]) => [value, { ...record }])); }
  find("#dh-day-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!dialog.open || !editable(selectedDay) || records.has(selectedDay)) return;
    const time = find("#dh-time").value;
    const completedAt = `${selectedDay}T${time}:00Z`;
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(time) || completedAt > seed.sampleNow) {
      find("#dh-day-error").textContent = "Choose a valid completion time no later than 20:00 on 24 April 2030.";
      return;
    }
    remember();
    records.set(selectedDay, { date: selectedDay, completedAt, loggedAt: seed.sampleNow });
    render(); closeDay(); announce(`Marked complete: ${formatDay(selectedDay)}.`);
  });
  find("#dh-remove").addEventListener("click", () => {
    if (!dialog.open || !editable(selectedDay) || !records.has(selectedDay)) return;
    remember(); records.delete(selectedDay);
    render(); closeDay(); announce(`Removed completion: ${formatDay(selectedDay)}.`);
  });
  find("#dh-undo").addEventListener("click", () => {
    if (!previous) return;
    records = previous; previous = null;
    render(); announce("Last change undone.");
  });
  find("#dh-reset").addEventListener("click", () => {
    if (dialog.open) closeDay();
    records = new Map(seed.completions.map((record) => [record.date, { ...record }]));
    previous = null; selectedDay = ""; origin = null; month = 3;
    render(); activate(find("#dh-month-tab"), false); announce("Example reset.");
  });
  find("#dh-prev").addEventListener("click", () => { if (month > 0) { month -= 1; renderMonth(); } });
  find("#dh-next").addEventListener("click", () => { if (month < 11) { month += 1; renderMonth(); } });
  find("#dh-close").addEventListener("click", closeDay);
  find("#dh-cancel").addEventListener("click", closeDay);
  dialog.addEventListener("cancel", (event) => { event.preventDefault(); closeDay(); });
  root.addEventListener("click", (event) => {
    const tab = event.target.closest('[role="tab"]');
    if (tab) activate(tab);
    const control = event.target.closest("[data-dh-day]");
    if (control) openDay(control.dataset.dhDay, control);
  });
  find("#dh-year-days").addEventListener("keydown", (event) => {
    const control = event.target.closest("[data-dh-day]");
    const movements = { ArrowRight: 7, ArrowLeft: -7, ArrowDown: 1, ArrowUp: -1 };
    if (!control || !Object.hasOwn(movements, event.key)) return;
    event.preventDefault();
    const value = addDays(control.dataset.dhDay, movements[event.key]);
    if (!editable(value)) return;
    const next = find(`#dh-year-days [data-dh-day="${value}"]`);
    if (next) {
      find("#dh-year-days").querySelectorAll("button").forEach((button) => { button.tabIndex = button === next ? 0 : -1; });
      next.focus();
    }
  });
  render();
})();