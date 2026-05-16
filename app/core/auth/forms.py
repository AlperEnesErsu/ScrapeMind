from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, Email, EqualTo, Length


class LoginForm(FlaskForm):
    username = StringField(_l("Username or Email"), validators=[DataRequired()])
    password = PasswordField(_l("Password"), validators=[DataRequired()])
    remember_me = BooleanField(_l("Remember me"))
    submit = SubmitField(_l("Sign In"))


class PasswordResetRequestForm(FlaskForm):
    email = StringField(_l("Email"), validators=[DataRequired(), Email()])
    submit = SubmitField(_l("Send Reset Link"))


class PasswordResetForm(FlaskForm):
    password = PasswordField(_l("New Password"), validators=[DataRequired(), Length(min=8)])
    password2 = PasswordField(
        _l("Confirm Password"),
        validators=[DataRequired(), EqualTo("password")],
    )
    submit = SubmitField(_l("Reset Password"))
