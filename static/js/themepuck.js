/* tokenmeter — themepuck.js
 *
 * Renders the header puck + dropdown into #theme-puck-mount.
 * Reads/writes via window.tokenmeter.theme.
 */
(function () {
  const PALETTES = [
    { id: "teal-coral",        name: "Teal · Coral",     a: "#0F766E", b: "#F472B6" },
    { id: "emerald-violet",    name: "Emerald · Violet", a: "#047857", b: "#7C3AED" },
    { id: "cyan-amber",        name: "Cyan · Amber",     a: "#0369A1", b: "#F59E0B" },
    { id: "forest-terracotta", name: "Forest · Terra",   a: "#14532D", b: "#C2410C" },
  ];

  function el(tag, attrs, children) {
    const e = document.createElement(tag);
    for (const k of Object.keys(attrs || {})) {
      if (k === "text") e.textContent = attrs[k];
      else if (k.startsWith("on")) e.addEventListener(k.slice(2).toLowerCase(), attrs[k]);
      else e.setAttribute(k, attrs[k]);
    }
    for (const child of children || []) {
      if (child) e.appendChild(child);
    }
    return e;
  }

  function paletteLabel(id) {
    const p = PALETTES.find((x) => x.id === id);
    return p ? p.name.split(" · ")[0] : "Theme";
  }

  function modeLabel(mode) {
    return mode === "dark" ? "Dark" : "Light";
  }

  function mount(node) {
    if (!node) return;
    const theme = window.tokenmeter.theme.getTheme();

    const puck = el("span", { class: "tp-puck", "aria-hidden": "true" }, []);
    const labelSpan = el("span", { class: "tp-label" }, [
      document.createTextNode(`${paletteLabel(theme.palette)} · ${modeLabel(theme.mode)}`),
    ]);
    const chev = el("span", { class: "tp-chev", "aria-hidden": "true", text: "▾" }, []);

    const trigger = el("button", {
      type: "button",
      class: "tp-trigger",
      "aria-haspopup": "true",
      "aria-expanded": "false",
      "aria-label": "Theme selector",
    }, [puck, labelSpan, chev]);

    const dropdown = renderDropdown(() => {
      // re-render label after change
      const t = window.tokenmeter.theme.getTheme();
      labelSpan.textContent = `${paletteLabel(t.palette)} · ${modeLabel(t.mode)}`;
    });
    dropdown.hidden = true;

    function open() {
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

    // Subscribe — keep label in sync if theme changes elsewhere (e.g. ⌘K later).
    window.tokenmeter.theme.subscribe(() => {
      const t = window.tokenmeter.theme.getTheme();
      labelSpan.textContent = `${paletteLabel(t.palette)} · ${modeLabel(t.mode)}`;
    });
  }

  function renderDropdown(onChange) {
    const t = window.tokenmeter.theme.getTheme();

    // Mode segmented
    const modeLabel = el("div", { class: "tp-section-label", text: "Mode" }, []);
    const lightBtn = el("button", {
      type: "button", class: "tp-seg-btn",
      "aria-pressed": String(t.mode === "light"),
      text: "☀ Light",
      onClick: () => {
        window.tokenmeter.theme.applyTheme({ mode: "light", matchSystem: false });
        lightBtn.setAttribute("aria-pressed", "true");
        darkBtn.setAttribute("aria-pressed", "false");
        matchInput.checked = false;
        onChange();
      },
    }, []);
    const darkBtn = el("button", {
      type: "button", class: "tp-seg-btn",
      "aria-pressed": String(t.mode === "dark"),
      text: "☾ Dark",
      onClick: () => {
        window.tokenmeter.theme.applyTheme({ mode: "dark", matchSystem: false });
        darkBtn.setAttribute("aria-pressed", "true");
        lightBtn.setAttribute("aria-pressed", "false");
        matchInput.checked = false;
        onChange();
      },
    }, []);
    const segmented = el("div", { class: "tp-segmented" }, [lightBtn, darkBtn]);

    // Palette grid
    const palLabel = el("div", { class: "tp-section-label", text: "Palette" }, []);
    const tiles = PALETTES.map((p) => {
      const aHalf = el("div", { style: `background:linear-gradient(135deg,${p.a},${p.a}99);` }, []);
      const bHalf = el("div", { style: `background:linear-gradient(135deg,${p.b},${p.b}99);` }, []);
      const preview = el("div", { class: "tp-palette-preview" }, [aHalf, bHalf]);
      const nameSpan = el("span", { class: "tp-palette-name", text: p.name }, []);
      const tile = el("button", {
        type: "button",
        class: "tp-palette-tile",
        "aria-pressed": String(p.id === t.palette),
        "aria-label": `Use ${p.name} palette`,
        onClick: () => {
          window.tokenmeter.theme.applyTheme({ palette: p.id });
          for (const other of grid.children) other.setAttribute("aria-pressed", "false");
          tile.setAttribute("aria-pressed", "true");
          onChange();
        },
      }, [preview, nameSpan]);
      return tile;
    });
    const grid = el("div", { class: "tp-palette-grid" }, tiles);

    // Match-system row
    const matchInput = el("input", { type: "checkbox", id: "tp-match-system" }, []);
    matchInput.checked = !!t.matchSystem;
    matchInput.addEventListener("change", () => {
      window.tokenmeter.theme.applyTheme({ matchSystem: matchInput.checked });
      onChange();
    });
    const matchRow = el("label", { class: "tp-match-row", for: "tp-match-system" }, [
      matchInput,
      document.createTextNode("Match system color scheme"),
    ]);

    return el("div", { class: "tp-dropdown", role: "menu" }, [
      modeLabel, segmented, palLabel, grid, matchRow,
    ]);
  }

  function init() {
    const node = document.getElementById("theme-puck-mount");
    mount(node);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
