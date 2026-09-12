// Citation graph for the paper detail page (scrape/_citation_graph.html).
//
// Moved out of an inline <script> so the page can run under a `script-src`
// without 'unsafe-inline'. Everything the template used to interpolate into
// the script -- the two URLs and every translated string -- now arrives as
// data-* attributes on `.citation-graph-wrapper`, which also keeps a quote in
// a translation from breaking the JavaScript.
//
// The partial is swapped in by HTMX when the user picks the graph tab, and the
// <script src> tag that loads this file travels with it, so the file runs once
// per insertion. It starts every wrapper that has not been started yet; the
// `data-cg-ready` flag makes a second run harmless.
(function () {
  'use strict';

  const VIS_SRC = 'https://cdn.jsdelivr.net/npm/vis-network@9.1.9/standalone/umd/vis-network.min.js';

  function loadVis(onReady, onFail) {
    if (window.vis && window.vis.Network) {
      onReady();
      return;
    }
    const script = document.createElement('script');
    script.src = VIS_SRC;
    script.addEventListener('load', onReady);
    script.addEventListener('error', onFail);
    document.head.appendChild(script);
  }

  function notify(message) {
    if (typeof window.showToast === 'function') window.showToast(message, 'danger');
    else window.alert(message);
  }

  function start(wrapper) {
    wrapper.dataset.cgReady = '1';
    const t = wrapper.dataset;
    const $ = (id) => wrapper.querySelector('#' + id);
    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';

    const container = $('cg-network-container');
    const spinner = $('cg-spinner');
    const emptyOverlay = $('cg-empty-overlay');
    const statsEl = $('cg-stats');

    const card = $('cg-node-details');
    const cardBadge = $('cg-card-badge');
    const cardLibBadge = $('cg-card-lib-badge');
    const cardTitle = $('cg-card-title');
    const cardMeta = $('cg-card-meta');
    const cardLinkOut = $('cg-card-link-out');
    const cardViewLib = $('cg-card-view-lib');
    const cardAddLib = $('cg-card-add-lib');
    const cardAddMsg = $('cg-card-add-msg');
    const addLibLabel = cardAddLib.innerHTML;

    const filters = { all: $('cg-filter-all'), refs: $('cg-filter-refs'), cits: $('cg-filter-cits') };

    let network = null;
    let fullData = null;
    let currentFilter = 'all';
    let selectedNodeData = null;

    function showEmpty() {
      spinner.classList.add('d-none');
      emptyOverlay.classList.remove('d-none');
    }

    function initGraph(graphData) {
      fullData = graphData;
      spinner.classList.add('d-none');

      const nodesList = graphData.nodes || [];
      const edgesList = graphData.edges || [];

      if (nodesList.length <= 1 && edgesList.length === 0 && graphData.notice === 'no_external_data') {
        emptyOverlay.classList.remove('d-none');
        statsEl.textContent = t.msgNoData;
        return;
      }

      const refCount = graphData.stats ? graphData.stats.references_count : 0;
      const citCount = graphData.stats ? graphData.stats.citations_count : 0;
      statsEl.textContent = `${refCount} ${t.msgReferences} · ${citCount} ${t.msgCitations}`;

      renderNetwork();
    }

    function getNodeStyle(node) {
      const isCenter = node.is_center;
      const inLib = node.in_library;
      const bg = isCenter ? '#0d6efd' : (node.type === 'reference' ? '#198754' : '#6f42c1');
      const size = isCenter
        ? 26
        : Math.min(22, Math.max(12, 10 + Math.log10(Math.max(1, node.citation_count || 1)) * 3));

      let shortLabel = (node.title || '').length > 28 ? (node.title.slice(0, 26) + '…') : node.title;
      if (node.year) shortLabel += ` (${node.year})`;

      const byline = (node.authors || []).join(', ') + (node.year ? ' (' + node.year + ')' : '');
      return {
        id: node.id,
        label: shortLabel,
        // Was a hardcoded "Atıf:" -- Turkish in the English UI too.
        title: `${node.title}\n${byline}\n${node.citation_count || 0} ${t.msgCitations}`,
        shape: 'dot',
        size: size,
        color: { background: bg, border: inLib ? '#ffc107' : bg, highlight: { background: bg, border: '#ffffff' } },
        borderWidth: inLib ? 3 : 1,
        font: { color: '#212529', size: isCenter ? 14 : 11, strokeWidth: 1, strokeColor: '#ffffff' },
        rawNode: node,
      };
    }

    function renderNetwork() {
      if (!fullData) return;

      const filteredNodes = fullData.nodes.filter((n) => {
        if (n.is_center) return true;
        if (currentFilter === 'refs') return n.type === 'reference';
        if (currentFilter === 'cits') return n.type === 'citation';
        return true;
      });

      const nodeIds = new Set(filteredNodes.map((n) => n.id));
      const filteredEdges = fullData.edges.filter((e) => nodeIds.has(e.source) && nodeIds.has(e.target));

      const visNodes = new vis.DataSet(filteredNodes.map(getNodeStyle));
      const visEdges = new vis.DataSet(filteredEdges.map((e) => ({
        from: e.source,
        to: e.target,
        arrows: 'to',
        color: { color: 'rgba(0, 0, 0, 0.2)', highlight: '#0d6efd' },
        smooth: { type: 'continuous' },
      })));

      const options = {
        nodes: { scaling: { min: 10, max: 30 } },
        edges: { width: 1.2, selectionWidth: 2.5 },
        physics: {
          solver: 'forceAtlas2Based',
          forceAtlas2Based: {
            gravitationalConstant: -35,
            centralGravity: 0.015,
            springLength: 90,
            springConstant: 0.08,
            damping: 0.4,
          },
          stabilization: { iterations: 150, updateInterval: 25 },
        },
        interaction: { hover: true, tooltipDelay: 200, zoomView: true, dragView: true },
      };

      network = new vis.Network(container, { nodes: visNodes, edges: visEdges }, options);

      network.on('click', (params) => {
        if (params.nodes.length > 0) {
          const target = filteredNodes.find((n) => n.id === params.nodes[0]);
          if (target) showNodeDetails(target);
        } else {
          card.classList.add('d-none');
        }
      });
    }

    function showNodeDetails(node) {
      selectedNodeData = node;
      card.classList.remove('d-none');

      cardBadge.style.backgroundColor = '';
      if (node.is_center) {
        cardBadge.textContent = t.msgCenter;
        cardBadge.className = 'badge bg-primary me-2';
      } else if (node.type === 'reference') {
        cardBadge.textContent = t.msgReference;
        cardBadge.className = 'badge bg-success me-2';
      } else {
        cardBadge.textContent = t.msgCitation;
        cardBadge.className = 'badge bg-purple me-2';
        cardBadge.style.backgroundColor = '#6f42c1';
      }

      cardLibBadge.classList.toggle('d-none', !node.in_library);
      cardTitle.textContent = node.title || t.msgUntitled;

      const authorStr = (node.authors || []).join(', ') || t.msgUnknownAuthors;
      const yearStr = node.year ? ` (${node.year})` : '';
      const venueStr = node.venue ? ` · ${node.venue}` : '';
      cardMeta.textContent = `${authorStr}${yearStr}${venueStr} · ${node.citation_count || 0} ${t.msgCitations}`;

      if (node.url) cardLinkOut.href = node.url;
      cardLinkOut.classList.toggle('d-none', !node.url);

      cardAddMsg.classList.add('d-none');
      cardAddLib.disabled = false;

      if (node.in_library && node.user_paper_id) {
        cardViewLib.href = `/papers/${node.user_paper_id}`;
        cardViewLib.classList.remove('d-none');
        cardAddLib.classList.add('d-none');
      } else if (!node.in_library) {
        cardViewLib.classList.add('d-none');
        cardAddLib.classList.remove('d-none');
      } else {
        cardViewLib.classList.add('d-none');
        cardAddLib.classList.add('d-none');
      }
    }

    function setFilter(name) {
      currentFilter = name;
      Object.entries(filters).forEach(([key, btn]) => btn.classList.toggle('active', key === name));
      renderNetwork();
    }

    $('cg-card-close').addEventListener('click', () => card.classList.add('d-none'));
    $('cg-fit-btn').addEventListener('click', () => {
      if (network) network.fit({ animation: { duration: 500, easingFunction: 'easeInOutQuad' } });
    });
    Object.keys(filters).forEach((name) => filters[name].addEventListener('click', () => setFilter(name)));

    function restoreAddButton() {
      cardAddLib.disabled = false;
      cardAddLib.innerHTML = addLibLabel;
    }

    cardAddLib.addEventListener('click', () => {
      if (!selectedNodeData) return;
      cardAddLib.disabled = true;
      cardAddLib.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>';
      cardAddLib.append(t.msgAdding);

      fetch(t.addUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify({
          id: selectedNodeData.id,
          title: selectedNodeData.title,
          authors: selectedNodeData.authors,
          year: selectedNodeData.year,
          venue: selectedNodeData.venue,
          doi: selectedNodeData.doi,
          url: selectedNodeData.url,
        }),
      })
        .then((r) => r.json())
        .then((data) => {
          if (data.status === 'ok') {
            selectedNodeData.in_library = true;
            selectedNodeData.user_paper_id = data.user_paper_id;
            restoreAddButton();
            cardAddLib.classList.add('d-none');
            cardAddMsg.classList.remove('d-none');
            cardLibBadge.classList.remove('d-none');
            if (data.user_paper_id) {
              cardViewLib.href = `/papers/${data.user_paper_id}`;
              cardViewLib.classList.remove('d-none');
            }
            renderNetwork();
          } else {
            notify(data.message || t.msgAddFailed);
            restoreAddButton();
          }
        })
        .catch(() => {
          notify(t.msgAddFailed);
          restoreAddButton();
        });
    });

    loadVis(() => {
      fetch(t.graphUrl)
        .then((resp) => {
          if (!resp.ok) throw new Error('HTTP ' + resp.status);
          return resp.json();
        })
        .then(initGraph)
        .catch(showEmpty);
    }, showEmpty);
  }

  document.querySelectorAll('.citation-graph-wrapper:not([data-cg-ready])').forEach(start);
})();
