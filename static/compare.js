// static/compare.js — Tier D compare page. NO innerHTML.

const $ = (id) => document.getElementById(id);

let _allReports = [];

async function loadReports() {
  const r = await fetch("/api/reports?limit=200");
  if (!r.ok) return;
  const body = await r.json();
  _allReports = body.reports || [];
  for (const id of ["cmp-a", "cmp-b"]) {
    const sel = $(id);
    sel.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "(select a report)";
    sel.appendChild(placeholder);
    for (const r of _allReports) {
      const opt = document.createElement("option");
      opt.value = r.name;
      const date = (r.started_at || "").slice(0, 10);
      const models = (r.models || []).join(",") || r.model || "?";
      opt.textContent = `${r.name} · ${date} · ${models} · ${r.scenario_count || 0} scenarios`;
      sel.appendChild(opt);
    }
  }
  const p = new URLSearchParams(location.search);
  if (p.get("a")) $("cmp-a").value = p.get("a");
  if (p.get("b")) $("cmp-b").value = p.get("b");
  refreshModelDropdown();
  if (p.get("a") && p.get("b")) runComparison();
}

function refreshModelDropdown() {
  const aId = $("cmp-a").value;
  const bId = $("cmp-b").value;
  const a = _allReports.find((r) => r.name === aId);
  const b = _allReports.find((r) => r.name === bId);
  const aMods = new Set((a?.models) || (a?.model ? [a.model] : []));
  const bMods = new Set((b?.models) || (b?.model ? [b.model] : []));
  const common = [...aMods].filter((m) => bMods.has(m));
  const sel = $("cmp-model");
  sel.replaceChildren();
  if (common.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "(no common models)";
    sel.appendChild(opt);
    return;
  }
  for (const m of common) {
    const opt = document.createElement("option");
    opt.value = m;
    opt.textContent = m;
    sel.appendChild(opt);
  }
  const sonnet = common.find((m) => m.toLowerCase().includes("sonnet"));
  if (sonnet) sel.value = sonnet;
}

async function runComparison() {
  const a = $("cmp-a").value;
  const b = $("cmp-b").value;
  const model = $("cmp-model").value;
  if (!a || !b) return;
  const params = new URLSearchParams({ a, b });
  if (model) params.set("model", model);
  const r = await fetch(`/api/reports/compare?${params}`);
  if (!r.ok) {
    alert("Compare failed: " + r.status);
    return;
  }
  const body = await r.json();
  renderComparison(body);
}

function renderComparison(cmp) {
  const meta = $("cmp-meta");
  meta.replaceChildren();
  meta.hidden = false;
  if (cmp.incompatible) {
    const p = document.createElement("p");
    p.textContent = "These reports share no common model — comparison requires runs on the same model.";
    meta.appendChild(p);
    $("cmp-regressions").replaceChildren();
    $("cmp-others").replaceChildren();
    $("cmp-scope").replaceChildren();
    return;
  }
  const grid = document.createElement("div");
  grid.className = "compare-meta-grid";
  const row = (label, va, vb) => {
    const lab = document.createElement("div");
    lab.className = "compare-meta-label";
    lab.textContent = label;
    const aEl = document.createElement("div");
    aEl.textContent = va;
    const bEl = document.createElement("div");
    bEl.textContent = vb;
    grid.append(lab, aEl, bEl);
  };
  row("Started", (cmp.report_a.started_at || "").slice(0, 10),
      (cmp.report_b.started_at || "").slice(0, 10));
  row("Model", cmp.model_used, cmp.model_used);
  row("Operator", cmp.report_a.operator || "—", cmp.report_b.operator || "—");
  row("Scope",
      `${cmp.scope.shared.length + cmp.scope.removed.length} scenarios`,
      `${cmp.scope.shared.length + cmp.scope.added.length} scenarios`);
  meta.appendChild(grid);

  const regressed = cmp.scenarios.filter((s) => s.regressed);
  const others = cmp.scenarios.filter((s) => !s.regressed && s.presence === "both");
  const scope = cmp.scenarios.filter((s) => s.presence !== "both");

  renderSection($("cmp-regressions"), `Regressions (${regressed.length})`,
                 regressed, { warn: true });
  renderSection($("cmp-others"), `Other scenarios (${others.length})`, others);
  if (scope.length) {
    const cont = $("cmp-scope");
    cont.replaceChildren();
    const h = document.createElement("h3");
    h.textContent = `Scope changes (${scope.length})`;
    cont.appendChild(h);
    for (const sc of scope) {
      const p = document.createElement("p");
      p.textContent = `${sc.scenario_id}  ·  ${sc.presence === "added_in_b" ? "added in B" : "removed in B"}`;
      cont.appendChild(p);
    }
  } else {
    $("cmp-scope").replaceChildren();
  }
}

function renderSection(container, title, scenarios, opts = {}) {
  container.replaceChildren();
  if (scenarios.length === 0) return;
  const h = document.createElement("h3");
  h.textContent = (opts.warn ? "⚠ " : "") + title;
  container.appendChild(h);
  for (const sc of scenarios) {
    container.appendChild(renderScenarioCard(sc));
  }
}

function renderScenarioCard(sc) {
  const card = document.createElement("div");
  card.className = "compare-card" + (sc.regressed ? " regressed" : "");
  const head = document.createElement("h4");
  head.textContent = sc.scenario_id;
  card.appendChild(head);
  const rows = [
    ["Native", sc.native_cost, "$"],
    ["MCP", sc.mcp_cost, "$"],
    ["Success", sc.success_rate, "pct"],
    ["Ratio", sc.cost_multiplier, "x"],
    ["p95", sc.p95_duration_ms, "ms"],
  ];
  for (const [label, m, unit] of rows) {
    const r = document.createElement("div");
    r.className = "compare-metric";
    const lbl = document.createElement("span");
    lbl.className = "compare-metric-label";
    lbl.textContent = label;
    const val = document.createElement("span");
    if (!m) {
      val.textContent = "—";
    } else {
      const fmt = (v) => unit === "$" ? "$" + v.toFixed(4)
                       : unit === "pct" ? (v * 100).toFixed(0) + "%"
                       : unit === "x" ? v.toFixed(2) + "×"
                       : Math.round(v) + " ms";
      let pctStr = "—";
      if (m.delta_pct == null) {
        pctStr = unit === "pct"
          ? ((m.delta_abs * 100).toFixed(0) + "pp")
          : "(new)";
      } else {
        pctStr = (m.delta_pct >= 0 ? "+" : "") + m.delta_pct.toFixed(1) + "%";
      }
      const sign = m.delta_abs > 0 ? "up" : m.delta_abs < 0 ? "down" : "flat";
      val.replaceChildren();
      const pair = document.createElement("span");
      pair.textContent = `${fmt(m.a)} → ${fmt(m.b)}`;
      val.appendChild(pair);
      const deltaEl = document.createElement("span");
      deltaEl.className = "compare-delta " + sign;
      deltaEl.textContent = "  " + pctStr;
      val.appendChild(deltaEl);
    }
    r.append(lbl, val);
    card.appendChild(r);
  }
  return card;
}

(async function init() {
  await loadReports();
  $("cmp-a").addEventListener("change", refreshModelDropdown);
  $("cmp-b").addEventListener("change", refreshModelDropdown);
  $("cmp-run").addEventListener("click", runComparison);
})();
