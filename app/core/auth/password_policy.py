"""Merkezi şifre politikası.

Kurallar:
  - En az 8 karakter
  - En az 1 büyük harf
  - En az 1 küçük harf
  - En az 1 rakam

Kurallar burada tanımlanır; form validatörü, service ve template
hep buraya bakar — tek kaynak.
"""

from __future__ import annotations

from dataclasses import dataclass

from flask_babel import lazy_gettext as _l


@dataclass(frozen=True)
class PolicyRule:
    key: str  # makine adı (test için)
    check: object  # callable(password) -> bool
    label: object  # lazy_gettext string (template'de gösterilir)
    error: object  # lazy_gettext string (form hatasında gösterilir)


RULES: list[PolicyRule] = [
    PolicyRule(
        key="length",
        check=lambda p: len(p) >= 8,
        label=_l("At least 8 characters"),
        error=_l("Password must be at least 8 characters."),
    ),
    PolicyRule(
        key="uppercase",
        check=lambda p: any(c.isupper() for c in p),
        label=_l("At least one uppercase letter"),
        error=_l("Password must contain at least one uppercase letter."),
    ),
    PolicyRule(
        key="lowercase",
        check=lambda p: any(c.islower() for c in p),
        label=_l("At least one lowercase letter"),
        error=_l("Password must contain at least one lowercase letter."),
    ),
    PolicyRule(
        key="digit",
        check=lambda p: any(c.isdigit() for c in p),
        label=_l("At least one digit"),
        error=_l("Password must contain at least one digit."),
    ),
]


def check_password(password: str) -> list[PolicyRule]:
    """Başarısız olan kuralları döner. Boş liste = geçerli şifre."""
    return [r for r in RULES if not r.check(password)]


def is_valid(password: str) -> bool:
    return len(check_password(password)) == 0


def wtf_validator(form, field):
    """WTForms ValidationError fırlatan validator — formlara ekle."""
    from wtforms.validators import ValidationError

    failed = check_password(field.data or "")
    if failed:
        raise ValidationError(str(failed[0].error))
