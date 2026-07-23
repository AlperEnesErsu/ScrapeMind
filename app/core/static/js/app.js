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

// Dark / light theme toggle — wire up both the dropdown item AND the new
// visible topbar button. Persists to localStorage immediately (so a failed
// /settings/theme call doesn't leave the user with a mismatched preference)
// AND fires the API so server-rendered pages match on next load.
function toggleTheme() {
  const html = document.documentElement;
  const next = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  html.setAttribute('data-theme', next);
  html.setAttribute('data-bs-theme', next);
  try { localStorage.setItem('theme', next); } catch (e) { /* private mode */ }
  fetch('/settings/theme', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
    body: JSON.stringify({ theme: next }),
  }).catch(() => {}); // best-effort — localStorage is the source of truth
}

document.getElementById('theme-toggle')?.addEventListener('click', toggleTheme);
document.getElementById('theme-toggle-topbar')?.addEventListener('click', toggleTheme);

// Hydrate from localStorage on every page load so a logged-out user (or one
// whose /settings/theme write failed) still gets the right scheme.
try {
  const stored = localStorage.getItem('theme');
  if (stored && document.documentElement.getAttribute('data-theme') !== stored) {
    document.documentElement.setAttribute('data-theme', stored);
    document.documentElement.setAttribute('data-bs-theme', stored);
  }
} catch (e) { /* private mode */ }

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

// BibTeX copy-to-clipboard — Cite button on paper detail.
document.getElementById('cite-btn')?.addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  const url = btn.dataset.citeUrl;
  if (!url) return;
  const original = btn.innerHTML;
  try {
    const r = await fetch(url, { credentials: 'same-origin' });
    const text = await r.text();
    await navigator.clipboard.writeText(text);
    btn.innerHTML = '<i class="bi bi-check2 me-1"></i>Copied';
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
  const icon = (type === 'error' || type === 'danger') ? 'bi-exclamation-triangle-fill' : 'bi-check-circle-fill';
  
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
