MODULE = {
    "code": "academic",
    "name_key": "module.academic.name",
    "version": "0.1.0",
    "permissions": [
        {"code": "identifiers.self", "label_key": "perm.identifiers.self"},
        {"code": "identifiers.manage", "label_key": "perm.identifiers.manage"},
    ],
    "menu": [
        {
            # One entry for every module's settings tabs, not one per tab:
            # issue #58 asked to separate account settings from product
            # configuration without crowding the sidebar. The page is core's
            # (settings.workspace); this module names it "Research Settings".
            # Just above Profile (90), so the two settings pages sit together.
            "code": "research_settings",
            "label_key": "menu.research_settings",
            "icon": "bi-gear",
            "endpoint": "settings.workspace",
            "order": 85,
        },
    ],
    "settings_schema": {},
}
