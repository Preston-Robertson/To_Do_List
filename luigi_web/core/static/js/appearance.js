(function () {
  "use strict";

  const storageKey = "luigi.appearance";
  const modes = new Set(["light", "dark", "system"]);
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  let mode = "dark";

  function syncControls() {
    document.querySelectorAll("[data-appearance-mode]").forEach((input) => {
      input.checked = input.value === mode;
    });
  }

  function applyMode(value, persist = false) {
    mode = modes.has(value) ? value : "dark";
    const theme = mode === "system" ? (media.matches ? "dark" : "light") : mode;
    document.documentElement.dataset.appearance = mode;
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    document.querySelector('meta[name="theme-color"]')?.setAttribute(
      "content", theme === "dark" ? "#17191b" : "#f6f8f7",
    );
    if (persist) {
      try { localStorage.setItem(storageKey, mode); } catch {}
    }
    syncControls();
  }

  try { mode = localStorage.getItem(storageKey) || "dark"; } catch {}
  applyMode(mode);

  document.addEventListener("DOMContentLoaded", syncControls);
  document.addEventListener("change", (event) => {
    const input = event.target.closest("[data-appearance-mode]");
    if (input?.checked) applyMode(input.value, true);
  });
  media.addEventListener("change", () => {
    if (mode === "system") applyMode(mode);
  });
  window.addEventListener("storage", (event) => {
    if (event.key === storageKey || event.key === null) applyMode(event.newValue);
  });
})();