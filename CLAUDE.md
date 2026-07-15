# ScrapeMind — Bağlam Dosyası (Claude için)

## Projeye Genel Bakış
ScrapeMind aynı zamanda `flask-core-base` adlı yeniden kullanılabilir bir Flask iskeletidir.
**İki şapka:** `app/core/` = her projede kullanılabilecek çekirdek, `app/modules/` = ScrapeMind'a özel.

## Konum
`C:\Users\alper\OneDrive\Masaüstü\Project\ScrapeMind` (Windows geliştirme)

## Teknoloji
Flask 3.1 · SQLAlchemy 2 · PostgreSQL 17 · Bootstrap 5 + HTMX · Flask-Babel (TR/EN) · Celery 5.4 + Redis

## Veritabanı (yerel geliştirme)
- Postgres + Redis: `docker compose -f docker/docker-compose.yml up -d db redis` (container'lar: `docker-db-1`, `docker-redis-1`)
- Docker Desktop kararsız kapanırsa `docker-db-1` port mapping'ini kaybedebilir (`docker ps`'te `0.0.0.0:5432->5432` yerine sadece `5432/tcp` görünür). Çözüm: `docker rm -f docker-db-1 && docker compose -f docker/docker-compose.yml up -d db` (veri named volume'da, kaybolmaz)
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
> ⚠️ **`pybabel extract` + `pybabel update` KULLANMA** — bu akış bir kez mevcut
> TR çevirilerin ~300'ünü sildi (fuzzy-match kazası). Yeni string'leri Babel
> API ile **tek tek ekle**:
```python
# venv/Scripts/python.exe ile çalıştır:
from babel.messages.pofile import read_po, write_po
with open("translations/tr/LC_MESSAGES/messages.po", "rb") as f:
    cat = read_po(f)
cat.add("New string", string="Yeni metin")   # EN kataloğunda string=msgid
with open("translations/tr/LC_MESSAGES/messages.po", "wb") as f:
    write_po(f, cat, sort_output=False, sort_by_file=False)
```
```bash
# Sonra derle (bu güvenli):
pybabel compile -d translations
```
- TR ve EN kataloglarının **msgid key set'leri eşit olmalı** — CI bunu kontrol ediyor
- EN kataloğunda çeviri = msgid'nin kendisi (identity translation)

## Tamamlanan Faz Durumu (Temmuz 2026)
- **Faz 0** ✅ tam
- **Faz 1** ✅ tam (email servisi, password policy, session yönetimi `ed5d4ac` ile geldi)
- **Faz 2** 🔶 son düzlük — bkz. aşağıdaki tablo

### Faz 2 — Şu ana kadar merge'lenenler
| PR    | Konu |
|-------|------|
| #4-#6 | Multi-email, identity model düzeltmesi, ORCID/Scopus/WoS seed + admin panel |
| #7    | Celery + Redis worker + Beat scheduler + `/admin/tasks` paneli |
| #8    | arXiv scraping — `Paper` modeli, source adapter, `/papers` feed |
| #12-#16 | Yeni nesil frontend — Discover feed, `/library` timeline, notlar, AI analiz + TR çeviri (Claude API), BibTeX, mobil nav |
| #17   | RAG chat, bildirimler, read-later, bulk actions + HTTP test paketi |
| #18-#19 | Güvenlik sertleştirme (OAuth takeover, session fixation, open redirect, 2FA TTL, recovery race) + mimari düzeltmeler |
| #20-#22 | AI service testleri, TR çeviri kurtarma + audit label'ları, UX quick wins |
| —     | **2FA (TOTP)** — profil kurulum sihirbazı + login challenge + recovery kodları |
| #23   | **Avatar dosya yükleme** — Pillow yeniden kodlama, 256×256 WEBP |
| #24   | **API v1 (JWT)** — `/api/v1` bearer-token JSON API (bkz. `docs/API_V1.md`) |
| #25   | **Audit retention** — `AUDIT_RETENTION_DAYS` + gecelik purge task + admin rozeti |

### Faz 2 — Hâlâ bekleyenler
- **Ek scraping source'ları** (Semantic Scholar, PubMed) ve scrape-now UX — son kalan iş

## Bilinen Kısıtlar
- Email gönderimi `MAIL_SUPPRESS_SEND=true` ise dev modu — link `flash` ile gösteriliyor
- API v1 salt-okunur (auth + me/papers endpoint'leri); yazma endpoint'leri Faz 3'te bekliyor
- Scraping source'u yalnızca arXiv; Semantic Scholar/PubMed bekliyor (#8)

## Açık Kaynak Kuralları
- Bu repo **halka açık** — commit/PR/dokümantasyona gizli bilgi (şifre, API key, gerçek e-posta) yazma
- `.env` asla commit'lenmez; yeni config değişkeni eklerken `.env.example`'ı placeholder ile güncelle
- Örneklerde/testlerde `example.com` / `example.test` adresleri kullan

## Template Olarak Yeni Projede Kullanım
```bash
# 1. GitHub'da "Use this template" → yeni repo
# 2. Kopyaladıktan sonra:
python scripts/export_core_template.py --target . --name yeni_proje_adi
# 3. app/modules/_template/ kullanarak ilk modülü oluştur
python scripts/create_module.py ilk_modul_adi
```
