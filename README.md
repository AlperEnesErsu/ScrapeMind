# 🧠 ScrapeMind

> Akademik makalelerden ve güncel gelişmelerden anlamlı çıkarımlar üreten, kişiselleştirilmiş bir bilgi keşif platformu — **Flask Core Base** üzerine inşa edilmiş.

**🔗 Repo:** [github.com/AlperEnesErsu/ScrapeMind](https://github.com/AlperEnesErsu/ScrapeMind)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Phase 1](https://img.shields.io/badge/phase%201-complete-success.svg)](#-yol-haritası)

---

## 🎯 Proje Amacı

İnternet her gün milyonlarca yeni içerikle büyüyor; ama gerçekten **anlamlı** olan bilgiye ulaşmak giderek zorlaşıyor. ScrapeMind, kullanıcının ilgi alanlarına göre:

- 📰 **Güncel haberleri** ve sektörel gelişmeleri,
- 📚 **Akademik makaleleri** (arXiv, Semantic Scholar, PubMed vb.),
- 🔍 Bu kaynaklar arasındaki **örüntü, trend ve büyük çıkarımları**

otomatik olarak toplayıp özetler, sınıflandırır ve kullanıcıya günlük bir "bilgi özeti" olarak sunar.

## 🏗️ Mimari — "Base + Proje"

Proje iki şapkalı geliştirilir:

| Şapka | İçerik | Yer |
|---|---|---|
| 🧱 **`flask-core-base`** | Auth, RBAC, Menu, Settings, Audit, i18n, UI shell | `app/core/` |
| 🎯 **ScrapeMind** | Web scraping iş modülleri (proje-özgü, Faz 2) | `app/modules/` |

Gelecek projelerde sadece `app/modules/` boşaltılıp yeni modüller eklenerek aynı çekirdek kullanılabilir.

## 🧰 Teknoloji Stack'i

| Katman | Araç |
|---|---|
| Web framework | Flask 3.x + Blueprint + plugin discovery |
| ORM | SQLAlchemy 2.x (Flask-SQLAlchemy 3.x) |
| Migration | Alembic (Flask-Migrate) |
| DB | PostgreSQL 17 (Faz 2'de pgvector eklenecek) |
| Auth | Flask-Login + passlib (argon2) + Authlib (OAuth) |
| Form | Flask-WTF + WTForms |
| **i18n** | **Flask-Babel — TR + EN (Faz 1'den itibaren)** |
| Frontend | Jinja2 + Bootstrap 5.3 + HTMX + Bootstrap Icons |
| Logging | structlog (JSON log) |
| Test | pytest + pytest-flask + factory-boy |
| Lint | ruff + black + mypy + pre-commit |
| Container | Docker + docker-compose |
| Task queue (Faz 2) | Celery + Redis |
| Scraping (Faz 2) | Scrapy, Playwright, BeautifulSoup — modül katmanında |
| LLM (Faz 2) | Claude API (Anthropic SDK) |

> Scraping ve LLM bileşenleri Faz 2'de gelecek; `app/modules/`'a yazılacak, `core`'a sızmayacak.

## ✨ Faz 1'de Hazır

- 🔐 **Auth**: Local + OAuth (Google/Microsoft) — strategy pattern; LDAP/JWT için iskelet
- 👥 **Kullanıcı kaydı + şifre sıfırlama** (token tabanlı, 1 saat geçerli)
- 🛡️ **RBAC**: Rol/izin yönetimi, rol-izin matrisi, dinamik menü
- 🧭 **Dinamik menü**: DB'den okunan, izin filtreli sol sidebar
- 👤 **Profil sayfası**: 6 sekme (Kişisel/E-posta/Şifre/Tercih/OAuth/Hesap) — HTMX
- 📜 **Denetim kaydı (audit log)**: kritik aksiyonlarda otomatik kayıt
- 🌐 **i18n**: TR + EN, derlenmiş `.mo`, kullanıcı bazlı dil seçimi
- 🎨 **UI**: Sol sidebar + topbar, light/dark tema, mobile-responsive (Bootstrap 5)
- 🛑 **Güvenlik**: Argon2 hashing, CSRF, rate limit, brute-force lock (5/15dk)

## 🚀 Hızlı Başlangıç (Windows)

İlk kurulum:
```bash
setup.bat
```
Bu komut:
1. Python kontrolü + venv oluşturma
2. `requirements.txt` kurulumu
3. `.env` hazırlama
4. Docker Compose ile Postgres + Redis başlatma
5. Migration + seed
6. htmx indirme

Sonraki günler:
```bash
development.bat
```
Sunucu http://localhost:5000 adresinde açılır.

**Varsayılan admin:** `admin` / `admin1234`

### Manuel kurulum

```bash
git clone https://github.com/AlperEnesErsu/ScrapeMind.git
cd ScrapeMind
python -m venv venv
venv\Scripts\activate                 # Linux/macOS: source venv/bin/activate
pip install -r requirements.txt
copy .env.example .env                # Linux/macOS: cp .env.example .env
docker compose -f docker/docker-compose.yml up -d db redis
pybabel compile -d translations
set FLASK_APP=wsgi.py                 # Linux/macOS: export FLASK_APP=wsgi.py
flask db upgrade
python scripts/seed.py
flask run --debug
```

## 📧 Email Yapılandırması

Email kodu `app/core/email/service.py` içinde Flask-Mail üzerinden. **`MAIL_SERVER` boşsa dev modu** — gerçek SMTP çağrısı yapılmaz, üretilen link `flash` ile gösterilir (dev kullanıcı için).

### Lokal test — Mailhog (önerilen)

```bash
docker compose -f docker/docker-compose.yml --profile mail up -d mailhog
```

`.env`:
```env
MAIL_SERVER=localhost
MAIL_PORT=1025
MAIL_USE_TLS=false
MAIL_USE_SSL=false
```

Şifre sıfırlama / akademik email doğrulama tetikledikten sonra **http://localhost:8025** adresinden gelen kutusunu izle.

### Production sağlayıcıları

`.env.example` içinde Gmail, Resend ve Amazon SES için hazır blok mevcut. `MAIL_USE_TLS` (587/STARTTLS) ile `MAIL_USE_SSL` (465) **aynı anda true olamaz** — sağlayıcıya göre birini seç.

`MAIL_SUPPRESS_SEND=true` ile staging'de email göndermeyi geçici olarak kapatabilirsin (credential silmeden).

## 🧪 Test

```bash
pytest tests/                          # 37 test
pytest tests/ --cov=app --cov-report=term-missing
```

Test DB ayrı: `scrapemind_test`. `conftest.py` her oturumda `create_all` / `drop_all` yapar.

## 📂 Klasör Yapısı

```
ScrapeMind/
├── app/
│   ├── __init__.py              # create_app() factory
│   ├── extensions.py            # db, login_manager, migrate, oauth, babel, csrf, limiter
│   ├── config.py                # Dev/Prod/Test config sınıfları
│   ├── core/                    # 🧱 BASE — Faz 1'de tamamlandı
│   │   ├── base_model.py        # BaseModel (id, created_at, updated_at, deleted_at)
│   │   ├── models/              # user, role, permission, menu, module, settings, audit, oauth
│   │   ├── auth/                # strategies/, routes, forms, decorators, service
│   │   ├── rbac/                # service, routes, forms — rol-izin CRUD
│   │   ├── menu/                # builder, service, routes, forms — menü CRUD
│   │   ├── settings/            # routes, forms, service — profil/sistem ayarları
│   │   ├── audit/               # middleware (log_action), routes
│   │   ├── i18n/                # locale_selector
│   │   ├── templates/           # base.html, _sidebar, _topbar, auth/, rbac/, menu/, settings/, errors/
│   │   └── static/              # css/theme.css, js/htmx.min.js, js/app.js
│   └── modules/                 # 🎯 PROJE MODÜLLERİ
│       ├── __init__.py          # plugin discovery
│       ├── _template/           # yeni modül şablonu
│       └── dashboard/           # örnek modül
├── translations/                # Babel — tr/en .po + .mo
├── migrations/versions/         # Alembic
├── tests/core/                  # auth, rbac, menu, profile, register/reset, models
├── scripts/{seed.py, create_module.py}
├── docker/{Dockerfile, docker-compose.yml}
├── babel.cfg, pyproject.toml, requirements.txt
├── wsgi.py
├── setup.bat, development.bat   # Windows hızlı başlangıç
└── PROJECT.md                   # detaylı tasarım dokümanı
```

## 🗺️ Yol Haritası

- ✅ **Faz 0** — Repo + Docker + pyproject + pre-commit + CI iskeleti
- ✅ **Faz 1** — Auth (Local+OAuth), Register/Reset, RBAC, Menu, Profile, Audit, i18n (TR/EN), UI shell
- ⏳ **Faz 2** — Celery + Redis, **ScrapeMind iş modülleri** (Scrapy/Playwright/LLM), 2FA, SMTP, API v1 (JWT)
- 🔮 **Faz 3** — LDAP, JWT API auth, Audit partition, Redis cache, Sentry, Prometheus

Detaylı plan: [PROJECT.md](PROJECT.md)

## ⚖️ Etik & Yasal (Faz 2 scraping için)

- Hedef sitelerin `robots.txt` dosyasına uyar
- Rate limit + dağıtık scheduling ile sunucuları yormaz
- Mümkün olan her yerde **resmi API'leri** tercih eder (arXiv, Semantic Scholar, CrossRef)
- Telif hakkıyla korunan içeriği **yeniden yayınlamaz** — yalnızca özetler ve kaynağa link verir

## 👥 Ekip

| İsim | Rol | Sahip olduğu alan (Faz 1) |
|---|---|---|
| Alper Enes Ersü | Proje sahibi / Backend | `app/core/` genel + altyapı |
| Geliştirici A | Auth & User | `app/core/auth/` |
| Geliştirici B | RBAC & Menu & Plugin Loader | `app/core/{rbac,menu}/` |
| Geliştirici C | Profile, Settings, Audit, UI, i18n | `app/core/{settings,audit,i18n}/`, templates, translations |

## 🤝 Katkı

Branch stratejisi:
```
main ← prod
  └── dev ← entegrasyon
       └── feature/<alan>-<özet>
```

PR açmadan önce:
```bash
ruff check app/
black --check app/
pytest tests/
pybabel compile -d translations    # .po değiştiyse
```

## 📄 Lisans

[MIT](LICENSE) — Telif: Alper Enes Ersü
