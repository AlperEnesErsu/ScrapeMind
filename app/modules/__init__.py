"""Plugin discovery.

Called from create_app() AFTER db.init_app() and AFTER migrations have run.
The call sequence is enforced by docker/entrypoint.sh:
    flask db upgrade  →  gunicorn wsgi:app (which calls create_app → discover_and_sync_modules)

If migrations haven't run, the `modules` table won't exist and this will crash.
"""

import importlib
import pkgutil
from pathlib import Path

import structlog

from app.extensions import db

logger = structlog.get_logger()

_MODULES_PKG = Path(__file__).parent


def discover_and_sync_modules() -> None:
    """Scan app/modules/, import each manifest, upsert into DB (idempotent)."""
    from app.core.models.menu import MenuItem
    from app.core.models.module import Module
    from app.core.models.permission import Permission

    for finder, name, ispkg in pkgutil.iter_modules([str(_MODULES_PKG)]):
        if name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"app.modules.{name}.manifest")
            manifest: dict = mod.MODULE
        except (ImportError, AttributeError):
            continue

        _sync_module(Module, Permission, MenuItem, manifest)

    db.session.commit()
    logger.info("plugin_discovery_done")


def _sync_module(Module, Permission, MenuItem, manifest: dict) -> None:  # noqa: N803
    # Model classes are passed in (rather than imported at module scope) so the
    # discovery loop can be unit-tested with stand-ins. Capitalised names mirror
    # the class identifiers; ruff's snake_case rule (N803) is suppressed here.
    code = manifest["code"]

    module = Module.query.get(code)
    if module is None:
        module = Module(
            code=code,
            name=manifest.get("name_key", code),
            version=manifest.get("version", "0.0.1"),
            settings_schema=manifest.get("settings_schema"),
        )
        db.session.add(module)
    else:
        module.version = manifest.get("version", module.version)
        module.settings_schema = manifest.get("settings_schema", module.settings_schema)

    for perm_def in manifest.get("permissions", []):
        perm = Permission.query.filter_by(code=perm_def["code"]).first()
        if perm is None:
            db.session.add(
                Permission(
                    code=perm_def["code"],
                    label_key=perm_def["label_key"],
                    module_code=code,
                )
            )

    for menu_def in manifest.get("menu", []):
        item = MenuItem.query.filter_by(code=menu_def["code"]).first()
        if item is None:
            db.session.add(
                MenuItem(
                    code=menu_def["code"],
                    label_key=menu_def["label_key"],
                    icon=menu_def.get("icon"),
                    endpoint=menu_def.get("endpoint"),
                    required_permission=menu_def.get("required_permission"),
                    order_index=menu_def.get("order", 0),
                    module_code=code,
                )
            )
