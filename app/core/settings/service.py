from typing import Any

from app.core.models.settings import SystemSettings
from app.extensions import db


def get_system_setting(key: str, default: Any = None) -> Any:
    row = SystemSettings.query.get(key)
    return row.value if row else default


def set_system_setting(key: str, value: Any, updated_by_id: int | None = None) -> None:
    row = SystemSettings.query.get(key)
    if row:
        row.value = value
        row.updated_by = updated_by_id
    else:
        row = SystemSettings(key=key, value=value, updated_by=updated_by_id)
        db.session.add(row)
    db.session.commit()
