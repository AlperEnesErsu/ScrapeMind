# 🧠 ScrapeMind

> Akademik makalelerden ve güncel gelişmelerden anlamlı çıkarımlar üreten, kişiselleştirilmiş bir bilgi keşif platformu — **Flask Core Base** üzerine inşa edilmiş.

**🔗 Repo:** [github.com/AlperEnesErsu/ScrapeMind](https://github.com/AlperEnesErsu/ScrapeMind)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Phase 2](https://img.shields.io/badge/phase%202-complete-success.svg)](#-yol-haritası)

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
| DB | PostgreSQL 17 (pgvector henüz **yok** — semantik arama/RAG için planlı) |
| Auth | Flask-Login + passlib (argon2) + Authlib (OAuth) |
| Form | Flask-WTF + WTForms |
| **i18n** | **Flask-Babel — TR + EN (Faz 1'den itibaren)** |
| Frontend | Jinja2 + Bootstrap 5.3 + HTMX + Bootstrap Icons |
| Logging | structlog (JSON log) |
| Test | pytest + pytest-flask + factory-boy |
| Lint | ruff + black + mypy + pre-commit |
| Container | Docker + docker-compose |
| Task queue | Celery 5.4 + Redis (prefork `worker` + threaded `worker-io` + tek replika `beat`) |
| Scraping | `arxiv` SDK · `feedparser` · `requests` — **tarayıcı otomasyonu yok** (Scrapy/Playwright/Selenium kullanılmıyor) |
| LLM | Çok sağlayıcılı: OpenRouter (varsayılan) · Ollama (yerel) · Anthropic |

> Scraping ve LLM bileşenleri `app/modules/`'da yaşar, `core`'a sızmaz.
> Veri toplama katmanının tamamı: **[docs/SCRAPING.md](docs/SCRAPING.md)**

## ✨ Hazır Olanlar

### Çekirdek (`app/core/`)

- 🔐 **Auth**: Local + OAuth (Google/Microsoft) — strategy pattern; LDAP/JWT için iskelet
- 👥 **Kullanıcı kaydı + şifre sıfırlama** (token tabanlı, 1 saat geçerli)
- 🛡️ **RBAC**: Rol/izin yönetimi, rol-izin matrisi, dinamik menü
- 🧭 **Dinamik menü**: DB'den okunan, izin filtreli sol sidebar
- 👤 **Profil sayfası**: 6 sekme (Kişisel/E-posta/Şifre/Tercih/OAuth/Hesap) — HTMX
- 📜 **Denetim kaydı (audit log)**: kritik aksiyonlarda otomatik kayıt
- 🌐 **i18n**: TR + EN, derlenmiş `.mo`, kullanıcı bazlı dil seçimi
- 🎨 **UI**: Sol sidebar + topbar, light/dark tema, mobile-responsive (Bootstrap 5)
- 🛑 **Güvenlik**: Argon2 hashing, CSRF, rate limit, brute-force lock (5/15dk)
- 🔑 **2FA (TOTP)** + recovery kodları, oturum yönetimi, avatar upload
- 🔌 **API v1 (JWT)**: okuma + yazma + token revocation — [docs/API_V1.md](docs/API_V1.md)
- 📜 **Audit retention**: `AUDIT_RETENTION_DAYS` + gecelik purge

### ScrapeMind modülleri (`app/modules/`)

- 📚 **Akademik kaynaklar**: arXiv · Semantic Scholar · PubMed
- 📰 **RSS/Atom**: 4 küratörlü besleme + kullanıcının kendi beslemeleri (SSRF korumalı)
- 🎯 **İlgi-farkında kaynak seçici**: konu sınıflandırması + kaynak başına opt-out
- 🌍 **TR→EN anahtar kelime çevirisi**: Türkçe ilgi alanları İngilizce korpuslarda eşleşsin diye
- 🤖 **AI**: makale analizi, TR çeviri, makale sohbeti, günlük/haftalık özet (digest)
- 📊 **Tarama geçmişi**: `ScanRun` + canlı durum paneli, gerçek "sıradaki tarama" saati
- 📖 **Discover feed / Kütüphane**: favoriler, notlar, read-later, bulk actions, BibTeX

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

## ⚙️ Arka Plan İşleri (Celery)

```bash
docker compose -f docker/docker-compose.yml --profile tasks up -d worker worker-io beat
```

- `worker` — prefork, `-Q celery,scrape,llm`. DB/LLM ağırlıklı işler.
- `worker-io` — threaded, `-Q io`. Besleme çekme saf ağ beklemesi olduğu için 16 thread.
- `beat` — **tek replika olmalı**; zamanlama yerel dosyada, ikinci beat her şeyi çift tetikler.

Worker ayakta değilse uygulama çalışmaya devam eder — "Tara" butonu 90sn sonra
"worker yok" durumunu gösterir, sonsuza dek dönmez. Zamanlama tablosu ve gerekçeleri:
[docs/SCRAPING.md](docs/SCRAPING.md).

## 🤖 AI Yapılandırması

AI özellikleri **varsayılan olarak kapalı** ve açmak ücretsiz olabilir:

| Sağlayıcı | `LLM_PROVIDER` | Anahtar |
|---|---|---|
| OpenRouter *(varsayılan)* | `openrouter` | `OPENROUTER_API_KEY` — model varsayılanı `:free` sonekli |
| Ollama (yerel) | `ollama` | Gerekmez |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` |

Kullanıcılar Profil → AI Ayarları'ndan **kendi anahtarlarını** girebilir; kullanıcı
anahtarı global fallback'i ezer ve `UserSettings` içinde Fernet ile şifreli saklanır.

## 🧪 Test

```bash
pytest tests/                          # 426 test
pytest tests/ --cov=app --cov-report=term-missing
```

Test DB ayrı: `scrapemind_test`. `conftest.py` her oturumda `create_all` / `drop_all` yapar.
Test config'i bilinçli olarak Redis'i kapalı bir porta yönlendirir ve LLM anahtarlarını
boşaltır — gerekçeleri [docs/HANDOVER.md §4.5](docs/HANDOVER.md).

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
│   │   ├── health.py            # altyapı canlılık paneli (heartbeat okur)
│   │   └── api/v1/              # JWT JSON API
│   ├── modules/                 # 🎯 PROJE MODÜLLERİ
│   │   ├── __init__.py          # plugin discovery
│   │   ├── _template/           # yeni modül şablonu
│   │   ├── dashboard/           # ana sayfa, ilgi alanları, kaynak seçici
│   │   ├── academic/            # kimlikler (ORCID/Scopus/WoS) + Keyword sözlüğü
│   │   └── scrape/              # 🔍 veri toplama
│   │       ├── sources/         # adaptörler: arxiv, semantic_scholar, pubmed, rss
│   │       ├── service.py       # orkestrasyon, kilit, ScanRun, kaynak tercihleri
│   │       ├── ai_service.py    # çok sağlayıcılı LLM: analiz, çeviri, digest, konu
│   │       ├── net_guard.py     # SSRF koruması (kullanıcı URL'leri)
│   │       └── ratelimit.py     # Redis tabanlı, dağıtım geneli API bütçesi
│   └── tasks/                   # Celery
│       ├── schedule.py          # BEAT_SCHEDULE (saatler Europe/Istanbul!)
│       ├── schedule_info.py     # crontab → gerçek "sıradaki çalışma" zamanı
│       ├── fanout.py            # deterministik kullanıcı dağıtımı
│       └── {core,scrape,feed,digest}_tasks.py
├── translations/                # Babel — tr/en .po + .mo
├── migrations/versions/         # Alembic
├── tests/{core,modules}/        # 426 test
├── scripts/{seed.py, create_module.py, export_core_template.py}
├── docker/{Dockerfile, docker-compose.yml}
├── docs/                        # SCRAPING.md, HANDOVER.md, API_V1.md, UI_REVIEW.md
├── babel.cfg, pyproject.toml, requirements.txt
├── wsgi.py
├── setup.bat, development.bat   # Windows hızlı başlangıç
├── CLAUDE.md                    # AI asistanı bağlam dosyası
└── PROJECT.md                   # detaylı tasarım dokümanı
```

## 🗺️ Yol Haritası

- ✅ **Faz 0** — Repo + Docker + pyproject + pre-commit + CI iskeleti
- ✅ **Faz 1** — Auth (Local+OAuth), Register/Reset, RBAC, Menu, Profile, Audit, i18n (TR/EN), UI shell
- ✅ **Faz 2** — Celery + Redis · arXiv + Semantic Scholar + PubMed · Discover/Library UI · AI analiz & TR çeviri · 2FA (TOTP) · SMTP · Avatar upload · [API v1 (JWT)](docs/API_V1.md) + token revocation · Audit retention
- 🔶 **Faz 3** *(devam ediyor)* — ✅ RSS beslemeler + kullanıcı beslemeleri · ✅ konu sınıflandırma + kaynak seçici · ✅ TR→EN anahtar kelime çevirisi · ✅ tarama geçmişi + durum paneli · ✅ digest · ✅ çok sağlayıcılı LLM · ⏳ DOI tekilleştirmesi · ⏳ OpenAlex + Crossref · ⏳ RSS'siz site scrape'i
- 🔮 **Faz 4** — pgvector + semantik arama/RAG · yazar takibi (ORCID) · atıf grafiği · LDAP · Sentry/Prometheus

Detaylı plan: [PROJECT.md](PROJECT.md) · Sıradaki iş ve gerekçeleri: [docs/HANDOVER.md](docs/HANDOVER.md)

## ⚖️ Etik & Yasal

- Mümkün olan her yerde **resmî API'leri** tercih eder (arXiv, Semantic Scholar, PubMed, Crossref)
- Rate limit + dağıtık zamanlama ile hedef sunucuları yormaz
- Telif hakkıyla korunan içeriği **yeniden yayınlamaz** — yalnızca özet + kaynağa geri link
- Kullanıcının verdiği URL'ler SSRF guard'ından geçer (özel/iç ağ adresleri reddedilir)
- **X/Twitter kazınmaz** — ücretsiz okuma API'si yok, kazıma ToS ihlali olur
- `robots.txt` uyumu: genel sayfa scrape'i eklendiğinde **zorunlu**. Şu an yalnızca
  resmî API ve yayıncının kendi RSS'i çekildiği için devrede değil

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

> ⚠️ **Çeviri eklerken `pybabel extract` + `pybabel update` KULLANMA.** Bu akış bir kez
> fuzzy-match kazasıyla ~300 TR çeviriyi sildi. Yeni string'leri Babel API ile tek tek
> ekle — reçete: [CLAUDE.md](CLAUDE.md) "Çeviri İş Akışı" bölümünde.

Projeye yeni katılıyorsan: **[docs/HANDOVER.md](docs/HANDOVER.md)** ile başla —
kurulum, commit geçmişi, tuzaklar ve sıradaki görevler orada.

## 📄 Lisans

[MIT](LICENSE) — Telif: Alper Enes Ersü
