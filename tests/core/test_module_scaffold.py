"""What `scripts/create_module.py` generates has to actually run.

CLAUDE.md points a new contributor at this script for their first module, and
for some time it wrote `{{% extends 'base.html' %}}` into the template -- a
literal that Jinja refuses to parse. The doubled braces were correct while the
whole block was one f-string; once it became one literal per line, the parts
without an `f` prefix stopped escaping anything and started emitting the braces
verbatim. Nothing noticed, because generated code has no tests unless someone
writes them.

Generating into a temporary directory rather than `app/modules/`, so a failing
run cannot leave debris in the package.
"""

from __future__ import annotations

import ast

import pytest
from jinja2 import Environment


@pytest.fixture
def generated(tmp_path, monkeypatch):
    """Scaffold a module into tmp_path and hand back its files."""
    from scripts import create_module as scaffold

    template_src = scaffold.TEMPLATE_DIR
    if not template_src.exists():
        pytest.skip(f"module template missing at {template_src}")

    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    monkeypatch.setattr(scaffold, "MODULES_DIR", modules_dir)

    scaffold.create_module("probe")
    return modules_dir / "probe"


def test_generated_python_parses(generated):
    for name in ("__init__.py", "routes.py"):
        source = (generated / name).read_text(encoding="utf-8")
        ast.parse(source)  # raises SyntaxError if the scaffold emits bad Python


def test_generated_template_is_valid_jinja(generated):
    """The failure that prompted this file.

    `{{% extends %}}` parses as an unexpected '%' and takes the whole page with
    it, so a scaffolded module 500s the first time it is opened.
    """
    source = (generated / "templates" / "probe" / "index.html").read_text(encoding="utf-8")
    Environment().parse(source)
    assert "{{%" not in source, "doubled braces are back; the escaping broke again"
    assert "{% extends" in source


def test_generated_blueprint_is_named_after_the_module(generated):
    init = (generated / "__init__.py").read_text(encoding="utf-8")
    routes = (generated / "routes.py").read_text(encoding="utf-8")
    assert "probe_bp = Blueprint(" in init
    assert "from app.modules.probe import probe_bp" in routes
    assert 'render_template("probe/index.html")' in routes
