from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, Email, EqualTo, Length, Regexp

from app.core.auth.password_policy import wtf_validator as _pw_policy


class RegisterForm(FlaskForm):
    username = StringField(
        _l("Username"),
        validators=[
            DataRequired(),
            Length(min=3, max=64),
            Regexp(
                r"^[a-zA-Z0-9_.-]+$", message=_l("Use letters, digits, dot, dash, underscore only.")
            ),
        ],
    )
    email = StringField(_l("Email"), validators=[DataRequired(), Email(), Length(max=255)])
    full_name = StringField(_l("Full Name"), validators=[DataRequired(), Length(min=2, max=128)])
    password = PasswordField(_l("Password"), validators=[DataRequired(), _pw_policy])
    password2 = PasswordField(
        _l("Confirm Password"),
        validators=[DataRequired(), EqualTo("password", message=_l("Passwords do not match."))],
    )
    submit = SubmitField(_l("Create Account"))


class LoginForm(FlaskForm):
    username = StringField(_l("Username or Email"), validators=[DataRequired()])
    password = PasswordField(_l("Password"), validators=[DataRequired()])
    remember_me = BooleanField(_l("Remember me"))
    submit = SubmitField(_l("Sign In"))


class PasswordResetRequestForm(FlaskForm):
    email = StringField(_l("Email"), validators=[DataRequired(), Email()])
    submit = SubmitField(_l("Send Reset Link"))


class PasswordResetForm(FlaskForm):
    password = PasswordField(_l("New Password"), validators=[DataRequired(), _pw_policy])
    password2 = PasswordField(
        _l("Confirm Password"),
        validators=[DataRequired(), EqualTo("password")],
    )
    submit = SubmitField(_l("Reset Password"))
