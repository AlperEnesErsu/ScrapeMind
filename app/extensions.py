from authlib.integrations.flask_client import OAuth
from flask_babel import Babel
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_mail import Mail
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect

db = SQLAlchemy()
mail = Mail()
migrate = Migrate()
login_manager = LoginManager()
oauth = OAuth()
babel = Babel()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address)


@login_manager.user_loader
def load_user(user_id: str):
    from app.core.models.user import User

    # Session is signed by Flask, so a tampered cookie won't get here — but
    # an in-flight signing-key rotation or a hand-edited session can leave
    # a non-numeric id. Treat anything unparseable as "no user".
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    return User.query.filter_by(id=uid, deleted_at=None).first()
