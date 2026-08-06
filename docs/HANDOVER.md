# Devir Dokümanı

> **Tarih:** 6 Ağustos 2026 · **Branch:** `feat/openalex-crossref-youtube-channels` · **Hedef:** `main`
>
> Bu dosya projeyi devralan geliştirici için yazıldı. Sırayla oku: §1 durum → §2 kurulum
> → §3 commit geçmişi → §4 tuzaklar → §5 sıradaki iş.

---

## 1. Nerede Duruyoruz

**Faz 0, 1 ve 2 tam.** Faz 3 de fiilen kapandı: RSS beslemeler, kaynak seçici,
çok sağlayıcılı LLM, digest ve ScanRun `main`'e merge edildi. Üstüne bu dalda
5 akademik kaynak (OpenAlex + Crossref dahil), DOI tekilleştirme/zenginleştirme
ve YouTube kanal aboneliği + transkript özeti var.

Çalışan özellikler, kabaca:

| Alan | Durum |
|---|---|
| Auth (local + OAuth), RBAC, dinamik menü, audit, i18n (TR/EN) | ✅ |
| 2FA (TOTP + recovery kodları), oturum yönetimi, avatar upload | ✅ |
| API v1 (JWT, okuma + yazma, token revocation) | ✅ [docs/API_V1.md](API_V1.md) |
| Akademik kaynaklar: arXiv, Semantic Scholar, PubMed, **OpenAlex, Crossref** | ✅ |
| DOI normalizasyonu + `upsert_paper` zenginleştirmesi (boş alanı doldur, doluyu ezme) | ✅ |
| RSS: 4 küratörlü besleme + kullanıcının kendi beslemeleri | ✅ |
| Agent Reach adaptörleri: YouTube Videos, GitHub Repos, Web Reader | ✅ (`net_guard`, `ratelimit`, `yt-dlp`, `gh` fallback) |
| **YouTube kanal aboneliği** + transkript özeti (admin panelinden limitli) | ✅ (kanal RSS'i + `yt-dlp` transkript + LLM özeti) |
| Konu sınıflandırma + ilgi-farkında kaynak seçici | ✅ |
| TR→EN anahtar kelime çevirisi | ✅ |
| Tarama geçmişi (`ScanRun`) + canlı durum paneli | ✅ |
| Günlük/haftalık LLM özeti (digest) | ✅ |
| Çok sağlayıcılı LLM (OpenRouter/Ollama/Anthropic) + kullanıcı bazlı şifreli anahtar | ✅ |

**Doğrulama durumu (6 Ağustos 2026):**

```
pytest tests/ -q      →  626 passed in ~78s
ruff check app/       →  All checks passed!
black --check app/    →  121 files would be left unchanged
```

Yani devraldığında yeşil bir ağaç var. Uyarıların çoğu SQLAlchemy `Query.get()`
`LegacyAPIWarning`'i — testlerde, davranışı etkilemiyor, ama `Session.get()`'e
geçmek küçük ve temiz bir ilk iş olabilir.

⚠️ `venv`'in `requirements.txt` ile senkron olduğundan emin ol: `sentry-sdk` ve
`prometheus-flask-exporter` eksikken `tests/core/test_observability.py` 4 test
patlatıyor ve bu kolayca "kod bozuk" diye okunuyor.

Mimari için: [SCRAPING.md](SCRAPING.md) (veri toplama katmanı) ve
[../PROJECT.md](../PROJECT.md) (genel tasarım).

---

## 2. Kurulum ve Çalıştırma

```bash
# İlk kurulum (Windows)
setup.bat

# Sonraki günler
development.bat            # http://localhost:5000
```

Manuel:
```bash
python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
docker compose -f docker/docker-compose.yml up -d db redis
pybabel compile -d translations
set FLASK_APP=wsgi.py
flask db upgrade
python scripts/seed.py
flask run --debug
```

Varsayılan admin: `admin` / `admin1234`.

**Arka plan işleri** (opsiyonel, `tasks` profili):
```bash
docker compose -f docker/docker-compose.yml --profile tasks up -d worker worker-io beat
```
Worker ayaklanmazsa uygulama çalışmaya devam eder — "Tara" butonu 90sn sonra
`worker_stalled` durumunu gösterir, sonsuza dek dönmez.

**AI özellikleri** varsayılan olarak kapalı. Açmak için ya `.env`'de
`OPENROUTER_API_KEY` ver, ya da kullanıcı olarak Profil → AI Ayarları'ndan kendi
anahtarını gir. Varsayılan model ücretsiz (`qwen/qwen-2.5-72b-instruct:free`).
Ollama ile tamamen yerel de çalışır (`LLM_PROVIDER=ollama`).

**Testler:**
```bash
venv/Scripts/python.exe -m pytest tests/ -q
```
Test DB ayrı (`scrapemind_test`), her oturumda `create_all`/`drop_all`.

---

## 3. Commit Geçmişi

Dal `main`'in **9 commit** önünde. Önceki devir turunun dalı
(`feat/homepage-source-selection`, 17 commit) tamamen `main`'e merge edildi —
o turun "ara commit'ler yeşil değil, bisect güvenilmez" uyarısı artık geçersiz.

| Commit | Konu |
|---|---|
| `a108514` | `fix(deploy)` — prod worker'da `-Q` eksikti; routed task'ların hiçbiri tüketilmiyordu |
| `83583ca` | `feat(scrape)` — `normalize_doi` + `upsert_paper` zenginleştirmesi + migration |
| `a6d6081` | `feat(scrape)` — OpenAlex adaptörü |
| `4a1aa97` | `feat(scrape)` — Crossref adaptörü |
| `dd838ae` | `feat(core)` — admin panelinden düzenlenebilir sayısal limit (`max_user_channels`) |
| `7af31be` | `feat(scrape)` — `youtube_channel` kaynağı: çözümleme, kanal RSS'i, transkript |
| `882da70` | `feat(scrape)` — `UserChannel` modeli, CRUD, rotalar, ayar sekmesi |
| `b872d7a` | `feat(scrape)` — kanal yutma + `VideoSummary` + Celery task'ları + beat |
| `cc62bec` | `feat(ui)` — video özetini kartta ve detayda göster |

**Her commit tek başına test-yeşil** (pytest + ruff + black her adımda koşuldu),
yani `git bisect` bu aralıkta güvenilir.

### Prod'da fark edilmesi gereken bir şey

`a108514` öncesinde prod'da **gece taraması, besleme yutma ve digest hiç
çalışmıyordu**. `TASK_ROUTES` bunları `io`/`scrape`/`llm` kuyruklarına yolluyor,
`docker-compose.prod.yml`'deki worker ise `-Q` verilmediği için yalnızca varsayılan
`celery` kuyruğunu tüketiyordu — sadece `core.heartbeat` (routed olmayan tek task)
çalışıyordu. Prod'da bu tarihten önceki dönem için "tarama koştu" verisi beklemeyin.

---

## 4. Tuzaklar — Bunları Bilmeden Dokunma

### 4.1 Çeviri: `pybabel extract` + `update` KULLANMA
Bu akış bir kez fuzzy-match kazasıyla **~300 TR çeviriyi sildi**. Yeni string'i
Babel API ile tek tek ekle:

```python
from babel.messages.pofile import read_po, write_po
with open("translations/tr/LC_MESSAGES/messages.po", "rb") as f:
    cat = read_po(f)
cat.add("New string", string="Yeni metin")   # EN kataloğunda string=msgid
with open("translations/tr/LC_MESSAGES/messages.po", "wb") as f:
    write_po(f, cat, sort_output=False, sort_by_file=False)
```
Sonra `pybabel compile -d translations` (bu güvenli). **TR ve EN msgid key set'leri
eşit olmalı** — CI kontrol ediyor. EN kataloğunda çeviri = msgid'in kendisi.

### 4.2 Beat saatleri UTC değil
`BEAT_SCHEDULE` crontab'ları `BABEL_DEFAULT_TIMEZONE` (Europe/Istanbul) ile eşleşir.
`enable_utc=True` yalnızca mesaj zaman damgalarını etkiler. Kullanıcıya gösterilen
"sıradaki tarama" değerini elle hesaplama — `app/tasks/schedule_info.py` var.

### 4.3 `beat` tek replika olmalı
Zamanlama yerel dosyada tutuluyor; ikinci bir beat her şeyi çift tetikler.
Task'lardaki kullanıcı bazlı kilitler hasarı sınırlar ama bunlar emniyet ağıdır,
ölçekleme izni değil.

### 4.4 Bazı config'ler `BaseConfig`'i atlıyor
`SCRAPE_SOURCES`, `SEMANTIC_SCHOLAR_API_KEY`, `NCBI_API_KEY` doğrudan `os.getenv`
ile okunuyor. Testte `monkeypatch.setitem(app.config, ...)` **işe yaramaz**,
`monkeypatch.setenv` kullan.

### 4.5 Test config'i bilinçli olarak "kırık"
- `FEED_ALLOW_PRIVATE_HOSTS = True` — CI'da dışa DNS yok, guard her fixture URL'ini
  reddederdi. Guard'ın kendisi literal IP'lerle ayrıca test ediliyor.
- `REDIS_URL` kapalı bir porta bakıyor — kilitler ve rate limit fail-open olsun diye.
  Canlı Redis testleri sıraya bağımlı yapardı (test DB'si her oturumda yeniden
  kuruluyor, user id'ler 1'den başlıyor, ama önceki koşumun 900sn'lik kilidi Redis'te
  duruyor olurdu).
- `ANTHROPIC_API_KEY` ve `OPENROUTER_API_KEY` boşaltılıyor — geliştiricinin gerçek
  anahtarı yanlışlıkla faturalı çağrı yapmasın diye.

### 4.6 Docker Desktop port mapping kaybı
Docker Desktop kararsız kapanırsa `docker-db-1` port mapping'ini kaybedebilir
(`docker ps`'te `0.0.0.0:5432->5432` yerine sadece `5432/tcp`). Çözüm:
```bash
docker rm -f docker-db-1 && docker compose -f docker/docker-compose.yml up -d db
```
Veri named volume'da, kaybolmaz.

### 4.7 Mimari kurallar (ihlal etme)
1. `app/core/` asla `app/modules/`'dan import **etmez**.
2. `is_superuser` bypass **yalnızca** `app/core/auth/decorators.py:permission_required`'da.
3. Plugin discovery tablo yokken sessizce geçer — bunu "hata yutuyor" diye düzeltme.
4. Migration sırası: `flask db upgrade` → uygulama başlatma (`docker/entrypoint.sh`).

### 4.8 Açık kaynak
Repo halka açık. Commit/PR/dokümana gizli bilgi (şifre, API anahtarı, gerçek e-posta)
yazma. `.env` asla commit'lenmez; yeni config eklerken `.env.example`'ı placeholder
ile güncelle. Örneklerde `example.com` / `example.test` kullan.

---

## 5. Sıradaki İş

Öncelik sırasıyla. Gerekçeler [SCRAPING.md](SCRAPING.md) §10'da.

> **6 Ağustos 2026 — bu bölüm baştan yazıldı.** Eski §5.1 (DOI tekilleştirmesi) ve
> §5.2 (OpenAlex + Crossref) tamamlandı, eski §5.3 (conditional GET) ise kısmen:
> aşağıda kalan kısmı yazıyor. Numaralar kaydı.

### 5.1 Beslemelerde conditional GET'i bitir — **küçük ve net**
`UserFeed.etag`/`last_modified` kolonları **var** ve `add_user_feed` doğrulama
fetch'inde dolduruyor. Ama iki yutma yolu da (`feed_tasks.ingest_all` ve
`service.ingest_user_feeds`) `fetch_feed`'i çağırıyor, o da etag'i **geçirmiyor** →
304 yolu hâlâ ölü, her gece her besleme tam indiriliyor.

`ingest_user_channels` bunu doğru yapıyor — **kalıbı oradan kopyala**: `fetch_feed`
yerine `fetch_feed_conditional`'ı saklı etag ile çağır, dönen değeri satıra geri yaz,
`not_modified` durumunu sıfır olarak kaydet. Küratörlü beslemeler için etag'i tutacak
bir yer gerekiyor (`SystemSettings` yeterli, yeni tablo gerekmez).

### 5.2 RSS'siz sitelerden scrape + alan seçici
Kullanıcı URL verir, sistem sayfadaki alanları otomatik çıkarır, isterse CSS
seçiciyle override eder.

- **Önce ortak fetcher'ı çıkar:** redirect takibi + hop başına SSRF revalidation şu an
  `rss_source._get_with_redirects` içine gömülü. `app/modules/scrape/fetcher.py`'ye
  taşı, `rss_source` onu kullansın (davranış aynı kalır).
  ⚠️ Bu, **`CLAUDE.md` kural 7 ile çelişiyor** — çelişkinin çözümü
  [ADR-0001](adr/0001-headless-browser-yok.md) "Açık soru" bölümünde. Taşımadan önce
  oku; bu turda bilinçli olarak ertelendi.
- **robots.txt uyumu ekle:** `app/modules/scrape/robots.py`, host başına
  `RobotFileParser`, Redis'te 24s cache, `Crawl-delay` okuma. Host başına rate limit
  için mevcut `acquire_slot(f"host:{netloc}", ...)` yeterli — yeni mekanizma yazma.
- **Discovery sırası:** ① RSS autodiscovery (`<link rel="alternate">`) — *"RSS'i yok"
  sanılan sitelerin çoğunda gizli RSS var, en ucuz kazanç burada* → ② JSON-LD →
  ③ tekrar eden blok sezgisi → ④ trafilatura ile tek makale.
- Yeni bağımlılıklar: `beautifulsoup4`, `lxml`, `trafilatura`. **Playwright/Selenium
  yok** — tam gerekçe, reddedilen alternatifler ve kararın hangi koşulda yeniden
  açılacağı: [ADR-0001](adr/0001-headless-browser-yok.md).
- Yeni model `UserPage` (`mode`, `selectors` JSON, etag/last_modified), yeni kaynak
  `web_source.py` (`source="user_page"`, `kind="news"`).
- **XSS:** önizleme hedef siteden gelen **düz metni** gösterir, ham HTML'i asla.
  Jinja autoescape açık — hiçbir yerde `|safe` kullanma.

### 5.3 Sosyal beslemeler
**X/Twitter için ücretsiz yol yok.** 6 Şubat 2026'da pay-per-use'a geçtiler; okuma
$0.005/post, ücretsiz katman ~100 post/ay ve fiilen yazma için. Kazıma ToS ihlali
ve README'deki etik taahhütle çelişir. Yerine:

- **Bluesky** — `https://public.api.bsky.app` üzerinden `app.bsky.feed.getAuthorFeed`
  auth'suz ve ücretsiz; Jetstream (`wss://jetstream2.us-east.bsky.network/subscribe`)
  auth'suz JSON firehose. Akademik Twitter kitlesinin önemli kısmı orada.
- **Mastodon** — hesap başına yerleşik RSS (`https://sunucu/@kullanici.rss`).
  **Bugünkü altyapıyla zaten çalışıyor** — kullanıcı özel besleme olarak ekleyebilir.
  Kod değil, dokümantasyon işi.

### 5.4 Daha uzun vade
1. **pgvector + gerçek RAG.** README pgvector vaat ediyor, repoda tek satır yok.
   `ask_paper` "RAG chat" diye anılıyor ama başlık+abstract'ı prompt'a dolduruyor;
   `_get_similar_papers` de embedding tabanlı değil. Docker imajını
   `pgvector/pgvector:pg17`'ye çevirmek gerekir.
2. **Yazar takibi.** ORCID/Scopus/WoS kimlik modeli zaten var (PR #4-#6) ama gerçek
   bir özelliğe bağlanmadı. OpenAlex adaptörü artık mevcut, dolayısıyla author id
   üzerinden "bu yazarın yeni yayınları" beslemesi en yakın büyük kazanç.
3. **Atıf grafiği** — OpenAlex/S2 `referenced_works` + `cited_by`.
4. **Açık erişim tam metin** — OpenAlex `best_oa_location`. Etik sınır net: sadece OA.
5. **Kayıtlı arama + uyarı** — bildirim altyapısı (`add_notification`) hazır.
6. **Zotero/Mendeley dışa aktarım** — BibTeX var, API entegrasyonu doğal devam.

---

## 6. Doküman Haritası

| Dosya | İçerik |
|---|---|
| [README.md](../README.md) | Proje tanıtımı, kurulum, yol haritası |
| [CLAUDE.md](../CLAUDE.md) | AI asistanı için bağlam — kurallar ve tuzaklar |
| [PROJECT.md](../PROJECT.md) | Detaylı tasarım dokümanı, faz planı |
| [docs/SCRAPING.md](SCRAPING.md) | Veri toplama mimarisi — **yeni kaynak eklemeden önce oku** |
| [docs/API_V1.md](API_V1.md) | JSON API referansı |
| [docs/UI_REVIEW.md](UI_REVIEW.md) | UI inceleme notları |
| [docs/adr/](adr/) | Mimari karar kayıtları — neden **yapmadığımız** şeyler |
| [IMPROVEMENTS.md](../IMPROVEMENTS.md) | UI/UX punch list — ⚠️ dosya tablonun ortasında kesik, tamamlanmalı |
| [docs/HANDOVER.md](HANDOVER.md) | Bu dosya |
