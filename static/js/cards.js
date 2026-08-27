(function () {
  "use strict";

  const workspace = document.querySelector("[data-cards-workspace]");
  if (!workspace) return;

  function dialogById(id) {
    const dialog = document.getElementById(id);
    return dialog instanceof HTMLDialogElement ? dialog : null;
  }

  document.addEventListener("click", (event) => {
    const opener = event.target.closest("[data-cards-dialog-open]");
    if (opener) {
      const dialog = dialogById(opener.dataset.cardsDialogOpen);
      if (dialog && !dialog.open) dialog.showModal();
      return;
    }
    const closer = event.target.closest("[data-cards-dialog-close]");
    if (closer) closer.closest("dialog")?.close();
  });

  document.querySelectorAll("dialog.cards-dialog").forEach((dialog) => {
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
  });

  const quickCollect = dialogById("quick-collect-dialog");
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-cards-quick-collect]");
    if (!button || !quickCollect) return;
    quickCollect.querySelector("[data-cards-selected-id]").value = button.dataset.cardId || "";
    quickCollect.querySelector("[data-cards-selected-name]").textContent = button.dataset.cardName || "Selected card";
    quickCollect.showModal();
  });

  const preview = document.createElement("div");
  preview.className = "card-image-preview";
  preview.innerHTML = '<img alt="">';
  document.body.appendChild(preview);
  const previewImage = preview.querySelector("img");

  function positionPreview(event) {
    const gap = 16;
    const width = 240;
    const height = 335;
    const left = event.clientX + gap + width > window.innerWidth
      ? event.clientX - gap - width
      : event.clientX + gap;
    const top = Math.min(Math.max(8, event.clientY - 80), window.innerHeight - height - 8);
    preview.style.left = `${left}px`;
    preview.style.top = `${top}px`;
  }

  document.addEventListener("pointerover", (event) => {
    const target = event.target.closest("[data-card-preview]");
    if (!target || !target.dataset.cardPreview) return;
    previewImage.src = target.dataset.cardPreview;
    preview.classList.add("visible");
    positionPreview(event);
  });
  document.addEventListener("pointermove", (event) => {
    if (preview.classList.contains("visible")) positionPreview(event);
  });
  document.addEventListener("pointerout", (event) => {
    if (!event.target.closest("[data-card-preview]")) return;
    preview.classList.remove("visible");
    previewImage.removeAttribute("src");
  });

  const tabs = document.querySelector("[data-cards-tabs]");
  if (tabs) {
    tabs.addEventListener("click", (event) => {
      const button = event.target.closest("[data-cards-tab]");
      if (!button) return;
      tabs.querySelectorAll("[data-cards-tab]").forEach((item) => {
        item.classList.toggle("active", item === button);
        item.setAttribute("aria-selected", String(item === button));
      });
      document.querySelectorAll(".cards-tab-panel").forEach((panel) => {
        panel.classList.toggle("active", panel.id === button.dataset.cardsTab);
      });
      if (button.dataset.cardsTab === "notes-panel") mountNotes();
    });
  }

  function appendList(details, title, values, formatter) {
    if (!values.length) return;
    const section = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = title;
    const list = document.createElement("ul");
    values.forEach((value) => {
      const item = document.createElement("li");
      item.textContent = formatter(value);
      list.appendChild(item);
    });
    section.append(summary, list);
    details.appendChild(section);
  }

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-import-preview-button]");
    if (!button) return;
    const form = button.closest("[data-card-import-form]");
    const output = form?.querySelector("[data-import-preview]");
    const textarea = form?.querySelector("textarea[name='text']");
    if (!form || !output || !textarea) return;
    output.textContent = "Checking card names...";
    try {
      const response = await fetch(form.dataset.previewUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: textarea.value }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Import preview failed");
      output.replaceChildren();
      const summary = document.createElement("p");
      summary.textContent = `${data.counts.matched} matched · ${data.counts.unmatched} unmatched · ${data.counts.unparsed} unparsed`;
      output.appendChild(summary);
      appendList(output, "Unmatched lines", data.unmatched, (row) => `${row.qty} x ${row.name}${row.set ? ` (${row.set})` : ""}`);
      appendList(output, "Unparsed lines", data.unparsed, (line) => line);
    } catch (error) {
      output.textContent = error.message || "Import preview failed";
      window.showError?.(output.textContent);
    }
  });

  const notes = document.querySelector("[data-card-notes]");
  let notesMounted = false;
  let notesOrigin = "";
  let notesXml = "";

  function notesStatus(message) {
    const status = notes?.querySelector("[data-notes-status]");
    if (status) status.textContent = message;
  }

  function postToNotes(message) {
    const frame = notes?.querySelector("iframe");
    if (frame?.contentWindow && notesOrigin) {
      frame.contentWindow.postMessage(JSON.stringify(message), notesOrigin);
    }
  }

  async function loadNotes() {
    const response = await fetch(notes.dataset.loadUrl, { headers: { "Accept": "application/json" } });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not load diagram");
    notesXml = data.xml || "";
    postToNotes({ action: "load", xml: notesXml, autosave: 1 });
  }

  async function saveNotes(xml) {
    if (xml == null) {
      postToNotes({ action: "export", format: "xml" });
      return;
    }
    notesStatus("Saving...");
    const response = await fetch(notes.dataset.loadUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ xml }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not save diagram");
    notesXml = xml;
    notesStatus(`Saved · ${data.bytes} bytes`);
  }

  function mountNotes() {
    if (!notes || notesMounted) return;
    try {
      const drawio = new URL(notes.dataset.drawioUrl, window.location.href);
      notesOrigin = drawio.origin;
      notes.querySelector("iframe").src = drawio.href;
      notesMounted = true;
      notesStatus("Opening diagram...");
    } catch {
      notesStatus("Diagram URL is invalid");
    }
  }

  notes?.querySelector("[data-notes-save]")?.addEventListener("click", () => {
    saveNotes(null).catch((error) => {
      notesStatus("Save failed");
      window.showError?.(error.message);
    });
  });

  window.addEventListener("message", (event) => {
    const frame = notes?.querySelector("iframe");
    if (!notesMounted || event.source !== frame?.contentWindow || event.origin !== notesOrigin) return;
    let message;
    try { message = typeof event.data === "string" ? JSON.parse(event.data) : event.data; }
    catch { return; }
    if (!message || typeof message !== "object") return;
    if (message.event === "init") {
      loadNotes().catch((error) => {
        notesStatus("Load failed");
        window.showError?.(error.message);
      });
    } else if (message.event === "load") {
      notesStatus("Ready");
    } else if (["save", "autosave"].includes(message.event)) {
      saveNotes(message.xml).catch((error) => {
        notesStatus("Save failed");
        window.showError?.(error.message);
      });
    } else if (message.event === "export" && message.format === "xml") {
      saveNotes(message.xml).catch((error) => {
        notesStatus("Save failed");
        window.showError?.(error.message);
      });
    }
  });
})();