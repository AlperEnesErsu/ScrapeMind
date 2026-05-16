from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import BooleanField, IntegerField, SelectField, StringField, SubmitField
from wtforms.validators import DataRequired, Length, NumberRange, Optional, Regexp


class MenuItemForm(FlaskForm):
    code = StringField(
        _l("Code"),
        validators=[
            DataRequired(),
            Length(min=2, max=64),
            Regexp(
                r"^[a-z][a-z0-9_]*$", message=_l("Lowercase letters, digits and underscores only.")
            ),
        ],
    )
    label_key = StringField(_l("Label Key"), validators=[DataRequired(), Length(min=2, max=128)])
    icon = StringField(_l("Icon (Bootstrap Icons class)"), validators=[Optional(), Length(max=64)])
    url = StringField(_l("URL"), validators=[Optional(), Length(max=512)])
    endpoint = StringField(_l("Endpoint"), validators=[Optional(), Length(max=128)])
    parent_id = SelectField(_l("Parent"), coerce=int, choices=[], validate_choice=False)
    module_code = SelectField(_l("Module"), choices=[], validate_choice=False)
    required_permission = SelectField(_l("Required Permission"), choices=[], validate_choice=False)
    order_index = IntegerField(_l("Order"), validators=[NumberRange(min=0, max=9999)], default=0)
    is_visible = BooleanField(_l("Visible"), default=True)
    submit = SubmitField(_l("Save"))
