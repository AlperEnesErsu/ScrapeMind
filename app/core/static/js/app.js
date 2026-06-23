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
