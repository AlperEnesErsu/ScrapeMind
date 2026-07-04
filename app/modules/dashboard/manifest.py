MODULE = {
    "code": "dashboard",
    "name_key": "module.dashboard.name",
    "version": "1.0.0",
    "permissions": [
        {"code": "dashboard.view", "label_key": "perm.dashboard.view"},
        {"code": "dashboard.admin", "label_key": "perm.dashboard.admin"},
    ],
    "menu": [
        {
            "code": "dashboard_root",
            "label_key": "menu.dashboard",
            "icon": "bi-speedometer2",
            "endpoint": "dashboard.index",
            "order": 10,
        }
    ],
    "settings_schema": {},
}
