from abc import ABC, abstractmethod

from app.core.models.user import User


class AuthStrategy(ABC):
    name: str

    @abstractmethod
    def authenticate(self, credentials: dict) -> User | None: ...

    @abstractmethod
    def get_login_url(self) -> str | None: ...
