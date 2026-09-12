// Sidebar collapse (desktop) + mobile off-canvas toggle
const sidebar = document.getElementById('sidebar');
const collapseBtn = document.getElementById('sidebar-collapse-btn');
const mobileToggle = document.getElementById('sidebar-toggle');

if (sidebar && collapseBtn) {
  const COLLAPSED_KEY = 'sidebar_collapsed';

  function applySidebarState(collapsed) {
    sidebar.classList.toggle('collapsed', collapsed);
    sessionStorage.setItem(COLLAPSED_KEY, collapsed ? '1' : '0');
  }

  collapseBtn.addEventListener('click', () => {
    applySidebarState(!sidebar.classList.contains('collapsed'));
  });

  // Restore state from session
  applySidebarState(sessionStorage.getItem(COLLAPSED_KEY) === '1');
}

if (sidebar && mobileToggle) {
  mobileToggle.addEventListener('click', () => {
    sidebar.classList.toggle('mobile-open');
  });
  // Auto-close after picking a sidebar link on mobile — saves a tap.
  sidebar.addEventListener('click', (e) => {
    if (e.target.closest('a.nav-link') && window.innerWidth < 768) {
      sidebar.classList.remove('mobile-open');
    }
  });
}

function getCsrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute('content') : '';
}

// Generic copy-to-clipboard for [data-copy-target] buttons. Delegated on
// document so it also works on HTMX-swapped content (e.g. the 2FA recovery
// codes partial, which is loaded into the profile tab after page load).
document.addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-copy-target]');
  if (!btn) return;
  const target = document.querySelector(btn.dataset.copyTarget);
  if (!target) return;
  const original = btn.innerHTML;
  const copiedLabel = btn.dataset.copiedLabel || 'Copied';
  try {
    await navigator.clipboard.writeText(target.textContent.trim());
    btn.innerHTML = '<i class="bi bi-check2 me-1"></i>' + copiedLabel;
    btn.classList.add('btn-success');
    btn.classList.remove('btn-outline-secondary');
  } catch (err) {
    btn.innerHTML = '<i class="bi bi-x me-1"></i>Failed';
  }
  setTimeout(() => {
    btn.innerHTML = original;
    btn.classList.remove('btn-success');
    btn.classList.add('btn-outline-secondary');
  }, 1500);
});

// Cite button → open the BibTeX modal, fetch the entry, show a live preview.
// Copy is handled by the generic [data-copy-target] handler above; this just
// populates the modal and wires the download link.
document.getElementById('cite-btn')?.addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  const url = btn.dataset.citeUrl;
  const modalEl = document.getElementById('citeModal');
  const body = document.getElementById('cite-modal-body');
  if (!url || !modalEl || !body || typeof bootstrap === 'undefined') return;

  const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
  body.textContent = '…';
  const dl = document.getElementById('cite-modal-download');
  if (dl) dl.href = url + (url.includes('?') ? '&' : '?') + 'download=1';
  modal.show();

  try {
    const r = await fetch(url, { credentials: 'same-origin' });
    body.textContent = await r.text();
  } catch (err) {
    body.textContent = 'Failed to load citation.';
  }
});


// Keyboard navigation for feed / library lists
(function() {
  let currentFocusIndex = -1;

  function getPaperCards() {
    return document.querySelectorAll('.paper-card');
  }

  function updateFocusedCard() {
    const cards = getPaperCards();
    cards.forEach((card, index) => {
      if (index === currentFocusIndex) {
        card.classList.add('keyboard-focused');
        card.setAttribute('tabindex', '0');
        card.focus();
        card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      } else {
        card.classList.remove('keyboard-focused');
      }
    });
  }

  document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.isContentEditable) {
      return;
    }

    const cards = getPaperCards();
    if (cards.length === 0) return;

    if (e.key === 'j') {
      currentFocusIndex = (currentFocusIndex + 1) % cards.length;
      updateFocusedCard();
      e.preventDefault();
    } else if (e.key === 'k') {
      currentFocusIndex = (currentFocusIndex - 1 + cards.length) % cards.length;
      updateFocusedCard();
      e.preventDefault();
    } else if (e.key === 'f') {
      if (currentFocusIndex >= 0 && currentFocusIndex < cards.length) {
        const card = cards[currentFocusIndex];
        const favBtn = card.querySelector('[hx-post*="/favorite/toggle"]');
        if (favBtn) {
          favBtn.click();
        }
      }
    } else if (e.key === 'n') {
      if (currentFocusIndex >= 0 && currentFocusIndex < cards.length) {
        const card = cards[currentFocusIndex];
        const titleLink = card.querySelector('.paper-card__title');
        if (titleLink) {
          titleLink.click();
        }
      }
    }
  });
})();


// Global Toast Notification System
function showToast(message, type = 'success') {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    container.className = 'toast-container position-fixed top-0 end-0 p-3';
    container.style.zIndex = '1090';
    document.body.appendChild(container);
  }

  const toastEl = document.createElement('div');
  const bgClass = (type === 'error' || type === 'danger') ? 'bg-danger text-white' : (type === 'warning' ? 'bg-warning text-dark' : 'bg-success text-white');
  // A warning used to fall through to the tick, so "something went wrong" was
  // announced with a success icon. Nobody noticed while the only warning toast
  // said "Makale gizlendi"; the HTMX failure messages made it obvious.
  const icon = (type === 'error' || type === 'danger')
    ? 'bi-exclamation-triangle-fill'
    : (type === 'warning' ? 'bi-exclamation-circle-fill' : 'bi-check-circle-fill');
  
  toastEl.className = `toast align-items-center ${bgClass} border-0 shadow show`;
  toastEl.setAttribute('role', 'alert');
  toastEl.setAttribute('aria-live', 'assertive');
  toastEl.setAttribute('aria-atomic', 'true');
  toastEl.innerHTML = `
    <div class="d-flex">
      <div class="toast-body d-flex align-items-center gap-2">
        <i class="bi ${icon}"></i> ${message}
      </div>
      <button type="button" class="btn-close ${type === 'warning' ? '' : 'btn-close-white'} me-2 m-auto" data-bs-dismiss="toast" aria-label="Close"></button>
    </div>
  `;

  container.appendChild(toastEl);
  setTimeout(() => {
    toastEl.classList.remove('show');
    toastEl.remove();
  }, 3500);
}

// Messages come from <body data-msg-*>, rendered through `_()`. The fallbacks
// are here so a fragment swapped in without them still says something.
function msg(name, fallback) {
  return document.body.getAttribute('data-msg-' + name) || fallback;
}

// A failed HTMX request used to produce nothing at all: this handler only ever
// looked at `evt.detail.successful`, so a 400, a 403, a 500 and a dropped
// connection were all indistinguishable from the box simply not reacting.
//
// The case that made this worth fixing is the quietest one. CSRF tokens used to
// expire after an hour, so a tab left open across a working day stopped
// submitting, silently. That cause is gone -- tokens are bound to the session
// now (app/config.py) -- but the silence was the worse half of the bug, and it
// would have outlived the fix.
document.body.addEventListener('htmx:responseError', function(evt) {
  const status = evt.detail.xhr ? evt.detail.xhr.status : 0;
  if (status === 400) {
    // Reachable now only if the session itself is gone, which a reload fixes.
    showToast(msg('stale', 'Sayfa bir süredir açık. Yenileyip tekrar deneyin.'), 'warning');
  } else if (status === 401 || status === 403) {
    showToast(msg('forbidden', 'Buna izniniz yok.'), 'error');
  } else {
    showToast(msg('error', 'Bir şeyler ters gitti. Tekrar deneyin.'), 'error');
  }
});

// `htmx:responseError` only fires when there *was* a response. A request that
// never arrived -- offline, DNS, the server down -- raises this one instead,
// and it was the most silent case of all.
document.body.addEventListener('htmx:sendError', function() {
  showToast(msg('offline', 'Sunucuya ulaşılamadı. Bağlantınızı kontrol edin.'), 'error');
});

// ---------------------------------------------------------------------------
// Declarative hooks that replace inline event handlers.
//
// Content-Security-Policy blocks `onclick=` / `onsubmit=` exactly as it blocks
// inline <script>, so every handler written into a template stands between
// this app and a real `script-src` (docs/PRELAUNCH.md Y4). These listeners are
// delegated from `document`, which means markup swapped in by HTMX gets the
// behaviour without anything re-binding it.
// ---------------------------------------------------------------------------

// <form data-confirm="{{ _('Delete this?') }}">
//
// Replaces `onsubmit="return confirm('{{ _('…') }}')"`. That form put a
// translated string inside a JS string literal inside an HTML attribute, so a
// translation containing an apostrophe would have ended the string early and
// broken the page's script. None of the current ones do; a data attribute
// cannot, because Jinja escapes it as an attribute and JS never parses it.
document.addEventListener('submit', (e) => {
  const form = e.target;
  if (!(form instanceof HTMLFormElement)) return;
  const message = form.dataset.confirm;
  if (message && !window.confirm(message)) {
    e.preventDefault();
    e.stopImmediatePropagation();
  }
}, true);

// <button data-remove-on-click="#some-id">
//
// Removes the element the selector names. Used by the notification bell to
// clear its unread badge as the dropdown opens.
document.addEventListener('click', (e) => {
  const trigger = e.target.closest('[data-remove-on-click]');
  if (!trigger) return;
  document.querySelector(trigger.dataset.removeOnClick)?.remove();
});

// Wire up HTMX response triggers for toast notifications
document.body.addEventListener('htmx:afterRequest', function(evt) {
  if (evt.detail.successful) {
    const elt = evt.detail.elt;
    if (elt && elt.getAttribute('hx-post')) {
      const url = elt.getAttribute('hx-post');
      if (url.includes('/favorite/toggle')) {
        showToast('Favori durumu güncellendi', 'success');
      } else if (url.includes('/read-later/toggle')) {
        showToast('Sonra Oku listesi güncellendi', 'success');
      } else if (url.includes('/undismiss')) {
        // Must be checked BEFORE '/dismiss' — "undismiss" contains it.
        showToast('Makale geri getirildi', 'success');
      } else if (url.includes('/dismiss')) {
        showToast('Makale gizlendi', 'warning');
      }
    }
  }
});

// Toggle long paper abstracts
function toggleAbstract(id, btn) {
  const el = document.getElementById(id);
  if (el) {
    const isClamped = el.classList.contains('text-truncate-3');
    if (isClamped) {
      el.classList.remove('text-truncate-3');
      btn.textContent = 'Daralt';
    } else {
      el.classList.add('text-truncate-3');
      btn.textContent = 'Devamını Oku';
    }
  }
}

// Heatmap Date Filtering Helper
function filterByHeatmapDate(dateStr) {
  if (!dateStr) return;
  const indicator = document.getElementById('heatmap-filter-indicator');
  const dateSpan = document.getElementById('heatmap-filter-date');
  if (indicator && dateSpan) {
    dateSpan.textContent = dateStr;
    indicator.classList.remove('d-none');
    indicator.classList.add('d-flex');
  }
  
  document.querySelectorAll('.heatmap-day').forEach(el => el.style.outline = '');
  const clicked = document.querySelector(`.heatmap-day[data-date="${dateStr}"]`);
  if (clicked) clicked.style.outline = '2px solid var(--bs-primary)';

  const items = document.querySelectorAll('.paper-card, .timeline-event, .note-card');
  items.forEach(item => {
    const text = item.innerText || '';
    if (text.includes(dateStr)) {
      item.style.display = '';
    } else {
      item.style.display = 'none';
    }
  });
}

function clearHeatmapDateFilter() {
  const indicator = document.getElementById('heatmap-filter-indicator');
  if (indicator) {
    indicator.classList.add('d-none');
    indicator.classList.remove('d-flex');
  }
  document.querySelectorAll('.heatmap-day').forEach(el => el.style.outline = '');
  document.querySelectorAll('.paper-card, .timeline-event, .note-card').forEach(item => item.style.display = '');
}
