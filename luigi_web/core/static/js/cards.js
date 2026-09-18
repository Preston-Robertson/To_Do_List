(function () {
  "use strict";

  const workspace = document.querySelector("[data-cards-workspace]");
  if (!workspace) return;

  function dialogById(id) {
    const dialog = document.getElementById(id);
    return dialog instanceof HTMLDialogElement ? dialog : null;
  }

  const dialogOpeners = new WeakMap();

  function openDialog(dialog, opener) {
    if (!dialog) return;
    if (
      opener instanceof HTMLElement
      && (!dialog.open || !dialogOpeners.has(dialog))
    ) {
      dialogOpeners.set(dialog, opener);
    }
    if (!dialog.open) dialog.showModal();
  }

  document.addEventListener("click", (event) => {
    const opener = event.target.closest("[data-cards-dialog-open]");
    if (opener) {
      const dialog = dialogById(opener.dataset.cardsDialogOpen);
      const menu = opener.closest(".deck-action-menu");
      if (menu) menu.open = false;
      openDialog(dialog, menu?.querySelector("summary") || opener);
      return;
    }
    const closer = event.target.closest("[data-cards-dialog-close]");
    if (closer) closer.closest("dialog")?.close();
  });

  document.querySelectorAll("dialog.cards-dialog").forEach((dialog) => {
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
    dialog.addEventListener("close", () => {
      const opener = dialogOpeners.get(dialog);
      if (opener?.isConnected) opener.focus();
      dialogOpeners.delete(dialog);
    });
  });

  const quickCollect = dialogById("quick-collect-dialog");
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-cards-quick-collect]");
    if (!button || !quickCollect) return;
    const returnFocus = cardDetailDialog?.open
      ? dialogOpeners.get(cardDetailDialog)
      : button;
    dialogById("card-detail-dialog")?.close();
    quickCollect.querySelector("[data-cards-selected-id]").value = button.dataset.cardId || "";
    quickCollect.querySelector("[data-cards-selected-name]").textContent = button.dataset.cardName || "Selected card";
    openDialog(quickCollect, returnFocus || button);
  });

  const cardDetailDialog = dialogById("card-detail-dialog");
  const cardDetailContent = document.getElementById("card-detail-content");
  let cardDetailRequest = null;

  cardDetailDialog?.addEventListener("close", () => {
    if (cardDetailDialog.open) return;
    cardDetailRequest?.abort();
    cardDetailRequest = null;
  });

  function cardDetailStatus(message, failed = false) {
    cardDetailContent.className = failed ? "card-detail-loading card-detail-error" : "card-detail-loading";
    const status = document.createElement("p");
    status.textContent = message;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "btn";
    close.dataset.cardsDialogClose = "";
    close.textContent = "Close";
    cardDetailContent.replaceChildren(status, close);
    close.focus();
  }

  async function openCardDetail(trigger) {
    if (!cardDetailDialog || !cardDetailContent || !trigger?.dataset.cardDetailUrl) return;
    cardDetailRequest?.abort();
    const controller = new AbortController();
    cardDetailRequest = controller;
    openDialog(cardDetailDialog, trigger);
    cardDetailStatus("Loading card details...");
    try {
      const response = await fetch(trigger.dataset.cardDetailUrl, {
        headers: { "Accept": "text/html" },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`Could not load card details (${response.status})`);
      const content = await response.text();
      if (cardDetailRequest !== controller || !cardDetailDialog.open) return;
      cardDetailContent.className = "";
      cardDetailContent.innerHTML = content;
      window.htmx?.process(cardDetailContent);
      cardDetailContent.querySelector("[data-cards-dialog-close]")?.focus();
    } catch (error) {
      if (controller.signal.aborted || cardDetailRequest !== controller) return;
      cardDetailStatus(error.message || "Could not load card details", true);
    } finally {
      if (cardDetailRequest === controller) cardDetailRequest = null;
    }
  }

  function cardDetailTrigger(event) {
    const trigger = event.target.closest("[data-card-detail-url]");
    if (!trigger) return null;
    const interactive = event.target.closest("button, a, input, select, textarea, form");
    if (interactive && interactive !== trigger) return null;
    return trigger;
  }

  document.addEventListener("click", (event) => {
    const trigger = cardDetailTrigger(event);
    if (!trigger) return;
    event.preventDefault();
    openCardDetail(trigger);
  });

  document.addEventListener("keydown", (event) => {
    if (!['Enter', ' '].includes(event.key)) return;
    const trigger = cardDetailTrigger(event);
    if (!trigger) return;
    event.preventDefault();
    openCardDetail(trigger);
  });

  document.addEventListener("click", (event) => {
    const tab = event.target.closest("[data-card-info-tab]");
    if (tab) {
      const root = tab.closest("[data-card-detail-root]");
      root?.querySelectorAll("[data-card-info-tab]").forEach((item) => {
        item.classList.toggle("active", item === tab);
        item.setAttribute("aria-selected", String(item === tab));
      });
      root?.querySelectorAll(".card-info-panel").forEach((panel) => {
        panel.classList.toggle("active", panel.id === tab.dataset.cardInfoTab);
      });
      return;
    }
    const faceButton = event.target.closest("[data-card-face-button]");
    if (!faceButton) return;
    const root = faceButton.closest("[data-card-detail-root]");
    const faceIndex = faceButton.dataset.cardFaceButton;
    root?.querySelectorAll("[data-card-face-button]").forEach((item) => {
      item.classList.toggle("active", item === faceButton);
    });
    root?.querySelectorAll("[data-card-face], [data-card-face-copy]").forEach((item) => {
      const itemIndex = item.dataset.cardFace ?? item.dataset.cardFaceCopy;
      item.hidden = itemIndex !== faceIndex;
    });
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
      if (!button || button.disabled) return;
      tabs.querySelectorAll("[data-cards-tab]").forEach((item) => {
        item.classList.toggle("active", item === button);
        item.setAttribute("aria-selected", String(item === button));
        item.tabIndex = item === button ? 0 : -1;
      });
      document.querySelectorAll(".cards-tab-panel").forEach((panel) => {
        panel.classList.toggle("active", panel.id === button.dataset.cardsTab);
      });
      if (button.dataset.cardsTab === "notes-panel") mountNotes();
    });
    tabs.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      const buttons = Array.from(tabs.querySelectorAll("[data-cards-tab]:not(:disabled)"));
      const index = buttons.indexOf(event.target);
      if (index < 0) return;
      event.preventDefault();
      const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
        : (index + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next].focus();
      buttons[next].click();
    });
  }

  document.addEventListener("click", (event) => {
    document.querySelectorAll(".deck-action-menu[open]").forEach((menu) => {
      if (!menu.contains(event.target) || event.target.closest("a")) menu.open = false;
    });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const menu = document.querySelector(".deck-action-menu[open]");
    if (menu) {
      menu.open = false;
      menu.querySelector("summary")?.focus();
    }
  });

  const DECK_VIEW_KEY = "luigi.cards.deckView";
  const DECK_GROUP_KEY = "luigi.cards.collapsedGroups";
  const deckViewMedia = window.matchMedia("(max-width: 700px)");
  const deckViewKey = () => `${DECK_VIEW_KEY}.${deckViewMedia.matches ? "mobile" : "desktop"}`;
  const deckViewSelections = new Map();

  function revealLoadedStackImage(image) {
    if (image.closest(".deck-stack-card") && image.complete && image.naturalWidth > 0) {
      image.classList.add("deck-stack-image-loaded");
    }
  }

  function loadDeckViewImages(view) {
    view.querySelectorAll("img[data-deck-image-src]").forEach((image) => {
      const source = image.dataset.deckImageSrc;
      if (!source) return;
      if (image.closest(".deck-stack-card")) {
        image.addEventListener("load", () => revealLoadedStackImage(image), { once: true });
      }
      image.src = source;
      image.removeAttribute("data-deck-image-src");
      revealLoadedStackImage(image);
    });
  }

  function applyDeckView(root = document) {
    const panel = root.querySelector?.("#deck-card-panel") || document.getElementById("deck-card-panel");
    if (!panel) return;
    let selected = deckViewMedia.matches ? "stacks" : "table";
    if (deckViewSelections.has(deckViewKey())) {
      selected = deckViewSelections.get(deckViewKey());
    } else {
      try {
        selected = localStorage.getItem(deckViewKey())
          || (deckViewMedia.matches ? "stacks" : localStorage.getItem(DECK_VIEW_KEY))
          || selected;
      } catch {}
    }
    if (!['table', 'stacks'].includes(selected)) {
      selected = deckViewMedia.matches ? "stacks" : "table";
    }
    panel.querySelectorAll("[data-deck-view]").forEach((button) => {
      const active = button.dataset.deckView === selected;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    panel.querySelectorAll("[data-deck-view-panel]").forEach((view) => {
      const active = view.dataset.deckViewPanel === selected;
      view.hidden = !active;
      if (active) loadDeckViewImages(view);
    });
  }

  function deckGroupKey(panel) {
    return `${DECK_GROUP_KEY}.${panel?.dataset.deckId || "unknown"}`;
  }

  function collapsedDeckGroups(panel) {
    try {
      const parsed = JSON.parse(localStorage.getItem(deckGroupKey(panel)) || "[]");
      return new Set(Array.isArray(parsed) ? parsed : []);
    } catch {
      return new Set();
    }
  }

  function applyDeckGroupState(root = document) {
    const panel = root.querySelector?.("#deck-card-panel") || document.getElementById("deck-card-panel");
    if (!panel) return;
    const collapsed = collapsedDeckGroups(panel);
    panel.querySelectorAll("[data-deck-group-key]").forEach((group) => {
      group.open = !collapsed.has(group.dataset.deckGroupKey);
    });
  }

  function hideFailedStackImages(root = document) {
    root.querySelectorAll?.(".deck-stack-card img").forEach((image) => {
      if (image.hasAttribute("src") && image.complete && image.naturalWidth === 0) {
        image.hidden = true;
      } else {
        revealLoadedStackImage(image);
      }
    });
  }

  document.addEventListener("error", (event) => {
    const image = event.target;
    if (image instanceof HTMLImageElement && image.closest(".deck-stack-card")) {
      image.hidden = true;
    }
  }, true);

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-deck-view]");
    if (!button) return;
    deckViewSelections.set(deckViewKey(), button.dataset.deckView);
    try { localStorage.setItem(deckViewKey(), button.dataset.deckView); } catch {}
    applyDeckView(button.closest("#deck-card-panel"));
  });

  document.addEventListener("toggle", (event) => {
    const group = event.target.closest?.("[data-deck-group-key]");
    if (!group) return;
    const panel = group.closest("#deck-card-panel");
    const collapsed = collapsedDeckGroups(panel);
    if (group.open) collapsed.delete(group.dataset.deckGroupKey);
    else collapsed.add(group.dataset.deckGroupKey);
    try {
      localStorage.setItem(deckGroupKey(panel), JSON.stringify([...collapsed]));
    } catch {}
  }, true);

  applyDeckView();
  applyDeckGroupState();
  hideFailedStackImages();
  deckViewMedia.addEventListener?.("change", () => applyDeckView());
  document.body.addEventListener("htmx:afterSwap", (event) => {
    applyDeckView(event.target);
    applyDeckGroupState(event.target);
    hideFailedStackImages(event.target);
  });

  function deckAddStatus(event, pending) {
    const form = event.detail.elt;
    if (!(form instanceof HTMLFormElement) || !form.closest("#deck-search-results")) return;
    const status = document.querySelector("[data-deck-add-status]");
    if (!status) return;
    status.classList.toggle("is-error", !pending && !event.detail.successful);
    status.textContent = pending ? "Adding card..." : event.detail.successful
      ? "Card added to deck."
      : "Could not add card. Check the quantity and try again.";
  }

  document.body.addEventListener("htmx:beforeRequest", (event) => deckAddStatus(event, true));
  document.body.addEventListener("htmx:afterRequest", (event) => deckAddStatus(event, false));

  function appendList(details, title, values, formatter, expanded = false) {
    if (!values.length) return;
    const section = document.createElement("details");
    section.open = expanded;
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
      const boardLabels = {
        commander: "Commander",
        main: "Mainboard",
        side: "Sideboard",
        maybe: "Maybeboard",
      };
      const sections = (data.sections || []).map((section) =>
        `${boardLabels[section.board] || section.board}${section.category ? ` → ${section.category}` : ""}`
      );
      appendList(output, "Sections preserved", sections, (section) => section, true);
      appendList(output, "Unmatched lines", data.unmatched, (row) => {
        const section = `${boardLabels[row.board] || row.board}${row.category ? ` → ${row.category}` : ""}`;
        return `${row.qty} x ${row.name}${row.set ? ` (${row.set})` : ""} · ${section}`;
      });
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