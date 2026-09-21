(function () {
  "use strict";

  const drawer = document.getElementById("assistant-drawer");
  const content = drawer?.querySelector("[data-assistant-content]");
  if (!drawer || !content) return;
  let opener = null;
  let loading = false;

  async function loadPanel() {
    if (loading || content.querySelector("#chat-panel")) return;
    loading = true;
    content.setAttribute("aria-busy", "true");
    const status = document.createElement("p");
    status.className = "assistant-load-status";
    status.setAttribute("role", "status");
    status.textContent = "Loading Assistant...";
    content.replaceChildren(status);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(content.dataset.panelUrl, {
        credentials: "same-origin", headers: {Accept: "text/html"}, signal: controller.signal,
      });
      if (!response.ok || response.redirected) throw new Error("Panel unavailable");
      const markup = await response.text();
      const parsed = new DOMParser().parseFromString(markup, "text/html");
      const panel = parsed.querySelector("#chat-panel");
      if (!panel) throw new Error("Invalid panel response");
      content.replaceChildren(document.importNode(panel, true));
      window.htmx?.process(content);
      document.dispatchEvent(new Event("luigi:assistant-ready"));
      const log = content.querySelector("#chat-log");
      log.scrollTop = log.scrollHeight;
      if (drawer.open) content.querySelector("textarea:not(:disabled)")?.focus();
    } catch {
      status.setAttribute("role", "alert");
      status.textContent = "Assistant is unavailable. Your page has not changed.";
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "btn btn-ghost";
      retry.textContent = "Retry";
      retry.addEventListener("click", loadPanel);
      content.replaceChildren(status, retry);
    } finally {
      clearTimeout(timer);
      loading = false;
      content.removeAttribute("aria-busy");
    }
  }

  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-assistant-open]");
    if (!trigger) return;
    opener = trigger;
    drawer.showModal();
    document.body.classList.add("assistant-open");
    document.querySelectorAll("[data-assistant-open]").forEach(button => button.setAttribute("aria-expanded", "true"));
    loadPanel();
    content.querySelector("textarea:not(:disabled)")?.focus();
  });

  function finishClose() {
    if (drawer.open) return;
    document.body.classList.remove("assistant-open");
    document.querySelectorAll("[data-assistant-open]").forEach(button => button.setAttribute("aria-expanded", "false"));
    window.speechSynthesis?.cancel();
    if (opener?.isConnected && !document.querySelector("dialog[open]")) opener.focus();
  }
  function closePanel() {
    drawer.close();
    finishClose();
  }

  drawer.addEventListener("click", (event) => {
    if (event.target.closest("[data-assistant-close]")) closePanel();
    if (event.target === drawer) {
      const bounds = drawer.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) closePanel();
    }
  });
  drawer.addEventListener("close", finishClose);
  drawer.addEventListener("cancel", (event) => {
    event.preventDefault();
    closePanel();
  });
  drawer.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closePanel();
    } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      event.stopPropagation();
    }
  });
  document.body.addEventListener("htmx:afterRequest", (event) => {
    const form = event.detail.elt;
    if (!form?.matches("[data-assistant-composer]")) return;
    const failedReply = event.detail.xhr?.responseText.includes("chat-msg-error");
    if (event.detail.successful && !failedReply) form.reset();
    if (drawer.open) form.querySelector("textarea")?.focus();
    const log = content.querySelector("#chat-log");
    if (log) log.scrollTop = log.scrollHeight;
  });
})();