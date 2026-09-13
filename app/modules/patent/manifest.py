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
        },
        {
            # A separate row, not a link on the tracking page gated in the
            # template: deciding who may see it there would mean writing a
            # permission check outside `permission_required`, where the
            # superuser bypass lives and must stay (CLAUDE.md rule 2). The
            # menu already filters rows by `required_permission`.
            "code": "patent_admin",
            "label_key": "menu.patents_admin",
            "icon": "bi-cloud-download",
            "endpoint": "patent.admin",
            "required_permission": "patents.manage",
            "order": 18,
        },
    ],
    "settings_schema": {},
}
