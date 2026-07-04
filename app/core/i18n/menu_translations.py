from flask_babel import lazy_gettext

# Dummy file to force pybabel to extract database-backed menu labels.
# These keys are loaded dynamically from the MenuItem table and rendered in _sidebar.html.
lazy_gettext("menu.dashboard")
lazy_gettext("menu.feed")
lazy_gettext("menu.admin")
lazy_gettext("menu.profile")
lazy_gettext("menu.users")
lazy_gettext("menu.roles")
lazy_gettext("menu.permissions")
lazy_gettext("menu.menu_items")
lazy_gettext("menu.audit")
lazy_gettext("menu.tasks")
lazy_gettext("menu.system")
lazy_gettext("menu.for_you")
lazy_gettext("menu.library.timeline")
lazy_gettext("menu.library.favorites")
lazy_gettext("menu.library.notes")

# Other new translation strings used on the split dashboard
lazy_gettext("Total Users")
lazy_gettext("Active Users")
lazy_gettext("Locked Users")
lazy_gettext("Total Roles")
lazy_gettext("Recent System Activity")

# New user creation panel helpers
lazy_gettext("New User")
lazy_gettext("Must be at least 8 characters, with uppercase, lowercase, and a digit.")
lazy_gettext("Create User")

