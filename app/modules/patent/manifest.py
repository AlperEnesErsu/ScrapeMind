MODULE = {
    "code": "patent",
    "name_key": "module.patent.name",
    "version": "0.1.0",
    "permissions": [
        {"code": "patents.view", "label_key": "perm.patents.view"},
        {"code": "patents.manage", "label_key": "perm.patents.manage"},
    ],
    "menu": [
        {
            "code": "patent_tracking",
            "label_key": "menu.patents",
            "icon": "bi-clipboard-check",
            "endpoint": "patent.index",
            # Next to Prior art (15) and Reports (16): all three are patent
            # or output surfaces rather than the library itself.
            "order": 17,
        }
    ],
    "settings_schema": {},
}
