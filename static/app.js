// static/app.js — vanilla JS SPA. No innerHTML.

const state = {
  preflight: null,
  scenarios: [],
  runsPerPath: 3,
  model: "claude-4-5-sonnet",
  models: [],            // models[] selected for the current run
  availableModels: [],   // populated by /api/models
  activeModel: null,     // which model the scenario/summary view is currently rendering
  scenarioResults: {},   // { [sid]: { [model]: { native: RunResult[], mcp: RunResult[] } } }
  charts: {},
  reportPath: null,
  activeReportId: null,  // db row id of the report being viewed (for /api/reports/{id}/* calls)
  active: "setup",
  // Reports analytics view state. Populated by loadReports(); the
  // table re-renders on filter/sort changes from this cache without
  // re-fetching.
  reports: [],
  reportsKpis: {},
  reportsSortKey: "started_at",
  reportsSortDir: "desc",
  // True when the user opened the current report from the analytics
  // table; controls the "← Back to reports" link in scenario/summary
  // views. Reset on a fresh benchmark or when navigating home.
  cameFromReports: false,
  // Tier D compare flow: when set, the user has clicked "Compare" on a
  // report; the next "vs A" click navigates to /compare?a=…&b=….
  compareSelected: null,
};

// Guest mode for read-only share links. Set by share.html before app.js loads.
const isGuest = (typeof window !== "undefined") && !!window.__SHARE_GUEST__;
const shareToken = isGuest ? (window.__SHARE_TOKEN__ || "") : "";
state.guestMode = isGuest;

// Map authenticated paths -> /api/share/<token>/... mirrors when guest mode
// is active. Only the read-only endpoints are mirrored — all other paths
// pass through unchanged (and 404 in guest mode, which the existing failure
// handling already silently degrades).
function apiPath(path) {
  if (!isGuest || !shareToken) return path;
  if (path.startsWith("/api/reports/latest/data")) {
    return `/api/share/${shareToken}/data`;
  }
  if (path.match(/^\/api\/reports\/[^/]+\/data$/)) {
    return `/api/share/${shareToken}/data`;
  }
  if (path.match(/^\/api\/reports\/[^/]+\/projection/)) {
    const qs = path.split("?")[1] || "";
    return `/api/share/${shareToken}/projection${qs ? "?" + qs : ""}`;
  }
  if (path.match(/^\/api\/scenarios\/[^/]+\/trace$/)) {
    const sid = path.split("/")[3];
    return `/api/share/${shareToken}/scenarios/${sid}/trace`;
  }
  return path;
}

function defaultModel(models) {
  if (!models || !models.length) return "";
  for (const m of models) if (m.toLowerCase().includes("sonnet")) return m;
  return models[0];
}

function selectedModels(containerId) {
  return Array.from(document.querySelectorAll(
    `#${containerId} input[type=checkbox]:checked`
  )).map((i) => i.value);
}

function activeBucket(sid) {
  // state.scenarioResults[sid] is now { [model]: { native: [], mcp: [] } }.
  if (!state.scenarioResults[sid]) return { native: [], mcp: [] };
  const m = state.activeModel || Object.keys(state.scenarioResults[sid])[0];
  return state.scenarioResults[sid][m] || { native: [], mcp: [] };
}

let runStartTime = null;
let pollIntervalId = null;

const $ = (id) => document.getElementById(id);

function el(tag, opts = {}, ...children) {
  const node = document.createElement(tag);
  if (opts.className) node.className = opts.className;
  if (opts.text != null) node.textContent = opts.text;
  if (opts.attrs) {
    for (const [k, v] of Object.entries(opts.attrs)) node.setAttribute(k, v);
  }
  if (opts.onClick) node.addEventListener("click", opts.onClick);
  for (const c of children) {
    if (c == null) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

async function init() {
  await loadPreflight();
  await loadScenarios();
  await loadModels();

  // Decide between login splash and the home chooser. The whole app
  // is gated on a Salesforce session — preflight tells us the *server*
  // is healthy, sf/status tells us the *user* has authenticated.
  const loggedIn = await checkSfLoginStatus();
  if (!loggedIn) {
    renderLogin();
  } else {
    renderLanding();
  }

  // Wire the splash's login button. Reuses the same /api/sf/login flow
  // the old setup-view "Connect Salesforce" button used.
  const loginCta = $("login-cta-btn");
  if (loginCta) {
    loginCta.addEventListener("click", async () => {
      loginCta.disabled = true;
      $("login-cta-hint").textContent = "Redirecting to Salesforce…";
      $("login-cta-error").hidden = true;
      try {
        const res = await fetch("/api/sf/login", { method: "POST" });
        const body = await res.json();
        if (body.ok && body.authorize_url) {
          window.location.href = body.authorize_url;
        } else {
          $("login-cta-error").textContent =
            "Login could not start: " + (body.error || "unknown error");
          $("login-cta-error").hidden = false;
          loginCta.disabled = false;
        }
      } catch (e) {
        $("login-cta-error").textContent = "Network error: " + e.message;
        $("login-cta-error").hidden = false;
        loginCta.disabled = false;
      }
    });
  }

  $("run-btn").addEventListener("click", startRun);
  $("run-again").addEventListener("click", () => location.reload());
  // Brand mark = "home". Returns the user to the landing chooser
  // without discarding state — any in-progress run keeps streaming
  // in the background; loaded report state stays available via the stepper.
  const brandHome = $("brand-home");
  if (brandHome) brandHome.addEventListener("click", goHome);

  // Wire the three landing cards. Each routes to the same setup-view
  // section it always did, but only that subsection is revealed.
  document.querySelectorAll("#landing-view .landing-card").forEach((card) => {
    card.addEventListener("click", () => {
      const target = card.dataset.target;  // "benchmark" | "freeform" | "reports"
      state.active = "setup";
      renderSetup(target);
      // The reports analytics view fetches its data on demand so we
      // don't pay for the JSONB hydration on every page load.
      if (target === "reports") loadReports();
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  });
  const setupBack = $("setup-back-link");
  if (setupBack) {
    setupBack.addEventListener("click", (e) => {
      e.preventDefault();
      goHome();
    });
  }
  // Two "← Back to reports" links, one in scenario-view and one in
  // summary-view. Both visible only when state.cameFromReports is true.
  ["scenario-back-link", "summary-back-link"].forEach((id) => {
    const link = $(id);
    if (link) {
      link.addEventListener("click", (e) => {
        e.preventDefault();
        goBackToReports();
      });
    }
  });
  const pdfBtn = $("export-pdf");
  if (pdfBtn) pdfBtn.addEventListener("click", exportPdf);
  const sPdfBtn = $("scenario-export-pdf");
  if (sPdfBtn) sPdfBtn.addEventListener("click", exportPdf);

  // Tier D: share modal wiring (no-ops in guest mode — buttons are removed
  // by bootstrapGuestMode before this fires).
  const summaryShare = $("summary-share-btn");
  if (summaryShare) summaryShare.addEventListener("click", openShareModal);
  const scenarioShare = $("scenario-share-btn");
  if (scenarioShare) scenarioShare.addEventListener("click", openShareModal);
  const closeBtn = $("share-close");
  if (closeBtn) closeBtn.addEventListener("click", closeShareModal);
  const copyBtn = $("share-copy");
  if (copyBtn) {
    copyBtn.addEventListener("click", async () => {
      const u = $("share-url").value;
      try { await navigator.clipboard.writeText(u); } catch (_) { /* noop */ }
      copyBtn.textContent = "Copied";
      setTimeout(() => { copyBtn.textContent = "Copy"; }, 1500);
    });
  }
  const regenBtn = $("share-regenerate");
  if (regenBtn) {
    regenBtn.addEventListener("click", () => {
      if (state.activeReportId) regenerateShareLink(state.activeReportId, 30);
    });
  }
  for (const el of document.querySelectorAll("[data-close]")) {
    el.addEventListener("click", closeShareModal);
  }

  // Freeform scenario controls
  const ffBtn = $("freeform-run-btn");
  const ffPrompt = $("freeform-prompt");
  if (ffBtn && ffPrompt) {
    const updateEnabled = () => {
      const ok = state.preflight?.ok && ffPrompt.value.trim().length > 0;
      ffBtn.disabled = !ok;
    };
    ffPrompt.addEventListener("input", updateEnabled);
    ffBtn.addEventListener("click", startFreeformRun);
  }

  // Reports & analytics — wire filters and sortable column clicks.
  // The actual table is rendered by renderReportsTable() each time the
  // data changes; this just attaches the event handlers.
  if ($("reports-filter-kind")) {
    $("reports-filter-kind").addEventListener("change", renderReportsTable);
    $("reports-filter-model").addEventListener("change", renderReportsTable);
    $("reports-filter-search").addEventListener("input", renderReportsTable);
    document.querySelectorAll(".reports-table th[data-sort]").forEach((th) => {
      th.addEventListener("click", () => {
        const col = th.dataset.sort;
        if (state.reportsSortKey === col) {
          state.reportsSortDir = state.reportsSortDir === "asc" ? "desc" : "asc";
        } else {
          state.reportsSortKey = col;
          state.reportsSortDir = "desc";
        }
        renderReportsTable();
      });
    });
  }
  $("sf-login-btn").addEventListener("click", async () => {
    const btn = $("sf-login-btn");
    const hint = $("sf-login-hint");
    btn.disabled = true;
    hint.textContent = "Redirecting to Salesforce…";
    try {
      const res = await fetch("/api/sf/login", { method: "POST" });
      const body = await res.json();
      if (body.ok && body.authorize_url) {
        // Server-side flow returns the authorize URL; navigate the user there.
        // After OAuth completes, /callback writes the token into the session
        // row and the user can return to this tab.
        window.location.href = body.authorize_url;
      } else {
        hint.textContent = "Login failed: " + (body.error || "unknown error");
        btn.disabled = false;
      }
    } catch (e) {
      hint.textContent = "Login failed: " + e.message;
      btn.disabled = false;
    }
  });
}

async function loadPreflight() {
  const res = await fetch("/api/preflight", { cache: "no-store" });
  state.preflight = await res.json();
  const banner = $("preflight-status");
  if (state.preflight.ok) {
    banner.textContent = "● ready";
    banner.className = "status ok";
  } else {
    banner.textContent = "● preflight failed";
    banner.className = "status err";
  }
  // The dedicated login splash now gates the whole app, so the
  // legacy in-setup-view login row is redundant. Keep it permanently
  // hidden — it stays in the markup only so an old cached app.js
  // wouldn't crash trying to address it.
  $("sf-login-row").hidden = true;
}

async function loadScenarios() {
  const res = await fetch("/api/scenarios");
  state.scenarios = await res.json();

  const total = state.scenarios.length;
  const summaryEl = $("catalog-summary");
  if (summaryEl) summaryEl.textContent = "";  // catalog-meta pills replace this

  const meta = $("catalog-meta");
  if (meta) {
    meta.replaceChildren();
    meta.appendChild(el("span", { className: "pill", text: `${total} scenarios` }));
    meta.appendChild(el("span", { className: "pill", text: "3 runs per path" }));
    meta.appendChild(el("span", { className: "pill", text: `~${total * 3} min total` }));
  }

  renderScenarioCardGrid(state.scenarios);
  // Lazy-fetch sparkline data after first paint.
  setTimeout(() => enrichWithSparklines(state.scenarios.map((s) => s.id)), 50);

  const countEl = $("scenario-card-count");
  if (countEl) countEl.textContent = `${total} scenarios`;

  // Wire the "select all" checkbox + sync indeterminate state.
  wireScenarioSelectAll();

  if (state.preflight?.ok) $("run-btn").disabled = false;
  else showRemediation();
  buildStepper();
}

// Tier E (F4) — render the scenario catalog as a card grid. Selection state
// lives in the DOM (per-tile checkbox `data-sid`); startRun() reads checked
// boxes directly. The "select all" master is wired by wireScenarioSelectAll.
function renderScenarioCardGrid(scenarios) {
  const grid = document.getElementById("scenario-list");
  if (!grid) return;
  grid.replaceChildren();

  state.sparkData = state.sparkData || {};

  for (const sc of scenarios) {
    const tile = document.createElement("div");
    tile.className = "scenario-card-tile selected";
    tile.setAttribute("role", "listitem");
    tile.setAttribute("data-id", sc.id);
    tile.setAttribute("data-category", sc.category || "");

    const check = document.createElement("input");
    check.type = "checkbox";
    check.className = "check";
    check.checked = true;  // catalog defaults to all selected (matches legacy behavior)
    check.dataset.sid = sc.id;
    check.addEventListener("click", (e) => e.stopPropagation());
    check.addEventListener("change", () => {
      tile.classList.toggle("selected", check.checked);
    });
    tile.appendChild(check);

    const idLine = document.createElement("div");
    idLine.className = "scenario-id";
    const dot = document.createElement("span");
    dot.className = "cat-dot";
    idLine.appendChild(dot);
    idLine.appendChild(document.createTextNode(sc.id));
    tile.appendChild(idLine);

    const title = document.createElement("div");
    title.className = "scenario-title";
    title.textContent = sc.title;
    tile.appendChild(title);

    const pillRow = document.createElement("div");
    pillRow.className = "pill-row";
    const cat = document.createElement("span");
    cat.className = "pill";
    cat.textContent = sc.category;
    const diff = document.createElement("span");
    diff.className = "pill";
    diff.textContent = sc.difficulty;
    pillRow.append(cat, diff);
    tile.appendChild(pillRow);

    // Sparkline placeholder; populated lazily by enrichWithSparklines.
    const spark = document.createElement("div");
    spark.className = "sparkline-empty";
    spark.textContent = "no runs yet";
    spark.dataset.sparkSlot = sc.id;
    tile.appendChild(spark);

    tile.addEventListener("click", () => {
      check.checked = !check.checked;
      check.dispatchEvent(new Event("change", { bubbles: true }));
    });

    grid.appendChild(tile);
  }
}

async function enrichWithSparklines(scenarioIds) {
  if (!scenarioIds.length) return;
  try {
    const r = await fetch(`/api/scenarios/sparkline?ids=${encodeURIComponent(scenarioIds.join(","))}`);
    if (!r.ok) return;
    const data = await r.json();
    for (const sid of Object.keys(data)) {
      const slot = document.querySelector(`[data-spark-slot="${sid}"]`);
      if (!slot) continue;
      const native = (data[sid].native || []).slice(0, 8).reverse();
      const mcp = (data[sid].mcp || []).slice(0, 8).reverse();
      if (!native.length && !mcp.length) continue;
      const max = Math.max(...native, ...mcp, 0.001);
      const sparkEl = document.createElement("div");
      sparkEl.className = "sparkline";
      const all = [...native.map((v) => ({ v, cls: "" })),
                   ...mcp.map((v) => ({ v, cls: "mcp" }))];
      for (const { v, cls } of all) {
        const bar = document.createElement("span");
        bar.className = "bar" + (cls ? " " + cls : "");
        bar.style.height = `${Math.max(2, (v / max) * 24)}px`;
        sparkEl.appendChild(bar);
      }
      slot.replaceWith(sparkEl);
    }
  } catch (_) { /* silent */ }
}

async function loadModels() {
  try {
    const r = await fetch("/api/models", { cache: "no-store" });
    const { models } = await r.json();
    const list = models || [];
    state.availableModels = list;
    populateModelCheckboxes("model-select", list);
    populateModelCheckboxes("freeform-model", list);
  } catch (e) {
    // /api/models is supposed to never fail — but if it does (e.g. no
    // Inference addons attached on a dev install), leave the checkbox
    // grids empty and let the user know via console.
    console.warn("loadModels failed:", e);
  }
}

function populateModelCheckboxes(containerId, models) {
  const c = document.getElementById(containerId);
  if (!c) return;
  c.replaceChildren();
  const dflt = defaultModel(models);
  for (const m of models) {
    const lbl = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = m;
    if (m === dflt) cb.checked = true;
    lbl.appendChild(cb);
    lbl.appendChild(document.createTextNode(" " + m));
    c.appendChild(lbl);
  }
}

// Connect the master checkbox to the per-row checkboxes:
//  - Click master → toggles all rows to match
//  - Click a row → updates master to "all checked", "none checked", or
//    indeterminate based on the new collective state
function wireScenarioSelectAll() {
  const list = $("scenario-list");
  if (!list) return;

  // Look up by id every time — these helpers are called from listeners
  // that may outlive the original master <input> if the DOM changes.
  const getMaster = () => $("scenario-list-toggle-all");
  const dataCheckboxes = () =>
    Array.from(list.querySelectorAll(".scenario-card-tile input[type=checkbox]"));

  const syncMasterFromRows = () => {
    const master = getMaster();
    if (!master) return;
    const all = dataCheckboxes();
    if (!all.length) {
      master.checked = false;
      master.indeterminate = false;
      return;
    }
    const checked = all.filter((c) => c.checked).length;
    if (checked === 0) {
      master.checked = false;
      master.indeterminate = false;
    } else if (checked === all.length) {
      master.checked = true;
      master.indeterminate = false;
    } else {
      master.checked = false;
      master.indeterminate = true;
    }
  };

  // Idempotent listener attachment — the data attribute marks that we've
  // wired the master and the delegated row listener already.
  if (!list.dataset.toggleAllWired) {
    // Per-tile change events bubble to #scenario-list (the new card-grid
    // container) and update the master's indeterminate state.
    list.addEventListener("change", (e) => {
      if (!(e.target instanceof HTMLInputElement)) return;
      if (e.target.closest(".scenario-card-tile") &&
          e.target.matches("input[type=checkbox]")) {
        syncMasterFromRows();
      }
    });
    list.dataset.toggleAllWired = "1";
  }

  // The master checkbox now lives outside #scenario-list (in
  // .scenario-card-grid-controls), so its change event won't bubble into
  // the delegated listener above. Wire it directly — also idempotent.
  const master = getMaster();
  if (master && !master.dataset.toggleAllWired) {
    master.addEventListener("change", (e) => {
      const want = e.target.checked;
      for (const c of dataCheckboxes()) {
        if (c.checked !== want) {
          c.checked = want;
          // Reflect selection styling on the tile.
          const tile = c.closest(".scenario-card-tile");
          if (tile) tile.classList.toggle("selected", want);
        }
      }
      e.target.indeterminate = false;
    });
    master.dataset.toggleAllWired = "1";
  }

  syncMasterFromRows();
}

function showRemediation() {
  const box = $("preflight-remediation");
  box.hidden = false;
  box.replaceChildren();
  box.appendChild(el("strong", { text: "Preflight issues:" }));
  const ul = el("ul");
  for (const r of state.preflight.remediation) {
    ul.appendChild(el("li", { text: r }));
  }
  box.appendChild(ul);
}

function buildStepper() {
  const nav = $("stepper");
  nav.replaceChildren();
  // Freeform scenarios are tagged separately so the user can spot them at a
  // glance. If there are multiple, they get numbered (Free-form 1, 2, ...).
  let freeformIndex = 0;
  const freeformCount = state.scenarios.filter(
    (s) => s.category === "freeform" || s.id.startsWith("freeform_"),
  ).length;
  for (const s of state.scenarios) {
    const isFreeform = s.category === "freeform" || s.id.startsWith("freeform_");
    let label = s.id.split("_")[0];
    let cls = "step";
    if (isFreeform) {
      freeformIndex += 1;
      label = freeformCount > 1 ? `Free-form ${freeformIndex}` : "Free-form";
      cls += " freeform";
    }
    nav.appendChild(el("div", {
      className: cls,
      text: label,
      attrs: { "data-sid": s.id, title: s.title || s.id },
      onClick: () => showScenario(s.id),
    }));
  }
  nav.appendChild(el("div", {
    className: "step",
    text: "Summary",
    attrs: { "data-sid": "summary" },
    onClick: () => showSummary(),
  }));
}

function setStepStatus(sid, cls) {
  const node = document.querySelector(`.step[data-sid="${CSS.escape(sid)}"]`);
  if (!node) return;
  node.classList.remove("done", "active", "error");
  if (cls) node.classList.add(cls);
}

async function startRun() {
  // Scope the query to per-row checkboxes; the master "select all"
  // checkbox lives in a header row that has no data-sid and would
  // otherwise serialize as null.
  const checked = Array.from(document.querySelectorAll(
    "#scenario-list .scenario-card-tile input[type=checkbox]:checked"
  ))
    .map((i) => i.dataset.sid)
    .filter(Boolean);
  if (checked.length === 0) return;
  state.runsPerPath = parseInt($("runs-per-path").value, 10) || 3;
  const chosenModels = selectedModels("model-select");
  if (chosenModels.length === 0) {
    alert("Select at least one model.");
    return;
  }
  state.models = chosenModels;
  state.model = chosenModels[0];
  state.activeModel = null;  // first run_complete (or hydrate) will pick this
  state.maxTurns = parseInt($("max-turns").value, 10) || 30;
  for (const s of state.scenarios) {
    state.scenarioResults[s.id] = {};  // model-keyed; empty until first run
  }

  runStartTime = Date.now();
  state._pollEventCount = 0;
  state._pollDoneRuns = 0;
  state._seenRunKeys = new Set();
  if (pollIntervalId) clearInterval(pollIntervalId);
  pollIntervalId = setInterval(pollForCompletion, 5000);

  $("setup-view").hidden = true;
  $("progress-view").hidden = false;
  setStepperVisible(true);  // run is starting → stepper relevant
  state.cameFromReports = false;  // fresh run, not viewing a saved report
  state.activeReportId = null;    // fresh run — id resolves once we hit /api/reports?limit=1

  const body = {
    scenario_ids: checked,
    runs_per_path: state.runsPerPath,
    models: state.models,
    max_turns: state.maxTurns,
    operator: "local user",
    org_name: "(local org)",
  };

  const res = await fetch("/api/run", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    $("progress-text").textContent = "error starting run";
    return;
  }

  await consumeSseStream(res, checked.length * state.runsPerPath * 2 * state.models.length);

  // After the SSE reader loop exits, always try to complete the run. This
  // covers cases where the server wrote the report but the SSE stream got
  // closed before benchmark_complete made it to the browser (buffering,
  // proxy timeouts, browser sleep, etc.).
  if ($("progress-view").hidden === false) {
    try {
      const r = await fetch("/api/reports/latest");
      // If there's a report file, a benchmark completed — move to summary.
      if (r.ok) {
        $("progress-view").hidden = true;
        showSummary();
      }
    } catch (_) {
      // ignore
    }
  }
}

// Read SSE events off `res.body` and dispatch each one through handleEvent.
// `totalRuns` is used to update the progress bar percentage as runs complete.
async function consumeSseStream(res, totalRuns) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let doneRuns = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split("\n\n");
    buf = lines.pop();
    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      const ev = JSON.parse(line.slice(6));
      handleEvent(ev, () => {
        doneRuns += 1;
        const pct = Math.round((doneRuns / Math.max(totalRuns, 1)) * 100);
        $("progress-fill").style.width = pct + "%";
      });
    }
  }
}

// Kick off a free-format run. Same shape as startRun but only one scenario,
// synthesized from the user's prompt + optional title. The scenario is
// registered in state.scenarios *before* the POST, so the stepper shows
// a dot for it immediately and the user can click into the in-progress
// scenario detail to watch turns roll in.
async function startFreeformRun() {
  const promptEl = $("freeform-prompt");
  const titleEl = $("freeform-title");
  const prompt = (promptEl?.value || "").trim();
  if (!prompt) return;
  const title = (titleEl?.value || "").trim() || "Free-format scenario";

  // Freeform has its own controls so they're independent of the catalog's
  // dropdowns above.
  state.runsPerPath = parseInt($("freeform-runs").value, 10);
  const chosenModels = selectedModels("freeform-model");
  if (chosenModels.length === 0) {
    alert("Select at least one model.");
    return;
  }
  state.models = chosenModels;
  state.model = chosenModels[0];
  state.activeModel = null;
  state.maxTurns = parseInt($("freeform-max-turns").value, 10);

  // Generate a deterministic id the backend will accept verbatim.
  const ts = new Date().toISOString().replace(/[-:]/g, "").replace(/\..+/, "");
  const sid = `freeform_${ts}`;

  // Register in state.scenarios so the stepper has a dot for it. Mark
  // active immediately. Initialize an empty bucket so handleEvent's
  // run_complete handler doesn't have to lazy-init.
  if (!state.scenarios.some((s) => s.id === sid)) {
    state.scenarios.push({
      id: sid, title, category: "freeform", difficulty: "medium", prompt,
    });
  }
  state.scenarioResults[sid] = {};  // model-keyed
  buildStepper();
  setStepStatus(sid, "active");

  runStartTime = Date.now();
  state._pollEventCount = 0;
  state._pollDoneRuns = 0;
  state._seenRunKeys = new Set();
  state._freeformActive = true;
  if (pollIntervalId) clearInterval(pollIntervalId);
  pollIntervalId = setInterval(pollForCompletion, 5000);

  $("setup-view").hidden = true;
  $("progress-view").hidden = false;
  setStepperVisible(true);  // freeform run starting → stepper relevant
  state.cameFromReports = false;

  const body = {
    prompt,
    title,
    scenario_id: sid,
    runs_per_path: state.runsPerPath,
    models: state.models,
    max_turns: state.maxTurns,
    operator: "local user",
    org_name: "(local org)",
    timeout_s: 600,
  };

  const res = await fetch("/api/run/freeform", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json())?.error || ""; } catch (_) {}
    $("progress-text").textContent = "error starting run" + (detail ? ` — ${detail}` : "");
    setStepStatus(sid, "error");
    return;
  }

  await consumeSseStream(res, state.runsPerPath * 2 * state.models.length);

  // SSE stream closed; if the run finished cleanly the polling fallback
  // already hydrated state. As a belt-and-suspenders, hydrate now too.
  if ($("progress-view").hidden === false) {
    try {
      const r = await fetch("/api/reports/latest");
      if (r.ok) await hydrateSummaryFromBackend();
    } catch (_) {}
  }
}

// Reports & analytics page — fetch the list once, then re-render the
// table on filter/sort changes without refetching. KPIs come from the
// same endpoint so the tile strip stays in sync with whatever's filtered.
async function loadReports() {
  const tbody = $("reports-tbody");
  if (!tbody) return;
  tbody.replaceChildren();
  tbody.appendChild(
    el("tr", {}, el("td", { attrs: { colspan: "12" }, className: "muted reports-empty", text: "Loading…" })),
  );
  try {
    const res = await fetch("/api/reports?limit=100", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    state.reports = body.reports || [];
    state.reportsKpis = body.kpis || {};
    state.reportsSortKey = state.reportsSortKey || "started_at";
    state.reportsSortDir = state.reportsSortDir || "desc";
    // Populate the model filter from the actual report set.
    const sel = $("reports-filter-model");
    const models = Array.from(new Set(
      state.reports.flatMap((r) => r.models || (r.model ? [r.model] : []))
    )).sort();
    sel.replaceChildren(el("option", { attrs: { value: "" }, text: "All" }));
    for (const m of models) {
      sel.appendChild(el("option", { attrs: { value: m }, text: m }));
    }
    renderReportsKpis();
    renderReportsTable();
  } catch (e) {
    tbody.replaceChildren(
      el("tr", {}, el("td", { attrs: { colspan: "12" }, className: "muted reports-empty",
        text: "Failed to load reports: " + e.message })),
    );
  }
}

function renderReportsKpis() {
  const k = state.reportsKpis || {};
  $("kpi-total-runs").textContent = (k.total_runs ?? 0).toString();
  $("kpi-native-cost").textContent = "$" + (k.total_native_cost ?? 0).toFixed(2);
  $("kpi-mcp-cost").textContent = "$" + (k.total_mcp_cost ?? 0).toFixed(2);
  $("kpi-avg-ratio").textContent = k.avg_ratio == null ? "—" : k.avg_ratio.toFixed(2) + "×";
}

function renderReportsTable() {
  const tbody = $("reports-tbody");
  if (!tbody) return;
  const kindFilter = $("reports-filter-kind").value;
  const modelFilter = $("reports-filter-model").value;
  const search = ($("reports-filter-search").value || "").trim().toLowerCase();
  const sortKey = state.reportsSortKey || "started_at";
  const sortDir = state.reportsSortDir || "desc";

  let rows = (state.reports || []).filter((r) => {
    if (kindFilter && r.kind !== kindFilter) return false;
    if (modelFilter) {
      const ms = r.models || (r.model ? [r.model] : []);
      if (!ms.includes(modelFilter)) return false;
    }
    if (search) {
      const ms = r.models?.length ? r.models : (r.model ? [r.model] : []);
      const hay = [r.name, ...ms, r.operator, r.org_name]
        .filter(Boolean).join(" ").toLowerCase();
      if (!hay.includes(search)) return false;
    }
    return true;
  });
  rows.sort((a, b) => {
    let av = a[sortKey], bv = b[sortKey];
    // String compare for non-numeric columns; nulls last.
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === "number" && typeof bv === "number") {
      return sortDir === "asc" ? av - bv : bv - av;
    }
    av = String(av); bv = String(bv);
    return sortDir === "asc" ? av.localeCompare(bv) : bv.localeCompare(av);
  });

  tbody.replaceChildren();
  if (rows.length === 0) {
    tbody.appendChild(el("tr", {},
      el("td", { attrs: { colspan: "12" }, className: "muted reports-empty",
        text: "No reports match these filters." })));
    return;
  }

  for (const r of rows) {
    const tr = document.createElement("tr");
    tr.appendChild(td(formatReportTime(r.started_at)));
    tr.appendChild(td(r.kind ? kindPill(r.kind) : "—"));
    const ms = r.models || (r.model ? [r.model] : []);
    const modelsText = ms.slice(0, 2).join(", ") +
      (ms.length > 2 ? `, +${ms.length - 2}` : "");
    tr.appendChild(td(modelsText || "—", "col-models"));
    tr.appendChild(td(String(r.scenario_count || 0), "col-num"));
    tr.appendChild(td(String(r.runs_per_path || 0), "col-num"));
    tr.appendChild(td("$" + (r.native_cost || 0).toFixed(3), "col-num"));
    tr.appendChild(td("$" + (r.mcp_cost || 0).toFixed(3), "col-num"));
    const ratioCell = document.createElement("td");
    ratioCell.className = "col-num";
    if (r.mcp_native_ratio == null) {
      ratioCell.textContent = "—";
    } else {
      ratioCell.textContent = r.mcp_native_ratio.toFixed(2) + "×";
      ratioCell.classList.add(r.mcp_native_ratio > 1 ? "ratio-native-wins" : "ratio-mcp-wins");
    }
    tr.appendChild(ratioCell);
    // Tier A: combined success rate (native + mcp) and aggregate cache hit %.
    const okCount = (r.native_success || 0) + (r.mcp_success || 0);
    const totalCount = (r.native_total || 0) + (r.mcp_total || 0);
    tr.appendChild(td(totalCount > 0 ? `${okCount}/${totalCount}` : "—", "col-num"));
    const totalIn = (r.native_cache_hit_ratio || 0) * (r.native_total || 1)
      + (r.mcp_cache_hit_ratio || 0) * (r.mcp_total || 1);
    const totalRunsForCache = (r.native_total || 0) + (r.mcp_total || 0);
    const cachePct = totalRunsForCache > 0
      ? ((totalIn / totalRunsForCache) * 100).toFixed(0) + "%"
      : "—";
    tr.appendChild(td(cachePct, "col-num"));
    tr.appendChild(reportActionsCell(r));

    // Tier D: per-row Compare button. The state machine has three modes:
    //   1. compareSelected null     → buttons read "Compare". Clicking sets A.
    //   2. compareSelected.a_id = X → row X reads "✓ Cancel"; all others read "vs A".
    //                                  Clicking "vs A" navigates to /compare?a=X&b=current.
    //                                  Clicking "✓ Cancel" clears the selection.
    const cmpTd = document.createElement("td");
    cmpTd.className = "col-cmp";
    const cmpBtn = document.createElement("button");
    cmpBtn.type = "button";
    cmpBtn.className = "secondary cmp-btn";
    const isA = state.compareSelected?.a_id === r.name;
    const hasA = !!state.compareSelected?.a_id;
    cmpBtn.textContent = isA ? "✓ Cancel" : (hasA ? "vs A" : "Compare");
    cmpBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (isA) {
        state.compareSelected = null;
        renderReportsTable();
      } else if (hasA) {
        window.location.href = `/compare?a=${encodeURIComponent(state.compareSelected.a_id)}&b=${encodeURIComponent(r.name)}`;
      } else {
        state.compareSelected = { a_id: r.name };
        renderReportsTable();
      }
    });
    cmpTd.appendChild(cmpBtn);
    tr.appendChild(cmpTd);

    // Click anywhere in the row except the actions cell → load the report.
    tr.addEventListener("click", (e) => {
      if (e.target.closest(".reports-actions-cell")) return;
      if (e.target.closest(".col-cmp")) return;
      loadReportById(r.name);
    });
    tbody.appendChild(tr);
  }
}

function td(content, cls) {
  const t = document.createElement("td");
  if (cls) t.className = cls;
  if (content == null) {
    t.textContent = "—";
  } else if (content instanceof Node) {
    t.appendChild(content);
  } else {
    t.textContent = String(content);
  }
  return t;
}

function kindPill(kind) {
  return el("span", { className: "kind-pill kind-" + kind, text: kind });
}

function formatReportTime(iso) {
  if (!iso) return "—";
  try {
    const dt = new Date(iso);
    const month = dt.toLocaleString("en-US", { month: "short" });
    const day = dt.getDate();
    const time = dt.toTimeString().slice(0, 5);
    return `${month} ${day} · ${time}`;
  } catch (_) { return iso; }
}

function reportActionsCell(report) {
  const cell = document.createElement("td");
  cell.className = "reports-actions-cell";
  const btn = document.createElement("button");
  btn.className = "reports-actions-btn";
  btn.textContent = "Actions ▾";
  cell.appendChild(btn);

  let menu = null;
  function closeMenu() {
    if (menu) { menu.remove(); menu = null; }
    document.removeEventListener("click", onDocClick);
  }
  function onDocClick(e) {
    if (menu && !cell.contains(e.target)) closeMenu();
  }
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (menu) { closeMenu(); return; }
    menu = document.createElement("div");
    menu.className = "reports-actions-menu";
    const open = document.createElement("button");
    open.textContent = "Open in app";
    open.addEventListener("click", (ev) => { ev.stopPropagation(); closeMenu(); loadReportById(report.name); });
    menu.appendChild(open);
    const dlMd = document.createElement("a");
    dlMd.textContent = "Download Markdown";
    dlMd.href = `/api/reports/${encodeURIComponent(report.name)}/markdown`;
    dlMd.setAttribute("download", report.name + ".md");
    dlMd.addEventListener("click", () => closeMenu());
    menu.appendChild(dlMd);
    const dlPdf = document.createElement("button");
    dlPdf.textContent = "Download PDF";
    dlPdf.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      closeMenu();
      // PDF is rendered client-side via window.print. To make sure the
      // print captures *this* report, load it into the SPA first, then
      // export. exportPdf already iterates every scenario tab + the summary.
      await loadReportById(report.name);
      // Give the SPA a tick to render before opening the print dialog.
      setTimeout(() => exportPdf(), 200);
    });
    menu.appendChild(dlPdf);
    cell.appendChild(menu);
    document.addEventListener("click", onDocClick);
  });
  return cell;
}

async function loadReportById(reportId) {
  try {
    const res = await fetch(apiPath(`/api/reports/${encodeURIComponent(reportId)}/data`), { cache: "no-store" });
    const body = await res.json();
    if (!res.ok) {
      alert("Could not load report: " + (body.error || res.status));
      return;
    }
    state.cameFromReports = true;
    state.activeReportId = reportId;
    openLoadedReport(body);
  } catch (e) {
    alert("Could not load report: " + e.message);
  }
}

function openLoadedReport(summary) {
  // Same routing as the old load button: register the scenario ids
  // into state.scenarios, then go to the summary view (or the single
  // scenario detail if it's a one-off).
  registerLoadedScenarios(summary).then(() => {
    if (summary.scenario_count === 1 && summary.scenario_ids?.length === 1) {
      showScenario(summary.scenario_ids[0]);
    } else {
      showSummary();
    }
  });
}

function goBackToReports() {
  state.cameFromReports = false;
  state.active = "setup";
  renderSetup("reports");
  loadReports();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// After a load, register every scenario id from the report into state.scenarios
// so the stepper has tabs for them, and pre-fetch each scenario's runs from the
// freshly hydrated _current_run.
async function registerLoadedScenarios(summary) {
  const ids = summary.scenario_ids || [];
  if (!ids.length) return;

  // For any scenario we don't already have meta for, synthesize a minimal
  // entry. We'll get title/category/difficulty from the catalog YAMLs if they
  // match, otherwise the report's appendix shows the scenario_id only.
  const knownIds = new Set(state.scenarios.map((s) => s.id));
  for (const sid of ids) {
    if (knownIds.has(sid)) continue;
    state.scenarios.push({
      id: sid,
      title: prettyScenarioTitle(sid),
      category: sid.startsWith("freeform_") ? "freeform" : "loaded",
      difficulty: "medium",
      prompt: "",
    });
  }
  buildStepper();
  // All loaded scenarios are "done" by definition — they've already run.
  for (const sid of ids) setStepStatus(sid, "done");

  // Pull the full BenchmarkResult so each scenario detail has run buckets.
  try {
    const res = await fetch(apiPath("/api/reports/latest/data"));
    if (res.ok) {
      const data = await res.json();
      hydrateScenarioRunsFromData(data);
      state.reportPath = summary.source || null;
    }
  } catch (_) { /* silent */ }
}

function prettyScenarioTitle(sid) {
  // s01_soql_top_accounts → "soql top accounts"
  const tail = sid.split("_").slice(1).join(" ");
  return tail ? tail.replace(/\b\w/g, (c) => c.toUpperCase()) : sid;
}

function handleEvent(ev, onRunComplete) {
  switch (ev.kind) {
    case "benchmark_start":
      $("progress-text").textContent = "starting…";
      break;
    case "scenario_start":
      setStepStatus(ev.scenario_id, "active");
      $("progress-text").textContent = `Scenario ${ev.scenario_id} · starting`;
      break;
    case "run_start":
      $("progress-text").textContent =
        `Scenario ${ev.scenario_id} · ${ev.path} run ${ev.run_index}/${ev.total_runs}`;
      break;
    case "run_complete": {
      // Dedupe across the SSE stream and the /api/run/status poller —
      // both feed handleEvent and would otherwise double-push the same
      // run into the bucket. The natural id is (scenario, model, path, index).
      const evKey = `${ev.scenario_id}|${ev.model || ""}|${ev.path}|${ev.run_index}`;
      state._seenRunKeys = state._seenRunKeys || new Set();
      if (state._seenRunKeys.has(evKey)) break;
      state._seenRunKeys.add(evKey);
      const m = ev.model || state.model;
      // Lazy-init the cube — for freeform runs the scenario isn't in
      // state.scenarios until we hydrate it from the report.
      state.scenarioResults[ev.scenario_id] ??= {};
      state.scenarioResults[ev.scenario_id][m] ??= { native: [], mcp: [] };
      state.scenarioResults[ev.scenario_id][m][ev.path].push(ev.run_result);
      if (!state.activeModel) state.activeModel = m;  // first run sets default
      onRunComplete();
      if (state.active === ev.scenario_id) renderScenario(ev.scenario_id);
      break;
    }
    case "scenario_complete":
      setStepStatus(ev.scenario_id, "done");
      break;
    case "report_written":
      state.reportPath = ev.path;
      break;
    case "benchmark_complete":
      stopPolling();
      $("progress-view").hidden = true;
      // Freeform runs land on the scenario detail page directly; full
      // benchmark runs land on the summary.
      if (state._freeformActive) {
        hydrateFreeformAndShow();
      } else {
        showSummary();
      }
      break;
  }
}

// Render an editorial headline: pull out "X.Y×" multipliers (mono numerals)
// and "~NN% less/more/cheaper" phrases (italic) so the serif sentence has the
// quantitative emphasis the editorial design calls for.
function renderHeadline(targetEl, text) {
  targetEl.replaceChildren();
  if (!text) { targetEl.textContent = "—"; return; }
  // Match "1.5×" or "~34% less"/"~12% cheaper". Run a non-global match in a
  // loop so we walk the string once without using regex .exec() (which
  // confuses some lint hooks into thinking we're spawning a process).
  const matcher = /(\d+\.\d×|~\d+%\s+(?:less|more|cheaper))/;
  let remaining = text;
  while (remaining.length) {
    const found = remaining.match(matcher);
    if (!found) {
      targetEl.appendChild(document.createTextNode(remaining));
      break;
    }
    if (found.index > 0) {
      targetEl.appendChild(document.createTextNode(remaining.slice(0, found.index)));
    }
    const phrase = found[0];
    if (phrase.includes("×")) {
      targetEl.appendChild(el("span", { className: "num", text: phrase }));
    } else {
      targetEl.appendChild(el("em", { text: phrase }));
    }
    remaining = remaining.slice(found.index + phrase.length);
  }
}

// Median across ALL runs (success + failure). Failed runs spend tokens too,
// so we don't hide their cost from the per-scenario panel — the success rate
// badge already tells the user how many of the runs actually completed.
function medianOf(list, key) {
  const all = (list || []).map((r) => r[key]).sort((a, b) => a - b);
  if (!all.length) return 0;
  return all[Math.floor(all.length / 2)];
}

function totalInput(r) {
  return (r.input_tokens || 0) + (r.cache_read_input_tokens || 0) + (r.cache_creation_input_tokens || 0);
}

function medianTotalInput(list) {
  const all = (list || []).map(totalInput).sort((a, b) => a - b);
  if (!all.length) return 0;
  return all[Math.floor(all.length / 2)];
}

function showScenario(sid) {
  state.active = sid;
  $("setup-view").hidden = true;
  $("summary-view").hidden = true;
  $("scenario-view").hidden = false;
  $("scenario-back-link").hidden = !state.cameFromReports;
  setStepperVisible(true);  // viewing a scenario → stepper relevant
  renderScenario(sid);
}

function renderModelPills(containerId, models, activeModel, onSelect) {
  const c = document.getElementById(containerId);
  if (!c) return;
  c.replaceChildren();
  if (!models || models.length <= 1) { c.hidden = true; return; }
  c.hidden = false;
  for (const m of models) {
    const b = el("button", {
      className: "model-pill" + (m === activeModel ? " active" : ""),
      text: m,
      attrs: { type: "button" },
    });
    b.addEventListener("click", () => onSelect(m));
    c.appendChild(b);
  }
}

async function renderSparklines(scenarioId, model) {
  const row = $("sv-sparklines");
  if (!row) return;
  // Hide sparklines in guest (share) mode — they would query /api/history
  // and could leak names of other reports the recipient shouldn't see.
  if (state.guestMode) { row.hidden = true; return; }
  if (!scenarioId || !model) { row.hidden = true; return; }
  const metrics = ["cost", "cache", "success", "p95_duration"];
  const results = await Promise.all(metrics.map((m) =>
    fetch(`/api/history?scenario_id=${encodeURIComponent(scenarioId)}&model=${encodeURIComponent(model)}&metric=${m}`)
      .then((r) => r.ok ? r.json() : { points: [] })
      .catch(() => ({ points: [] }))
  ));

  const anyHasTrend = results.some((r) => r.points.length >= 2);
  if (!anyHasTrend) { row.hidden = true; return; }
  row.hidden = false;

  results.forEach((r, i) => {
    const cell = row.querySelector(`[data-metric="${metrics[i]}"]`);
    if (!cell) return;
    const svg = cell.querySelector("svg");
    const val = cell.querySelector(".sparkline-value");
    svg.replaceChildren();
    if (r.points.length < 2) {
      val.textContent = "(needs ≥2 reports)";
      return;
    }
    const seriesN = r.points.map((p) => p.native);
    const seriesM = r.points.map((p) => p.mcp);
    const max = Math.max(...seriesN, ...seriesM) || 1;
    // Stroke-width 1.5 means half the line sits below y=H if we draw at the
    // bottom edge. Reserve 1px of headroom on both edges so flat-zero series
    // (e.g. cache hit = 0) and flat-max series stay fully visible.
    const W = 80, H = 30, pad = 1.5;
    const ns = "http://www.w3.org/2000/svg";
    const mkPath = (vs, color) => {
      const usable = H - 2 * pad;
      const d = vs.map((v, j) =>
        `${j === 0 ? "M" : "L"} ${(j / Math.max(vs.length - 1, 1)) * W} ${(H - pad) - (v / max) * usable}`
      ).join(" ");
      const p = document.createElementNS(ns, "path");
      p.setAttribute("d", d);
      p.setAttribute("fill", "none");
      p.setAttribute("stroke", color);
      p.setAttribute("stroke-width", "1.5");
      return p;
    };
    svg.appendChild(mkPath(seriesN, "var(--signal-vivid, #1a73e8)"));
    svg.appendChild(mkPath(seriesM, "var(--counter-vivid, #d32f2f)"));
    const last = r.points[r.points.length - 1];
    const fmt = metrics[i] === "cost"
      ? `$${last.native.toFixed(4)} / $${last.mcp.toFixed(4)}`
      : metrics[i] === "cache" || metrics[i] === "success"
      ? `${(last.native * 100).toFixed(0)}% / ${(last.mcp * 100).toFixed(0)}%`
      : `${last.native.toFixed(0)} / ${last.mcp.toFixed(0)} ms`;
    val.textContent = fmt;
  });
}

function renderScenario(sid) {
  const scenario = state.scenarios.find((s) => s.id === sid);
  const bucket = activeBucket(sid);
  const models = Object.keys(state.scenarioResults[sid] || {});
  renderModelPills("sv-model-pills", models, state.activeModel, (m) => {
    state.activeModel = m;
    renderScenario(sid);
  });

  // Title: italicize the part after the colon/dash for editorial feel.
  const titleEl = $("sv-title");
  titleEl.replaceChildren();
  const t = scenario.title || "";
  const splitMatch = t.match(/^(.*?)(?:\s+(by|of|for|across|with|on|in)\s+)(.*)$/i);
  if (splitMatch) {
    titleEl.appendChild(document.createTextNode(splitMatch[1] + " "));
    const em = el("em", { text: `${splitMatch[2]} ${splitMatch[3]}` });
    titleEl.appendChild(em);
  } else {
    titleEl.textContent = t;
  }

  // Breadcrumb meta: scenario_id (bold), category pill, difficulty pill, runs.
  const metaEl = $("sv-meta");
  metaEl.replaceChildren();
  metaEl.appendChild(el("span", { className: "breadcrumb-id", text: scenario.id }));
  if (scenario.category) {
    metaEl.appendChild(el("span", { className: "crumb-tag", text: scenario.category }));
  }
  if (scenario.difficulty) {
    const dc = (scenario.difficulty || "").toLowerCase();
    metaEl.appendChild(el("span", {
      className: "crumb-tag" + (dc ? ` ${dc}` : ""),
      text: scenario.difficulty,
    }));
  }
  metaEl.appendChild(el("span", {
    text: `${state.runsPerPath} run${state.runsPerPath === 1 ? "" : "s"} per path`,
  }));

  $("sv-prompt").textContent = scenario.prompt;

  fillPanel("native", bucket.native);
  fillPanel("mcp", bucket.mcp);
  fillPerRunBreakdown(bucket);

  const nativeMed = medianOf(bucket.native, "total_cost_usd");
  const mcpMed = medianOf(bucket.mcp, "total_cost_usd");
  const nativeInput = medianTotalInput(bucket.native);
  const mcpInput = medianTotalInput(bucket.mcp);

  // Headline (above chart) + verdict bar (above panels)
  const headlineEl = $("sv-headline");
  headlineEl.replaceChildren();
  headlineEl.classList.remove("mcp");

  const verdict = $("sv-verdict");

  if (nativeMed && mcpMed) {
    const mult = mcpMed / nativeMed;
    verdict.hidden = false;
    verdict.classList.remove("mcp-win", "tied");

    const eyebrow = $("sv-verdict-eyebrow");
    const prefix = $("sv-verdict-prefix");
    const suffix = $("sv-verdict-suffix");
    const multSpan = $("sv-verdict-multiplier");
    const savingsEl = $("sv-verdict-savings");
    const tokEl = $("sv-verdict-tok-delta");

    let displayMult;
    if (mult > 1.05) {
      // Native wins
      headlineEl.appendChild(document.createTextNode("Native was "));
      headlineEl.appendChild(el("em", { text: `${mult.toFixed(1)}× cheaper` }));
      headlineEl.appendChild(document.createTextNode(" on this scenario."));

      eyebrow.textContent = `Verdict · ${sid || "scenario"}`;
      prefix.textContent = "Native is";
      suffix.textContent = "cheaper here.";
      displayMult = mult;
      verdict.classList.remove("mcp-win");
      verdict.classList.remove("glow-mcp");
      verdict.classList.add("glow-native");
    } else if (mult < 0.95) {
      // MCP wins
      headlineEl.classList.add("mcp");
      headlineEl.appendChild(document.createTextNode("MCP was "));
      headlineEl.appendChild(el("em", { text: `${(1 / mult).toFixed(1)}× cheaper` }));
      headlineEl.appendChild(document.createTextNode(" on this scenario."));

      eyebrow.textContent = `Verdict · ${sid || "scenario"}`;
      prefix.textContent = "MCP is";
      suffix.textContent = "cheaper here.";
      displayMult = 1 / mult;
      verdict.classList.add("mcp-win");
      verdict.classList.remove("glow-native");
      verdict.classList.add("glow-mcp");
    } else {
      headlineEl.textContent = "Effectively tied on token cost.";

      eyebrow.textContent = `Verdict · ${sid || "scenario"}`;
      prefix.textContent = "Costs are";
      suffix.textContent = "essentially equal.";
      displayMult = 1.0;
      verdict.classList.add("tied");
    }

    // Animated counter on multiplier (1.0× → displayMult).
    multSpan.textContent = `${displayMult.toFixed(1)}×`;
    if (window.tokenmeter && window.tokenmeter.motion) {
      window.tokenmeter.motion.animateCounter(multSpan, 1.0, displayMult, {
        duration: 600,
        format: (v) => `${v.toFixed(1)}×`,
      });
    }

    // Callouts: monthly savings @ 10k runs, token delta.
    const cheaper = Math.min(nativeMed, mcpMed);
    const dearer = Math.max(nativeMed, mcpMed);
    const savePerRun = dearer - cheaper;
    savingsEl.textContent = `$${(savePerRun * 10000).toFixed(0).replace(/\B(?=(\d{3})+(?!\d))/g, ",")}`;
    const tokDelta = mcpInput - nativeInput;
    tokEl.textContent = (tokDelta >= 0 ? "−" : "+") + Math.abs(tokDelta).toLocaleString() + " tok";
  } else {
    verdict.hidden = true;
    headlineEl.textContent = "—";
  }

  renderChartRows(bucket);
  loadAndRenderTrace(sid);

  // Best-effort: pull regression history sparklines for this scenario+model.
  // Don't block the page render if /api/history is slow.
  renderSparklines(sid, state.activeModel || defaultModel(Object.keys(state.scenarioResults[sid] || {})));

  // Wire the scenario page's Download report button. The Export PDF button
  // is wired once at init() — it doesn't need per-render updating.
  const sDl = $("scenario-download-report");
  if (sDl) {
    if (state.reportPath) {
      sDl.href = "/api/reports/latest";
      sDl.setAttribute("download", state.reportPath.split("/").pop());
      sDl.removeAttribute("aria-disabled");
    } else {
      sDl.href = "/api/reports/latest";
      sDl.setAttribute("download", "benchmark.md");
    }
  }
}

// ─── Tier A helpers ─────────────────────────────────────────────────────

function formatCacheHit(runs) {
  // Aggregate cache_read_input_tokens / total_input_tokens across all
  // runs the panel summarizes. Same formula the server's
  // _cache_hit_ratio uses, kept in sync so the panel matches the
  // per-run breakdown rows.
  let totalIn = 0, cacheRead = 0;
  for (const r of runs) {
    totalIn += (r.input_tokens || 0) + (r.cache_read_input_tokens || 0)
      + (r.cache_creation_input_tokens || 0);
    cacheRead += (r.cache_read_input_tokens || 0);
  }
  if (totalIn <= 0) return "—";
  const pct = (cacheRead / totalIn) * 100;
  return pct.toFixed(0) + "%";
}

function formatDuration(ms) {
  if (!ms) return "—";
  if (ms < 1000) return ms + " ms";
  return (ms / 1000).toFixed(1) + " s";
}

function classifyOutcome(run) {
  // Mirror of server-side outcomes.classify, kept short. Used by the
  // per-run breakdown table so each row shows why a failed run failed.
  if (run.succeeded) return "succeeded";
  const err = (run.error || "").toLowerCase();
  if (err.includes("mcp_init_failed")) return "mcp_init_failed";
  if (err.includes("max_turns") || err.includes("max turns")) return "max_turns";
  if (err.includes("inference error") || err.includes("inference call failed")
      || err.includes("mcp_unresolved_tool_use") || err.includes("anthropic")) {
    return "inference_error";
  }
  if (err.includes("http 401") || err.includes("http 403")
      || err.includes("invalid_scopes") || err.includes("unauthorized")) {
    return "tool_auth_error";
  }
  if (err.includes("no tool calls") || err.includes("model declined")) {
    return "no_tool_calls";
  }
  return "other";
}

function outcomeLabel(kind) {
  return ({
    succeeded: "succeeded",
    max_turns: "max turns",
    inference_error: "inference error",
    mcp_init_failed: "MCP init failed",
    no_tool_calls: "no tool calls",
    tool_auth_error: "auth error",
    other: "failed",
  })[kind] || kind;
}

function fillPerRunBreakdown(bucket) {
  // bucket = { native: RunResult[], mcp: RunResult[] }. Render every
  // run from both paths, native first then MCP, in arrival order.
  // Each run produces TWO rows: a summary row with a chevron toggle, and
  // an expansion row with replay context (tool I/O, error response, etc.).
  // Failed runs start expanded; successful runs start collapsed.
  const tbody = $("per-run-tbody");
  const card = $("per-run-card");
  if (!tbody || !card) return;
  const allRows = [
    ...bucket.native.map((r, i) => ({ r, idx: i + 1, label: "Native" })),
    ...bucket.mcp.map((r, i) => ({ r, idx: i + 1, label: "MCP" })),
  ];
  if (allRows.length === 0) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  tbody.replaceChildren();
  for (const { r, idx, label } of allRows) {
    const tr = document.createElement("tr");
    tr.className = "per-run-row";
    const kind = classifyOutcome(r);

    // Chevron column — click toggles the expansion row below.
    const chevTd = document.createElement("td");
    chevTd.className = "col-chev";
    const chev = document.createElement("button");
    chev.type = "button";
    chev.className = "chev-toggle";
    chev.setAttribute("aria-label", "Show details");
    tr.appendChild(chevTd);
    chevTd.appendChild(chev);

    tr.appendChild(elTd(`${idx}`, "col-num"));
    tr.appendChild(elTd(label, "col-path-" + label.toLowerCase()));
    const pill = el("span", { className: "outcome-pill outcome-" + kind, text: outcomeLabel(kind) });
    const tdOutcome = document.createElement("td");
    tdOutcome.appendChild(pill);
    tr.appendChild(tdOutcome);
    tr.appendChild(elTd(String(r.num_turns || 0), "col-num"));
    const totalIn = (r.input_tokens || 0) + (r.cache_read_input_tokens || 0)
      + (r.cache_creation_input_tokens || 0);
    tr.appendChild(elTd(totalIn.toLocaleString(), "col-num"));
    tr.appendChild(elTd((r.output_tokens || 0).toLocaleString(), "col-num"));
    const cachePct = totalIn > 0
      ? ((r.cache_read_input_tokens || 0) / totalIn * 100).toFixed(0) + "%"
      : "—";
    tr.appendChild(elTd(cachePct, "col-num"));
    tr.appendChild(elTd(formatDuration(r.duration_ms), "col-num"));
    tr.appendChild(elTd("$" + (r.total_cost_usd || 0).toFixed(4), "col-num"));
    tbody.appendChild(tr);

    // Expansion row.
    const detailTr = document.createElement("tr");
    detailTr.className = "per-run-detail";
    const detailTd = document.createElement("td");
    detailTd.colSpan = 10;  // chev + 9 data columns
    renderRunReplay(detailTd, r);
    detailTr.appendChild(detailTd);
    detailTr.hidden = (kind === "succeeded");  // failed: open by default
    chev.textContent = detailTr.hidden ? "▸" : "▾";
    tbody.appendChild(detailTr);

    chev.addEventListener("click", () => {
      detailTr.hidden = !detailTr.hidden;
      chev.textContent = detailTr.hidden ? "▸" : "▾";
    });
  }
}

function renderRunReplay(container, r) {
  // Render the per-run replay block: error_response (MCP HTTP errors),
  // inference_error (Anthropic API errors), runner_traceback (last-resort),
  // and tool_call_details (every tool call's input/output, for any run).
  container.replaceChildren();
  let any = false;
  if (r.error_response) {
    any = true;
    const lines = [
      `HTTP ${r.error_response.status_code}`,
      `Body: ${r.error_response.body_excerpt || "(empty)"}`,
    ];
    const headerEntries = Object.entries(r.error_response.headers || {});
    if (headerEntries.length) {
      lines.push("Headers: " + headerEntries
        .map(([k, v]) => `${k}=${v}`).join(", "));
    }
    container.appendChild(buildReplayBlock("MCP gateway error", lines));
  }
  if (r.inference_error) {
    any = true;
    const lines = [`${r.inference_error.type}: ${r.inference_error.message}`];
    if (r.inference_error.body_excerpt) {
      lines.push(`Body: ${r.inference_error.body_excerpt}`);
    }
    container.appendChild(buildReplayBlock("Inference error", lines));
  }
  if (r.runner_traceback) {
    any = true;
    container.appendChild(buildReplayBlock("Runner traceback",
      [r.runner_traceback], { mono: true }));
  }
  const tcd = r.tool_call_details || [];
  if (tcd.length > 0) {
    any = true;
    const block = document.createElement("div");
    block.className = "replay-block";
    const h = document.createElement("h4");
    h.textContent = `Tool calls (${tcd.length})`;
    block.appendChild(h);
    for (let i = 0; i < tcd.length; i++) {
      const d = tcd[i];
      const item = document.createElement("div");
      item.className = "tool-call-item";
      const head = document.createElement("div");
      head.className = "tool-call-head";
      head.textContent = `${i + 1}. ${d.name}`;
      item.appendChild(head);
      const inputPre = document.createElement("pre");
      inputPre.className = "tool-io";
      inputPre.textContent = d.input_excerpt;
      item.appendChild(inputPre);
      const arrow = document.createElement("div");
      arrow.className = "tool-arrow"; arrow.textContent = "→";
      item.appendChild(arrow);
      const outputPre = document.createElement("pre");
      outputPre.className = "tool-io";
      outputPre.textContent = d.output_excerpt;
      item.appendChild(outputPre);
      if (d.error) {
        const err = document.createElement("div");
        err.className = "tool-call-error";
        err.textContent = "Tool error: " + d.error;
        item.appendChild(err);
      }
      block.appendChild(item);
    }
    container.appendChild(block);
  }
  if (!any) {
    const empty = document.createElement("div");
    empty.className = "muted";
    empty.textContent = "(no detail captured for this run)";
    container.appendChild(empty);
  }
}

function buildReplayBlock(title, lines, opts = {}) {
  const block = document.createElement("div");
  block.className = "replay-block";
  const h = document.createElement("h4");
  h.textContent = title;
  block.appendChild(h);
  for (const line of lines) {
    const p = document.createElement(opts.mono ? "pre" : "div");
    p.className = opts.mono ? "tool-io" : "replay-line";
    p.textContent = line;
    block.appendChild(p);
  }
  return block;
}

function elTd(text, cls) {
  const t = document.createElement("td");
  if (cls) t.className = cls;
  t.textContent = text == null ? "—" : text;
  return t;
}

function formatPct(ratio) {
  if (ratio == null || isNaN(ratio)) return "—";
  return (ratio * 100).toFixed(0) + "%";
}

function renderOutcomeBar(elementId, counts) {
  // Render a stacked horizontal bar where each segment is a fraction
  // of total runs colored by outcome. Empty counts → nothing rendered.
  const bar = $(elementId);
  if (!bar) return;
  bar.replaceChildren();
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  if (total === 0) {
    bar.appendChild(el("span", { className: "muted", text: "(no runs)" }));
    return;
  }
  // Order: succeeded first (it's the good one), then failures.
  const order = ["succeeded", "max_turns", "inference_error",
    "tool_auth_error", "no_tool_calls", "mcp_init_failed", "other"];
  for (const kind of order) {
    const n = counts[kind] || 0;
    if (n === 0) continue;
    const pct = (n / total) * 100;
    const seg = el("div", {
      className: "outcome-seg outcome-" + kind,
      attrs: {
        style: `width: ${pct.toFixed(2)}%`,
        title: `${outcomeLabel(kind)}: ${n} (${pct.toFixed(0)}%)`,
      },
      text: `${n}`,
    });
    bar.appendChild(seg);
  }
}

// ─── End Tier A helpers ────────────────────────────────────────────────

function fillPanel(pathName, runs) {
  const pre = `sv-${pathName}`;
  const med = {
    input: medianTotalInput(runs),
    cost: medianOf(runs, "total_cost_usd"),
    turns: medianOf(runs, "num_turns"),
  };
  const done = runs.length;
  const ok = runs.filter((r) => r.succeeded).length;
  const status = $(`${pre}-status`);
  if (done === 0) {
    status.className = "panel-status";
    status.textContent = "—";
  } else if (done < state.runsPerPath) {
    status.className = "panel-status running";
    status.textContent = `Running ${done}/${state.runsPerPath}`;
  } else {
    status.className = ok === done ? "panel-status done" : "panel-status error";
    status.textContent = `${ok}/${done} runs succeeded`;
  }
  $(`${pre}-input`).textContent = med.input.toLocaleString();
  $(`${pre}-turns`).textContent = med.turns || "—";
  $(`${pre}-cost`).textContent = med.cost ? `$${med.cost.toFixed(3)}` : "—";

  // Tier A: cache-hit ratio + wall-clock (shown as secondary panel-meta).
  const cacheEl = $(`${pre}-cache`);
  const durEl = $(`${pre}-duration`);
  if (cacheEl) cacheEl.textContent = formatCacheHit(runs);
  if (durEl) durEl.textContent = formatDuration(medianOf(runs, "duration_ms"));

  const tools = $(`${pre}-tools`);
  tools.replaceChildren();
  const firstOk = runs.find((r) => r.succeeded && r.tool_calls?.length);
  for (const t of firstOk?.tool_calls || []) {
    tools.appendChild(el("li", { text: t }));
  }
  // After the basic tool name list renders, asynchronously enrich each entry
  // with the first ~80 chars of that call's input so users see WHAT the tool
  // did (e.g., "Bash: sf data query 'SELECT ...'") not just the tool name.
  // This pulls from the same /trace data the trace card uses.
  enrichToolList(pathName).catch(() => {});
}

async function enrichToolList(pathName) {
  if (state.active === "setup" || state.active === "summary") return;
  // Trace endpoint only has data AFTER a run completes. Don't spam 404s
  // during an active run — polling will re-render the panel anyway.
  if (runStartTime && pollIntervalId) return;
  const sid = state.active;
  const tools = $(`sv-${pathName}-tools`);
  if (!tools || !tools.children.length) return;

  let traceData = state._traceCache?.[sid];
  if (!traceData) {
    try {
      const r = await fetch(apiPath(`/api/scenarios/${encodeURIComponent(sid)}/trace`));
      if (!r.ok) return;
      traceData = await r.json();
      state._traceCache = state._traceCache || {};
      state._traceCache[sid] = traceData;
    } catch (_) { return; }
  }

  // Pick the matching path's first successful trace, fall back to first.
  const traces = pathName === "native" ? traceData.native_traces : traceData.mcp_traces;
  const trace = traces.find((t) => t.succeeded) || traces[0];
  if (!trace) return;

  // Build a flat ordered list of (toolName, toolInput) from the trace.
  const calls = [];
  for (const turn of trace.turns || []) {
    for (let i = 0; i < (turn.tool_calls || []).length; i++) {
      calls.push({
        name: turn.tool_calls[i],
        input: turn.tool_inputs?.[i] || "",
      });
    }
  }

  // Re-render the list with name + summarized input
  tools.replaceChildren();
  for (const c of calls) {
    const summary = summarizeToolInput(c.name, c.input);
    const li = el("li");
    li.appendChild(el("strong", { text: c.name }));
    if (summary) {
      li.appendChild(document.createTextNode(": "));
      const code = el("code", { text: summary });
      li.appendChild(code);
    }
    tools.appendChild(li);
  }
}

function summarizeToolInput(name, inputJson) {
  if (!inputJson) return "";
  let parsed;
  try { parsed = JSON.parse(inputJson); } catch { return inputJson.slice(0, 100); }
  if (!parsed || typeof parsed !== "object") return String(parsed).slice(0, 100);

  // Bash: prefer description if short, else the command.
  if (name === "Bash") {
    const desc = parsed.description || "";
    const cmd = parsed.command || "";
    // Use the command (more informative than the description Claude wrote).
    return (cmd || desc).replace(/\s+/g, " ").slice(0, 120);
  }
  // SOQL/SQL queries
  if (parsed.q || parsed.query || parsed.sql) {
    return (parsed.q || parsed.query || parsed.sql).replace(/\s+/g, " ").slice(0, 120);
  }
  // Read tool: file_path
  if (parsed.file_path) {
    return parsed.file_path.split("/").slice(-2).join("/");
  }
  // MCP get_dc_metadata: entity name
  if (parsed.entityName || parsed.entityType) {
    const parts = [];
    if (parsed.entityType) parts.push(`type=${parsed.entityType}`);
    if (parsed.entityName) parts.push(`name=${parsed.entityName}`);
    return parts.join(" ");
  }
  // Generic: show first key:value pair
  const k = Object.keys(parsed)[0];
  if (k) return `${k}=${String(parsed[k]).slice(0, 80)}`;
  return "";
}

function renderChartRows(bucket) {
  // Custom HTML/CSS horizontal bar chart per the editorial mockup.
  // Each metric gets a row: [axis label, two stacked bars, delta % vs native].
  const root = $("sv-chart-rows");
  if (!root) return;
  root.replaceChildren();

  const metrics = [
    { axis: "Input tokens", n: medianTotalInput(bucket.native), m: medianTotalInput(bucket.mcp), fmt: (v) => v.toLocaleString() },
    { axis: "Output tokens", n: medianOf(bucket.native, "output_tokens"), m: medianOf(bucket.mcp, "output_tokens"), fmt: (v) => v.toLocaleString() },
    { axis: "Cost ($×1000)", n: medianOf(bucket.native, "total_cost_usd") * 1000, m: medianOf(bucket.mcp, "total_cost_usd") * 1000, fmt: (v) => v.toFixed(1) },
    { axis: "Turns", n: medianOf(bucket.native, "num_turns"), m: medianOf(bucket.mcp, "num_turns"), fmt: (v) => Math.round(v).toString() },
  ];

  for (const met of metrics) {
    const max = Math.max(met.n, met.m, 1);
    const nPct = Math.max((met.n / max) * 100, met.n > 0 ? 6 : 0);
    const mPct = Math.max((met.m / max) * 100, met.m > 0 ? 6 : 0);

    // Delta = (mcp - native) / native, expressed as % change in MCP vs Native.
    let deltaText = "—";
    let deltaClass = "delta";
    if (met.n > 0 && met.m > 0) {
      const pct = ((met.m - met.n) / met.n) * 100;
      if (Math.abs(pct) < 0.5) {
        deltaText = "±0";
        deltaClass += " flat";
      } else if (pct > 0) {
        deltaText = `+${pct.toFixed(1)}%`;
        deltaClass += " up";
      } else {
        deltaText = `−${Math.abs(pct).toFixed(1)}%`;  // Unicode minus
      }
    }

    const nativeBar = el("div", {
      className: "bar native",
      attrs: { style: `width: ${nPct.toFixed(1)}%` },
    }, el("span", { className: "bar-label", text: met.fmt(met.n) }));

    const mcpBar = el("div", {
      className: "bar mcp",
      attrs: { style: `width: ${mPct.toFixed(1)}%` },
    }, el("span", { className: "bar-label", text: met.fmt(met.m) }));

    const row = el("div", { className: "chart-row" },
      el("div", { className: "axis", text: met.axis }),
      el("div", { className: "bars" }, nativeBar, mcpBar),
      el("div", { className: deltaClass, text: deltaText }),
    );
    root.appendChild(row);
  }
}

async function loadAndRenderTrace(sid) {
  const card = $("trace-card");
  card.hidden = true;
  // Trace endpoint only has data AFTER a run completes. Skip while running.
  if (runStartTime && pollIntervalId) return;
  try {
    const res = await fetch(apiPath(`/api/scenarios/${encodeURIComponent(sid)}/trace`));
    if (!res.ok) return;
    const data = await res.json();
    if (!data || !data.native_traces) return;

    // Pick the first successful run from each side; fall back to first run.
    const pickRun = (traces) => {
      const succ = traces.find((t) => t.succeeded);
      return succ || traces[0] || null;
    };
    const nat = pickRun(data.native_traces);
    const mcp = pickRun(data.mcp_traces);
    if (!nat && !mcp) return;

    $("trace-explanation").textContent = data.explanation || "—";

    const tbody = $("trace-tbody");
    tbody.replaceChildren();

    // First row: init metadata (no Δ for the init row)
    const initRow = el("tr", {},
      el("td", { className: "turn-num", text: "init" }),
      el("td", { className: "meta-row" }, formatInit(nat)),
      el("td", { className: "meta-row" }, formatInit(mcp)),
      el("td", { className: "trace-delta" }),
    );
    tbody.appendChild(initRow);

    // Per-turn rows: align by turn_index
    const maxTurns = Math.max(
      nat ? nat.turns.length : 0,
      mcp ? mcp.turns.length : 0,
    );
    const turnDiffs = data.turn_diffs || [];
    for (let i = 0; i < maxTurns; i++) {
      const natTurn = nat?.turns[i];
      const mcpTurn = mcp?.turns[i];
      const tr = el("tr", {},
        el("td", { className: "turn-num", text: String(i + 1) }),
        el("td", {}, ...renderTurnCell(natTurn)),
        el("td", {}, ...renderTurnCell(mcpTurn)),
        el("td", { className: "trace-delta" }, ...renderDeltaCell(turnDiffs[i])),
      );
      tbody.appendChild(tr);
    }

    card.hidden = false;
  } catch (_) { /* silent */ }
}

function renderDeltaCell(diff) {
  // Build the Δ column for one turn. Empty when delta is below noise floor
  // (<100 tokens). Color: red if MCP > native (MCP burned more), green if
  // MCP < native (MCP saved). Reason chip below the number when set, with
  // a tooltip explaining the heuristic is a best guess.
  if (!diff || Math.abs(diff.total_delta) < 100) return [];
  const sign = diff.total_delta > 0 ? "+" : "−";
  const num = Math.abs(diff.total_delta).toLocaleString();
  const numEl = el("div", {
    className: "trace-delta-num " + (diff.total_delta > 0 ? "up" : "down"),
    text: sign + num,
  });
  const nodes = [numEl];
  if (diff.reason) {
    const chip = el("span", {
      className: "trace-reason-chip reason-" + diff.reason,
      text: diff.hint || diff.reason,
      attrs: { title: (diff.hint || diff.reason) + " — best guess based on token shape" },
    });
    nodes.push(chip);
  }
  return nodes;
}

function formatInit(trace) {
  if (!trace) return el("span", { text: "(no run)" });
  const toolCount = trace.init_tools.length;
  const mcpServers = trace.init_mcp_servers.length
    ? trace.init_mcp_servers.join(", ")
    : "none";
  return el("span", {
    text: `tools available at startup: ${toolCount} · MCP servers: ${mcpServers}`,
  });
}

// Format a number with commas for readability (e.g., 1820 -> "1,820").
function fmtNum(n) {
  return (n || 0).toLocaleString();
}

function renderTurnCell(turn) {
  if (!turn) return [el("span", { className: "muted", text: "—" })];
  const nodes = [];

  // Total input = new + cache_read + cache_create. The number people care about.
  const totalIn = (turn.input_new || 0) + (turn.input_cache_read || 0)
                + (turn.input_cache_create || 0);

  // Headline: total in / out (the number people actually compare)
  const head = el("div", { className: "tokens-headline" });
  head.appendChild(el("strong", { text: `${fmtNum(totalIn)} in` }));
  head.appendChild(el("span", { text: ` · ${fmtNum(turn.output_tokens)} out` }));
  nodes.push(head);

  // Detail line broken down (so the savvy reader can see the cache split)
  // Only show non-zero components to reduce noise.
  const parts = [];
  if (turn.input_new) parts.push(`${fmtNum(turn.input_new)} new`);
  if (turn.input_cache_read) parts.push(`${fmtNum(turn.input_cache_read)} cache read`);
  if (turn.input_cache_create) parts.push(`${fmtNum(turn.input_cache_create)} cache write`);
  if (parts.length) {
    nodes.push(el("div", { className: "tokens-breakdown",
      text: parts.join(" + ") }));
  }

  // Text snippet
  if (turn.text_snippet) {
    nodes.push(el("div", { text: turn.text_snippet }));
  }
  // Tool calls — render with a small colored pill for the tool name, then
  // the (truncated) input. Native = green tint, MCP = indigo tint.
  for (let i = 0; i < (turn.tool_calls || []).length; i++) {
    const toolName = turn.tool_calls[i] || "";
    const toolInput = (turn.tool_inputs[i] || "").slice(0, 200);
    const isNative = toolName === "Bash";
    const pill = el("strong", {
      className: isNative ? "tool-pill native" : "tool-pill mcp",
      text: toolName,
    });
    const callDiv = el("div", { className: "tool-call" }, pill,
      document.createTextNode(" " + toolInput));
    nodes.push(callDiv);
    const res = (turn.tool_results || [])[i];
    if (res != null) {
      const cell = el("div", {
        className: "tool-result" + ((turn.tool_errors || [])[i] ? " err" : ""),
        text: res,
      });
      nodes.push(cell);
    }
  }
  return nodes;
}

async function pollForCompletion() {
  if (!runStartTime) return;
  try {
    const res = await fetch("/api/run/status");
    if (!res.ok) return;
    const status = await res.json();

    // Ingest any events we haven't seen yet. Track a high-water mark.
    const seenCount = state._pollEventCount || 0;
    if (status.events.length > seenCount) {
      for (let i = seenCount; i < status.events.length; i++) {
        handleEvent(status.events[i], () => {
          state._pollDoneRuns = (state._pollDoneRuns || 0) + 1;
        });
      }
      state._pollEventCount = status.events.length;
    }

    // If the run finished, stop polling and hydrate the right view.
    if (!status.active && status.report_path) {
      stopPolling();
      $("progress-view").hidden = true;
      if (status.freeform_scenario || state._freeformActive) {
        await hydrateFreeformAndShow();
      } else {
        await hydrateSummaryFromBackend();
      }
    }
  } catch (_) {
    // ignore transient errors
  }
}

// Pull the just-completed freeform scenario from the backend, register it
// in state.scenarios so the stepper + scenario detail know about it, then
// land directly on its detail page (skipping the summary deck).
async function hydrateFreeformAndShow() {
  try {
    const res = await fetch(apiPath("/api/reports/latest/data"));
    if (!res.ok) return;
    const data = await res.json();
    const ff = data.freeform_scenario;
    if (!ff) {
      // Fallback: backend didn't send the synthesized scenario; try to
      // recover the id from the result_data and treat title generically.
      const sid = data.scenarios?.[0]?.scenario_id;
      if (!sid) { showSummary(); return; }
      addFreeformScenarioToState({
        id: sid, title: "Free-format scenario",
        category: "freeform", difficulty: "medium", prompt: "",
      });
      hydrateScenarioRunsFromData(data);
      showScenario(sid);
      return;
    }
    addFreeformScenarioToState(ff);
    hydrateScenarioRunsFromData(data);
    state._freeformActive = false;
    showScenario(ff.id);
  } catch (_) {}
}

function addFreeformScenarioToState(scenario) {
  const exists = state.scenarios.some((s) => s.id === scenario.id);
  if (!exists) state.scenarios.push(scenario);
  buildStepper();
  setStepStatus(scenario.id, "done");
}

function hydrateScenarioRunsFromData(data) {
  if (!data.scenarios) return;
  for (const sr of data.scenarios) {
    state.scenarioResults[sr.scenario_id] = {};
    const rbm = sr.runs_by_model || {};
    for (const [m, b] of Object.entries(rbm)) {
      state.scenarioResults[sr.scenario_id][m] = {
        native: b.native_runs || [], mcp: b.mcp_runs || [],
      };
    }
    if (Object.keys(state.scenarioResults[sr.scenario_id]).length === 0) {
      // legacy fallback: no runs_by_model, use flat lists
      const m = (data.models && data.models[0]) || data.model || "unknown";
      state.scenarioResults[sr.scenario_id][m] = {
        native: sr.native_runs || [], mcp: sr.mcp_runs || [],
      };
    }
  }
  if (!state.activeModel) {
    state.activeModel = defaultModel(data.models || [data.model].filter(Boolean));
  }
}

async function hydrateSummaryFromBackend() {
  try {
    const res = await fetch(apiPath("/api/reports/latest/data"));
    if (!res.ok) return;
    const data = await res.json();
    if (!data.scenarios) return;
    // Rebuild state.scenarioResults from the backend's BenchmarkResult dump
    state.scenarioResults = {};
    for (const sr of data.scenarios) {
      state.scenarioResults[sr.scenario_id] = {};
      const rbm = sr.runs_by_model || {};
      for (const [m, b] of Object.entries(rbm)) {
        state.scenarioResults[sr.scenario_id][m] = {
          native: b.native_runs || [], mcp: b.mcp_runs || [],
        };
      }
      if (Object.keys(state.scenarioResults[sr.scenario_id]).length === 0) {
        const m = (data.models && data.models[0]) || data.model || "unknown";
        state.scenarioResults[sr.scenario_id][m] = {
          native: sr.native_runs || [], mcp: sr.mcp_runs || [],
        };
      }
    }
    if (!state.activeModel) {
      state.activeModel = defaultModel(data.models || [data.model].filter(Boolean));
    }
    $("progress-view").hidden = true;
    showSummary();
  } catch (_) {}
}

function stopPolling() {
  if (pollIntervalId) {
    clearInterval(pollIntervalId);
    pollIntervalId = null;
  }
}

// ═════════════════════════════════════════════════════════════
//  Cost-at-scale projection panel (Tier B 3.3)
//
//  Lives inside the summary view. Hits /api/reports/{id}/projection
//  every time an input changes (debounced). Persists thresholds +
//  growth to localStorage so the user's preferred sizing sticks.
// ═════════════════════════════════════════════════════════════

const PROJ_DEBOUNCE_MS = 300;
let projDebounceTimer = null;
let projCurrentReportId = null;
let _projectionInputsBound = false;

function readProjectionInputs() {
  const volume = parseInt($("projection-volume").value, 10) || 10000;
  const period = document.querySelector("#projection-period .active")
    ?.dataset.period || "month";
  const growth = parseFloat($("projection-growth").value) || 0;
  const ths = [
    parseFloat($("proj-th-a").value) || 0,
    parseFloat($("proj-th-b").value) || 0,
    parseFloat($("proj-th-c").value) || 0,
  ].filter((n) => n > 0);
  const sel = $("projection-model");
  const model = sel ? (sel.value || null) : null;
  return { volume, period, growth, ths, model };
}

function persistProjectionPrefs() {
  const { ths, growth } = readProjectionInputs();
  try {
    localStorage.setItem("tcs.proj.thresholds", JSON.stringify(ths));
    localStorage.setItem("tcs.proj.growth", String(growth));
  } catch (_) { /* localStorage may be disabled */ }
}

function loadProjectionPrefs() {
  try {
    const raw = localStorage.getItem("tcs.proj.thresholds");
    if (raw) {
      const ths = JSON.parse(raw);
      if (Array.isArray(ths) && ths.length === 3) {
        $("proj-th-a").value = ths[0];
        $("proj-th-b").value = ths[1];
        $("proj-th-c").value = ths[2];
      }
    }
    const g = localStorage.getItem("tcs.proj.growth");
    if (g != null) $("projection-growth").value = g;
  } catch (_) { /* localStorage may be disabled */ }
}

function refreshThresholdHeaders() {
  const fmt = (n) => "$" + Number(n).toLocaleString();
  const a = $("proj-th-a-h"); if (a) a.textContent = fmt($("proj-th-a").value);
  const b = $("proj-th-b-h"); if (b) b.textContent = fmt($("proj-th-b").value);
  const c = $("proj-th-c-h"); if (c) c.textContent = fmt($("proj-th-c").value);
}

async function fetchProjection(reportId) {
  const { volume, period, growth, ths, model } = readProjectionInputs();
  const params = new URLSearchParams({
    volume: String(volume),
    period,
    growth_rate_pct: String(growth),
  });
  if (ths.length > 0) params.set("thresholds", ths.join(","));
  if (model) params.set("model", model);
  try {
    const r = await fetch(apiPath(`/api/reports/${reportId}/projection?${params}`));
    if (!r.ok) return null;
    return await r.json();
  } catch (_) {
    return null;
  }
}

function renderProjectionResult(p) {
  const fmt$ = (v) => "$" + Number(v).toFixed(2);
  $("proj-native-total").textContent = fmt$(p.native_total);
  $("proj-mcp-total").textContent = fmt$(p.mcp_total);
  const delta = p.delta;
  const sign = delta >= 0 ? "+" : "−";
  $("proj-delta").textContent = sign + fmt$(Math.abs(delta)).slice(1) +
    (p.multiplier ? ` (${p.multiplier.toFixed(2)}×)` : "");
  refreshThresholdHeaders();

  const tbody = $("projection-breakeven-tbody");
  tbody.replaceChildren();
  // Group breakevens by scenario_id, preserving the order they came back in.
  const byScenario = new Map();
  for (const b of p.breakevens || []) {
    if (!byScenario.has(b.scenario_id)) byScenario.set(b.scenario_id, []);
    byScenario.get(b.scenario_id).push(b);
  }
  for (const [sid, rows] of byScenario.entries()) {
    const tr = document.createElement("tr");
    const sidTd = document.createElement("td");
    sidTd.textContent = sid;
    tr.appendChild(sidTd);
    for (const b of rows) {
      const td = document.createElement("td");
      td.className = "col-num";
      if (b.frame === "near_break_even") td.textContent = "≈ break-even";
      else if (b.frame === "single_path_failed") td.textContent = "—";
      else if (b.runs_to_breakeven == null) td.textContent = "—";
      else td.textContent = b.runs_to_breakeven.toLocaleString();
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  renderProjectionCurve(p.curve || []);
}

function renderProjectionCurve(curve) {
  const svg = $("projection-curve");
  if (!svg) return;
  svg.replaceChildren();
  if (!curve.length) return;
  const W = 600, H = 200, pad = 20;
  const maxY = Math.max(
    ...curve.map((c) => Math.max(c.native_cum, c.mcp_cum))
  ) || 1;
  const months = curve.length;
  const x = (m) => pad + (months > 1 ? ((m - 1) / (months - 1)) : 0) * (W - 2 * pad);
  const y = (v) => H - pad - (v / maxY) * (H - 2 * pad);
  const ns = "http://www.w3.org/2000/svg";
  const mkPath = (key, color, dasharray) => {
    const d = curve.map((c, i) =>
      `${i === 0 ? "M" : "L"} ${x(c.month).toFixed(1)} ${y(c[key]).toFixed(1)}`
    ).join(" ");
    const p = document.createElementNS(ns, "path");
    p.setAttribute("d", d);
    p.setAttribute("fill", "none");
    p.setAttribute("stroke", color);
    p.setAttribute("stroke-width", "2");
    if (dasharray) p.setAttribute("stroke-dasharray", dasharray);
    return p;
  };
  // Native = green (signal), MCP = indigo (counter) — same swatches as
  // the per-scenario cost bars so the panel reads as a continuation.
  svg.appendChild(mkPath("native_cum", "var(--signal-vivid, #22C55E)"));
  svg.appendChild(mkPath("mcp_cum", "var(--counter-vivid, #6366F1)"));
}

function bindProjectionInputs() {
  const triggerRefresh = () => {
    clearTimeout(projDebounceTimer);
    projDebounceTimer = setTimeout(async () => {
      persistProjectionPrefs();
      refreshThresholdHeaders();
      if (!projCurrentReportId) return;
      const p = await fetchProjection(projCurrentReportId);
      if (p) renderProjectionResult(p);
    }, PROJ_DEBOUNCE_MS);
  };
  for (const id of ["projection-volume", "projection-growth",
                     "proj-th-a", "proj-th-b", "proj-th-c"]) {
    const el2 = $(id);
    if (el2) el2.addEventListener("input", triggerRefresh);
  }
  for (const btn of document.querySelectorAll("#projection-period button")) {
    btn.addEventListener("click", async () => {
      document.querySelectorAll("#projection-period .active")
        .forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      if (!projCurrentReportId) return;
      const p = await fetchProjection(projCurrentReportId);
      if (p) renderProjectionResult(p);
    });
  }
  $("projection-model")?.addEventListener("change", async () => {
    if (!projCurrentReportId) return;
    const p = await fetchProjection(projCurrentReportId);
    if (p) renderProjectionResult(p);
  });
}

async function initProjectionPanel(reportId, models, defaultModelName) {
  projCurrentReportId = reportId;
  loadProjectionPrefs();
  refreshThresholdHeaders();

  const sel = $("projection-model");
  if (sel) {
    sel.replaceChildren();
    for (const m of models) {
      const o = document.createElement("option");
      o.value = m;
      o.textContent = m;
      sel.appendChild(o);
    }
    sel.value = defaultModelName || (models[0] || "");
    // Hide the Model picker when there's only one model in the report —
    // the API will fall back to it implicitly.
    const label = sel.closest("label");
    if (label) label.hidden = (models.length <= 1);
  }

  // Bind once across re-entries to showSummary.
  if (!_projectionInputsBound) {
    bindProjectionInputs();
    _projectionInputsBound = true;
  }

  const p = await fetchProjection(reportId);
  if (p) renderProjectionResult(p);
}

async function showSummary() {
  state.active = "summary";
  stopPolling();
  $("setup-view").hidden = true;
  $("scenario-view").hidden = true;
  $("summary-view").hidden = false;
  $("summary-back-link").hidden = !state.cameFromReports;
  setStepperVisible(true);  // summary view → stepper still relevant

  let analysis;
  try {
    const res = await fetch("/api/reports/latest/summary");
    if (!res.ok) {
      $("summary-headline").textContent = "No benchmark data yet.";
      return;
    }
    analysis = await res.json();
  } catch (e) {
    $("summary-headline").textContent = "Failed to load summary.";
    return;
  }

  // Render per-model pill row above the headline.
  const allModels = new Set();
  for (const sid in state.scenarioResults) {
    for (const m of Object.keys(state.scenarioResults[sid])) allModels.add(m);
  }
  renderModelPills("summary-model-pills", [...allModels], state.activeModel, (m) => {
    state.activeModel = m;
    showSummary();
  });

  // Headline — render the multiplier number in mono and italicize savings phrase.
  renderHeadline($("summary-headline"), analysis.headline || "—");

  $("summary-headline-caveat").textContent = analysis.runs_per_path === 1
    ? `Single-run measurements · ${analysis.scenarios.length} scenarios`
    : `Median across ${analysis.runs_per_path} runs · ${analysis.scenarios.length} scenarios`;

  // ─── Cost totals → 3 stat cards (CSS rewrites <table> into 3-up grid) ───
  const tbody = $("summary-tbody");
  tbody.replaceChildren();
  const addStatRow = (label, nativeVal, mcpVal, nativeWins) => {
    const tr = el("tr", {},
      el("td", { text: label }),
      el("td", { text: nativeVal, className: nativeWins ? "win" : "" }),
      el("td", { text: mcpVal, className: !nativeWins && mcpVal && nativeVal && mcpVal !== nativeVal ? "win" : "" }),
    );
    tbody.appendChild(tr);
  };

  // Calc avg input tokens + success rates from per-scenario bucket data.
  // Walk every model in the cube so totals reflect the full sweep.
  let nativeInputSum = 0, mcpInputSum = 0, nativeInputCount = 0, mcpInputCount = 0;
  let nativeSuccTotal = 0, nativeRunsTotal = 0, mcpSuccTotal = 0, mcpRunsTotal = 0;
  for (const sid in state.scenarioResults) {
    for (const m in state.scenarioResults[sid]) {
      const b = state.scenarioResults[sid][m];
      for (const r of b.native || []) {
        const tin = (r.input_tokens || 0) + (r.cache_read_input_tokens || 0) + (r.cache_creation_input_tokens || 0);
        nativeInputSum += tin;
        nativeInputCount += 1;
        nativeRunsTotal += 1;
        if (r.succeeded) nativeSuccTotal += 1;
      }
      for (const r of b.mcp || []) {
        const tin = (r.input_tokens || 0) + (r.cache_read_input_tokens || 0) + (r.cache_creation_input_tokens || 0);
        mcpInputSum += tin;
        mcpInputCount += 1;
        mcpRunsTotal += 1;
        if (r.succeeded) mcpSuccTotal += 1;
      }
    }
  }
  const avgNativeIn = nativeInputCount ? Math.round(nativeInputSum / nativeInputCount) : 0;
  const avgMcpIn = mcpInputCount ? Math.round(mcpInputSum / mcpInputCount) : 0;
  const nativeSuccPct = nativeRunsTotal ? Math.round((nativeSuccTotal / nativeRunsTotal) * 100) : 0;
  const mcpSuccPct = mcpRunsTotal ? Math.round((mcpSuccTotal / mcpRunsTotal) * 100) : 0;

  const totalNative = analysis.total_native_cost || 0;
  const totalMcp = analysis.total_mcp_cost || 0;
  addStatRow("Total cost",
    `$${totalNative.toFixed(2)}`,
    `$${totalMcp.toFixed(2)}`,
    totalNative < totalMcp);
  addStatRow("Avg input tokens",
    avgNativeIn.toLocaleString(),
    avgMcpIn.toLocaleString(),
    avgNativeIn > 0 && avgNativeIn < avgMcpIn);
  addStatRow("Success rate",
    `${nativeSuccPct}%`,
    `${mcpSuccPct}%`,
    nativeSuccPct >= mcpSuccPct && nativeSuccPct > 0);

  // ─── Cost-at-scale projection panel (Tier B) ───
  // Resolve the report id we're rendering against. Fresh benchmarks
  // don't carry one through state — fall back to "the latest" since
  // the just-finished run is always at the head of the list.
  let activeReportId = state.activeReportId;
  if (!activeReportId) {
    try {
      const r = await fetch("/api/reports?limit=1");
      if (r.ok) {
        const body = await r.json();
        activeReportId = body.reports?.[0]?.name || null;
      }
    } catch (_) { /* silent — projection panel will no-op */ }
  }
  if (activeReportId) {
    state.activeReportId = activeReportId;
    // Build the model list for the projection model picker. /data is
    // the canonical source; fall back to scenarioResults cube if it 404s.
    let projModels = [];
    let projDefault = "";
    try {
      const r = await fetch(apiPath(`/api/reports/${activeReportId}/data`));
      if (r.ok) {
        const body = await r.json();
        projModels = body.models || (body.model ? [body.model] : []);
        projDefault = defaultModel(projModels);
      }
    } catch (_) { /* fall through to cube */ }
    if (projModels.length === 0) {
      projModels = [...allModels];
      projDefault = state.activeModel || defaultModel(projModels);
    }
    await initProjectionPanel(activeReportId, projModels, projDefault);
  }

  // ─── Per-scenario cost bars ───
  const bars = $("summary-bars");
  bars.replaceChildren();
  const maxCost = Math.max(
    ...analysis.scenarios.flatMap((s) => [s.native_cost || 0, s.mcp_cost || 0]),
    0.001,
  );
  for (const s of analysis.scenarios) {
    const mult = s.multiplier;
    const title = s.title || s.scenario_id;
    const sidShort = s.scenario_id.split("_")[0];

    let multText = "—";
    let multClass = "ps-meter flat";
    if (mult != null) {
      if (mult > 1.05) {
        multText = `Native ${mult.toFixed(1)}× ↓`;
        multClass = "ps-meter";
      } else if (mult < 0.95) {
        multText = `MCP ${(1 / mult).toFixed(1)}× ↓`;
        multClass = "ps-meter up";
      } else {
        multText = "tied";
      }
    } else if (s.winner === "native") {
      multText = "MCP failed";
    } else if (s.winner === "mcp") {
      multText = "Native failed";
      multClass = "ps-meter up";
    } else {
      multText = "inconclusive";
    }

    const nativePct = ((s.native_cost || 0) / maxCost) * 100;
    const mcpPct = ((s.mcp_cost || 0) / maxCost) * 100;
    const nameCell = el("div", { className: "ps-name" },
      el("code", { text: sidShort }),
      title.length > 60 ? title.slice(0, 57) + "…" : title,
    );
    const barsCell = el("div", { className: "ps-bars" },
      el("div", { className: "ps-bar native", attrs: { style: `width: ${Math.max(nativePct, 4).toFixed(1)}%` } }),
      el("div", { className: "ps-bar mcp",    attrs: { style: `width: ${Math.max(mcpPct, 4).toFixed(1)}%` } }),
    );
    const meterCell = el("div", { className: multClass, text: multText });
    // Tier A: confidence chip — small badge next to the multiplier so
    // a 2× ratio off N=3 doesn't read as a hard claim.
    if (s.confidence === "low") {
      const chip = el("span", {
        className: "confidence-chip low",
        attrs: { title: s.confidence_reason || "Low confidence" },
        text: "low conf.",
      });
      meterCell.appendChild(chip);
    }
    bars.appendChild(el("li", {}, nameCell, barsCell, meterCell));
  }

  // ─── Framework grid (apply win/lose classes) ───
  const nativeCol = $("framework-native-col");
  const mcpCol = $("framework-mcp-col");
  if (nativeCol && mcpCol) {
    nativeCol.classList.remove("win", "lose");
    mcpCol.classList.remove("win", "lose");
    const nativeWinCount = (analysis.framework_native_wins || []).length;
    const mcpWinCount = (analysis.framework_mcp_wins || []).length;
    if (nativeWinCount > mcpWinCount) {
      nativeCol.classList.add("win");
      mcpCol.classList.add("lose");
    } else if (mcpWinCount > nativeWinCount) {
      mcpCol.classList.add("win");
      nativeCol.classList.add("lose");
    }
  }
  $("framework-native-pattern").textContent =
    analysis.framework_native_pattern || "(no clear native-win pattern detected)";
  $("framework-mcp-pattern").textContent =
    analysis.framework_mcp_pattern || "(no clear MCP-win pattern detected)";
  const renderBullets = (id, items) => {
    const ul = $(id);
    ul.replaceChildren();
    if (!items || !items.length) {
      ul.appendChild(el("li", { className: "muted", text: "(none in this run)" }));
      return;
    }
    for (const txt of items) ul.appendChild(el("li", { text: txt }));
  };
  renderBullets("framework-native-bullets", analysis.framework_native_wins);
  renderBullets("framework-mcp-bullets", analysis.framework_mcp_wins);

  // Tier A: outcome rollup + cache rollup
  renderOutcomeBar("outcomes-native-bar", analysis.native_outcomes_total || {});
  renderOutcomeBar("outcomes-mcp-bar", analysis.mcp_outcomes_total || {});
  $("cache-native").textContent =
    formatPct(analysis.native_cache_hit_ratio);
  $("cache-mcp").textContent =
    formatPct(analysis.mcp_cache_hit_ratio);

  // Caveats
  const caveatsList = $("summary-caveats");
  caveatsList.replaceChildren();
  for (const c of analysis.caveats || []) {
    caveatsList.appendChild(el("li", { text: c }));
  }

  // Download button
  const dl = $("download-report");
  if (state.reportPath) {
    dl.href = "/api/reports/latest";
    dl.setAttribute("download", state.reportPath.split("/").pop());
  }
}

async function checkSfLoginStatus() {
  // Returns true if the current browser session has an SF token.
  // If the endpoint itself errors, we err on the side of "not logged
  // in" so the splash gives the user a path forward instead of a
  // half-broken catalog.
  try {
    const res = await fetch("/api/sf/status", { cache: "no-store" });
    if (!res.ok) return false;
    const body = await res.json();
    return !!body.logged_in;
  } catch (e) {
    return false;
  }
}

function setStepperVisible(visible) {
  // The stepper (s01 / s02 / … / Summary chips) only makes sense when
  // the user is inside the benchmark flow itself — running, watching a
  // scenario detail, or on the summary. Everywhere else (landing,
  // freeform, reports, login splash), it's confusing chrome.
  document.body.classList.toggle("has-stepper", !!visible);
}

function renderLogin() {
  state.active = "login";
  $("login-view").hidden = false;
  $("landing-view").hidden = true;
  $("setup-view").hidden = true;
  $("scenario-view").hidden = true;
  $("summary-view").hidden = true;
  $("progress-view").hidden = true;
  setStepperVisible(false);
}

function renderLanding() {
  state.active = "landing";
  $("login-view").hidden = true;
  $("landing-view").hidden = false;
  $("setup-view").hidden = true;
  $("scenario-view").hidden = true;
  $("summary-view").hidden = true;
  $("progress-view").hidden = true;
  // Reset which sub-section is visible inside setup-view so the next
  // landing-card click starts fresh.
  document.querySelectorAll("#setup-view .setup-section").forEach((n) => {
    n.hidden = true;
  });
  setStepperVisible(false);
}

function renderSetup(section) {
  // section = "benchmark" | "freeform" | "reports" — which sub-card to reveal.
  $("login-view").hidden = true;
  $("landing-view").hidden = true;
  $("setup-view").hidden = false;
  $("scenario-view").hidden = true;
  $("summary-view").hidden = true;
  $("progress-view").hidden = true;
  document.querySelectorAll("#setup-view .setup-section").forEach((n) => {
    n.hidden = section ? n.dataset.section !== section : false;
  });
  // Show the stepper only on the benchmark sub-section. Freeform and
  // reports are conceptually separate flows and don't need it.
  setStepperVisible(section === "benchmark");
}

// Click handler for the brand mark — returns the user to the home
// chooser without tearing down any in-progress work. If a benchmark
// is mid-run, SSE/polling continues and the user can navigate back
// into a scenario tab from the stepper to watch its progress.
function goHome() {
  state.active = "landing";
  state.cameFromReports = false;
  document.querySelectorAll(".step.active").forEach((n) => {
    n.classList.remove("active");
  });
  renderLanding();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// Export the current benchmark as PDF by rendering every scenario detail page
// + the summary page into a hidden print container, then triggering the
// browser's native print dialog (user picks "Save as PDF").
//
// Why client-side print: zero new dependencies, prints crisp vector type at
// the user's chosen page size, and the user already has a browser open.
async function exportPdf() {
  const btn = $("export-pdf");
  if (btn) { btn.disabled = true; btn.textContent = "Preparing…"; }

  // Save current view so we can restore it after print.
  const previousActive = state.active;

  // Build a print container that holds: each scenario page, then the summary.
  let printRoot = document.getElementById("print-root");
  if (printRoot) printRoot.remove();
  printRoot = document.createElement("div");
  printRoot.id = "print-root";

  try {
    // Make sure the summary analysis is loaded (so the cloned summary has data).
    await showSummary();

    // Capture the rendered summary first while it's already populated.
    const summaryClone = capturePrintSection($("summary-view"), {
      title: "Overall Summary",
    });

    // Render and capture each scenario in turn.
    for (const sc of state.scenarios) {
      // Skip scenarios with no run data (user may have unchecked them).
      const bucket = activeBucket(sc.id);
      if (!bucket || (!bucket.native?.length && !bucket.mcp?.length)) continue;

      // Render this scenario into the live scenario-view, then clone it.
      renderScenario(sc.id);
      // Synchronously clone after render; trace fetch is async, so wait briefly
      // to give it a chance to populate the trace card.
      await waitForTrace(sc.id, 1500);
      const clone = capturePrintSection($("scenario-view"), {
        title: `${sc.id} — ${sc.title}`,
      });
      printRoot.appendChild(clone);
    }
    // Summary page goes last in the deck.
    printRoot.appendChild(summaryClone);

    document.body.appendChild(printRoot);
    document.body.classList.add("printing");
    window.print();
  } catch (e) {
    alert("PDF export failed: " + (e?.message || e));
  } finally {
    // Restore on next tick — print dialog blocks the thread until dismissed.
    setTimeout(() => {
      document.body.classList.remove("printing");
      const root = document.getElementById("print-root");
      if (root) root.remove();
      // Restore previous active view.
      if (previousActive === "summary") showSummary();
      else if (previousActive && previousActive !== "setup") showScenario(previousActive);
      if (btn) { btn.disabled = false; btn.textContent = "⎙ Export PDF"; }
    }, 200);
  }
}

// Build a print "page" wrapping a clone of the source section's children.
// We page-break before each page so each scenario starts on its own sheet.
function capturePrintSection(sourceEl, opts = {}) {
  const page = document.createElement("section");
  page.className = "print-page";
  if (opts.title) {
    const h = document.createElement("h1");
    h.className = "print-page-title";
    h.textContent = opts.title;
    page.appendChild(h);
  }
  // Deep-clone all children except the actions row (no buttons in PDFs).
  for (const child of sourceEl.children) {
    if (child.classList?.contains("actions")) continue;
    page.appendChild(child.cloneNode(true));
  }
  return page;
}

// Wait until the trace card has rendered (or we hit the timeout). Used so the
// captured clone for each scenario includes the turn-by-turn trace.
function waitForTrace(sid, timeoutMs) {
  return new Promise((resolve) => {
    const start = Date.now();
    const tick = () => {
      const card = document.getElementById("trace-card");
      const tbody = document.getElementById("trace-tbody");
      if (card && !card.hidden && tbody && tbody.children.length > 0) {
        return resolve();
      }
      if (Date.now() - start > timeoutMs) return resolve();
      setTimeout(tick, 80);
    };
    tick();
  });
}

// Tier D: share modal helpers. The modal lives in index.html; these
// functions open/close it and back the Copy/Regenerate buttons.
async function openShareModal() {
  const reportId = state.activeReportId;
  if (!reportId) {
    alert("This view isn't tied to a saved report yet — share once it's persisted.");
    return;
  }
  const modal = document.getElementById("share-modal");
  if (modal) modal.hidden = false;
  await regenerateShareLink(reportId, 30);
}

async function regenerateShareLink(reportId, ttlDays) {
  const res = await fetch(`/api/reports/${encodeURIComponent(reportId)}/share`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ttl_days: ttlDays }),
  });
  if (!res.ok) {
    alert("Couldn't issue share link (" + res.status + ")");
    return;
  }
  const body = await res.json();
  const urlInput = document.getElementById("share-url");
  if (urlInput) {
    urlInput.value = body.url;
    urlInput.select();
  }
  const expiresEl = document.getElementById("share-expires");
  if (expiresEl) {
    expiresEl.textContent = new Date(body.expires_at).toLocaleDateString();
  }
}

function closeShareModal() {
  const m = document.getElementById("share-modal");
  if (m) m.hidden = true;
}

// Guest-mode bootstrap (read-only share view). Skips SF login, hides nav
// and run controls, fetches the shared report, lands on the summary view.
async function bootstrapGuestMode() {
  // Hide nav links — recipients can't navigate to admin/history/compare.
  const nav = document.querySelector(".header-nav");
  if (nav) nav.replaceChildren();
  // Hide login/landing/setup/progress views (they're not on share.html
  // anyway, but defensive).
  for (const id of ["login-view", "landing-view", "setup-view", "progress-view"]) {
    const e = document.getElementById(id);
    if (e) e.hidden = true;
  }
  // Remove every "data-hide-in-guest" element (Share buttons, run controls).
  for (const e of document.querySelectorAll("[data-hide-in-guest]")) {
    e.remove();
  }
  // Footer banner with expiry.
  if (window.__SHARE_EXPIRES_AT__) {
    const banner = document.createElement("div");
    banner.className = "share-banner";
    banner.textContent = "Read-only shared view · expires "
      + new Date(window.__SHARE_EXPIRES_AT__).toLocaleDateString();
    document.body.appendChild(banner);
  }
  // Pull the shared report and render the summary view.
  const r = await fetch(apiPath("/api/reports/latest/data"));
  if (!r.ok) {
    const err = document.createElement("p");
    err.style.padding = "32px";
    err.textContent = r.status === 410
      ? "This share link has expired. Ask the owner for a new one."
      : "This share link is invalid or the report no longer exists.";
    document.body.appendChild(err);
    return;
  }
  const data = await r.json();
  hydrateScenarioRunsFromData(data);
  showSummary();
}

// When the script is loaded by the cache-bust snippet (dynamically appended
// to document.body), DOMContentLoaded has already fired, so we'd never run
// init(). Check readyState and call init() directly when the DOM is ready.
const _bootEntry = isGuest ? bootstrapGuestMode : init;
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", _bootEntry);
} else {
  _bootEntry();
}
