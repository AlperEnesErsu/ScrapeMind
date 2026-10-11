// Loads vis-data + vis-network on demand -- the one place their URLs and
// hashes live. Used by the citation graph (scrape/_citation_graph.html) and
// the learning map (reports/_learning_map_report.html); include this file
// before either page script. It defines `window.ScrapeMindVis.load()`, which
// resolves to the `vis` global once both scripts have run.
//
// A second inclusion (the citation graph partial is re-inserted by HTMX on
// every tab switch) keeps the first definition, so a load already in flight
// is shared rather than started twice.
(function () {
  'use strict';
  if (window.ScrapeMindVis) return;

  // The "peer" builds, not "standalone": standalone vis-network injects its CSS
  // as <style> elements while it loads, which a `style-src` without
  // 'unsafe-inline' blocks. Peer ships the CSS as a file and takes vis-data as
  // a separate script; both must stay in SCRIPT_SRC / STYLE_SRC in
  // app/__init__.py at these exact versions.
  // Each file is pinned by `integrity`: the CSP allows these CDN paths, so
  // without it a changed file on the CDN would run with this site's privileges.
  const VIS_CSS = {
    href: 'https://cdn.jsdelivr.net/npm/vis-network@9.1.9/styles/vis-network.min.css',
    integrity: 'sha384-dyJxWiQJAf9QvdWzvgaFgxG2+qGBlxRcGINxMWNJBYZ7731QjuquKfhX2OUkb22J',
  };
  const VIS_SCRIPTS = [
    {
      src: 'https://cdn.jsdelivr.net/npm/vis-data@7.1.9/peer/umd/vis-data.min.js',
      integrity: 'sha384-pwdzkDObTMVncQuVOyJQ9QEfF1uUiMptCqZjqFTUmVY8Nki5FiQBKIkJrvuE1uRo',
    },
    {
      src: 'https://cdn.jsdelivr.net/npm/vis-network@9.1.9/peer/umd/vis-network.min.js',
      integrity: 'sha384-+WLSahiHaIxvAupfGxl7zuTh3TF5cciRUfv2qmyH0zELr0VzIUpeBrb3CmRCf8Jh',
    },
  ];

  let pending = null;

  function ready() {
    return window.vis && window.vis.Network && window.vis.DataSet;
  }

  function loadScript(asset) {
    return new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = asset.src;
      script.integrity = asset.integrity;
      script.crossOrigin = 'anonymous';
      script.addEventListener('load', resolve);
      script.addEventListener('error', reject);
      document.head.appendChild(script);
    });
  }

  function load() {
    if (ready()) return Promise.resolve(window.vis);
    if (pending) return pending;
    if (!document.querySelector(`link[href="${VIS_CSS.href}"]`)) {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = VIS_CSS.href;
      link.integrity = VIS_CSS.integrity;
      link.crossOrigin = 'anonymous';
      document.head.appendChild(link);
    }
    // In order: vis-network's peer build reads vis-data from the `vis` global.
    pending = VIS_SCRIPTS.reduce((chain, asset) => chain.then(() => loadScript(asset)), Promise.resolve())
      .then(() => {
        if (!ready()) throw new Error('vis did not load');
        return window.vis;
      });
    // A failed load (offline, blocked CDN) may be retried by the next caller.
    pending.catch(() => { pending = null; });
    return pending;
  }

  window.ScrapeMindVis = Object.freeze({ load });
})();
