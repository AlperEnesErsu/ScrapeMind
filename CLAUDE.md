# ScrapeMind — Bağlam Dosyası (Claude için)

## Projeye Genel Bakış
ScrapeMind aynı zamanda `flask-core-base` adlı yeniden kullanılabilir bir Flask iskeletidir.
**İki şapka:** `app/core/` = her projede kullanılabilecek çekirdek, `app/modules/` = ScrapeMind'a özel.

## Konum
`C:\Users\alper\OneDrive\Masaüstü\Project\ScrapeMind` (Windows geliştirme)

## Teknoloji
Flask 3.1 · SQLAlchemy 2 · PostgreSQL 17 · Bootstrap 5 + HTMX · Flask-Babel (TR/EN) · Celery 5.4 + Redis

## Veritabanı (yerel geliştirme)
- Postgres: `myo_postgres17` Docker container'ı (port 5432, user: postgres, pw: myopassword123, db: scrapemind)
- Redis: `shared_redis` Docker container'ı (port 6379)
- `.env.local` → `.env`'e kopyalanarak aktif edilir

## Kritik Mimari Kurallar
1. `app/core/` asla `app/modules/`'dan import ETMEZ
2. `is_superuser` bypass YALNIZCA `app/core/auth/decorators.py`'deki `permission_required`'da
3. Plugin discovery (`app/modules/__init__.py`) tablo yokken sessizce geçer
4. Migration sırası: `flask db upgrade` → uygulama başlatma (bkz. `docker/entrypoint.sh`)
5. Profil tab'ları genişletilebilir: `app/core/settings/tab_registry.py`

## Klasör Yapısı
```
app/core/          → Auth, RBAC, Menü, Settings, Audit, i18n, Email, Sessions, UI — dokunma
app/modules/       → ScrapeMind modülleri (scrape, academic, dashboard)
app/tasks/         → Celery task'ları (core_tasks, scrape_tasks, schedule)
translations/      → TR + EN .po/.mo dosyaları
scripts/           → seed.py, create_module.py, export_core_template.py
```

## Çeviri İş Akışı
```bash
# Yeni string eklenince:
pybabel extract -F babel.cfg -o translations/messages.pot .
pybabel update -i translations/messages.pot -d translations
# .po dosyalarını düzenle
pybabel compile -d translations
```

## Tamamlanan Faz Durumu (Mayıs 2026)
- **Faz 0** ✅ tam
- **Faz 1** ✅ tam (email servisi, password policy, session yönetimi `ed5d4ac` ile geldi)
- **Faz 2** 🔶 ilerliyor — bkz. aşağıdaki tablo

### Faz 2 — Şu ana kadar merge'lenenler
| PR  | Konu |
|-----|------|
| #4  | Multi-email + akademik kimlik temeli + UI cilası |
| #5  | Identity model düzeltmesi — `User.email` sadece auth, akademik kimlikler ayrı tabloda |
| #6  | ORCID / Scopus / WoS seed + admin academic profile paneli |
| #7  | Celery + Redis worker + Beat scheduler + `/admin/tasks` paneli |
| #8  | arXiv scraping — `Paper` modeli, source adapter, `/papers` feed |

### Faz 2 — Hâlâ bekleyenler
- **SMTP gerçek yapılandırması** (kod hazır; `.env.example` + prod dokümanı eksik)
- **2FA (TOTP)** — profile tab + login ikinci adım
- **Avatar dosya yükleme** (şu an sadece URL)
- **API v1 (JWT)** — `app/api/v1/` boş, JWT strategy iskelet hâlinde
- **Audit log retention + admin filtre/sayfalama**
- **Ek scraping source'ları** (Semantic Scholar, PubMed) ve scrape-now UX

## Bilinen Kısıtlar
- Email gönderimi `MAIL_SUPPRESS_SEND=true` ise dev modu — link `flash` ile gösteriliyor
- 2FA yok — sadece tek faktörlü auth (yerel + OAuth)
- Avatar yalnızca URL alanı, dosya upload yok
- JSON API yok — `/api/v1/` planlandı ama implement edilmedi

## Template Olarak Yeni Projede Kullanım
```bash
# 1. GitHub'da "Use this template" → yeni repo
# 2. Kopyaladıktan sonra:
python scripts/export_core_template.py --target . --name yeni_proje_adi
# 3. app/modules/_template/ kullanarak ilk modülü oluştur
python scripts/create_module.py ilk_modul_adi
```
