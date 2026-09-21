(() => {
  "use strict";
  const maximum = 100000000000000n;
  const endpoints = Object.freeze({
    state: "/finance/planning/state", calculate: "/finance/planning/calculate",
    compare: "/finance/planning/compare", save: "/finance/planning/save",
    delete: "/finance/planning/delete", incomeSave: "/finance/planning/income/save",
    incomeDelete: "/finance/planning/income/delete", lock: "/finance/lock"
  });

  function scaledInteger(raw, scale = 2) {
    const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(String(raw).trim());
    if (!match || (match[3] || "").length > scale) throw new Error("Enter exact decimal values with at most two decimal places.");
    const factor = 10n ** BigInt(scale);
    const magnitude = BigInt(match[2]) * factor + BigInt((match[3] || "").padEnd(scale, "0") || "0");
    if (magnitude > maximum) throw new Error("A value exceeds the supported range.");
    return Number(match[1] ? -magnitude : magnitude);
  }

  function decimalString(raw) {
    if (!Number.isSafeInteger(raw)) throw new Error("Value unavailable.");
    const value = BigInt(raw);
    const magnitude = value < 0n ? -value : value;
    return `${value < 0n ? "-" : ""}${magnitude / 100n}.${String(magnitude % 100n).padStart(2, "0")}`;
  }

  function formatMinor(raw, currency) {
    if (!Number.isSafeInteger(raw)) throw new Error("Value unavailable.");
    const value = BigInt(raw);
    const magnitude = value < 0n ? -value : value;
    const whole = magnitude / 100n;
    const signed = value < 0n ? (whole === 0n ? -0 : -whole) : whole;
    return new Intl.NumberFormat(undefined, {style: "currency", currency,
      minimumFractionDigits: 2, maximumFractionDigits: 2}).formatToParts(signed)
      .map(part => part.type === "fraction" ? String(magnitude % 100n).padStart(2, "0") : part.value).join("");
  }

  function transport(onLocked) {
    const controllers = new Set();
    let disposed = false;
    return {
      async request(command, body) {
        if (disposed || !Object.hasOwn(endpoints, command)) throw new Error("Request unavailable.");
        const controller = new AbortController();
        controllers.add(controller);
        const timer = setTimeout(() => controller.abort(), 30000);
        try {
          const response = await fetch(endpoints[command], {
            method: body === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store",
            redirect: command === "lock" ? "manual" : "error", referrerPolicy: "no-referrer",
            headers: {"Accept": "application/json", "Content-Type": "application/json"},
            ...(body === undefined ? {} : {body: JSON.stringify(body)}), signal: controller.signal
          });
          if (disposed) throw new Error("Request unavailable.");
          if (response.status === 401 || response.status === 403) {
            onLocked(response.status === 401 ? "/login" : "/finance/unlock");
          }
          if (command === "lock" && (response.ok || response.type === "opaqueredirect" || response.status === 303)) return {};
          if (!response.ok) {
            const message = response.status === 409 ? "Record changed. Reload saved lists and load the record again."
              : response.status === 422 ? "Invalid planning request. Check values, dates and comparison horizons."
              : "Finance planning is unavailable. Reload and try again.";
            throw Object.assign(new Error(message), {status: response.status});
          }
          return await response.json();
        } catch (error) {
          if (error.status) throw error;
          throw Object.assign(new Error("Request not confirmed. Reload before trying again."), {status: 0});
        } finally {
          clearTimeout(timer);
          controllers.delete(controller);
        }
      },
      destroy() { disposed = true; for (const controller of controllers) controller.abort(); controllers.clear(); }
    };
  }

  function mount(root) {
    if (root.dataset.planningReady) return;
    root.dataset.planningReady = "true";
    const find = identifier => root.querySelector(`#${identifier}`);
    const seed = find("fp-seed");
    let state;
    try { state = JSON.parse(seed.textContent); } catch { find("fp-status").textContent = "Planning data unavailable. Reload to try again."; return; }
    seed.remove();
    let mode = "cashflow";
    let busy = false;
    let uncertain = false;
    let chart = null;
    let rendered = null;
    let incomeLoaded = null;
    const loaded = {cashflow: null, housing: null};
    const names = {cashflow: "", housing: ""};
    const lifetime = new AbortController();
    const on = (target, event, action) => target.addEventListener(event, action, {signal: lifetime.signal});
    const kind = () => mode === "housing" ? "housing" : "cashflow";
    const fields = () => find(mode === "housing" ? "fp-housing-fields" : "fp-cash-fields");
    const money = value => formatMinor(value, state.currency);
    const element = (tag, text, className) => {
      const node = document.createElement(tag);
      if (text !== undefined) node.textContent = text;
      if (className) node.className = className;
      return node;
    };
    const api = transport(destination => { cleanup(); location.replace(destination); });

    function status(message, error = false) {
      find("fp-status").textContent = message;
      find("fp-status").dataset.error = String(error);
    }

    function sync() {
      if (!state) return;
      find("fp-load").disabled = busy || !find("fp-saved").value;
      find("fp-delete").disabled = busy || uncertain || !loaded[kind()];
      find("fp-income-delete").disabled = busy || uncertain || !incomeLoaded;
      find("fp-save").disabled = busy || uncertain;
      find("fp-income-form").querySelector('[type="submit"]').disabled = busy || uncertain;
      find("fp-compare").disabled = busy || root.querySelectorAll('[data-plan-choice]:checked').length < 2;
      find("fp-compare-current").disabled = busy || !find("fp-compare-draft").value;
      const record = loaded[kind()];
      const current = record && state.plans.find(plan => plan.id === record.id);
      find("fp-version").textContent = !record ? "Unsaved" : current?.version === record.version
        ? `Loaded version ${record.version}` : `Loaded version ${record.version}; reload required`;
      const incomeMode = find("fp-cash-fields").querySelector('[name="income_mode"]').value;
      find("fp-fixed-income").hidden = incomeMode !== "fixed";
      find("fp-income-growth").hidden = incomeMode === "streams";
    }

    async function perform(action) {
      if (busy || !state) return;
      busy = true;
      root.setAttribute("aria-busy", "true");
      const disabled = new Map();
      for (const control of root.querySelectorAll("input, select, button")) {
        if (control.closest("#fp-lock")) continue;
        disabled.set(control, control.disabled);
        control.disabled = true;
      }
      try { await action(); }
      catch (error) { if (state) status(error.status !== undefined ? error.message : "Check the required values and try again.", true); }
      finally {
        busy = false;
        if (state) {
          for (const [control, previous] of disabled) control.disabled = previous;
          root.removeAttribute("aria-busy");
          sync();
        }
      }
    }

    function read(group) {
      const result = {};
      for (const input of group.querySelectorAll("[name]")) {
        const value = input.value.trim();
        if (input.required && !value) throw new Error("Required value missing.");
        result[input.name] = input.type === "checkbox" ? input.checked
          : input.dataset.scale ? scaledInteger(value, Number(input.dataset.scale))
          : input.type === "number" ? scaledInteger(value, 0) : value;
      }
      return result;
    }

    function fill(group, values) {
      for (const input of group.querySelectorAll("[name]")) {
        const value = values[input.name];
        if (input.type === "checkbox") input.checked = Boolean(value);
        else input.value = value === undefined || value === null ? "" : input.dataset.scale ? decimalString(value) : String(value);
      }
    }

    function config() {
      return {schema_version: 1, currency: state.currency, ...read(fields())};
    }

    function dirty() {
      if (rendered) find("fp-result-state").textContent = "Inputs changed; previous calculation shown";
    }

    function lists() {
      const selected = find("fp-saved").value;
      const comparison = find("fp-compare-draft").value;
      const checked = new Set(Array.from(root.querySelectorAll('[data-plan-choice]:checked'), node => node.value));
      for (const identifier of ["fp-saved", "fp-compare-draft"]) {
        find(identifier).replaceChildren(new Option("Choose a scenario", ""));
      }
      find("fp-compare-list").replaceChildren(element("legend", "Saved scenarios (two or three)"));
      for (const plan of state.plans.filter(item => item.kind === kind())) {
        find("fp-saved").add(new Option(plan.name, plan.id));
        find("fp-compare-draft").add(new Option(plan.name, plan.id));
        const label = element("label", undefined, "fp-checkbox");
        const checkbox = element("input");
        checkbox.type = "checkbox"; checkbox.value = plan.id; checkbox.dataset.planChoice = "true";
        checkbox.checked = checked.has(plan.id);
        label.append(checkbox, element("span", plan.name));
        find("fp-compare-list").append(label);
      }
      find("fp-saved").value = selected;
      find("fp-compare-draft").value = comparison;
      if (find("fp-saved").options.length === 1) find("fp-compare-list").append(element("p", "No saved scenarios of this kind.", "fp-note"));
      find("fp-income-list").replaceChildren();
      for (const stream of state.income_streams) {
        const row = element("tr");
        for (const text of [stream.name, money(stream.amount_minor), stream.cadence,
          `${stream.start_date} / ${stream.end_date || "Ongoing"}`, stream.active ? "Yes" : "No"]) row.append(element("td", text));
        const cell = element("td");
        const button = element("button", "Load", "btn");
        button.type = "button"; button.dataset.incomeId = stream.id;
        const icon = element("span", undefined, "shell-icon");
        icon.setAttribute("aria-hidden", "true");
        icon.style.setProperty("--shell-icon", "url('/static/icons/lucide/pencil.svg')");
        button.prepend(icon); cell.append(button); row.append(cell);
        find("fp-income-list").append(row);
      }
      if (!state.income_streams.length) {
        const row = element("tr"); const cell = element("td", "No income schedules recorded.");
        cell.colSpan = 6; row.append(cell); find("fp-income-list").append(row);
      }
      sync();
    }

    async function refresh() {
      const fresh = await api.request("state");
      if (!state) return;
      state = fresh; lists();
    }

    async function mutation(command, body, collection) {
      let acknowledged = false;
      try {
        const response = await api.request(command, body);
        acknowledged = true;
        await refresh();
        if (!state) return null;
        const identifier = response.record?.id || body.id;
        const record = state[collection].find(item => item.id === identifier);
        if (response.record ? !record || record.version !== response.record.version : record || response.deleted !== true) {
          throw new Error("Write verification unavailable.");
        }
        uncertain = false;
        return record || null;
      } catch (error) {
        if (acknowledged || error.status === 0 || error.status >= 500) {
          uncertain = true;
          throw Object.assign(new Error("Write not confirmed. Reload saved lists before any further changes."), {status: 0});
        }
        throw error;
      }
    }

    function newIncome() {
      incomeLoaded = null;
      find("fp-income-form").reset();
      find("fp-income-fields").querySelector('[name="start_date"]').value = `${state.defaults.start_month}-01`;
      find("fp-income-version").textContent = "New income schedule";
      sync();
    }

    function clearResult() {
      chart?.destroy(); chart = null; rendered = null;
      find("fp-output").hidden = true;
      for (const identifier of ["fp-tables", "fp-summary", "fp-warnings", "fp-assumptions"]) find(identifier).replaceChildren();
    }

    function setMode(next) {
      names[kind()] = find("fp-name").value;
      mode = next;
      find("fp-name").value = names[kind()];
      for (const tab of root.querySelectorAll("[data-tab]")) {
        const selected = tab.dataset.tab === mode;
        tab.setAttribute("aria-selected", String(selected)); tab.tabIndex = selected ? 0 : -1;
      }
      find("fp-cash-fields").hidden = mode === "housing";
      find("fp-cash-fields").disabled = mode === "housing";
      find("fp-cash-fields").setAttribute("aria-labelledby", `fp-tab-${mode === "wealth" ? "wealth" : "cashflow"}`);
      find("fp-housing-fields").hidden = mode !== "housing";
      find("fp-housing-fields").disabled = mode !== "housing";
      for (const node of root.querySelectorAll("[data-cash-only]")) node.hidden = mode !== "cashflow";
      for (const node of root.querySelectorAll("[data-wealth-only]")) node.hidden = mode !== "wealth";
      clearResult(); lists();
      find("fp-saved").value = loaded[kind()]?.id || "";
      sync();
    }

    function draw() {
      chart?.destroy(); chart = null;
      if (!rendered || !state) return;
      find("fp-chart-wrap").hidden = false;
      find("fp-chart-status").hidden = true;
      try {
        if (typeof Chart === "undefined") throw new Error("Chart unavailable.");
        const style = getComputedStyle(root);
        const color = name => style.getPropertyValue(name).trim();
        const datasets = [];
        for (const [index, scenario] of rendered.scenarios.entries()) {
          const stroke = color(`--fp-series-${index + 1}`);
          const series = rendered.kind === "housing" ? [["total_cash_out_minor", "Ownership cash out"], ["cumulative_rent_cost_minor", "Rent cash out"]]
            : rendered.kind === "wealth" ? [["net_worth_minor", "Nominal"], ["real_net_worth_minor", "Inflation adjusted"]]
            : [["ending_cash_minor", "Ending cash"]];
          for (const [seriesIndex, [key, label]] of series.entries()) datasets.push({
            label: `${scenario.name} / ${label}`, data: scenario.result.rows.map(row => row[key] / 100),
            borderColor: stroke, backgroundColor: stroke, borderDash: seriesIndex ? [5, 4] : [],
            pointRadius: 2, borderWidth: 2, tension: 0
          });
        }
        const labels = rendered.scenarios[0].result.rows.map(row => row.month || `Year ${row.year}`);
        chart = new Chart(find("fp-chart"), {type: "line", data: {labels, datasets}, options: {
          responsive: true, maintainAspectRatio: false, animation: false,
          plugins: {legend: {position: "bottom", labels: {color: color("--text-dim"), boxWidth: 12, font: {family: style.fontFamily}}}},
          scales: {x: {ticks: {color: color("--text-dim"), maxTicksLimit: 6, maxRotation: 0}, grid: {display: false}},
            y: {title: {display: true, text: state.currency, color: color("--text-dim")},
              ticks: {color: color("--text-dim")}, grid: {color: color("--border")}}}
        }});
      } catch {
        if (typeof Chart !== "undefined") Chart.getChart(find("fp-chart"))?.destroy();
        chart = null; find("fp-chart-wrap").hidden = true; find("fp-chart-status").hidden = false;
      }
    }

    function render(response) {
      clearResult();
      rendered = response.scenarios ? response : {kind: response.kind, scenarios: [{name: "Current draft", config: response.config, result: response.result}]};
      find("fp-output").hidden = false;
      find("fp-result-state").textContent = "Calculated from submitted assumptions";
      const columns = response.kind === "housing"
        ? [["year", "Year"], ["rent_cost_minor", "Annual rent"], ["owner_cost_minor", "Annual ownership"], ["mortgage_balance_minor", "Mortgage balance"], ["equity_minor", "Equity"], ["total_cash_out_minor", "Total ownership cash out"]]
        : response.kind === "wealth"
          ? [["year", "Year"], ["cash_minor", "Cash"], ["investment_value_minor", "Holdings"], ["net_worth_minor", "Nominal net worth"], ["real_net_worth_minor", "Inflation adjusted"], ["min_cash_minor", "Minimum cash"], ["flag", "Funding"]]
          : [["month", "Month"], ["income_minor", "Take-home"], ["bills_minor", "Bills"], ["variable_minor", "Additional spending"], ["contribution_minor", "Investment transfer"], ["ending_cash_minor", "Ending cash"], ["flag", "Funding"]];
      const assumptions = new Set();
      for (const scenario of rendered.scenarios) {
        const result = scenario.result;
        const heading = `${scenario.name}${result.start_month ? ` / opening ${result.start_month}-01` : " / purchase at year zero"}`;
        const summary = response.kind === "housing"
          ? [["Monthly mortgage principal and interest", result.monthly_payment_minor], ["First-year rent cash out", result.rows[0].rent_cost_minor],
            ["First-year ownership cash out, including upfront", result.rows[0].total_cash_out_minor], ["First-year equity", result.rows[0].equity_minor]]
          : [["Ending cash", result.totals.ending_cash_minor], ["Ending nominal net worth", result.totals.net_worth_minor], ["Minimum cash", result.totals.min_cash_minor]];
        for (const [label, value] of summary) {
          const description = element("dl");
          description.append(element("dt", `${scenario.name} / ${label}`), element("dd", money(value)));
          find("fp-summary").append(description);
        }
        const notices = [...(result.warnings || [])];
        if (result.totals?.first_below_reserve_month) notices.push(`Cash below reserve: ${result.totals.first_below_reserve_month}.`);
        if (result.totals?.first_shortfall_month) notices.push(`Unfunded cash begins: ${result.totals.first_shortfall_month}.`);
        for (const text of notices) find("fp-warnings").append(element("p", `${scenario.name}: ${text}`));
        const table = element("table"); table.append(element("caption", `${heading} / ${state.currency}`));
        const head = element("thead"); const titles = element("tr");
        for (const [, label] of columns) { const cell = element("th", label); cell.scope = "col"; titles.append(cell); }
        head.append(titles); table.append(head);
        const body = element("tbody");
        for (const row of result.rows) {
          const line = element("tr");
          for (const [index, [key]] of columns.entries()) {
            const text = key === "flag" ? row.funding_shortfall ? "Unfunded" : row.below_reserve ? "Below reserve" : "Funded"
              : key.endsWith("_minor") ? money(row[key]) : String(row[key]);
            const cell = element(index ? "td" : "th", text);
            if (!index) cell.scope = "row";
            if (row.funding_shortfall && key === "flag") cell.className = "fp-shortfall";
            line.append(cell);
          }
          body.append(line);
        }
        table.append(body); const wrapper = element("div", undefined, "fp-table-wrap"); wrapper.append(table); find("fp-tables").append(wrapper);
        for (const assumption of result.assumptions) assumptions.add(assumption.replace("investment_return_bps does not change cash costs", "opportunity returns do not change cash costs"));
      }
      find("fp-warnings").hidden = !find("fp-warnings").childElementCount;
      for (const assumption of assumptions) find("fp-assumptions").append(element("li", assumption));
      draw();
    }

    on(root, "input", event => {
      if (event.target.id === "fp-name") names[kind()] = event.target.value;
      if (event.target.closest("#fp-calculate")) dirty();
      sync();
    });
    on(root, "change", event => {
      if (event.target.matches("[data-plan-choice]") && root.querySelectorAll('[data-plan-choice]:checked').length > 3) {
        event.target.checked = false; status("Choose at most three saved scenarios.", true);
      }
      sync();
    });
    on(root.querySelector('[role="tablist"]'), "keydown", event => {
      const tabs = Array.from(root.querySelectorAll("[data-tab]"));
      const index = tabs.indexOf(event.target);
      if (busy || index < 0 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === "Home" ? 0 : event.key === "End" ? 2 : (index + (event.key === "ArrowRight" ? 1 : 2)) % 3;
      setMode(tabs[next].dataset.tab); tabs[next].focus();
    });
    for (const tab of root.querySelectorAll("[data-tab]")) on(tab, "click", () => { if (!busy) setMode(tab.dataset.tab); });
    on(find("fp-calculate"), "submit", event => {
      event.preventDefault();
      perform(async () => {
        const values = config();
        const response = await api.request("calculate", {kind: mode, config: values, ...(mode === "wealth" ? {years: values.years} : {})});
        if (state) { render(response); status("Calculation complete."); }
      });
    });
    on(find("fp-load"), "click", () => {
      const record = state.plans.find(plan => plan.id === find("fp-saved").value && plan.kind === kind());
      if (!record) return;
      loaded[kind()] = record; names[kind()] = record.name; find("fp-name").value = record.name;
      fill(fields(), record.config);
      find("fp-baseline").textContent = `Saved opening amounts / ${record.config.start_month || "purchase at year zero"}; not refreshed from accounts.`;
      clearResult(); sync(); status("Saved scenario loaded.");
    });
    on(find("fp-new"), "click", () => {
      loaded[kind()] = null; names[kind()] = ""; find("fp-name").value = "";
      if (kind() === "cashflow") fill(fields(), state.defaults);
      else for (const input of fields().querySelectorAll("input")) input.value = input.defaultValue;
      find("fp-baseline").textContent = `Recorded balance snapshot / ${state.baseline_as_of}.`;
      clearResult(); sync(); status("New unsaved scenario.");
    });
    on(find("fp-current"), "click", () => perform(async () => {
      await refresh(); if (!state) return;
      for (const name of ["opening_cash_minor", "opening_investments_minor"]) find("fp-cash-fields").querySelector(`[name="${name}"]`).value = decimalString(state.defaults[name]);
      find("fp-baseline").textContent = `Recorded balance snapshot / ${state.baseline_as_of}.`;
      dirty(); status("Current balances applied to this draft only.");
    }));
    on(find("fp-save"), "click", () => perform(async () => {
      if (uncertain) return;
      const previous = loaded[kind()];
      const record = await mutation("save", {kind: kind(), name: find("fp-name").value.trim(), config: config(),
        ...(previous ? {id: previous.id, expected_version: previous.version} : {})}, "plans");
      if (!state || !record) return;
      loaded[kind()] = record; names[kind()] = record.name; find("fp-name").value = record.name;
      find("fp-saved").value = record.id; status("Scenario saved and verified.");
    }));
    on(find("fp-delete"), "click", () => {
      if (!loaded[kind()] || !confirm("Delete the loaded scenario?")) return;
      perform(async () => {
        const record = loaded[kind()];
        await mutation("delete", {id: record.id, expected_version: record.version}, "plans");
        if (!state) return;
        loaded[kind()] = null; names[kind()] = ""; find("fp-name").value = ""; clearResult(); status("Scenario deleted and verified.");
      });
    });
    on(find("fp-reload"), "click", () => perform(async () => {
      await refresh(); if (!state) return;
      uncertain = false; status("Saved lists reloaded. Load a record to use its current version.");
    }));
    for (const [identifier, current] of [["fp-compare", false], ["fp-compare-current", true]]) on(find(identifier), "click", () => perform(async () => {
      const values = current ? config() : null;
      const years = mode === "wealth" ? scaledInteger(find("fp-cash-fields").querySelector('[name="years"]').value, 0) : null;
      const selected = current && state.plans.find(plan => plan.id === find("fp-compare-draft").value && plan.kind === kind());
      const payload = {kind: mode, ...(mode === "wealth" ? {years} : {}),
        ...(current ? {configs: [values, selected.config]} : {plan_ids: Array.from(root.querySelectorAll('[data-plan-choice]:checked'), node => node.value)})};
      const response = await api.request("compare", payload);
      if (!state) return;
      if (current) { response.scenarios[0].name = "Current draft"; response.scenarios[1].name = selected.name; }
      render(response); status("Comparison complete.");
    }));
    on(find("fp-income-list"), "click", event => {
      const button = event.target.closest("[data-income-id]");
      if (!button || busy) return;
      incomeLoaded = state.income_streams.find(stream => stream.id === button.dataset.incomeId);
      if (!incomeLoaded) return;
      fill(find("fp-income-fields"), incomeLoaded);
      find("fp-income-version").textContent = `Loaded income version ${incomeLoaded.version}`;
      sync(); status("Income schedule loaded.");
    });
    on(find("fp-income-new"), "click", () => { newIncome(); status("New unsaved income schedule."); });
    on(find("fp-income-form"), "submit", event => {
      event.preventDefault();
      perform(async () => {
        if (uncertain) return;
        const values = read(find("fp-income-fields")); values.end_date ||= null;
        const record = await mutation("incomeSave", {...values, currency: state.currency,
          ...(incomeLoaded ? {id: incomeLoaded.id, expected_version: incomeLoaded.version} : {})}, "income_streams");
        if (!state || !record) return;
        incomeLoaded = record; find("fp-income-version").textContent = `Loaded income version ${record.version}`;
        dirty(); status("Income schedule saved and verified.");
      });
    });
    on(find("fp-income-delete"), "click", () => {
      if (!incomeLoaded || !confirm("Delete the loaded income schedule?")) return;
      perform(async () => {
        await mutation("incomeDelete", {id: incomeLoaded.id, expected_version: incomeLoaded.version}, "income_streams");
        if (!state) return;
        newIncome(); dirty(); status("Income schedule deleted and verified.");
      });
    });
    on(find("fp-lock"), "submit", async event => {
      event.preventDefault();
      try { await api.request("lock", {}); cleanup(); location.replace("/finance/unlock"); }
      catch { if (state) status("Lock not confirmed. Try again.", true); }
    });
    const observer = new MutationObserver(draw);
    observer.observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme", "data-appearance"]});
    function cleanup() {
      observer.disconnect(); lifetime.abort(); api.destroy(); chart?.destroy(); chart = null;
      state = null; rendered = null; incomeLoaded = null;
      loaded.cashflow = null; loaded.housing = null; names.cashflow = ""; names.housing = "";
      root.replaceChildren();
    }
    on(window, "pagehide", cleanup);
    on(document, "htmx:beforeSwap", event => { if (event.detail.target?.contains(root)) cleanup(); });
    for (const control of root.querySelectorAll("button, fieldset")) control.disabled = false;
    fill(find("fp-cash-fields"), state.defaults);
    find("fp-baseline").textContent = `Recorded balance snapshot / ${state.baseline_as_of}.`;
    newIncome(); setMode("cashflow");
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {scaledInteger, decimalString, formatMinor, transport, mount};
    return;
  }
  const initialize = () => { const root = document.getElementById("finance-planning"); if (root) mount(root); };
  document.addEventListener("htmx:afterSwap", initialize);
  window.addEventListener("pageshow", event => { if (event.persisted && document.getElementById("finance-planning")) location.replace("/finance/planning"); });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize, {once: true});
  else initialize();
})();