// Sidebar collapse
const sidebar = document.getElementById('sidebar');
const collapseBtn = document.getElementById('sidebar-collapse-btn');

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

// Dark / light theme toggle
const themeToggle = document.getElementById('theme-toggle');
if (themeToggle) {
  themeToggle.addEventListener('click', () => {
    const html = document.documentElement;
    const isDark = html.getAttribute('data-theme') === 'dark';
    const next = isDark ? 'light' : 'dark';
    // Sync both attributes: our CSS variables and Bootstrap 5.3 native dark mode
    html.setAttribute('data-theme', next);
    html.setAttribute('data-bs-theme', next);

    // Persist via HTMX patch (Faz 1 profile settings endpoint)
    fetch('/settings/theme', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
      body: JSON.stringify({ theme: next }),
    }).catch(() => {}); // best-effort — page still works
  });
}

function getCsrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute('content') : '';
}
