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

# User Dashboard Redesign Helpers
lazy_gettext("Getting Started with ScrapeMind")
lazy_gettext(
    "ScrapeMind monitors academic papers matching your specific keywords. Follow these steps to set up your personalized research feed:"
)
lazy_gettext("Add Research Interests")
lazy_gettext("Enter keywords in the right sidebar, or click on trending topics to follow them.")
lazy_gettext("Trigger Paper Scraping")
lazy_gettext(
    'Once interests are added, click the "Scrape Now" button to search and import papers from arXiv.'
)
lazy_gettext("Read and Analyze")
lazy_gettext(
    "Papers will populate your homepage. Use ScrapeMind's AI features to instantly translate abstracts or write notes."
)
lazy_gettext("Surfaced Publications")
lazy_gettext("See full Discover feed")
lazy_gettext("Open my library")
lazy_gettext("My Research Interests")
lazy_gettext("Add keyword...")
lazy_gettext("Remove interest")
lazy_gettext("No followed keywords yet.")
lazy_gettext("Trending in ScrapeMind")
lazy_gettext("Following")
lazy_gettext("Follow")
lazy_gettext("Data Scraper")
lazy_gettext(
    "Scraping queries ArXiv and other databases using your research interest keywords to find matching papers."
)
lazy_gettext("Interest added successfully.")
lazy_gettext("Failed to add interest.")
lazy_gettext("Interest name cannot be empty.")
lazy_gettext("Interest removed successfully.")
lazy_gettext("Failed to remove interest.")
