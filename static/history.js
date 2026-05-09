const $ = (id) => document.getElementById(id);

async function loadScenarios() {
  try {
    const r = await fetch("/api/scenarios");
    if (!r.ok) return;
    const list = await r.json();
    const sel = $("hist-scenario");
    sel.replaceChildren();
    for (const s of list) {
      const o = document.createElement("option");
      o.value = s.id;
      o.textContent = s.title || s.id;
      sel.appendChild(o);
    }
  } catch (_) { /* leave dropdown empty */ }
}

async function loadModels() {
  try {
    const r = await fetch("/api/models");
    if (!r.ok) return;
    const body = await r.json();
    const list = body.models || [];
    const sel = $("hist-model");
    sel.replaceChildren();
    for (const m of list) {
      const o = document.createElement("option");
      o.value = m;
      o.textContent = m;
      sel.appendChild(o);
    }
    // Default-select sonnet if present.
    for (const m of list) {
      if (m.toLowerCase().includes("sonnet")) {
        sel.value = m;
        break;
      }
    }
  } catch (_) {}
}

async function fetchSeries(metric) {
  const sid = $("hist-scenario").value;
  const m = $("hist-model").value;
  const range = $("hist-range").value;
  if (!sid || !m) return { points: [], change_markers: [] };
  const params = new URLSearchParams({ scenario_id: sid, model: m, metric });
  if (range) {
    const since = new Date();
    since.setDate(since.getDate() - parseInt(range, 10));
    params.set("since", since.toISOString());
  }
  try {
    const r = await fetch(`/api/history?${params}`);
    return r.ok ? await r.json() : { points: [], change_markers: [] };
  } catch (_) {
    return { points: [], change_markers: [] };
  }
}

function renderChart(svgId, series, showMarkers) {
  const svg = $(svgId);
  if (!svg) return;
  svg.replaceChildren();
  if (!series.points || series.points.length < 2) {
    // Empty placeholder text for the user.
    const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
    text.setAttribute("x", "300"); text.setAttribute("y", "100");
    text.setAttribute("text-anchor", "middle");
    text.setAttribute("fill", "var(--ink-mute, #888)");
    text.setAttribute("font-size", "12");
    text.textContent = "(needs ≥2 reports for trend)";
    svg.appendChild(text);
    return;
  }
  const W = 600, H = 200, pad = 24;
  const maxY = Math.max(
    ...series.points.map((p) => Math.max(p.native, p.mcp))
  ) || 1;
  const x = (i) => pad + (i / (series.points.length - 1)) * (W - 2 * pad);
  const y = (v) => H - pad - (v / maxY) * (H - 2 * pad);
  const ns = "http://www.w3.org/2000/svg";
  const mkPath = (key, color) => {
    const d = series.points.map((p, i) =>
      `${i === 0 ? "M" : "L"} ${x(i).toFixed(1)} ${y(p[key]).toFixed(1)}`
    ).join(" ");
    const e = document.createElementNS(ns, "path");
    e.setAttribute("d", d); e.setAttribute("fill", "none");
    e.setAttribute("stroke", color); e.setAttribute("stroke-width", "1.5");
    return e;
  };
  svg.appendChild(mkPath("native", "var(--signal-vivid, #1a73e8)"));
  svg.appendChild(mkPath("mcp", "var(--counter-vivid, #d32f2f)"));
  if (showMarkers) {
    for (const m of series.change_markers || []) {
      const idx = series.points.findIndex((p) => p.report_id === m.report_id);
      if (idx < 0) continue;
      const line = document.createElementNS(ns, "line");
      line.setAttribute("x1", x(idx)); line.setAttribute("x2", x(idx));
      line.setAttribute("y1", pad); line.setAttribute("y2", H - pad);
      line.setAttribute("stroke", "var(--ink-mute, #888)");
      line.setAttribute("stroke-dasharray", "3,3");
      const t = document.createElementNS(ns, "title");
      t.textContent = `${m.kind}: ${m.detail}`;
      line.appendChild(t);
      svg.appendChild(line);
    }
  }
}

function renderTable(seriesByMetric) {
  const tbody = $("hist-tbody");
  if (!tbody) return;
  tbody.replaceChildren();
  const cost = seriesByMetric.cost;
  if (!cost || !cost.points || cost.points.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 6;
    td.className = "muted";
    td.textContent = "No reports for this scenario+model in the selected range.";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }
  for (let i = 0; i < cost.points.length; i++) {
    const p = cost.points[i];
    const prev = cost.points[i - 1];
    const delta = prev
      ? `${(((p.native - prev.native) / Math.max(prev.native, 1e-12)) * 100).toFixed(1)}%`
      : "—";
    const tr = document.createElement("tr");
    const cells = [
      String(p.started_at).slice(0, 19),
      p.report_id,
      $("hist-model").value,
      `$${p.native.toFixed(4)}`,
      `$${p.mcp.toFixed(4)}`,
      delta,
    ];
    for (const v of cells) {
      const td = document.createElement("td");
      td.textContent = v;
      tr.appendChild(td);
    }
    tr.style.cursor = "pointer";
    tr.addEventListener("click", () => {
      window.location.href = `/?report=${encodeURIComponent(p.report_id)}`;
    });
    tbody.appendChild(tr);
  }
}

async function refresh() {
  const showMarkers = $("hist-markers").checked;
  localStorage.setItem("tcs.hist.markers", showMarkers ? "1" : "0");
  const metrics = ["cost", "cache", "success", "p95_duration"];
  const results = await Promise.all(metrics.map(fetchSeries));
  results.forEach((s, i) => renderChart(`hist-chart-${metrics[i]}`, s, showMarkers));
  const byMetric = {};
  metrics.forEach((m, i) => { byMetric[m] = results[i]; });
  renderTable(byMetric);
}

(async function init() {
  await Promise.all([loadScenarios(), loadModels()]);
  $("hist-markers").checked = localStorage.getItem("tcs.hist.markers") === "1";
  for (const id of ["hist-scenario", "hist-model", "hist-range", "hist-markers"]) {
    $(id).addEventListener("change", refresh);
  }
  await refresh();
})();
