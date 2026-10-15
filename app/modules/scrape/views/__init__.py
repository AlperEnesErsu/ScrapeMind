"""Route handlers split out of `routes`.

Importing `app.modules.scrape.routes` loads these modules, which register their
handlers on `scrape_bp`. They read helpers through `routes.<name>` at call time, so
patching a name on `routes` keeps working; never import them directly.
"""
