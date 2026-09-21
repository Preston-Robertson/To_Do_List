(() => {
  "use strict";
  const root = document.querySelector("[data-review-root]");
  if (!root) return;
  const error = root.querySelector("[data-review-error]");
  let submitting = false;
  root.addEventListener("submit", async (event) => {
    const form = event.target.closest("[data-review-form]");
    if (!form) return;
    event.preventDefault();
    if (submitting || !form.reportValidity()) return;
    const cookie = document.cookie.split(";").map((part) => part.trim())
      .find((part) => part.startsWith("luigi_csrf="));
    if (!cookie) {
      error.textContent = "Request verification expired. Reload and review again; nothing was queued.";
      error.hidden = false;
      return;
    }
    const payload = new URLSearchParams(new FormData(form));
    const buttons = [...root.querySelectorAll('button[type="submit"]')];
    submitting = true;
    buttons.forEach((button) => { button.disabled = true; });
    error.hidden = true;
    try {
      const response = await fetch(form.action, {
        method: "POST", credentials: "same-origin", cache: "no-store",
        headers: { "Accept": "application/json", "X-CSRF-Token": decodeURIComponent(cookie.slice("luigi_csrf=".length)) },
        body: payload,
      });
      if (response.ok && !response.redirected) {
        const result = await response.json();
        const destination = new URL(result.location, location.origin);
        if (destination.origin === location.origin && destination.pathname === location.pathname
            && !destination.search && !destination.hash) {
          location.assign(destination.pathname);
          return;
        }
      }
      error.textContent = response.status === 409 || response.status === 412
        ? "This review changed. Reload and review it again; nothing was queued."
        : "The action was not confirmed. Reload and check the current review before trying again.";
    } catch {
      error.textContent = "The result could not be verified. Reload and check the current review before trying again.";
    }
    error.hidden = false;
    error.scrollIntoView({ block: "nearest" });
    submitting = false;
  });
  root.addEventListener("error", (event) => {
    if (event.target instanceof HTMLImageElement) {
      const figure = event.target.closest("figure");
      figure.querySelector("a").hidden = true;
      figure.querySelector("figcaption").textContent = "Verified screenshot unavailable";
    }
  }, true);
})();
