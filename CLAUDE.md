# ScrapeMind — Bağlam Dosyası (Claude için)

## Projeye Genel Bakış
ScrapeMind aynı zamanda `flask-core-base` adlı yeniden kullanılabilir bir Flask iskeletidir.
**İki şapka:** `app/core/` = her projede kullanılabilecek çekirdek, `app/modules/` = ScrapeMind'a özel.

## Konum
`d:\app\ScrapeMind` (Windows geliştirme)

## Teknoloji
Flask 3.1 · SQLAlchemy 2 · PostgreSQL 17 · Bootstrap 5 + HTMX · Flask-Babel (TR/EN) · Celery 5.4 + Redis
LLM çok sağlayıcılı: OpenRouter (varsayılan, ücretsiz model) · Ollama (yerel) · Anthropic
Scraping: `arxiv` SDK · `feedparser` · `requests` · `yt-dlp` (yalnızca transkript)
— **tarayıcı otomasyonu yok**. Scrapy/Playwright/Selenium kurulu değil ve
**kurulmayacak**; gerekçe + kararın yeniden açılma koşulu:
`docs/adr/0001-headless-browser-yok.md`. (README/PROJECT.md'nin eski hali bunları
vaat ediyordu, doğru değil.)

## Önce Bunları Oku
- `docs/SCRAPING.md` — veri toplama mimarisi, **yeni kaynak eklemeden önce zorunlu**
- `docs/HANDOVER.md` — durum, commit'lenmemiş iş, tuzaklar, sıradaki görevler
- `docs/PHASE5.md` — Faz 5 planı (patentler, dergi kalitesi, yazar takibi, opsiyonel Scopus)

## Veritabanı (yerel geliştirme)
- **Bu makine paylaşımlı altyapı kullanıyor** (`docker/docker-compose.local.yml`'nin anlattığı kurulum): `myo_postgres17` container'ı **5432**'de, `shared_redis` **6379**'da. Bunlar myoChtBt projesinin compose'u tarafından ayağa kaldırılıyor; ScrapeMind sadece üzerlerinde `scrapemind` ve `scrapemind_test` veritabanlarını kullanıyor.
  > ⚠️ Eski not "ScrapeMind 5433'te, `docker/.env` içinde `SCRAPEMIND_DB_PORT=5433`" diyordu — **artık doğru değil**, `docker/.env` diye bir dosya da yok. `docker/docker-compose.yml`'deki kendi `db` servisi (5432/`scrapemind` kullanıcısı) bu makinede kullanılmıyor.
- **`TEST_DATABASE_URL` kökteki `.env`'de bulunmak zorunda** — yoksa `create_app()` conftest override'ından önce TestingConfig default'uyla bağlanmaya kalkar ve testlerin tamamı `psycopg2.OperationalError` verir. Bu, "kod bozuk" gibi okunur; ilk bakılacak yer burasıdır.
- Docker kapalıysa `docker ps` boş döner ve yine aynı tabloya çıkarsın — önce Docker Desktop'ı aç.
- **`venv`'i `requirements.txt` ile senkron tut.** `sentry-sdk` ve `prometheus-flask-exporter` eksikken `tests/core/test_observability.py` 4 test patlatır; kodla ilgisi yoktur.
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
                     net_guard.py (SSRF) · ratelimit.py (Redis bütçe) · doi.py · forms.py
app/tasks/         → core_tasks, scrape_tasks, feed_tasks, digest_tasks, channel_tasks,
                     schedule (BEAT_SCHEDULE), schedule_info (crontab→zaman), fanout
translations/      → TR + EN .po/.mo dosyaları
scripts/           → seed.py, create_module.py, export_core_template.py
docs/              → SCRAPING.md, HANDOVER.md, API_V1.md, UI_REVIEW.md
docs/adr/          → mimari karar kayıtları — neden **yapmadığımız** şeyler
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

### Faz 3 ✅ — `feat/homepage-source-selection` main'e merge edildi
RSS beslemeler · SSRF guard · Redis rate limit · TR→EN anahtar kelime çevirisi ·
konu sınıflandırma + ilgi-farkında kaynak seçici · ScanRun + durum paneli · digest ·
çok sağlayıcılı LLM · worker ayrımı + deterministik fan-out · health paneli.

### Faz 4 — branch `feat/openalex-crossref-youtube-channels` (main'in 9 commit önünde)
Her commit tek başına test-yeşil, `git bisect` güvenilir. Commit listesi
`docs/HANDOVER.md §3`'te. Kapsam:
- **DOI normalizasyonu + `upsert_paper` zenginleştirmesi** — boş alanı doldur, doluyu
  asla ezme (`app/modules/scrape/doi.py`)
- **OpenAlex + Crossref** adaptörleri (akademik kaynak sayısı 3 → 5)
- **YouTube kanal aboneliği** — kanal RSS'i ile yeni video tespiti, `yt-dlp` ile
  transkript, LLM ile TR özet; kanal limiti admin panelinden (`max_user_channels`)
- **`fix(deploy)`** — prod worker `-Q` olmadan çalışıyordu, routed task'ların hiçbiri
  tüketilmiyordu

### Faz 5 — planlandı, uygulanmadı: `docs/PHASE5.md`
Patent kaynakları (EPO OPS 4 GB/hafta ücretsiz + PatentsView) · dergi kalite katmanı
(Scimago quartile + atıf sayısı) · yazar takibi (mevcut kimlik modelini canlandırır) ·
admin panelinden açılan, varsayılan kapalı, **discovery-only** Scopus.
> **WoS Lite / Elsevier tekrar sorulursa:** engel kota değil. WoS Starter ücretsiz
> katmanı **50 istek/gün** ve atıf döndürmüyor; Elsevier sözleşmesi içeriğin **kalıcı
> saklanmasını** ve rekabet eden türev servisi yasaklıyor; Scopus anahtarı kurum IP'sine
> bağlı. Tam tablo ve karar `docs/PHASE5.md §2`.

### Sıradaki iş (öncelik sırasıyla)
1. Beslemelerde conditional GET'i bitir — kalıp `ingest_user_channels`'da hazır
   (Faz 5'ten bağımsız, küçük)
2. Faz 5.1 — kimlik gerektiren kaynak altyapısı + kalıcı haftalık kota sayacı
   (Faz 5'in diğer üç adımının ön koşulu)
3. RSS'siz site scrape'i + alan seçici (önce `docs/adr/0001-headless-browser-yok.md` oku)
4. Bluesky adaptörü
Gerekçeler: `docs/HANDOVER.md §5` · Faz 5 detayı: `docs/PHASE5.md`

## Bilinen Kısıtlar / Tuzaklar
- Email gönderimi `MAIL_SUPPRESS_SEND=true` ise dev modu — link `flash` ile gösteriliyor
- API v1: auth (token/refresh/logout) + okuma + yazma (favorite, read-later, dismiss, notlar) — bkz. `docs/API_V1.md`
- **Tekilleştirme sırası: normalize DOI → `(source, external_id)`.** Eşleşen satır
  zenginleştirilir (boş alan dolar, dolu alan **asla** ezilmez). `doi` üzerinde UNIQUE
  yok, sadece index — yarış penceresi var. İki yol farklı satırlara işaret ederse DOI
  satırı kazanır, diğeri olduğu gibi kalır; satır birleştirme yok
- **Conditional GET beslemelerde hâlâ ölü** — `UserFeed.etag` kolonu var ve dolduruluyor,
  ama iki yutma yolu da `fetch_feed`'i çağırıp etag'i geçirmiyor. `ingest_user_channels`
  doğru yapıyor, kalıbı oradan al
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
- **Video transkripti saklanmaz** — `VideoSummary` yalnızca özeti ve `transcript_chars`
  sayacını tutar (`docs/SCRAPING.md §11` telif sınırı). `paper_id` üzerinde tekil, yani
  dil başına cache yok — feed'de N+1 olmasın diye bilinçli
- **Yeni Celery task modülü `app/tasks/__init__.py`'deki import satırına eklenmezse**
  worker task'ı hiç görmez, hata da vermez. `TASK_ROUTES` girdisi de gerekir

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
