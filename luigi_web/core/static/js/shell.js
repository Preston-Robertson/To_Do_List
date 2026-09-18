(function () {
  "use strict";

  const mobileMedia = window.matchMedia("(max-width: 900px)");
  const tooltip = document.getElementById("shell-tooltip");
  let tooltipAnchor = null;

  function hideTooltip() {
    if (!tooltip) return;
    tooltip.hidden = true;
    if (tooltipAnchor) {
      const descriptions = (tooltipAnchor.getAttribute("aria-describedby") || "")
        .split(/\s+/).filter((value) => value && value !== tooltip.id);
      if (descriptions.length) tooltipAnchor.setAttribute("aria-describedby", descriptions.join(" "));
      else tooltipAnchor.removeAttribute("aria-describedby");
    }
    tooltipAnchor = null;
  }

  function showTooltip(target) {
    if (!tooltip || !(target instanceof Element)) return;
    const anchor = target.closest("[data-shell-tooltip]");
    if (!anchor || anchor === tooltipAnchor || anchor.closest("[inert]")) return;
    hideTooltip();
    if (anchor.hasAttribute("data-tooltip-collapsed")
        && (!document.body.classList.contains("sidebar-collapsed") || mobileMedia.matches)) return;
    const label = anchor.dataset.shellTooltip || anchor.getAttribute("aria-label");
    if (!label) return;
    tooltipAnchor = anchor;
    tooltip.textContent = label;
    tooltip.hidden = false;
    const bounds = anchor.getBoundingClientRect();
    const besideSidebar = anchor.closest(".sidebar") && !mobileMedia.matches;
    const left = besideSidebar ? bounds.right + 10 : bounds.left + (bounds.width - tooltip.offsetWidth) / 2;
    const top = besideSidebar ? bounds.top + (bounds.height - tooltip.offsetHeight) / 2 : bounds.bottom + 8;
    tooltip.style.left = `${Math.max(8, Math.min(left, window.innerWidth - tooltip.offsetWidth - 8))}px`;
    tooltip.style.top = `${Math.max(8, Math.min(top, window.innerHeight - tooltip.offsetHeight - 8))}px`;
    const descriptions = anchor.getAttribute("aria-describedby") || "";
    anchor.setAttribute("aria-describedby", `${descriptions} ${tooltip.id}`.trim());
  }

  document.addEventListener("pointerover", (event) => {
    if (event.pointerType !== "touch") showTooltip(event.target);
  });
  document.addEventListener("pointerout", (event) => {
    if (tooltipAnchor && !tooltipAnchor.contains(event.relatedTarget)) hideTooltip();
  });
  document.addEventListener("focusin", (event) => showTooltip(event.target));
  document.addEventListener("focusout", hideTooltip);
  document.addEventListener("click", hideTooltip);
  document.addEventListener("scroll", hideTooltip, true);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hideTooltip();
  });
  window.addEventListener("resize", hideTooltip);

  function initModules(root = document) {
    const form = root.querySelector("[data-module-form]");
    if (!form || form.dataset.moduleReady === "true") return;
    form.dataset.moduleReady = "true";

    const page = form.closest("[data-module-page]");
    const managed = form.dataset.environmentManaged === "true";
    const inputs = Array.from(form.querySelectorAll('input[name="enabled"]'));
    const byId = new Map(inputs.map((input) => [input.value, input]));
    const original = new Map(inputs.map((input) => [input.value, input.checked]));
    const requirements = new Map(inputs.map((input) => [
      input.value, (input.dataset.requires || "").split(",").map((value) => value.trim()).filter(Boolean),
    ]));
    const rows = Array.from(form.querySelectorAll("[data-module-row]"));
    const filter = form.querySelector("[data-module-filter]");
    const clearFilter = form.querySelector("[data-module-clear]");
    const filterCount = form.querySelector("[data-module-filter-count]");
    const empty = form.querySelector("[data-module-empty]");
    const save = form.querySelector("[data-module-save]");
    const saveLabel = form.querySelector("[data-module-save-label]");
    const reset = form.querySelector("[data-module-reset]");
    const state = form.querySelector("[data-module-change-state]");
    const initialState = state.textContent.trim();
    const errorNotice = page.querySelector("[data-module-error]");
    let busy = false;

    function updateState() {
      const changed = inputs.some((input) => input.checked !== original.get(input.value));
      const selected = inputs.filter((input) => input.checked).length;
      form.dataset.dirty = String(changed);
      save.disabled = managed || busy || !changed;
      reset.disabled = managed || busy || !changed;
      if (!busy) state.textContent = changed ? `${selected} selected. Unsaved changes.` : initialState;
      inputs.forEach((input) => {
        const row = input.closest("[data-module-row]");
        const pending = row.querySelector("[data-module-pending]");
        const unsaved = input.checked !== original.get(input.value);
        const restart = input.checked !== (row.dataset.moduleActive === "true");
        pending.hidden = !unsaved && !restart;
        pending.textContent = unsaved ? "Unsaved" : "Restart pending";
      });
    }

    function filterRows() {
      const query = filter.value.trim().toLocaleLowerCase();
      let visible = 0;
      rows.forEach((row) => {
        row.hidden = !(row.dataset.moduleSearch || row.dataset.moduleName || "").toLocaleLowerCase().includes(query);
        if (!row.hidden) visible += 1;
      });
      clearFilter.hidden = !query;
      empty.hidden = visible > 0 || rows.length === 0;
      filterCount.textContent = query ? `${visible} of ${rows.length}` : `${rows.length} installed`;
    }

    function enableRequirements(input, visited = new Set()) {
      if (visited.has(input.value) || input.disabled) return;
      visited.add(input.value);
      input.checked = true;
      (requirements.get(input.value) || []).forEach((requiredId) => {
        const required = byId.get(requiredId);
        if (required) enableRequirements(required, visited);
      });
    }

    function disableDependents(input, visited = new Set()) {
      if (visited.has(input.value) || input.disabled) return;
      visited.add(input.value);
      input.checked = false;
      inputs.forEach((dependent) => {
        if (dependent.checked && requirements.get(dependent.value).includes(input.value)) {
          disableDependents(dependent, visited);
        }
      });
    }

    filter.addEventListener("input", filterRows);
    filter.addEventListener("search", filterRows);
    filter.addEventListener("keydown", (event) => {
      if (event.key === "Enter") event.preventDefault();
    });
    clearFilter.addEventListener("click", () => {
      filter.value = "";
      filterRows();
      filter.focus();
    });
    form.addEventListener("change", (event) => {
      const input = event.target;
      if (managed || busy || !inputs.includes(input)) return;
      if (input.checked) enableRequirements(input);
      else disableDependents(input);
      errorNotice.hidden = true;
      updateState();
    });
    form.addEventListener("reset", (event) => {
      if (managed || busy) {
        event.preventDefault();
        return;
      }
      requestAnimationFrame(() => {
        errorNotice.hidden = true;
        filterRows();
        updateState();
      });
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (managed || busy) return;
      const body = new FormData(form);
      const originalDisabled = inputs.map((input) => input.disabled);
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 20000);
      busy = true;
      errorNotice.hidden = true;
      form.setAttribute("aria-busy", "true");
      inputs.forEach((input) => { input.disabled = true; });
      updateState();
      state.textContent = "Saving selection...";
      saveLabel.textContent = "Saving...";

      try {
        const response = await window.fetch(form.action, {
          method: "POST",
          body,
          credentials: "same-origin",
          headers: { Accept: "text/html" },
          signal: controller.signal,
        });
        const content = await response.text();
        const contentType = response.headers.get("content-type") || "";
        const result = contentType.includes("text/html") ? new DOMParser().parseFromString(content, "text/html") : null;
        const incoming = result?.querySelector("[data-module-page]");
        let serverError = incoming?.querySelector("[data-module-error]")?.textContent.trim() || "";
        if (contentType.includes("application/json")) {
          try {
            const detail = JSON.parse(content).detail;
            if (typeof detail === "string") serverError = detail;
          } catch {}
        }
        if (!response.ok || serverError) {
          throw new Error(serverError || `Could not confirm the save (HTTP ${response.status}). Reload to check the saved selection.`);
        }
        const responseUrl = new URL(response.url || form.action, window.location.href);
        if (response.redirected || responseUrl.origin !== window.location.origin || responseUrl.pathname !== "/modules"
            || incoming?.dataset.moduleSaved !== "true") {
          throw new Error("The server did not confirm the save. Your session may have expired; reload and try again.");
        }
        const replacement = document.importNode(incoming, true);
        page.replaceWith(replacement);
        initModules(replacement);
        replacement.querySelector("[data-module-notice]")?.focus({ preventScroll: true });
      } catch (error) {
        const message = error.name === "AbortError"
          ? "Save confirmation timed out. Reload to check the saved selection before trying again."
          : error instanceof TypeError
            ? "Unable to confirm the save. Check your connection, then reload to verify the selection."
            : error.message || "Unable to confirm the save. Reload to verify the selection.";
        errorNotice.textContent = message;
        errorNotice.hidden = false;
        errorNotice.focus();
      } finally {
        window.clearTimeout(timeout);
        busy = false;
        if (form.isConnected) {
          inputs.forEach((input, index) => { input.disabled = originalDisabled[index]; });
          form.removeAttribute("aria-busy");
          saveLabel.textContent = "Save selection";
          updateState();
        }
      }
    });

    filterRows();
    updateState();
  }

  window.addEventListener("beforeunload", (event) => {
    if (document.querySelector('[data-module-form][data-dirty="true"]')) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  initModules();
})();