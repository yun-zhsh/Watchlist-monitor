// The order of the stocks on the home page and which of them are hidden (see home_order.py, which keeps the rules).
// config.yaml holds both, and the page is rendered from it. When the page loads, the newest order published on the GitHub Pages
// site (home-order.json) is fetched and applied, so a page served from an older config.yaml (the Cloud Run image) follows a new
// order without a redeploy; if the fetch fails, the page keeps the order it was rendered with.
// The "Order" panel saves a new order by starting the workflow home-order.yml through the GitHub API, with a fine-grained token
// that is kept in this browser only.
(function () {
  // The same rule as home_order.apply in Python; tests/test_home_order.py runs both on the same cases.
  function applyOrder(symbols, hidden, published) {
    const own = new Set(hidden);
    if (!published || typeof published !== "object" || !Array.isArray(published.order) || !Array.isArray(published.hidden)) {
      return [symbols.slice(), symbols.filter((s) => own.has(s))];
    }
    const known = new Set(symbols);
    const listed = [...new Set(published.order.filter((s) => typeof s === "string" && known.has(s)))];
    const isListed = new Set(listed);
    const order = listed.concat(symbols.filter((s) => !isListed.has(s)));
    const publishedHidden = new Set(published.hidden.filter((s) => typeof s === "string"));
    const shown = new Set([...listed.filter((s) => publishedHidden.has(s)), ...symbols.filter((s) => !isListed.has(s) && own.has(s))]);
    return [order, order.filter((s) => shown.has(s))];
  }

  if (typeof module !== "undefined" && module.exports) module.exports = { applyOrder };
  if (typeof document === "undefined") return;

  const list = document.getElementById("stock-list");
  const panel = document.getElementById("order-panel");
  const settings = (window.ValuationView && window.ValuationView.view.home_order) || {};
  if (!list || !panel) return;

  const TOKEN_KEY = "valuation.githubToken";
  const PENDING_KEY = "valuation.homeOrder.pending";
  const PENDING_MINUTES = 30;  // how long a save made on this device is preferred to an older published file
  const read = (key) => { try { return localStorage.getItem(key); } catch (e) { return null; } };
  const write = (key, value) => { try { localStorage.setItem(key, value); return true; } catch (e) { return false; } };
  const remove = (key) => { try { localStorage.removeItem(key); } catch (e) { /* nothing stored */ } };

  const cards = {};
  list.querySelectorAll("[data-symbol]").forEach((c) => { cards[c.dataset.symbol] = c; });
  const symbols = Object.keys(cards);
  const rendered = { order: symbols, hidden: symbols.filter((s) => cards[s].hidden) };
  let saved = rendered;  // what the page shows: the published order, or this device's save that is still being published
  let draft = null;  // the order being edited in the panel

  const pageNotice = document.getElementById("order-notice");
  const message = document.getElementById("order-message");
  const tokenInput = document.getElementById("order-token");
  const tokenState = document.getElementById("order-token-state");
  const orderList = document.getElementById("order-list");

  function say(el, text, error) {
    el.textContent = text;
    el.hidden = !text;
    el.classList.toggle("notice-error", !!error);
  }

  function show(state) {
    state.order.forEach((s) => list.appendChild(cards[s]));
    symbols.forEach((s) => { cards[s].hidden = state.hidden.includes(s); });
    saved = { order: state.order.slice(), hidden: state.hidden.slice() };
  }

  function same(a, b) {
    return JSON.stringify(a.order) === JSON.stringify(b.order) && JSON.stringify(a.hidden) === JSON.stringify(b.hidden);
  }

  function fromPublished(published) {
    const [order, hidden] = applyOrder(rendered.order, rendered.hidden, published);
    return { order, hidden };
  }

  function pending() {
    try {
      const p = JSON.parse(read(PENDING_KEY));
      if (p && Date.now() - p.at < PENDING_MINUTES * 60000) return fromPublished(p);
    } catch (e) { /* nothing usable stored */ }
    remove(PENDING_KEY);
    return null;
  }

  function applyPending(published) {
    const mine = pending();
    if (!mine) return;
    if (published && same(mine, published)) {
      remove(PENDING_KEY);
      return;
    }
    show(mine);
    say(pageNotice, "Your saved order is still being published; other devices show it within a few minutes.");
  }

  if (settings.url) {
    fetch(settings.url + "?t=" + Date.now(), { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status))))
      .then((published) => {
        const state = fromPublished(published);
        show(state);
        applyPending(state);
      })
      .catch(() => applyPending(null));  // keep the order the page was rendered with
  } else {
    applyPending(null);
  }

  // The panel
  function move(from, to) {
    if (to < 0 || to >= draft.order.length || from === to) return;
    const [s] = draft.order.splice(from, 1);
    draft.order.splice(to, 0, s);
    renderList();
  }

  function button(text, label, onClick, disabled) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn btn-secondary btn-small";
    b.textContent = text;
    b.setAttribute("aria-label", label);
    b.disabled = !!disabled;
    b.addEventListener("click", onClick);
    return b;
  }

  let dragged = null;
  function renderList() {
    orderList.textContent = "";
    draft.order.forEach((s, i) => {
      const name = cards[s].dataset.name;
      const isHidden = draft.hidden.includes(s);
      const li = document.createElement("li");
      li.className = "order-item" + (isHidden ? " order-hidden" : "");
      li.draggable = true;
      li.dataset.symbol = s;
      const label = document.createElement("span");
      label.className = "order-label";
      label.innerHTML = "<strong></strong> <span class=\"muted\"></span>";
      label.querySelector("strong").textContent = name;
      label.querySelector(".muted").textContent = s + (isHidden ? " · hidden" : "");
      const tools = document.createElement("span");
      tools.className = "order-tools";
      tools.append(
        button("Top", "Move " + name + " to the top", () => move(i, 0), i === 0),
        button("Up", "Move " + name + " up", () => move(i, i - 1), i === 0),
        button("Down", "Move " + name + " down", () => move(i, i + 1), i === draft.order.length - 1),
        button(isHidden ? "Show" : "Hide", (isHidden ? "Show " : "Hide ") + name + " on the home page", () => {
          draft.hidden = isHidden ? draft.hidden.filter((h) => h !== s) : draft.hidden.concat(s);
          renderList();
        }),
      );
      li.append(label, tools);
      li.addEventListener("dragstart", (e) => { dragged = i; e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", s); });
      li.addEventListener("dragover", (e) => { e.preventDefault(); li.classList.add("order-over"); });
      li.addEventListener("dragleave", () => li.classList.remove("order-over"));
      li.addEventListener("drop", (e) => { e.preventDefault(); if (dragged !== null) move(dragged, i); dragged = null; });
      li.addEventListener("dragend", () => { dragged = null; });
      orderList.appendChild(li);
    });
  }

  function showTokenState() {
    tokenState.textContent = read(TOKEN_KEY) ? "A token is remembered in this browser." : "No token is remembered in this browser.";
  }

  function open() {
    draft = { order: saved.order.slice(), hidden: saved.hidden.slice() };
    say(message, "");
    renderList();
    showTokenState();
    if (typeof panel.showModal === "function") panel.showModal(); else panel.setAttribute("open", "");
  }

  function close() {
    if (typeof panel.close === "function") panel.close(); else panel.removeAttribute("open");
  }

  const ERRORS = {
    401: "GitHub rejected the token (401): it is wrong or has expired. Enter a new one below.",
    403: "The token may not start workflows here (403): it needs Repository permissions, Actions: Read and write, for this repository.",
    404: "Not found (404): the token cannot see this repository, or the workflow home-order.yml is not on the main branch.",
    422: "GitHub refused the request (422): the workflow may be disabled, or its input has changed.",
  };

  async function save() {
    const token = tokenInput.value.trim() || read(TOKEN_KEY);
    if (!settings.repository) return say(message, "Saving is not set up: config.yaml has no github_repository.", true);
    if (!token) {
      panel.querySelector(".order-token").open = true;
      return say(message, "Enter a GitHub token first (see below how to create one).", true);
    }
    const request = { order: draft.order, hidden: draft.order.filter((s) => draft.hidden.includes(s)) };
    const saveButton = panel.querySelector("[data-order-save]");
    saveButton.disabled = true;
    say(message, "Saving...");
    try {
      const r = await fetch(`https://api.github.com/repos/${settings.repository}/actions/workflows/${settings.workflow}/dispatches`, {
        method: "POST",
        headers: { Accept: "application/vnd.github+json", Authorization: "Bearer " + token, "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json" },
        body: JSON.stringify({ ref: settings.ref, inputs: { order: JSON.stringify(request) } }),
      });
      if (r.status === 204 || r.status === 200) {
        if (tokenInput.value.trim()) { write(TOKEN_KEY, tokenInput.value.trim()); tokenInput.value = ""; showTokenState(); }
        write(PENDING_KEY, JSON.stringify({ ...request, at: Date.now() }));
        show(request);
        say(message, "Saved. GitHub is writing the new order into config.yaml and republishing the site; every device shows it within a few minutes.");
        say(pageNotice, "");
      } else {
        say(message, ERRORS[r.status] || `GitHub answered with HTTP ${r.status}; the order was not saved.`, true);
      }
    } catch (e) {
      say(message, "Could not reach GitHub; check the connection. The order was not saved.", true);
    } finally {
      saveButton.disabled = false;
    }
  }

  document.getElementById("order-open").addEventListener("click", open);
  panel.querySelector("[data-order-close]").addEventListener("click", close);
  panel.querySelector("[data-order-undo]").addEventListener("click", () => {
    draft = { order: saved.order.slice(), hidden: saved.hidden.slice() };
    renderList();
    say(message, "Back to the order and hidden stocks last saved.");
  });
  panel.querySelector("[data-order-show-all]").addEventListener("click", () => {
    draft.hidden = [];
    renderList();
    say(message, "Every stock is shown. Press Save to keep it.");
  });
  panel.querySelector("[data-order-save]").addEventListener("click", save);
  panel.querySelector("[data-token-remember]").addEventListener("click", () => {
    const token = tokenInput.value.trim();
    if (!token) return say(message, "Paste a token into the field first.", true);
    say(message, write(TOKEN_KEY, token) ? "The token is remembered in this browser." : "This browser does not allow storing it; paste it again when saving.", !read(TOKEN_KEY));
    tokenInput.value = "";
    showTokenState();
  });
  panel.querySelector("[data-token-forget]").addEventListener("click", () => {
    remove(TOKEN_KEY);
    tokenInput.value = "";
    showTokenState();
    say(message, "The token is forgotten in this browser.");
  });
  panel.querySelectorAll("[data-order-repo]").forEach((el) => { if (settings.repository) el.textContent = settings.repository; });
})();
