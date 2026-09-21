(() => {
  "use strict";
  const source = document.getElementById("media-insights-data");
  if (!source || typeof Chart === "undefined") return;
  let data;
  try { data = JSON.parse(source.textContent || "{}"); }
  catch { return; }

  const colors = ["#5f8ff1", "#36b989", "#e2a93b", "#d96b72", "#9a78d2", "#5db4c8", "#8994a5"];
  const common = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { display: false } },
    scales: {
      x: { ticks: { color: "#9eabc0" }, grid: { display: false } },
      y: { beginAtZero: true, ticks: { color: "#9eabc0", precision: 0 }, grid: { color: "rgba(158,171,192,.12)" } },
    },
  };
  document.querySelectorAll("[data-insights-chart]").forEach((canvas) => {
    const key = canvas.dataset.insightsChart;
    const series = data[key];
    if (!series) return;
    new Chart(canvas, {
      type: key === "status" ? "doughnut" : "bar",
      data: {
        labels: series.labels,
        datasets: [{
          label: series.label || "Count",
          data: series.values,
          backgroundColor: series.labels.map((_, index) => colors[index % colors.length]),
          borderWidth: 0,
        }],
      },
      options: key === "status"
        ? { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: "bottom", labels: { color: "#c9d3e3", boxWidth: 10 } } } }
        : common,
    });
  });
})();