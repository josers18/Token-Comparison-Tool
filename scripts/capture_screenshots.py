"""Capture README screenshots from the deployed Token Comparison Tool.

Renders three surfaces against the live Heroku deployment in the
default Spatial Glass theme (light + teal-coral):

    docs/screenshots/catalog.png         — home catalog grid
    docs/screenshots/scenario-detail.png — scenario detail view
    docs/screenshots/summary.png         — summary deck

The live app gates the home view behind SF OAuth. We bypass the splash
for screenshot purposes via an init script that stubs
/api/sf/status to return logged_in=true (keeps the SPA on the landing
chooser instead of the login splash). All other API responses come
from the real deployment.

Usage:
    .venv/bin/python scripts/capture_screenshots.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, Page

BASE = "https://token-comparison-tool-cb60c8f1dcc3.herokuapp.com"
OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
VIEWPORT = {"width": 1440, "height": 900}

# Theme to apply for the screenshots — Spatial Glass default.
THEME = {"mode": "light", "palette": "teal-coral", "matchSystem": False}

# Init script: pin the theme in localStorage + intercept /api/sf/status
# so the SPA doesn't bounce us to the login splash on the home page.
# All other endpoints hit the real deployment.
INIT_SCRIPT = """
(function () {
  try {
    localStorage.setItem("tokenmeter_theme", JSON.stringify(%s));
    document.cookie = "tokenmeter_theme=" + encodeURIComponent(JSON.stringify(%s)) + "; path=/; max-age=31536000; SameSite=Lax";
  } catch (e) { }

  // Wrap fetch so /api/sf/status reports logged_in=true. Everything else
  // passes through unchanged. The SPA reads this ONCE on init then routes
  // to the landing view; subsequent API calls (preflight, scenarios,
  // reports, etc.) hit the real Heroku deployment.
  var realFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    var url = typeof input === "string" ? input : input.url;
    if (url && url.includes("/api/sf/status")) {
      return Promise.resolve(new Response(
        JSON.stringify({ logged_in: true, instance_url: "https://example.my.salesforce.com" }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      ));
    }
    return realFetch(input, init);
  };
})();
""" % (
    repr(THEME).replace("'", '"'),
    repr(THEME).replace("'", '"'),
)


def find_latest_report_id(page: Page) -> str:
    """Hit /api/reports and return the most recent finalized rpt id."""
    body = page.evaluate(
        """() => fetch("/api/reports?limit=1").then((r) => r.json())"""
    )
    if not body or not body.get("reports"):
        raise RuntimeError("no reports available on the deployment")
    return body["reports"][0]["name"]


def capture_catalog(page: Page) -> None:
    print("→ catalog (home)")
    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_selector("#landing-view:not([hidden])", timeout=10_000)
    page.click('button[data-target="benchmark"]')
    page.wait_for_selector("#scenario-list .scenario-card-tile", timeout=10_000)
    time.sleep(1.5)  # sparkline lazy-fetch (~50ms after) + render
    # Hide every other panel so the catalog stands alone.
    page.evaluate("""() => {
      ['landing-view', 'login-view', 'progress-view', 'scenario-view', 'summary-view'].forEach((id) => {
        const e = document.getElementById(id); if (e) e.hidden = true;
      });
    }""")
    page.evaluate("() => window.scrollTo(0, 0)")
    page.screenshot(
        path=str(OUT / "catalog.png"),
        full_page=True,
        animations="disabled",
    )


def navigate_to_loaded_report(page: Page, report_id: str) -> None:
    """Load a report via the SPA's globally-exposed `loadReportById`.

    app.js doesn't wrap in an IIFE, so every top-level function (state,
    loadReportById, showSummary, showScenario, ...) is reachable from
    `window`. Cleaner than trying to walk the UI which has timing
    sensitivity around the reports-table render.
    """
    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_selector("#landing-view:not([hidden])", timeout=10_000)
    page.evaluate(f"() => window.loadReportById && window.loadReportById('{report_id}')")
    page.wait_for_selector("#scenario-view:not([hidden]), #summary-view:not([hidden])",
                           timeout=15_000)


def capture_scenario_detail(page: Page, report_id: str) -> None:
    print(f"→ scenario detail (from {report_id})")
    navigate_to_loaded_report(page, report_id)
    # The stepper renders one chip per scenario after load. Wait for it.
    page.wait_for_selector(".stepper a[data-sid], .stepper span[data-sid], .step", timeout=15_000)
    # Pull the first scenario id from the DOM (`.step[data-sid]`) and call
    # showScenario with it. `state` is const, not on window, but the stepper
    # surfaces the ids we need.
    first_sid = page.evaluate("""() => {
      const node = document.querySelector(".stepper [data-sid], .step[data-sid]");
      return node ? node.dataset.sid : null;
    }""")
    if not first_sid:
        # Last-resort fallback: pull from the report payload directly.
        first_sid = page.evaluate(f"""async () => {{
          const r = await fetch("/api/reports/{report_id}/data");
          const d = await r.json();
          return (d.scenarios && d.scenarios[0] && d.scenarios[0].scenario_id) || null;
        }}""")
    if not first_sid:
        raise RuntimeError("could not determine first scenario id")
    page.evaluate(f"() => window.showScenario('{first_sid}')")
    page.wait_for_selector("#sv-verdict:not([hidden])", timeout=15_000)
    # Hide the landing/setup views that linger from the load step so the
    # screenshot is just the scenario detail itself.
    page.evaluate("""() => {
      ['landing-view', 'setup-view', 'login-view', 'progress-view'].forEach((id) => {
        const e = document.getElementById(id); if (e) e.hidden = true;
      });
    }""")
    # Sparklines are best-effort; don't fail the capture if they don't render.
    try:
        page.wait_for_selector(".sparkline-cell svg path", timeout=4_000)
    except Exception:
        pass
    time.sleep(2.5)  # multiplier counter + chart bars + sparklines settle
    page.evaluate("() => window.scrollTo(0, 0)")
    page.screenshot(
        path=str(OUT / "scenario-detail.png"),
        full_page=True,
        animations="disabled",
    )


def capture_summary(page: Page, report_id: str) -> None:
    print(f"→ summary (from {report_id})")
    navigate_to_loaded_report(page, report_id)
    page.evaluate(
        "() => typeof window.showSummary === 'function' && window.showSummary()"
    )
    page.wait_for_selector("#summary-view:not([hidden])", timeout=10_000)
    page.evaluate("""() => {
      ['landing-view', 'setup-view', 'login-view', 'progress-view', 'scenario-view'].forEach((id) => {
        const e = document.getElementById(id); if (e) e.hidden = true;
      });
    }""")
    time.sleep(3.0)  # projection panel fetches + draws + per-scenario bars settle
    page.evaluate("() => window.scrollTo(0, 0)")
    page.screenshot(
        path=str(OUT / "summary.png"),
        full_page=True,
        animations="disabled",
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=2,  # retina — sharper PNGs
            color_scheme="light",
        )
        context.add_init_script(INIT_SCRIPT)
        page = context.new_page()

        # Discover a real finalized report for scenario + summary screens.
        try:
            page.goto(BASE + "/api/reports?limit=1", wait_until="domcontentloaded")
            body = page.evaluate("() => document.body.innerText")
            import json as _json
            reports = _json.loads(body).get("reports", [])
            if not reports:
                print("ERROR: no reports on deployment", file=sys.stderr)
                return 1
            report_id = reports[0]["name"]
        except Exception as e:
            print(f"ERROR discovering report: {e}", file=sys.stderr)
            return 1

        try:
            capture_catalog(page)
        except Exception as e:
            print(f"WARN: catalog capture failed: {e}", file=sys.stderr)

        try:
            capture_scenario_detail(page, report_id)
        except Exception as e:
            print(f"WARN: scenario-detail capture failed: {e}", file=sys.stderr)

        try:
            capture_summary(page, report_id)
        except Exception as e:
            print(f"WARN: summary capture failed: {e}", file=sys.stderr)

        browser.close()
    print(f"Wrote screenshots to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
