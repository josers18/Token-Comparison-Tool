// static/app.js — vanilla JS SPA. No innerHTML.

const state = {
  preflight: null,
  scenarios: [],
  runsPerPath: 3,
  model: "claude-opus-4-7",
  scenarioResults: {},   // { [sid]: { native: RunResult[], mcp: RunResult[] } }
  charts: {},
  reportPath: null,
  active: "setup",
};

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
  renderSetup();
  $("run-btn").addEventListener("click", startRun);
  $("run-again").addEventListener("click", () => location.reload());
  // Brand mark = "home". Returns the user to the catalog without
  // discarding state — any in-progress run keeps streaming in the
  // background; loaded report state stays available via the stepper.
  const brandHome = $("brand-home");
  if (brandHome) brandHome.addEventListener("click", goHome);
  const pdfBtn = $("export-pdf");
  if (pdfBtn) pdfBtn.addEventListener("click", exportPdf);
  const sPdfBtn = $("scenario-export-pdf");
  if (sPdfBtn) sPdfBtn.addEventListener("click", exportPdf);

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

  // Load saved report controls
  const loadBtn = $("report-load-btn");
  const reportPick = $("report-pick");
  const reportUpload = $("report-upload");
  if (loadBtn && reportPick && reportUpload) {
    const updateLoadEnabled = () => {
      loadBtn.disabled = !reportPick.value && !reportUpload.files?.length;
    };
    // Picking from the dropdown clears the upload, and vice versa — only
    // one source at a time keeps the load action unambiguous.
    reportPick.addEventListener("change", () => {
      if (reportPick.value) reportUpload.value = "";
      updateLoadEnabled();
    });
    reportUpload.addEventListener("change", () => {
      if (reportUpload.files?.length) reportPick.value = "";
      updateLoadEnabled();
    });
    loadBtn.addEventListener("click", loadSelectedReport);
    populateReportPicker();
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
  // Always show the Connect Salesforce button — preflight only knows
  // about env config, not whether the *user* has authenticated. If the
  // ECA isn't configured the click will surface a clear server error,
  // which is more useful than a hidden button.
  $("sf-login-row").hidden = false;
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

  const list = $("scenario-list");
  // Preserve the static header row at the top — only wipe the data rows.
  list.querySelectorAll("li:not(.scenario-list-header)").forEach((n) => n.remove());
  for (const s of state.scenarios) {
    const checkbox = el("input", {
      attrs: { type: "checkbox", "data-sid": s.id, checked: "checked" },
    });
    const sid = el("div", { className: "sid", text: s.id.split("_")[0] });
    // Strip a leading "scenario_id —" or em-dash prefix only; don't touch
    // hyphenated words like "High-value".
    const titleText = (s.title || "").replace(/^[^—:]*[—:]\s+/, "");
    const title = el("div", { className: "stitle" },
      el("strong", { text: s.id.split("_").slice(1).join("_") || s.id }),
      " · ",
      el("em", { text: titleText || s.title }),
    );
    const scope = el("div", { className: "scope", text: s.category || "" });
    const diffClass = (s.difficulty || "").toLowerCase();
    const tag = el("div", {
      className: "stag" + (diffClass ? ` ${diffClass}` : ""),
      text: s.difficulty || "",
    });
    list.appendChild(el("li", {}, checkbox, sid, title, scope, tag));
  }

  // Wire the "select all" checkbox + sync indeterminate state.
  wireScenarioSelectAll();

  if (state.preflight?.ok) $("run-btn").disabled = false;
  else showRemediation();
  buildStepper();
}

async function loadModels() {
  const sel = document.getElementById("model-select");
  if (!sel) return;
  try {
    const r = await fetch("/api/models", { cache: "no-store" });
    const { models } = await r.json();
    sel.replaceChildren();
    for (const m of (models || [])) {
      const o = document.createElement("option");
      o.value = m;
      o.textContent = m;
      sel.appendChild(o);
    }
    // Default selection: sonnet if available, otherwise first option.
    const preferred = (models || []).find(m => m === "claude-4-5-sonnet");
    if (preferred) sel.value = preferred;
  } catch (e) {
    // /api/models is supposed to never fail — but if it does (e.g. no
    // Inference addons attached on a dev install), leave the dropdown
    // empty and let the user know via console.
    console.warn("loadModels failed:", e);
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
    Array.from(list.querySelectorAll("li:not(.scenario-list-header) input[type=checkbox]"));

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
    list.addEventListener("change", (e) => {
      if (!(e.target instanceof HTMLInputElement)) return;
      if (e.target.id === "scenario-list-toggle-all") {
        // Master toggled → sync all data rows.
        const want = e.target.checked;
        for (const c of dataCheckboxes()) c.checked = want;
        e.target.indeterminate = false;
        return;
      }
      if (e.target.matches("li:not(.scenario-list-header) input[type=checkbox]")) {
        syncMasterFromRows();
      }
    });
    list.dataset.toggleAllWired = "1";
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
  const checked = Array.from(document.querySelectorAll("#scenario-list input:checked"))
    .map((i) => i.dataset.sid);
  if (checked.length === 0) return;
  state.runsPerPath = parseInt($("runs-per-path").value, 10);
  const modelEl = document.getElementById("model-select");
  state.model = modelEl ? modelEl.value : "claude-4-5-sonnet";
  state.maxTurns = parseInt($("max-turns").value, 10);
  for (const s of state.scenarios) {
    state.scenarioResults[s.id] = { native: [], mcp: [] };
  }

  runStartTime = Date.now();
  state._pollEventCount = 0;
  state._pollDoneRuns = 0;
  if (pollIntervalId) clearInterval(pollIntervalId);
  pollIntervalId = setInterval(pollForCompletion, 5000);

  $("setup-view").hidden = true;
  $("progress-view").hidden = false;

  const body = {
    scenario_ids: checked,
    runs_per_path: state.runsPerPath,
    model: state.model,
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

  await consumeSseStream(res, checked.length * state.runsPerPath * 2);

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
  state.model = $("freeform-model").value;
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
  state.scenarioResults[sid] = { native: [], mcp: [] };
  buildStepper();
  setStepStatus(sid, "active");

  runStartTime = Date.now();
  state._pollEventCount = 0;
  state._pollDoneRuns = 0;
  state._freeformActive = true;
  if (pollIntervalId) clearInterval(pollIntervalId);
  pollIntervalId = setInterval(pollForCompletion, 5000);

  $("setup-view").hidden = true;
  $("progress-view").hidden = false;

  const body = {
    prompt,
    title,
    scenario_id: sid,
    runs_per_path: state.runsPerPath,
    model: state.model,
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

  await consumeSseStream(res, state.runsPerPath * 2);

  // SSE stream closed; if the run finished cleanly the polling fallback
  // already hydrated state. As a belt-and-suspenders, hydrate now too.
  if ($("progress-view").hidden === false) {
    try {
      const r = await fetch("/api/reports/latest");
      if (r.ok) await hydrateSummaryFromBackend();
    } catch (_) {}
  }
}

// Populate the recent-reports dropdown from the server's reports/ directory.
async function populateReportPicker() {
  const sel = $("report-pick");
  if (!sel) return;
  try {
    const res = await fetch("/api/reports");
    if (!res.ok) return;
    const body = await res.json();
    sel.replaceChildren(
      el("option", { attrs: { value: "" }, text: "(none — upload a file instead)" }),
    );
    for (const r of body.reports || []) {
      // mtime → "May 6 · 16:38" so the user can spot the report they want
      let label = r.name;
      try {
        const dt = new Date(r.mtime_iso);
        const month = dt.toLocaleString("en-US", { month: "short" });
        const day = dt.getDate();
        const time = dt.toTimeString().slice(0, 5);
        const sizeKb = Math.round((r.size_bytes || 0) / 1024);
        label = `${r.name}  ·  ${month} ${day}, ${time}  ·  ${sizeKb} KB${r.has_json ? "" : " (md only)"}`;
      } catch (_) {}
      sel.appendChild(el("option", { attrs: { value: r.name }, text: label }));
    }
  } catch (_) { /* silent */ }
}

// Load whichever source the user picked: the dropdown name, or the upload.
async function loadSelectedReport() {
  const btn = $("report-load-btn");
  const sel = $("report-pick");
  const upload = $("report-upload");
  const hint = $("report-load-hint");
  if (!btn || !sel || !upload) return;

  btn.disabled = true;
  if (hint) hint.textContent = "Loading…";

  try {
    let summary;
    if (sel.value) {
      // Server-side: load by name.
      const res = await fetch(`/api/reports/${encodeURIComponent(sel.value)}/data`);
      const body = await res.json();
      if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
      summary = body;
    } else if (upload.files && upload.files[0]) {
      // Client upload: POST as multipart/form-data.
      const fd = new FormData();
      fd.append("file", upload.files[0]);
      const res = await fetch("/api/reports/load", { method: "POST", body: fd });
      const body = await res.json();
      if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
      summary = body;
    } else {
      throw new Error("nothing selected");
    }

    // The backend hydrated _current_run for us. Pull the data and route the
    // user to the right view: scenario detail if there's only one scenario
    // (most freeform/single-run reports), otherwise the summary deck.
    await registerLoadedScenarios(summary);
    if (summary.scenario_count === 1 && summary.scenario_ids?.length === 1) {
      showScenario(summary.scenario_ids[0]);
    } else {
      showSummary();
    }
  } catch (e) {
    if (hint) hint.textContent = "Load failed: " + (e?.message || e);
    btn.disabled = false;
    return;
  } finally {
    btn.disabled = false;
    if (hint && hint.textContent === "Loading…") hint.textContent = "Loaded.";
  }
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
    const res = await fetch("/api/reports/latest/data");
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
      // Lazy-init the bucket — for freeform runs the scenario isn't in
      // state.scenarios until we hydrate it from the report.
      if (!state.scenarioResults[ev.scenario_id]) {
        state.scenarioResults[ev.scenario_id] = { native: [], mcp: [] };
      }
      const bucket = state.scenarioResults[ev.scenario_id];
      bucket[ev.path].push(ev.run_result);
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
  renderScenario(sid);
}

function renderScenario(sid) {
  const scenario = state.scenarios.find((s) => s.id === sid);
  const bucket = state.scenarioResults[sid] || { native: [], mcp: [] };

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

  const nativeMed = medianOf(bucket.native, "total_cost_usd");
  const mcpMed = medianOf(bucket.mcp, "total_cost_usd");
  const nativeInput = medianTotalInput(bucket.native);
  const mcpInput = medianTotalInput(bucket.mcp);

  // Headline (above chart) + verdict bar (above panels)
  const headlineEl = $("sv-headline");
  headlineEl.replaceChildren();
  headlineEl.classList.remove("mcp");

  const verdict = $("sv-verdict");
  const verdictText = $("sv-verdict-text");
  const verdictIcon = $("sv-verdict-icon");
  const verdictNum = $("sv-verdict-num");

  if (nativeMed && mcpMed) {
    const mult = mcpMed / nativeMed;
    verdict.hidden = false;
    verdict.classList.remove("mcp-win", "tied");
    verdictNum.replaceChildren();

    if (mult > 1.05) {
      // Native wins
      headlineEl.appendChild(document.createTextNode("Native was "));
      headlineEl.appendChild(el("em", { text: `${mult.toFixed(1)}× cheaper` }));
      headlineEl.appendChild(document.createTextNode(" on this scenario."));

      verdictIcon.textContent = "↓";
      verdictText.replaceChildren();
      verdictText.appendChild(document.createTextNode("Native came in at "));
      verdictText.appendChild(el("strong", { text: `$${nativeMed.toFixed(3)}` }));
      verdictText.appendChild(document.createTextNode(", MCP at "));
      verdictText.appendChild(el("strong", { text: `$${mcpMed.toFixed(3)}` }));
      verdictText.appendChild(document.createTextNode(" — "));
      verdictText.appendChild(el("strong", { text: `${mult.toFixed(1)}× cheaper` }));
      verdictText.appendChild(document.createTextNode(" on this scenario."));

      const delta = mcpInput - nativeInput;
      verdictNum.appendChild(document.createTextNode("Δ tokens"));
      verdictNum.appendChild(el("span", {
        className: "delta",
        text: `${delta < 0 ? "" : "−"}${Math.abs(delta).toLocaleString()} input`,
      }));
    } else if (mult < 0.95) {
      // MCP wins
      verdict.classList.add("mcp-win");
      headlineEl.classList.add("mcp");
      headlineEl.appendChild(document.createTextNode("MCP was "));
      headlineEl.appendChild(el("em", { text: `${(1 / mult).toFixed(1)}× cheaper` }));
      headlineEl.appendChild(document.createTextNode(" on this scenario."));

      verdictIcon.textContent = "↓";
      verdictText.replaceChildren();
      verdictText.appendChild(document.createTextNode("MCP came in at "));
      verdictText.appendChild(el("strong", { text: `$${mcpMed.toFixed(3)}` }));
      verdictText.appendChild(document.createTextNode(", Native at "));
      verdictText.appendChild(el("strong", { text: `$${nativeMed.toFixed(3)}` }));
      verdictText.appendChild(document.createTextNode(" — "));
      verdictText.appendChild(el("strong", { text: `${(1 / mult).toFixed(1)}× cheaper` }));
      verdictText.appendChild(document.createTextNode(" on this scenario."));

      const delta = nativeInput - mcpInput;
      verdictNum.appendChild(document.createTextNode("Δ tokens"));
      verdictNum.appendChild(el("span", {
        className: "delta",
        text: `${delta < 0 ? "" : "−"}${Math.abs(delta).toLocaleString()} input`,
      }));
    } else {
      verdict.classList.add("tied");
      headlineEl.textContent = "Effectively tied on token cost.";
      verdictIcon.textContent = "≈";
      verdictText.textContent = "Within 5% on this scenario — token cost is effectively tied.";
    }
  } else {
    verdict.hidden = true;
    headlineEl.textContent = "—";
  }

  renderChartRows(bucket);
  loadAndRenderTrace(sid);

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
      const r = await fetch(`/api/scenarios/${encodeURIComponent(sid)}/trace`);
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
    const res = await fetch(`/api/scenarios/${encodeURIComponent(sid)}/trace`);
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

    // First row: init metadata
    const initRow = el("tr", {},
      el("td", { className: "turn-num", text: "init" }),
      el("td", { className: "meta-row" }, formatInit(nat)),
      el("td", { className: "meta-row" }, formatInit(mcp)),
    );
    tbody.appendChild(initRow);

    // Per-turn rows: align by turn_index
    const maxTurns = Math.max(
      nat ? nat.turns.length : 0,
      mcp ? mcp.turns.length : 0,
    );
    for (let i = 0; i < maxTurns; i++) {
      const natTurn = nat?.turns[i];
      const mcpTurn = mcp?.turns[i];
      const tr = el("tr", {},
        el("td", { className: "turn-num", text: String(i + 1) }),
        el("td", {}, ...renderTurnCell(natTurn)),
        el("td", {}, ...renderTurnCell(mcpTurn)),
      );
      tbody.appendChild(tr);
    }

    card.hidden = false;
  } catch (_) { /* silent */ }
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
    const res = await fetch("/api/reports/latest/data");
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
    state.scenarioResults[sr.scenario_id] = {
      native: sr.native_runs || [],
      mcp: sr.mcp_runs || [],
    };
  }
}

async function hydrateSummaryFromBackend() {
  try {
    const res = await fetch("/api/reports/latest/data");
    if (!res.ok) return;
    const data = await res.json();
    if (!data.scenarios) return;
    // Rebuild state.scenarioResults from the backend's BenchmarkResult dump
    state.scenarioResults = {};
    for (const sr of data.scenarios) {
      state.scenarioResults[sr.scenario_id] = {
        native: sr.native_runs || [],
        mcp: sr.mcp_runs || [],
      };
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

async function showSummary() {
  state.active = "summary";
  stopPolling();
  $("setup-view").hidden = true;
  $("scenario-view").hidden = true;
  $("summary-view").hidden = false;

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

  // Calc avg input tokens + success rates from per-scenario bucket data
  let nativeInputSum = 0, mcpInputSum = 0, nativeInputCount = 0, mcpInputCount = 0;
  let nativeSuccTotal = 0, nativeRunsTotal = 0, mcpSuccTotal = 0, mcpRunsTotal = 0;
  for (const sid in state.scenarioResults) {
    const b = state.scenarioResults[sid];
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

  // ─── Cost-at-scale extrapolation ───
  const scaleInput = $("summary-scale");
  const scaleHint = $("summary-scale-summary");
  function updateScale() {
    const n = Math.max(1, parseInt(scaleInput.value, 10) || 0);
    let totalPerRunNative = 0, totalPerRunMcp = 0;
    for (const s of analysis.scenarios) {
      totalPerRunNative += s.native_cost || 0;
      totalPerRunMcp += s.mcp_cost || 0;
    }
    const monthlyNative = totalPerRunNative * n;
    const monthlyMcp = totalPerRunMcp * n;
    const delta = Math.abs(monthlyMcp - monthlyNative);
    const cheaperLabel = monthlyNative < monthlyMcp ? "Native" : "MCP";

    scaleHint.replaceChildren();
    scaleHint.appendChild(document.createTextNode("At "));
    scaleHint.appendChild(el("strong", { className: "num", text: n.toLocaleString() }));
    scaleHint.appendChild(document.createTextNode(" runs/scenario/month: Native "));
    scaleHint.appendChild(el("strong", { className: "num", text: `$${Math.round(monthlyNative).toLocaleString()}` }));
    scaleHint.appendChild(document.createTextNode(" · MCP "));
    scaleHint.appendChild(el("strong", { className: "num", text: `$${Math.round(monthlyMcp).toLocaleString()}` }));
    scaleHint.appendChild(document.createTextNode(" → "));
    const save = el("strong", { className: "num", text: `${cheaperLabel} saves $${Math.round(delta).toLocaleString()}/mo` });
    save.style.color = "var(--signal-deep)";
    scaleHint.appendChild(save);
  }
  scaleInput.removeEventListener("input", updateScale);
  scaleInput.addEventListener("input", updateScale);
  updateScale();

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

function renderSetup() {
  $("setup-view").hidden = false;
  $("scenario-view").hidden = true;
  $("summary-view").hidden = true;
  $("progress-view").hidden = true;
}

// Click handler for the brand mark — returns the user to the catalog
// without tearing down any in-progress work. If a benchmark is mid-run,
// SSE/polling continues and the user can navigate back into a scenario
// tab from the stepper to watch its progress.
function goHome() {
  state.active = "setup";
  // Clear "active" highlight on whatever step was visible.
  document.querySelectorAll(".step.active").forEach((n) => {
    n.classList.remove("active");
  });
  renderSetup();
  // Scroll back to the top so the user lands on the catalog headline,
  // not somewhere down by the freeform / load-report cards.
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
      const bucket = state.scenarioResults[sc.id];
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

// When the script is loaded by the cache-bust snippet (dynamically appended
// to document.body), DOMContentLoaded has already fired, so we'd never run
// init(). Check readyState and call init() directly when the DOM is ready.
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
