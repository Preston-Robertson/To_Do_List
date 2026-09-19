(() => {
  "use strict";

  const editors = new WeakMap();
  const requests = new WeakMap();
  const handledResponses = new WeakSet();
  const endpointPattern = /^\/(tasks|recurring)(?:\/([a-zA-Z0-9_-]+))?$/;

  function showError(form, message) {
    const error = form.querySelector("[data-task-editor-error]");
    error.textContent = message;
    error.hidden = !message;
  }

  function setBusy(form, busy) {
    const state = editors.get(form);
    if (!state) return;
    state.busy = busy;
    form.setAttribute("aria-busy", String(busy));
    state.submit.disabled = busy;
    state.submit.textContent = busy ? "Saving..." : state.submitLabel;
  }

  function initializeForm(form) {
    if (editors.has(form)) return;
    const submit = form.querySelector('button[type="submit"]');
    const toggle = form.querySelector("[data-task-repeat]");
    const schedule = form.querySelector("[data-task-schedule]");
    const interval = form.elements.namedItem("recurring_interval");
    const days = [...form.querySelectorAll('input[name="recurring_days"]')];
    const ordinal = form.elements.namedItem("recurring_month_ordinal");
    const weekday = form.elements.namedItem("recurring_month_weekday");
    const title = form.elements.namedItem("task");
    const state = { busy: false, submit, submitLabel: submit.textContent, expected: null };
    editors.set(form, state);

    function validate() {
      title.setCustomValidity(title.value.trim() ? "" : "Enter a task.");
      if (!toggle) return;
      [schedule, interval, ordinal, weekday, ...days].forEach((control) => control.setCustomValidity(""));
      if (!toggle.checked) return;
      if (!["interval", "weekdays", "monthly"].includes(schedule.value)) {
        schedule.setCustomValidity("Choose a schedule.");
      } else if (schedule.value === "interval") {
        const value = Number(interval.value);
        if (!Number.isSafeInteger(value) || value < 1) {
          interval.setCustomValidity("Enter a whole number of days greater than zero.");
        }
      } else if (schedule.value === "weekdays" && !days.some((day) => day.checked)) {
        days[0].setCustomValidity("Choose at least one weekday.");
      } else if (schedule.value === "monthly") {
        if (!["1", "2", "3", "4", "-1"].includes(ordinal.value)) {
          ordinal.setCustomValidity("Choose a position in the month.");
        }
        if (!["0", "1", "2", "3", "4", "5", "6"].includes(weekday.value)) {
          weekday.setCustomValidity("Choose a weekday.");
        }
      }
    }

    function sync() {
      if (toggle) {
        toggle.setAttribute("aria-expanded", String(toggle.checked));
        form.querySelector("[data-task-repeat-settings]").hidden = !toggle.checked;
        schedule.disabled = !toggle.checked;
        form.querySelectorAll("[data-task-repeat-panel]").forEach((panel) => {
          const active = toggle.checked && panel.dataset.taskRepeatPanel === schedule.value;
          panel.hidden = !active;
          panel.querySelectorAll("input, select, button").forEach((control) => { control.disabled = !active; });
        });
        form.querySelectorAll("[data-task-repeat-preset]").forEach((button) => {
          const selected = button.dataset.taskRepeatPreset === interval.value;
          button.classList.toggle("is-active", selected);
          button.setAttribute("aria-pressed", String(selected));
        });
      }
      validate();
    }

    form.addEventListener("input", sync);
    form.addEventListener("change", sync);
    form.addEventListener("invalid", (event) => {
      const details = event.target.closest("details");
      if (details) details.open = true;
    }, true);
    form.querySelectorAll("[data-task-repeat-preset]").forEach((button) => {
      button.addEventListener("click", () => {
        interval.value = button.dataset.taskRepeatPreset;
        sync();
      });
    });
    form.addEventListener("submit", (event) => {
      sync();
      if (state.busy || !form.reportValidity()) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    }, true);
    form.addEventListener("htmx:beforeRequest", (event) => {
      if (event.defaultPrevented) return;
      sync();
      if (state.busy || !form.reportValidity()) {
        event.preventDefault();
        event.stopImmediatePropagation();
        return;
      }
      const endpoint = form.getAttribute("hx-post");
      const match = endpointPattern.exec(endpoint || "");
      const config = event.detail.requestConfig;
      if (!match || config?.verb?.toLowerCase() !== "post" || config.path !== endpoint) return;
      const isNew = form.dataset.taskEditorNew === "true";
      const source = isNew && toggle?.checked ? "/recurring" : `/${match[1]}`;
      state.expected = {
        source,
        uuid: isNew ? null : match[2],
        message: `${source === "/recurring" ? "Recurring task" : "Task"} ${isNew ? "created" : "saved"}`,
      };
      requests.set(event.detail.xhr, form);
      showError(form, "");
      setBusy(form, true);
    });
    sync();
  }

  function initialize(root) {
    if (root.matches?.("form[data-task-editor]")) initializeForm(root);
    root.querySelectorAll?.("form[data-task-editor]").forEach(initializeForm);
  }

  function verifiedResponse(xhr, expected) {
    if (!expected || !(xhr.getResponseHeader("Content-Type") || "").toLowerCase().startsWith("text/html")) return false;
    let triggers;
    try {
      triggers = JSON.parse(xhr.getResponseHeader("HX-Trigger") || "{}");
    } catch {
      return false;
    }
    if (triggers?.flashSuccess?.message !== expected.message) return false;
    const content = new DOMParser().parseFromString(xhr.responseText, "text/html");
    const card = content.body.firstElementChild;
    return content.body.children.length === 1 && card?.matches("article.card[data-uuid][data-endpoint]") &&
      card.dataset.endpoint === expected.source && !!card.dataset.uuid &&
      card.id === `card-${card.dataset.uuid}` && (!expected.uuid || card.dataset.uuid === expected.uuid);
  }

  document.addEventListener("htmx:beforeOnLoad", (event) => {
    const xhr = event.detail.xhr;
    const form = requests.get(xhr);
    if (!form || handledResponses.has(xhr)) return;
    setBusy(form, false);
    const successful = xhr.status >= 200 && xhr.status < 300;
    if (successful && form.hasAttribute("data-home-task-form")) return;
    if (!successful) {
      event.preventDefault();
      showError(form, xhr.status >= 400 && xhr.status < 500
        ? "Task was not saved. Check the fields and try again."
        : "Save could not be confirmed. Check the task list before trying again.");
      return;
    }
    if (!document.getElementById("kanban-board")) return;
    event.preventDefault();
    const expected = editors.get(form).expected;
    if (!verifiedResponse(xhr, expected)) {
      showError(form, "Save could not be confirmed. Check the task list before trying again.");
      return;
    }
    handledResponses.add(xhr);
    document.body.dispatchEvent(new CustomEvent("flashSuccess", { bubbles: true, detail: { message: expected.message } }));
    document.body.dispatchEvent(new CustomEvent("closeModal", { bubbles: true }));
    document.body.dispatchEvent(new CustomEvent("reloadBoard", { bubbles: true }));
  });

  ["htmx:afterRequest", "htmx:sendError", "htmx:timeout", "htmx:sendAbort"].forEach((name) => {
    document.addEventListener(name, (event) => {
      const xhr = event.detail.xhr;
      const form = requests.get(xhr);
      if (!form) return;
      setBusy(form, false);
      if (!handledResponses.has(xhr) && (name !== "htmx:afterRequest" || event.detail.failed)) {
        if (form.querySelector("[data-task-editor-error]").hidden) {
          showError(form, "Save could not be confirmed. Check the task list before trying again.");
        }
      }
    });
  });

  document.addEventListener("htmx:afterSwap", (event) => initialize(event.detail?.elt || event.target));
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => initialize(document));
  } else {
    initialize(document);
  }
})();