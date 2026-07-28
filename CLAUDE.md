# ScrapeMind — Bağlam Dosyası (Claude için)

## Projeye Genel Bakış
ScrapeMind aynı zamanda `flask-core-base` adlı yeniden kullanılabilir bir Flask iskeletidir.
**İki şapka:** `app/core/` = her projede kullanılabilecek çekirdek, `app/modules/` = ScrapeMind'a özel.

## Konum
`d:\app\ScrapeMind` (Windows geliştirme)

## Teknoloji
Flask 3.1 · SQLAlchemy 2 · PostgreSQL 17 · Bootstrap 5 + HTMX · Flask-Babel (TR/EN) · Celery 5.4 + Redis
LLM çok sağlayıcılı: OpenRouter (varsayılan, ücretsiz model) · Ollama (yerel) · Anthropic
Scraping: `arxiv` SDK · `feedparser` · `requests` — **tarayıcı otomasyonu yok**
(Scrapy/Playwright/Selenium kurulu değil; README/PROJECT.md'nin eski hali bunları
vaat ediyordu, doğru değil)

## Önce Bunları Oku
- `docs/SCRAPING.md` — veri toplama mimarisi, **yeni kaynak eklemeden önce zorunlu**
- `docs/HANDOVER.md` — durum, commit'lenmemiş iş, tuzaklar, sıradaki görevler

## Veritabanı (yerel geliştirme)
- Postgres + Redis: `docker compose -f docker/docker-compose.yml up -d db redis` (container'lar: `docker-db-1`, `docker-redis-1`)
- **Bu makinede ScrapeMind DB 5433'te** — 5432'yi başka bir projenin Postgres'i (`aisigner_db`, ona dokunma) tutuyor. `docker/.env` içinde `SCRAPEMIND_DB_PORT=5433`; kökteki `.env`'de `DATABASE_URL` ve `TEST_DATABASE_URL` 5433'ü gösterir. `TEST_DATABASE_URL` şart: `create_app()` conftest override'ından önce TestingConfig default'uyla (5432) bağlanmaya kalkar.
- Docker Desktop kararsız kapanırsa `docker-db-1` port mapping'ini kaybedebilir (`docker ps`'te mapping görünmez). Çözüm: `docker rm -f docker-db-1 && docker compose -f docker/docker-compose.yml up -d db` (veri named volume'da, kaybolmaz)
- `.env.local` → `.env`'e kopyalanarak aktif edilir

## Kritik Mimari Kurallar
1. `app/core/` asla `app/modules/`'dan import ETMEZ
2. `is_superuser` bypass YALNIZCA `app/core/auth/decorators.py`'deki `permission_required`'da
3. Plugin discovery (`app/modules/__init__.py`) tablo yokken sessizce geçer
4. Migration sırası: `flask db upgrade` → uygulama başlatma (bkz. `docker/entrypoint.sh`)
5. Profil tab'ları genişletilebilir: `app/core/settings/tab_registry.py`
6. Kaynak adaptörleri **duck-typed modül**, ABC yok — `SOURCE_NAME` + `search()` +
   `search_for_keywords()`. Yeni kaynak = modül + `sources/__init__.py`'de 3 satır
7. Adaptörler modül seviyesinde `requests` kullanır — testler modülün kendi
   `requests`'ini monkeypatch'liyor, ortak wrapper'ın arkasına saklama

## Klasör Yapısı
```
app/core/          → Auth, RBAC, Menü, Settings, Audit, i18n, Email, Sessions, API v1, UI — dokunma
app/modules/
  ├── dashboard/   → ana sayfa, ilgi alanları, kaynak seçici kartı
  ├── academic/    → kimlikler (ORCID/Scopus/WoS), Keyword sözlüğü (+ TR→EN çeviri kolonları)
  └── scrape/      → sources/ (adaptörler) · service.py (orkestrasyon) · ai_service.py (LLM)
                     net_guard.py (SSRF) · ratelimit.py (Redis bütçe) · forms.py
app/tasks/         → core_tasks, scrape_tasks, feed_tasks, digest_tasks,
                     schedule (BEAT_SCHEDULE), schedule_info (crontab→zaman), fanout
translations/      → TR + EN .po/.mo dosyaları
scripts/           → seed.py, create_module.py, export_core_template.py
docs/              → SCRAPING.md, HANDOVER.md, API_V1.md, UI_REVIEW.md
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
- **Faz 2** ✅ tam — Semantic Scholar + PubMed `#27` ile kapandı
- **Faz 3** 🔶 devam ediyor — 17 commit `feat/homepage-source-selection`'da, aşağıya bak

### Faz 2 — merge'lenenler
| PR    | Konu |
|-------|------|
| #4-#6 | Multi-email, identity model düzeltmesi, ORCID/Scopus/WoS seed + admin panel |
| #7    | Celery + Redis worker + Beat scheduler + `/admin/tasks` paneli |
| #8    | arXiv scraping — `Paper` modeli, source adapter, `/papers` feed |
| #12-#16 | Yeni nesil frontend — Discover feed, `/library` timeline, notlar, AI analiz + TR çeviri (Claude API), BibTeX, mobil nav |
| #17   | Makale sohbeti, bildirimler, read-later, bulk actions + HTTP test paketi |
| #18-#19 | Güvenlik sertleştirme (OAuth takeover, session fixation, open redirect, 2FA TTL, recovery race) + mimari düzeltmeler |
| #20-#22 | AI service testleri, TR çeviri kurtarma + audit label'ları, UX quick wins |
| —     | **2FA (TOTP)** — profil kurulum sihirbazı + login challenge + recovery kodları |
| #23   | **Avatar dosya yükleme** — Pillow yeniden kodlama, 256×256 WEBP |
| #24   | **API v1 (JWT)** — `/api/v1` bearer-token JSON API (bkz. `docs/API_V1.md`) |
| #25   | **Audit retention** — `AUDIT_RETENTION_DAYS` + gecelik purge task + admin rozeti |

| #27   | **Semantic Scholar + PubMed** — çok kaynaklı tarama (Faz 2 kapandı) |
| #28-#30 | Refresh-token revocation · API yazma endpoint'leri · RBAC izin cache'i |

### Faz 3 — branch `feat/homepage-source-selection` (main'in 17 commit önünde)
~9.968 satır, 78 dosya. Commit listesi `docs/HANDOVER.md §3`'te.
⚠️ Ara commit'ler tek tek test-yeşil değil, yalnızca dalın ucu doğrulandı —
`git bisect` bu aralıkta güvenilir değil. Kapsam: RSS beslemeler (küratörlü + kullanıcı) ·
SSRF guard · Redis rate limit · TR→EN anahtar kelime çevirisi · konu sınıflandırma +
ilgi-farkında kaynak seçici · ScanRun geçmişi + durum paneli · digest ·
çok sağlayıcılı LLM + kullanıcı bazlı şifreli anahtar · worker ayrımı + deterministik
fan-out · health paneli · menü grup pruning.

### Sıradaki iş (öncelik sırasıyla)
1. **DOI tekilleştirmesi** — yeni akademik kaynak eklemeden **önce** şart
2. OpenAlex + Crossref adaptörleri
3. Conditional GET'i canlandır (aşağıda)
4. RSS'siz site scrape'i + alan seçici
Gerekçeler: `docs/HANDOVER.md §5`

## Bilinen Kısıtlar / Tuzaklar
- Email gönderimi `MAIL_SUPPRESS_SEND=true` ise dev modu — link `flash` ile gösteriliyor
- API v1: auth (token/refresh/logout) + okuma + yazma (favorite, read-later, dismiss, notlar) — bkz. `docs/API_V1.md`
- **Tekilleştirme yalnızca `(source, external_id)`** — DOI yok. `upsert_paper` mevcut
  satırı güncellemiyor, yani boş abstract başka kaynaktan dolmuyor
- **Conditional GET ölü kod** — `fetch_feed_conditional` etag/last_modified alıyor ama
  hiçbir model saklamıyor; her gece her besleme tam indiriliyor
- **BEAT_SCHEDULE saatleri UTC değil**, `BABEL_DEFAULT_TIMEZONE` (Europe/Istanbul).
  Kullanıcıya gösterilen zamanı elle hesaplama — `app/tasks/schedule_info.py` var
- **`beat` tek replika olmalı** — zamanlama yerel dosyada, ikincisi her şeyi çift tetikler
- **`SCRAPE_SOURCES`, `SEMANTIC_SCHOLAR_API_KEY`, `NCBI_API_KEY` `BaseConfig`'i atlar**
  (doğrudan `os.getenv`). Testte `monkeypatch.setenv` kullan, `setitem(app.config)` değil
- Test config'i bilinçli "kırık": `FEED_ALLOW_PRIVATE_HOSTS=True` (CI'da dışa DNS yok),
  `REDIS_URL` kapalı porta bakar (kilit/rate limit fail-open olsun), LLM anahtarları
  boşaltılır (yanlışlıkla faturalı çağrı olmasın)
- `ask_paper` "RAG chat" diye anılıyor ama RAG **değil** — başlık+abstract prompt'a
  dolduruluyor. pgvector repoda yok

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
