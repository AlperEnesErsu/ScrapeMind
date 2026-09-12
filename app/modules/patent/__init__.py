"""Patent tracking — a rolling window of USPTO full text.

No blueprint yet: routes arrive in Phase 8.2 together with the menu entry.
Registering a menu item before its endpoint exists turns *every* page into a
BuildError (`_sidebar.html` builds nav links unguarded), which Faz 6 hit once
in the running app. See `docs/PHASE8.md` §9.2.
"""
