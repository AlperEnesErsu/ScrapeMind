"""LLM feature modules split out of `ai_service`.

Import through `app.modules.scrape.ai_service` (the facade): these modules read the
shared LLM plumbing and each other via `ai_service.<name>` at call time, so
monkeypatching a name on `ai_service` keeps working.
"""
