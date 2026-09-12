"""Patent tracking — a rolling window of USPTO full text.

The blueprint lives in routes.py, not here: defining it in both places
used to leave a dangling Blueprint that was never registered (see the
same note on `app/modules/scrape/__init__.py`).
"""
