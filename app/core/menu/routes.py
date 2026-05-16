from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _
from flask_login import login_required

from app.core.audit.middleware import log_action
from app.core.auth.decorators import permission_required
from app.core.menu.forms import MenuItemForm
from app.core.menu.service import (
    create_item,
    endpoint_exists,
    get_item,
    hard_delete,
    has_cycle,
    is_critical,
    list_items,
    module_choices,
    parent_choices,
    permission_choices,
    toggle_visible,
    update_item,
)
from app.core.models.menu import MenuItem

menu_bp = Blueprint("menu", __name__)


def _populate_choices(form: MenuItemForm, exclude_id: int | None = None) -> None:
    form.parent_id.choices = parent_choices(exclude_id=exclude_id)
    form.module_code.choices = module_choices()
    form.required_permission.choices = permission_choices()


def _form_data_dict(form: MenuItemForm) -> dict:
    return {
        "code": form.code.data or "",
        "label_key": form.label_key.data or "",
        "icon": form.icon.data,
        "url": form.url.data,
        "endpoint": form.endpoint.data,
        "module_code": form.module_code.data,
        "required_permission": form.required_permission.data,
        "order_index": form.order_index.data,
        "is_visible": form.is_visible.data,
        "parent_id": form.parent_id.data,
    }


def _validate_business(form: MenuItemForm, item_id: int | None) -> bool:
    """Cycle + endpoint validation. Returns True if ok."""
    ok = True
    if form.endpoint.data and not endpoint_exists(form.endpoint.data.strip()):
        form.endpoint.errors = [*form.endpoint.errors, _("Unknown endpoint.")]
        ok = False
    parent_id = form.parent_id.data if form.parent_id.data and int(form.parent_id.data) != 0 else None
    if parent_id and has_cycle(item_id, parent_id):
        form.parent_id.errors = [*form.parent_id.errors, _("Parent would create a cycle.")]
        ok = False
    return ok


@menu_bp.route("/")
@login_required
@permission_required("menu.view")
def menu_list():
    return render_template("menu/list.html", items=list_items(), is_critical=is_critical)


@menu_bp.route("/new", methods=["GET", "POST"])
@login_required
@permission_required("menu.manage")
def item_new():
    form = MenuItemForm()
    _populate_choices(form)
    if form.validate_on_submit() and _validate_business(form, item_id=None):
        if MenuItem.query.filter_by(code=form.code.data.strip()).first():
            form.code.errors = [*form.code.errors, _("This code is already in use.")]
        else:
            item = create_item(_form_data_dict(form))
            log_action("menu.create", entity_type="menu_item", entity_id=item.id,
                       changes={"code": item.code})
            flash(_("Menu item created."), "success")
            return redirect(url_for("menu.menu_list"))
    return render_template("menu/menu_form.html", form=form, item=None)


@menu_bp.route("/<int:item_id>/edit", methods=["GET", "POST"])
@login_required
@permission_required("menu.manage")
def item_edit(item_id: int):
    item = get_item(item_id)
    if item is None:
        abort(404)
    form = MenuItemForm(obj=item)
    _populate_choices(form, exclude_id=item.id)
    if request.method == "GET":
        form.parent_id.data = item.parent_id or 0
        form.module_code.data = item.module_code or ""
        form.required_permission.data = item.required_permission or ""
    if form.validate_on_submit() and _validate_business(form, item_id=item.id):
        update_item(item, _form_data_dict(form))
        log_action("menu.update", entity_type="menu_item", entity_id=item.id,
                   changes={"code": item.code})
        flash(_("Menu item updated."), "success")
        return redirect(url_for("menu.menu_list"))
    return render_template("menu/menu_form.html", form=form, item=item)


@menu_bp.route("/<int:item_id>/delete", methods=["POST"])
@login_required
@permission_required("menu.manage")
def item_delete(item_id: int):
    item = get_item(item_id)
    if item is None:
        abort(404)
    if is_critical(item):
        flash(_("This menu item is critical and cannot be deleted."), "danger")
        return redirect(url_for("menu.menu_list"))
    code = item.code
    hard_delete(item)
    log_action("menu.delete", entity_type="menu_item", entity_id=item_id,
               changes={"code": code})
    flash(_("Menu item deleted."), "success")
    return redirect(url_for("menu.menu_list"))


@menu_bp.route("/<int:item_id>/toggle", methods=["POST"])
@login_required
@permission_required("menu.manage")
def item_toggle(item_id: int):
    item = get_item(item_id)
    if item is None:
        abort(404)
    new_state = toggle_visible(item)
    log_action("menu.toggle", entity_type="menu_item", entity_id=item.id,
               changes={"is_visible": new_state})
    flash(_("Visibility updated."), "success")
    return redirect(url_for("menu.menu_list"))
