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

// Generic copy-to-clipboard. Delegated on document so it also works on
// HTMX-swapped content (e.g. the 2FA recovery codes partial, which is loaded
// into the profile tab after page load).
//
//   data-copy-target="#el"   copies that element's text
//   data-copy-text="…"       copies the literal value
//
// `data-copy-text` replaced the collection page's "Copy Link" button, which
// wrote the share URL into a JS string literal in an `onclick` and then
// announced success with a blocking `alert()`. The button's own "Copied" state
// says the same thing without stopping the page.
document.addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-copy-target], [data-copy-text]');
  if (!btn) return;
  let text = btn.dataset.copyText;
  if (text === undefined) {
    const target = document.querySelector(btn.dataset.copyTarget);
    if (!target) return;
    text = target.textContent.trim();
  }
  const original = btn.innerHTML;
  const copiedLabel = btn.dataset.copiedLabel || 'Copied';
  try {
    await navigator.clipboard.writeText(text);
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

// <select data-autosubmit>   <input type="checkbox" data-autosubmit>
//
// Submits the control's form when its value changes. Replaces a mix of
// `onchange="this.form.submit()"` and `onchange="this.form.requestSubmit()"`,
// which are not the same thing: `submit()` skips the submit event, so it skips
// HTMX, validation, and `data-confirm` alike. The four settings lists that are
// HTMX forms were right to use `requestSubmit()`; the two plain forms that used
// `submit()` behave identically under it (neither has a required field), so
// one hook covers all of them without anyone having to pick correctly.
document.addEventListener('change', (e) => {
  const control = e.target.closest('[data-autosubmit]');
  control?.form?.requestSubmit();
});

// <textarea data-submit-on-enter>     Enter sends, Shift+Enter is a newline
// <textarea data-submit-on-mod-enter> Ctrl/Cmd+Enter sends
//
// `isComposing` is checked because Enter also confirms an IME composition, and
// sending half-composed text is not what anyone pressing Enter meant. The
// inline handlers these replace tested `keyCode == 13` and did not check it.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' || e.isComposing) return;
  const field = e.target;
  if (!(field instanceof HTMLElement) || !field.form) return;

  const plainEnter = field.hasAttribute('data-submit-on-enter') && !e.shiftKey;
  const modEnter = field.hasAttribute('data-submit-on-mod-enter') && (e.ctrlKey || e.metaKey);
  if (plainEnter || modEnter) {
    e.preventDefault();
    field.form.requestSubmit();
  }
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

// <button data-toggle-abstract="abstract-42"
//         data-label-more="{{ _('Show more') }}" data-label-less="{{ _('Show less') }}">
//
// The labels travel with the button. The function this replaces overwrote the
// button's translated "Show more" with hard-coded Turkish, so an English reader
// saw one language before the first click and another after it.
document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-toggle-abstract]');
  if (!btn) return;
  const el = document.getElementById(btn.dataset.toggleAbstract);
  if (!el) return;
  const expanding = el.classList.contains('text-truncate-3');
  el.classList.toggle('text-truncate-3', !expanding);
  btn.textContent = expanding ? btn.dataset.labelLess : btn.dataset.labelMore;
  btn.setAttribute('aria-expanded', expanding ? 'true' : 'false');
});

// <input type="checkbox" data-bulk-select>   <button data-bulk-clear>
//
// Shows the bulk-action panel while any paper is selected. The inline version
// called `toggleBulkPanel()`, which was defined only in feed.html's inline
// script — but the checkbox is part of the paper card, and the card renders on
// every page that lists papers. On /library/search every click threw
// `ReferenceError: toggleBulkPanel is not defined`. The panel only exists on
// the Discover feed, so everywhere else this is now a quiet no-op.
function refreshBulkPanel() {
  const panel = document.getElementById('bulk-action-panel');
  if (!panel) return;
  const selected = document.querySelectorAll('[data-bulk-select]:checked').length;
  panel.classList.toggle('d-none', selected === 0);
  const count = document.getElementById('selected-count');
  if (count) count.textContent = selected;
}
document.addEventListener('change', (e) => {
  if (e.target.closest('[data-bulk-select]')) refreshBulkPanel();
});
document.addEventListener('click', (e) => {
  if (!e.target.closest('[data-bulk-clear]')) return;
  document.querySelectorAll('[data-bulk-select]').forEach((cb) => { cb.checked = false; });
  refreshBulkPanel();
});
// Swapped-in cards arrive unchecked, so the panel has to follow them.
document.body.addEventListener('htmx:afterSwap', refreshBulkPanel);

// <button data-notes-filter="soru">
//
// Filters the note cards by type. `aria-pressed` marks the active chip, which
// the version this replaced did not expose at all.
document.addEventListener('click', (e) => {
  const chip = e.target.closest('[data-notes-filter]');
  if (!chip) return;
  const tag = chip.dataset.notesFilter;
  document.querySelectorAll('.note-card').forEach((card) => {
    card.style.display = (tag === 'all' || card.classList.contains('note-card--' + tag)) ? 'block' : 'none';
  });
  chip.parentElement?.querySelectorAll('[data-notes-filter]').forEach((c) => {
    c.setAttribute('aria-pressed', c === chip ? 'true' : 'false');
  });
});

// <button data-chat-question="{{ _('…') }}">
//
// Fills the paper chat with a suggested question and sends it. The questions
// used to be hard-coded Turkish inside `onclick`, outside `_()` entirely.
document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-chat-question]');
  if (!btn) return;
  const textarea = document.querySelector('.chat-input-area textarea[name="message"]');
  if (!textarea) return;
  textarea.value = btn.dataset.chatQuestion;
  textarea.focus();
  textarea.form?.requestSubmit();
});

// Keep the paper chat pinned to its newest message, on first render and after
// every HTMX swap that adds one.
function scrollChatToBottom() {
  const box = document.getElementById('chat-messages-box');
  if (box) box.scrollTop = box.scrollHeight;
}
document.addEventListener('DOMContentLoaded', scrollChatToBottom);
document.body.addEventListener('htmx:afterSwap', scrollChatToBottom);

// Heatmap: <input type="date" data-heatmap-date-input>,
//          <button class="heatmap-day" data-date="…">,
//          <button data-heatmap-clear>
document.addEventListener('change', (e) => {
  const input = e.target.closest('[data-heatmap-date-input]');
  if (input) filterByHeatmapDate(input.value);
});
document.addEventListener('click', (e) => {
  const day = e.target.closest('.heatmap-day[data-date]');
  if (day) { filterByHeatmapDate(day.dataset.date); return; }
  if (e.target.closest('[data-heatmap-clear]')) clearHeatmapDateFilter();
});

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

// Login splash (core/_splash.html): remove the overlay node once it has played.
// Cosmetic cleanup only -- the CSS has already faded it out and made it
// non-interactive by then, so nothing depends on this running.
(function () {
  const el = document.querySelector('.splash');
  if (!el) return;
  const drop = () => el.remove();
  el.addEventListener('animationend', (e) => { if (e.target === el) drop(); });
  setTimeout(drop, 4000);
})();

// Profile tabs: htmx swaps the tab content but does not manage the sidebar's
// active state, so mark the clicked tab here.
document.addEventListener('click', (e) => {
  const link = e.target.closest('#profile-tabs a');
  if (!link) return;
  document.querySelectorAll('#profile-tabs a').forEach((a) => a.classList.remove('active'));
  link.classList.add('active');
});

// Bootstrap tooltips for any [data-bs-toggle="tooltip"], on first render and in
// swapped-in content. getOrCreateInstance keeps a second pass from stacking.
function initTooltips(root) {
  if (!window.bootstrap || !bootstrap.Tooltip) return;
  (root || document).querySelectorAll('[data-bs-toggle="tooltip"]').forEach((el) => {
    bootstrap.Tooltip.getOrCreateInstance(el);
  });
}
document.addEventListener('DOMContentLoaded', () => initTooltips(document));
document.body.addEventListener('htmx:afterSwap', (e) => initTooltips(e.target));
