(() => {
  "use strict";
  const charts = new Map();
  let disposed = false;

  function formatMinor(raw, currency) {
    const amount = BigInt(raw);
    const negative = amount < 0n;
    const magnitude = negative ? -amount : amount;
    const whole = magnitude / 100n;
    const fraction = String(magnitude % 100n).padStart(2, "0");
    const signedWhole = negative ? (whole === 0n ? -0 : -whole) : whole;
    const options = { minimumFractionDigits: 2, maximumFractionDigits: 2 };
    if (currency) Object.assign(options, { style: "currency", currency });
    return new Intl.NumberFormat(undefined, options).formatToParts(signedWhole)
      .map(part => part.type === "fraction" ? fraction : part.value).join("");
  }

  function chartNumber(raw) {
    const amount = BigInt(raw);
    if (amount > BigInt(Number.MAX_SAFE_INTEGER) || amount < BigInt(Number.MIN_SAFE_INTEGER)) {
      throw new RangeError("Chart range unavailable");
    }
    return Number(amount) / 100;
  }

  function clearCharts() {
    for (const chart of charts.values()) chart.destroy();
    charts.clear();
  }

  function cleanup() {
    disposed = true;
    observer.disconnect();
    const root = document.getElementById("finance-overview");
    if (root) root.hidden = true;
    clearCharts();
    if (root) root.replaceChildren();
  }

  function draw(root, identifier, configuration) {
    const canvas = root.querySelector(`#${identifier}`);
    if (!canvas) return;
    try {
      if (typeof Chart === "undefined") throw new Error("Chart unavailable");
      charts.set(canvas, new Chart(canvas, configuration()));
    } catch {
      const partial = typeof Chart !== "undefined" && Chart.getChart(canvas);
      if (partial) partial.destroy();
      canvas.parentElement.hidden = true;
      const status = canvas.closest("section").querySelector(".fo-chart-status");
      if (status) status.hidden = false;
    }
  }

  function initialize() {
    if (disposed) { cleanup(); return; }
    const root = document.getElementById("finance-overview");
    if (!root) { clearCharts(); return; }
    for (const element of root.querySelectorAll("[data-minor]")) {
      try { element.textContent = formatMinor(element.dataset.minor, element.dataset.currency); }
      catch { continue; }
    }
    const seedElement = root.querySelector("#fo-chart-seed");
    if (!seedElement || charts.has(root.querySelector("#fo-trend-chart"))) return;
    clearCharts();
    let seed;
    try { seed = JSON.parse(seedElement.textContent); } catch { return; }
    const style = getComputedStyle(root);
    const color = name => style.getPropertyValue(name).trim();
    const palette = [color("--fo-inflow"), color("--fo-history"), color("--fo-outflow"), "#bb7996", "#91a34a", "#849a9e"];
    const options = () => ({
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { position: "bottom", labels: { color: color("--text-dim"), boxWidth: 10,
        font: { family: style.fontFamily, size: 11 } } } },
      scales: {
        x: { ticks: { color: color("--text-dim"), maxRotation: 0, maxTicksLimit: 6 }, grid: { display: false } },
        y: { ticks: { color: color("--text-dim"), maxTicksLimit: 5 }, grid: { color: color("--border") } }
      }
    });
    draw(root, "fo-trend-chart", () => ({
      type: "bar", data: { labels: seed.trend.map(row => row.month), datasets: [
        { label: `Inflows (${seed.currency})`, data: seed.trend.map(row => chartNumber(row.inflow_minor)), backgroundColor: palette[0] },
        { label: `Gross outflows (${seed.currency})`, data: seed.trend.map(row => chartNumber(row.outflow_minor)), backgroundColor: palette[2] }
      ] }, options: options()
    }));
    draw(root, "fo-history-chart", () => ({
      type: "line", data: { labels: seed.snapshots.map(row => row.date), datasets: [
        { label: "Recorded total (currency unrecorded)", data: seed.snapshots.map(row => chartNumber(row.value_minor)),
          borderColor: palette[1], backgroundColor: palette[1], showLine: false, pointRadius: 4 }
      ] }, options: options()
    }));
    draw(root, "fo-allocation-chart", () => ({
      type: "doughnut", data: { labels: seed.allocation.map(row => row.label), datasets: [
        { data: seed.allocation.map(row => chartNumber(row.value_minor)), backgroundColor: palette, borderWidth: 0 }
      ] }, options: { responsive: true, maintainAspectRatio: false, animation: false, cutout: "68%",
        plugins: { legend: { display: false } } }
    }));
  }

  document.addEventListener("htmx:beforeSwap", event => {
    if (event.detail.target?.id === "finance-overview") clearCharts();
  });
  document.addEventListener("htmx:afterSwap", initialize);
  document.addEventListener("htmx:afterRequest", event => {
    if (event.detail.elt?.id === "fo-lock" && event.detail.successful) {
      cleanup();
      location.replace("/finance/unlock");
    }
  });
  window.addEventListener("pagehide", cleanup);
  window.addEventListener("pageshow", event => {
    if (event.persisted) { cleanup(); location.reload(); }
  });
  const observer = new MutationObserver(() => { if (!disposed) { clearCharts(); initialize(); } });
  observer.observe(document.documentElement, {
    attributes: true, attributeFilter: ["data-theme", "data-appearance"]
  });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize, { once: true });
  else initialize();
})();