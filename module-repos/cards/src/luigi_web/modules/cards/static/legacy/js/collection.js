(() => {
  "use strict";
  const root = () => document.querySelector("[data-collection-page]");
  if (!root()) return;
  const base = () => `/cards/${root().dataset.game}/collection`;
  const csrf = () => decodeURIComponent(document.cookie.split("; ").find(value => value.startsWith("luigi_csrf="))?.split("=").slice(1).join("=") || "");

  async function request(url, body, signal) {
    const response = await fetch(url, {method: body ? "POST" : "GET", body, signal,
      credentials: "same-origin", cache: "no-store",
      headers: {"X-CSRF-Token": csrf(), "HX-Request": "true"}});
    if (!response.ok) {
      let message = "Request failed. No success was confirmed.";
      try { const result = await response.json(); if (typeof result.detail === "string") message = result.detail; } catch {}
      throw new Error(message);
    }
    return response;
  }

  function showError(error, context) {
    const target = context?.querySelector("[data-collection-form-error]") || root().querySelector("[data-collection-error]");
    target.textContent = error.message;
    target.hidden = false;
  }

  async function refresh(page = 1) {
    const body = new FormData(root().querySelector("[data-collection-filters]"));
    body.set("page", String(page));
    const response = await request(`${base()}/query`, body);
    const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
    const replacement = parsed.querySelector("[data-collection-page]");
    if (!replacement) throw new Error("Collection refresh failed.");
    root().replaceWith(replacement);
    window.htmx?.process(replacement);
  }

  function invalidateEstimate(form) {
    const output = form.querySelector("[data-collection-estimate-output]");
    if (output) output.textContent = "No estimate selected";
    const confirmation = form.elements.namedItem("estimate_confirmed");
    if (confirmation) confirmation.checked = false;
    form.dataset.estimateKey = "";
  }

  function estimateBody(form) {
    const data = new FormData(form);
    const body = new FormData();
    for (const key of ["card_id", "acquired_date", "foil"]) body.set(key, data.get(key) || (key === "foil" ? "0" : ""));
    body.set("currency", data.get("acquired_currency"));
    return body;
  }

  document.addEventListener("change", event => {
    const form = event.target.closest("[data-collection-purchase]");
    if (!form || event.target.name === "estimate_confirmed") return;
    if (["card_id", "foil", "acquired_date", "acquired_currency", "price_source", "acquired_price"].includes(event.target.name)) invalidateEstimate(form);
  });

  document.addEventListener("click", async event => {
    const button = event.target.closest("button");
    if (!button || !button.closest("[data-collection-page]")) return;
    try {
      if (button.hasAttribute("data-collection-close")) button.closest("dialog").close();
      if (button.dataset.collectionOpen) root().querySelector(`#${button.dataset.collectionOpen}`).showModal();
      if (button.dataset.collectionPageNumber) await refresh(Number(button.dataset.collectionPageNumber));
      if (button.dataset.collectionSelect) {
        const dialog = button.closest("dialog");
        const form = dialog.querySelector("[data-collection-purchase]");
        form.elements.namedItem("card_id").value = button.dataset.collectionSelect;
        dialog.querySelector("[data-collection-selected]").textContent = button.dataset.cardName;
        dialog.querySelector("[data-collection-search-results]").replaceChildren();
        invalidateEstimate(form);
      }
      if (button.dataset.collectionDetail || button.hasAttribute("data-collection-history")) {
        const response = await request(button.dataset.collectionDetail ? `${base()}/${button.dataset.collectionDetail}/lots` : `${base()}/removed/history`);
        const dialog = root().querySelector("#collection-detail");
        dialog.querySelector("[data-collection-detail-body]").innerHTML = await response.text();
        dialog.showModal();
      }
      if (button.hasAttribute("data-collection-estimate")) {
        const form = button.closest("form");
        const body = estimateBody(form);
        const key = new URLSearchParams(body).toString();
        const response = await request(`${base()}/estimate`, body);
        const value = await response.json();
        if (new URLSearchParams(estimateBody(form)).toString() !== key) return;
        const output = form.querySelector("[data-collection-estimate-output]");
        output.textContent = value.available ? `${value.currency} ${(value.unit_price_minor / 100).toFixed(2)} per card - ${value.snapshot_date} - ${value.snapshot_source} - estimate, not actual paid` : "Unknown cost - no exact-day market snapshot";
        form.dataset.estimateKey = value.available ? key : "";
        form.elements.namedItem("estimate_confirmed").checked = false;
        if (!value.available && form.elements.namedItem("price_source").value === "market_estimate") {
          form.elements.namedItem("price_source").value = "unknown";
        }
      }
      if (button.hasAttribute("data-collection-export")) {
        const response = await request(`${base()}/export`, new FormData(root().querySelector("[data-collection-filters]")));
        const url = URL.createObjectURL(await response.blob());
        const link = document.createElement("a"); link.href = url; link.download = "collection.csv"; link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
    } catch (error) { showError(error, button.closest("form")); }
  });

  document.addEventListener("submit", async event => {
    const form = event.target;
    if (!form.matches("[data-collection-filters], [data-collection-submit], [data-collection-preview]")) return;
    event.preventDefault();
    if (form.dataset.busy) return;
    form.dataset.busy = "true";
    const submit = event.submitter;
    if (submit) submit.disabled = true;
    try {
      if (form.hasAttribute("data-collection-filters")) { await refresh(); return; }
      if (form.hasAttribute("data-collection-remove") && !confirm("Remove this holding? Local purchase history will be retained.")) return;
      const body = new FormData(form);
      if (form.hasAttribute("data-collection-purchase")) {
        if (!body.get("card_id")) throw new Error("Select a printing first.");
        if (body.get("price_source") === "market_estimate") {
          if (form.dataset.estimateKey !== new URLSearchParams(estimateBody(form)).toString() || !body.get("estimate_confirmed")) throw new Error("Check and confirm the exact-day estimate first.");
          if (body.get("acquired_price")) throw new Error("Clear the actual price before choosing a market estimate.");
        }
      }
      if (form.hasAttribute("data-collection-preview")) {
        const upload = body.get("upload");
        if (upload.size > 500000) throw new Error("CSV exceeds 500000 bytes.");
        const response = await request(form.action, body);
        root().querySelector("[data-collection-preview-body]").innerHTML = await response.text();
      } else {
        await request(form.action, body);
        await refresh();
      }
    } catch (error) { showError(error, form); }
    finally { delete form.dataset.busy; if (submit) submit.disabled = false; }
  });
})();