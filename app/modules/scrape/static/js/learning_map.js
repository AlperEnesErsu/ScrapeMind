// Learning map: the canvas and the step card (Faz 9.4, docs/PHASE9.md §6).
//
// Progressive enhancement over reports/_learning_map_report.html. The server
// renders the full reading list (`<ol data-testid="lm-steps">`) and, hidden,
// the explorer block: a canvas container and a step card. This file unhides
// the block, fills the card from the list items and draws the map. Without
// it the page is the list, complete.
//
// The list stays the accessible content: the canvas is aria-hidden, and the
// step card is built by copying a list item's own children, so it can never
// say something the list does not. Everything the template knows -- the
// graph, translated strings -- arrives as data-* attributes (no inline
// script under the CSP).
//
// Which step is current is held in one place, `select(index)`, and every
// input -- Previous/Next, arrow keys, a click on a node or a list item --
// goes through it.
(function () {
  'use strict';

  const explorer = document.getElementById('lm-explorer');
  const list = document.querySelector('[data-testid="lm-steps"]');
  if (!explorer || !list || explorer.dataset.lmReady) return;
  const items = Array.from(list.querySelectorAll('li.lm-step'));
  if (!items.length) return;
  explorer.dataset.lmReady = '1';

  const t = explorer.dataset;
  const prev = document.getElementById('lm-prev');
  const next = document.getElementById('lm-next');
  const position = document.getElementById('lm-step-position');
  const body = document.getElementById('lm-step-body');
  const canvasEl = document.getElementById('lm-canvas');
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const indexById = new Map(items.map((li, i) => [li.dataset.nodeId, i]));
  let current = -1;
  let network = null;
  let nodes = null;
  let nodeStyle = null;

  function render(index) {
    const li = items[index];
    const copy = li.cloneNode(true);
    copy.querySelectorAll('[id], [data-testid]').forEach((el) => {
      el.removeAttribute('id');
      el.removeAttribute('data-testid');
    });
    body.replaceChildren(...copy.childNodes);
    position.textContent = (t.msgStep || '{n} / {total}')
      .replace('{n}', String(index + 1))
      .replace('{total}', String(items.length));
  }

  function select(index, { reveal = false } = {}) {
    if (index < 0 || index >= items.length || index === current) return;
    const before = current;
    current = index;
    items.forEach((li, i) => {
      if (i === index) li.setAttribute('aria-current', 'step');
      else li.removeAttribute('aria-current');
    });
    render(index);

    // A disabled button drops focus to <body>; hand it to the other one.
    const focused = document.activeElement;
    prev.disabled = index === 0;
    next.disabled = index === items.length - 1;
    if (focused === prev && prev.disabled) next.focus();
    if (focused === next && next.disabled) prev.focus();

    if (network && nodes && nodeStyle) {
      const changes = [];
      if (before >= 0 && nodes.get(items[before].dataset.nodeId)) {
        changes.push(nodeStyle(items[before].dataset.nodeId, false));
      }
      if (nodes.get(items[index].dataset.nodeId)) {
        changes.push(nodeStyle(items[index].dataset.nodeId, true));
      }
      nodes.update(changes);
    }

    if (reveal) {
      const box = explorer.getBoundingClientRect();
      if (box.top < 0 || box.top > window.innerHeight) {
        explorer.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' });
      }
    }
  }

  prev.addEventListener('click', () => select(current - 1));
  next.addEventListener('click', () => select(current + 1));
  // Arrow keys while focus is on either button: the pair acts as one control.
  [prev, next].forEach((button) => button.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault();
      select(current + (event.key === 'ArrowLeft' ? -1 : 1));
    }
  }));

  // A click anywhere on a list item opens it in the card -- except on its
  // links, which keep doing what links do.
  list.classList.add('lm-steps--interactive');
  list.addEventListener('click', (event) => {
    if (event.target.closest('a, button')) return;
    const li = event.target.closest('li.lm-step');
    if (li && list.contains(li)) select(items.indexOf(li), { reveal: true });
  });

  explorer.hidden = false;
  select(0);

  // --- The canvas ---------------------------------------------------------

  function parseGraph() {
    try {
      const graph = JSON.parse(t.lmGraph || 'null');
      return graph && Array.isArray(graph.nodes) && Array.isArray(graph.edges) ? graph : null;
    } catch (err) {
      return null;
    }
  }

  const graph = canvasEl ? parseGraph() : null;
  if (!graph || !window.ScrapeMindVis) {
    if (canvasEl) canvasEl.hidden = true;
    return;
  }

  // The four role families of .lm-role--* in theme.css, read from the same
  // custom properties so the canvas cannot drift off the palette. Slate for
  // a role the page does not know.
  const css = getComputedStyle(document.documentElement);
  const token = (name) => css.getPropertyValue(name).trim();
  const FAMILY = { prerequisite: 'blue', core: 'indigo', method: 'teal', current: 'green' };
  function family(role) {
    const name = FAMILY[role] || 'slate';
    return { tint: token(`--cat-${name}-tint`), ink: token(`--cat-${name}-ink`) };
  }
  const fontFace = getComputedStyle(document.body).fontFamily;
  const fontSize = parseInt(token('--text-sm'), 10) || 13;
  const edgeColor = token('--neutral-400');
  const edgeHover = token('--neutral-700');
  const ground = token('--neutral-50') || '#FFFFFF';

  function roleWord(role) {
    const key = 'role' + (role ? role.charAt(0).toUpperCase() + role.slice(1) : '');
    return t[key] || role || '';
  }

  const byId = new Map(graph.nodes.map((n) => [n.id, n]));

  // The current step is drawn inverted (filled with its ink, label on the
  // page ground) and with a heavier border -- a change of form, not only of
  // shade, so it reads without telling the four hues apart.
  nodeStyle = function (id, selected) {
    const n = byId.get(id);
    const c = family(n.role);
    const fill = selected ? c.ink : c.tint;
    const text = selected ? ground : c.ink;
    const color = { background: fill, border: c.ink };
    return {
      id,
      color: { ...color, highlight: color, hover: color },
      font: { color: text, face: fontFace, size: fontSize, multi: false },
      borderWidth: selected ? 3 : 1.5,
      borderWidthSelected: selected ? 3 : 1.5,
    };
  };

  function tooltip(n) {
    const el = document.createElement('div');
    el.textContent = `${n.label} — ${roleWord(n.role)}`;
    return el;
  }

  window.ScrapeMindVis.load().then((vis) => {
    const selectedId = items[current].dataset.nodeId;
    nodes = new vis.DataSet(graph.nodes.map((n) => ({
      ...nodeStyle(n.id, n.id === selectedId),
      // The role is written under the label: on the canvas too, colour
      // never carries it alone.
      label: `${n.label}\n${roleWord(n.role)}`,
      title: tooltip(n),
      level: Number.isInteger(n.layer) ? n.layer : 0,
      shape: 'box',
      margin: 8,
      widthConstraint: { maximum: 170 },
      shapeProperties: { borderRadius: 6 },
    })));
    const maxFlow = Math.max(1, ...graph.edges.map((e) => e.flow || 0));
    const edges = new vis.DataSet(graph.edges.map((e, i) => ({
      id: i,
      from: e.from,
      to: e.to,
      arrows: 'to',
      width: 1 + 2.5 * ((e.flow || 0) / maxFlow),
      color: { color: edgeColor, highlight: edgeHover, hover: edgeHover },
      smooth: { type: 'cubicBezier', forceDirection: 'horizontal', roundness: 0.4 },
    })));

    network = new vis.Network(canvasEl, { nodes, edges }, {
      layout: {
        hierarchical: {
          enabled: true,
          direction: 'LR',
          sortMethod: 'directed',
          levelSeparation: 200,
          nodeSpacing: 90,
        },
      },
      physics: false,
      interaction: {
        hover: true,
        dragNodes: false,
        dragView: true,
        // Off: a wheel over the map would stop scrolling the page.
        zoomView: false,
        keyboard: false,
        selectConnectedEdges: false,
        tooltipDelay: 200,
      },
    });

    // vis-network makes its frame focusable. Inside an aria-hidden container
    // that is a tab stop a screen reader cannot name (axe: aria-hidden-focus);
    // the buttons and the list are the keyboard path, so take it out of the
    // tab order. Not `inert`, which would also stop pointer clicks.
    canvasEl.querySelectorAll('[tabindex]').forEach((el) => el.setAttribute('tabindex', '-1'));

    const fit = () => network.fit({ animation: reduceMotion ? false : { duration: 300 } });
    network.once('afterDrawing', () => network.fit({ animation: false }));
    network.on('resize', fit);
    network.on('click', (params) => {
      const id = params.nodes && params.nodes[0];
      if (id !== undefined && indexById.has(id)) select(indexById.get(id));
    });
    network.on('hoverNode', () => { canvasEl.classList.add('lm-canvas--pointer'); });
    network.on('blurNode', () => { canvasEl.classList.remove('lm-canvas--pointer'); });
  }, () => {
    // The CDN is unreachable or blocked: the card and the list still work.
    canvasEl.hidden = true;
  });
})();
