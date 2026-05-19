"""Seed script — ilk admin kullanıcı, default roller, default menü.

Kullanım:
    python scripts/seed.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app
from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.menu import MenuItem
from app.core.models.permission import Permission
from app.core.models.role import Role
from app.core.models.settings import SystemSettings
from app.core.models.user import User
from app.extensions import db

app = create_app()

with app.app_context():
    # --- Permissions ---
    # --- Academic identifier types ---
    from app.modules.academic.models import IdentifierType

    identifier_seeds = [
        # code, name, regex, verification_method
        ("email", "Email", r"^[^@]+@[^@]+\.[^@]+$", "email_link"),
        ("orcid", "ORCID", r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$", "oauth"),
        ("scopus_id", "Scopus Author ID", r"^\d{10,11}$", "manual"),
        ("wos_id", "Web of Science Researcher ID", r"^[A-Z]-\d{4}-\d{4}$", "manual"),
    ]
    for code, name, regex, vmeth in identifier_seeds:
        if not IdentifierType.query.filter_by(code=code).first():
            db.session.add(
                IdentifierType(
                    code=code, name=name, validation_regex=regex, verification_method=vmeth
                )
            )
    db.session.flush()

    core_perms = [
        ("roles.view", "perm.roles.view"),
        ("roles.manage", "perm.roles.manage"),
        ("permissions.view", "perm.permissions.view"),
        ("menu.view", "perm.menu.view"),
        ("menu.manage", "perm.menu.manage"),
        ("audit.view", "perm.audit.view"),
        ("dashboard.view", "perm.dashboard.view"),
        ("users.view", "perm.users.view"),
        ("users.manage", "perm.users.manage"),
        ("system.manage", "perm.system.manage"),
        ("identifiers.self", "perm.identifiers.self"),
        ("identifiers.manage", "perm.identifiers.manage"),
        ("tasks.view", "perm.tasks.view"),
    ]
    for code, label_key in core_perms:
        if not Permission.query.filter_by(code=code).first():
            db.session.add(Permission(code=code, label_key=label_key))

    db.session.flush()

    # --- Admin role ---
    admin_role = Role.query.filter_by(name="admin").first()
    if not admin_role:
        admin_role = Role(name="admin", description="Full access")
        db.session.add(admin_role)
        db.session.flush()

    admin_role.permissions = Permission.query.all()

    # --- Default "user" role (assigned on self-register) ---
    user_role = Role.query.filter_by(name="user").first()
    if not user_role:
        user_role = Role(name="user", description="Default role for self-registered users")
        db.session.add(user_role)
        db.session.flush()
    # Give the default role just dashboard.view so newcomers can land somewhere.
    dashboard_view = Permission.query.filter_by(code="dashboard.view").first()
    if dashboard_view and dashboard_view not in user_role.permissions:
        user_role.permissions = [dashboard_view]

    # --- Admin user ---
    if not User.query.filter_by(username="admin").first():
        admin = User(
            username="admin",
            email="admin@scrapemind.local",
            full_name="Admin",
            is_superuser=True,
            password_hash=LocalAuthStrategy.hash_password("admin1234"),
        )
        db.session.add(admin)
        db.session.flush()
        admin.roles.append(admin_role)

    # --- System settings defaults ---
    defaults = {
        "oauth_auto_register": False,
        "app_name": "ScrapeMind",
    }
    for key, value in defaults.items():
        if not db.session.get(SystemSettings, key):
            db.session.add(SystemSettings(key=key, value=value))

    # --- Core menu items ---
    # Önce parent'lar, sonra children (flush ile ID üretilir)
    top_level = [
        dict(code="dashboard_root",  label_key="menu.dashboard", icon="bi-speedometer2",
             endpoint="dashboard.index", order_index=10),
        dict(code="scrape_feed",     label_key="menu.feed",      icon="bi-newspaper",
             endpoint="scrape.feed",    order_index=20),
        # Sistem Yönetimi grup başlığı (endpoint yok = sadece accordion)
        dict(code="admin_group",     label_key="menu.admin",     icon="bi-shield-shaded",
             order_index=80),
        dict(code="settings_profile", label_key="menu.profile",  icon="bi-person-circle",
             endpoint="settings.profile", order_index=90),
    ]
    for m in top_level:
        if not MenuItem.query.filter_by(code=m["code"]).first():
            db.session.add(MenuItem(**m))
    db.session.flush()

    # Sistem Yönetimi grubunun altındaki öğeler
    admin_group = MenuItem.query.filter_by(code="admin_group").first()
    admin_children = [
        dict(code="admin_users",       label_key="menu.users",        icon="bi-people",
             endpoint="users.user_list",         required_permission="users.view",        order_index=10),
        dict(code="admin_roles",       label_key="menu.roles",        icon="bi-shield-lock",
             endpoint="rbac.role_list",           required_permission="roles.view",        order_index=20),
        dict(code="admin_permissions", label_key="menu.permissions",  icon="bi-key",
             endpoint="rbac.permission_list",     required_permission="permissions.view",  order_index=30),
        dict(code="admin_menu",        label_key="menu.menu_items",   icon="bi-list-nested",
             endpoint="menu.menu_list",           required_permission="menu.view",         order_index=40),
        dict(code="admin_audit",       label_key="menu.audit",        icon="bi-journal-text",
             endpoint="audit.log_list",           required_permission="audit.view",        order_index=50),
        dict(code="admin_tasks",       label_key="menu.tasks",        icon="bi-cpu",
             endpoint="tasks_admin.overview",     required_permission="tasks.view",        order_index=60),
        dict(code="settings_system",   label_key="menu.system",       icon="bi-gear",
             endpoint="settings.system",          required_permission="system.manage",     order_index=70),
    ]
    for m in admin_children:
        if not MenuItem.query.filter_by(code=m["code"]).first():
            item = MenuItem(parent_id=admin_group.id if admin_group else None, **m)
            db.session.add(item)

    db.session.commit()
    print("Seed tamamlandi. Admin: admin / admin1234")
