/* tokenmeter — authchip.js
 *
 * Salesforce login/logout chip mounted into #auth-chip-mount in the
 * header. Polls /api/sf/status on load (and periodically) so the indicator
 * reflects truth, not stale state. Login posts to /api/sf/login (which
 * blocks on the OAuth popup), logout posts to /api/sf/logout.
 */
(function () {
  const STATUS_URL = "/api/sf/status";
  const LOGIN_URL  = "/api/sf/login";
  const LOGOUT_URL = "/api/sf/logout";
  const POLL_MS = 30_000;

  const state = { loggedIn: false, instanceUrl: "", checking: true };
  const subscribers = [];

  function el(tag, attrs, children) {
    const e = document.createElement(tag);
    for (const k of Object.keys(attrs || {})) {
      if (k === "text") e.textContent = attrs[k];
      else if (k.startsWith("on")) e.addEventListener(k.slice(2).toLowerCase(), attrs[k]);
      else e.setAttribute(k, attrs[k]);
    }
    for (const c of children || []) if (c) e.appendChild(c);
    return e;
  }

  function shortHost(url) {
    if (!url) return "";
    try { return new URL(url).host; } catch (_) { return url.replace(/^https?:\/\//, "").split("/")[0]; }
  }

  function notify() {
    subscribers.forEach((cb) => { try { cb(state); } catch (_) {} });
    // Also broadcast a custom event so app.js (which doesn't import this
    // module directly) can react to login/logout from anywhere.
    try {
      window.dispatchEvent(new CustomEvent("tokenmeter:auth-change", {
        detail: { ...state },
      }));
    } catch (_) { /* IE etc */ }
  }

  function subscribe(cb) {
    subscribers.push(cb);
    return () => {
      const i = subscribers.indexOf(cb);
      if (i >= 0) subscribers.splice(i, 1);
    };
  }

  async function refresh() {
    try {
      const r = await fetch(STATUS_URL, { cache: "no-store" });
      if (!r.ok) throw new Error("status " + r.status);
      const body = await r.json();
      state.loggedIn = !!body.logged_in;
      state.instanceUrl = body.instance_url || "";
      state.checking = false;
    } catch (_) {
      state.loggedIn = false;
      state.instanceUrl = "";
      state.checking = false;
    }
    notify();
  }

  async function login(button) {
    button.disabled = true;
    const original = button.textContent;
    button.textContent = "Opening Salesforce…";
    try {
      const r = await fetch(LOGIN_URL, { method: "POST" });
      if (!r.ok) {
        const txt = await r.text();
        alert("Login failed: " + txt);
        return;
      }
      // Wait briefly for the cookie to settle, then refresh.
      await new Promise((res) => setTimeout(res, 250));
      await refresh();
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  async function logout(button) {
    button.disabled = true;
    const original = button.textContent;
    button.textContent = "Signing out…";
    try {
      await fetch(LOGOUT_URL, { method: "POST" });
      await refresh();
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  function mount(node) {
    if (!node) return;

    const dot = el("span", { class: "ac-dot", "aria-hidden": "true" }, []);
    const label = el("span", { class: "ac-label" }, [document.createTextNode("Checking…")]);
    const chev = el("span", { class: "ac-chev", "aria-hidden": "true", text: "▾" }, []);
    const trigger = el("button", {
      type: "button",
      class: "ac-trigger",
      "data-state": "checking",
      "aria-haspopup": "true",
      "aria-expanded": "false",
      "aria-label": "Salesforce connection status",
    }, [dot, label, chev]);

    // Dropdown body — we rebuild the action button on each open so
    // it always reflects the latest state.
    const statusDot = el("span", { class: "ac-status-dot", "aria-hidden": "true" }, []);
    const statusLabel = el("span", { class: "ac-status-label", text: "—" }, []);
    const statusDetail = el("span", { class: "ac-status-detail", text: "" }, []);
    const statusText = el("div", { class: "ac-status-text" }, [statusLabel, statusDetail]);
    const statusRow = el("div", { class: "ac-status-row" }, [statusDot, statusText]);

    const actionSlot = el("div", { class: "ac-action-slot" }, []);

    const help = el("p", { class: "ac-help" }, []);

    const dropdown = el("div", { class: "ac-dropdown", role: "menu" }, [statusRow, actionSlot, help]);
    dropdown.hidden = true;

    function renderDropdown() {
      const s = state;
      dropdown.dataset.state = s.loggedIn ? "connected" : "signed-out";
      statusLabel.textContent = s.loggedIn ? "Connected to Salesforce" : "Not connected";
      statusDetail.textContent = s.loggedIn ? shortHost(s.instanceUrl) : "Sign in with OAuth to run benchmarks";

      actionSlot.replaceChildren();
      help.replaceChildren();

      if (s.loggedIn) {
        const btn = el("button", {
          type: "button",
          class: "ac-action secondary",
          text: "Sign out",
          onClick: async (e) => { await logout(e.currentTarget); },
        }, []);
        actionSlot.appendChild(btn);
        help.appendChild(document.createTextNode(
          "Signing out clears the cached access token. You can sign back in any time."
        ));
      } else {
        const btn = el("button", {
          type: "button",
          class: "ac-action primary",
          text: "Connect Salesforce →",
          onClick: async (e) => { await login(e.currentTarget); },
        }, []);
        actionSlot.appendChild(btn);
        help.appendChild(document.createTextNode(
          "You'll be redirected to your Salesforce org's login page, then sent back."
        ));
      }
    }

    function renderTrigger() {
      const s = state;
      const newState = s.checking ? "checking" : (s.loggedIn ? "connected" : "signed-out");
      trigger.dataset.state = newState;
      label.textContent = s.checking
        ? "Checking…"
        : (s.loggedIn ? "Connected" : "Sign in");
    }

    function open() {
      renderDropdown();
      dropdown.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      document.addEventListener("click", onDocClick, true);
    }
    function close() {
      dropdown.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
      document.removeEventListener("click", onDocClick, true);
    }
    function onDocClick(ev) {
      if (!node.contains(ev.target)) close();
    }
    trigger.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (dropdown.hidden) open();
      else close();
    });

    node.replaceChildren(trigger, dropdown);

    subscribers.push(() => {
      renderTrigger();
      if (!dropdown.hidden) renderDropdown();
    });
    renderTrigger();
  }

  function init() {
    const node = document.getElementById("auth-chip-mount");
    if (!node) return;
    mount(node);
    refresh();
    // Refresh periodically so a session timeout / external logout reflects.
    setInterval(refresh, POLL_MS);
    // Refresh when the page regains focus (e.g., the OAuth popup returns).
    window.addEventListener("focus", refresh);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.tokenmeter = window.tokenmeter || {};
  window.tokenmeter.auth = { refresh, subscribe, getState: () => ({ ...state }) };
})();
