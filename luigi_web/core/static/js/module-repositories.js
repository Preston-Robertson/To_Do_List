(function () {
  "use strict";

  document.addEventListener("submit", async (event) => {
    const form = event.target.closest("form[data-repository-form]");
    if (!form) return;
    event.preventDefault();
    if (form.getAttribute("aria-busy") === "true" || !form.reportValidity()) return;
    const panel = form.closest("#module-repositories-panel");
    const error = panel.querySelector("[data-repository-error]");
    const body = new URLSearchParams(new FormData(form));
    const buttons = Array.from(form.querySelectorAll("button[type='submit']"));
    form.setAttribute("aria-busy", "true");
    buttons.forEach((button) => { button.disabled = true; });
    error.hidden = true;
    try {
      const response = await fetch(form.action, {
        method: "POST",
        credentials: "same-origin",
        redirect: "error",
        headers: { "X-CSRF-Token": body.get("csrf_token") || "", "Accept": "text/html" },
        body,
      });
      if (response.status === 401) throw new Error("session");
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      const replacement = page.querySelector("#module-repositories-panel");
      if (!replacement) throw new Error("response");
      panel.replaceWith(replacement);
      const message = replacement.querySelector("[data-repository-error]:not([hidden]), [data-repository-notice]");
      if (message) message.focus();
    } catch (failure) {
      error.textContent = failure.message === "session"
        ? "Session expired. Sign in and refresh before retrying."
        : "The request result could not be verified. Refresh before retrying.";
      error.hidden = false;
      error.focus();
    } finally {
      form.removeAttribute("aria-busy");
      buttons.forEach((button) => { button.disabled = false; });
    }
  });
}());