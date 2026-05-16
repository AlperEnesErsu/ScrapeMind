"""Yeni modül iskeleti oluşturur.

Kullanım:
    python scripts/create_module.py scrape
"""

import shutil
import sys
from pathlib import Path

MODULES_DIR = Path(__file__).parent.parent / "app" / "modules"
TEMPLATE_DIR = MODULES_DIR / "_template"


def create_module(code: str) -> None:
    target = MODULES_DIR / code
    if target.exists():
        print(f"Hata: {target} zaten mevcut.")
        sys.exit(1)

    shutil.copytree(TEMPLATE_DIR, target)

    # manifest.py içindeki REPLACE_ME'yi gerçek kod ile değiştir
    manifest = target / "manifest.py"
    manifest.write_text(manifest.read_text().replace("REPLACE_ME", code))

    # __init__.py oluştur
    (target / "__init__.py").write_text(
        f'from flask import Blueprint\n\n'
        f'{code}_bp = Blueprint("{code}", __name__, template_folder="templates")\n\n'
        f'from app.modules.{code} import routes  # noqa: E402, F401\n'
    )

    # routes.py oluştur
    (target / "routes.py").write_text(
        f'from flask import render_template\n'
        f'from flask_login import login_required\n\n'
        f'from app.modules.{code} import {code}_bp\n\n\n'
        f'@{code}_bp.route("/")\n'
        f'@login_required\n'
        f'def index():\n'
        f'    return render_template("{code}/index.html")\n'
    )

    # templates klasörü
    (target / "templates" / code).mkdir(parents=True)
    (target / "templates" / code / "index.html").write_text(
        '{{% extends \'base.html\' %}}\n'
        '{{% block content %}}\n'
        f'<h4>{code}</h4>\n'
        '{{% endblock %}}\n'
    )

    print(f"✓ Modül oluşturuldu: app/modules/{code}/")
    print(f"  → app/__init__.py içinde blueprint'i kaydet:")
    print(f"     from app.modules.{code} import {code}_bp")
    print(f"     app.register_blueprint({code}_bp, url_prefix='/{code}')")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Kullanım: python scripts/create_module.py <module_code>")
        sys.exit(1)
    create_module(sys.argv[1])
