# Devir Dokümanı

> **Tarih:** 26 Temmuz 2026 · **Branch:** `feat/homepage-source-selection` · **Hedef:** `main`
>
> Bu dosya projeyi devralan geliştirici için yazıldı. Sırayla oku: §1 durum → §2 kurulum
> → §3 commit'lenmemiş iş → §4 tuzaklar → §5 sıradaki iş.

---

## 1. Nerede Duruyoruz

**Faz 0 ve Faz 1 tam.** Faz 2 fiilen tamamlandı; README'de "bekliyor" görünen
Semantic Scholar/PubMed işi bitti ve testli. Onun üzerine, henüz commit'lenmemiş
büyük bir Faz 3 dilimi var (§3).

Çalışan özellikler, kabaca:

| Alan | Durum |
|---|---|
| Auth (local + OAuth), RBAC, dinamik menü, audit, i18n (TR/EN) | ✅ |
| 2FA (TOTP + recovery kodları), oturum yönetimi, avatar upload | ✅ |
| API v1 (JWT, okuma + yazma, token revocation) | ✅ [docs/API_V1.md](API_V1.md) |
| Akademik kaynaklar: arXiv, Semantic Scholar, PubMed | ✅ |
| RSS: 4 küratörlü besleme + kullanıcının kendi beslemeleri | ✅ *(commit'lenmemiş)* |
| Konu sınıflandırma + ilgi-farkında kaynak seçici | ✅ *(commit'lenmemiş)* |
| TR→EN anahtar kelime çevirisi | ✅ *(commit'lenmemiş)* |
| Tarama geçmişi (`ScanRun`) + canlı durum paneli | ✅ *(commit'lenmemiş)* |
| Günlük/haftalık LLM özeti (digest) | ✅ *(commit'lenmemiş)* |
| Çok sağlayıcılı LLM (OpenRouter/Ollama/Anthropic) + kullanıcı bazlı şifreli anahtar | ✅ *(commit'lenmemiş)* |

**Doğrulama durumu (26 Temmuz 2026, bu dokümanın yazıldığı an):**

```
pytest tests/ -q      →  426 passed, 17 warnings in 62s
ruff check app/       →  All checks passed!
black app/            →  7 dosya formatlandı, sonrasında temiz
```

Yani devraldığında yeşil bir ağaç var. Uyarıların tamamı SQLAlchemy `Query.get()`
`LegacyAPIWarning`'i — testlerde, davranışı etkilemiyor, ama `Session.get()`'e
geçmek küçük ve temiz bir ilk iş olabilir.

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

## 3. Commit'lenmemiş İş — Önerilen Bölünme

Çalışma ağacında **42 değişmiş + 32 yeni dosya, ~5.200 satır** var. Tek commit'e
sıkıştırmak yerine aşağıdaki sırayla bölmeni öneririm. Migration'ları kendi
özelliğiyle aynı commit'te tut; `flask db upgrade` sırası commit sırasına değil,
Alembic'in `down_revision` zincirine bağlı.

| # | Commit | Ana dosyalar |
|---|---|---|
| 1 | `feat(llm): multi-provider LLM layer with per-user encrypted keys` | `ai_service.py`, `forms.py`, `templates/settings/_tab_ai.html`, `config.py`, `requirements.txt` |
| 2 | `feat(scrape): shared Redis rate limiting for external APIs` | `ratelimit.py`, 3 adaptör, `SCRAPE_RATE_*` |
| 3 | `feat(scrape): keyword translation so Turkish interests match English corpora` | `academic/models.py`, `e7d3b5a1c9f2_*`, `ensure_keyword_translations` |
| 4 | `feat(scrape): RSS/Atom ingestion with SSRF-guarded fetching` | `rss_source.py`, `net_guard.py`, `feed_tasks.py`, `f7c4b8e1a2d9_*` |
| 5 | `feat(scrape): topic classification and interest-aware source picker` | `sources/__init__.py`, `b7e2f9a1c3d4_*`, `_sources_card.html` |
| 6 | `feat(scrape): scan run history and live status UI` | `c3e5f7a9b1d2_*`, `record_scan_run`, `_scan_status.html` |
| 7 | `feat(scrape): daily and weekly LLM digests` | `d4a8c1f29b3e_*`, `digest_tasks.py`, `_digest_card.html` |
| 8 | `perf(tasks): queue routing, worker split, and deterministic fan-out` | `fanout.py`, `tasks/__init__.py`, `docker-compose.yml` |
| 9 | `feat(core): infrastructure health panel` | `core/health.py`, `_sidebar_footer.html` |
| 10 | `feat(admin): resolve beat schedule into concrete next-run times` | `schedule_info.py`, `tasks_admin/` |
| 11 | `fix(menu): prune permission-emptied accordion groups` | `menu/builder.py`, 2 migration |
| 12 | `feat(scrape): news/paper distinction and wider external ids` | `payload.py`, `cd6dd3da4bba_*`, `e5f1a2b3c4d6_*` |
| 13 | `feat(ui): Discover feed, dashboard cards, bulk actions` | `for_you.html`, `feed.html`, `theme.css` |
| 14 | `refactor(users): hoist function-local imports to module level` | `core/users/forms.py` |
| 15 | `docs: scraping architecture + handover; sync README/CLAUDE/PROJECT` | bu dosyalar |
| 16 | `i18n: TR/EN catalog updates` | `translations/**` |

**11 ve 14 hiçbir şeye bağlı değil** — istersen en başa alıp ayrı, küçük PR yap.
**16'yı sona bıraktım** çünkü `.po`/`.mo` tek blok değişmiş; özellik başına ayırmak
elle uğraş gerektirir.

> **Not:** Bu liste hazırlanırken `black app/` çalıştırıldı ve 7 dosya yeniden
> formatlandı (`ai_service.py`, `service.py`, `routes.py` ×2, `models.py`,
> `rss_source.py`, `feed_tasks.py`). Yani her commit kendi format düzeltmesini
> taşıyacak — bu beklenen davranış, ayrı bir "style" commit'i gerekmiyor.
> Formatlama sonrası 426 test yeniden çalıştırıldı ve geçti.

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

### 5.1 DOI tekilleştirmesi — **diğer her şeyden önce**
Tekilleştirme şu an yalnızca `(source, external_id)`. Yeni akademik kaynak eklenirse
aynı makale N ayrı `Paper` satırı olur ve feed kullanılamaz hale gelir. Yapılacaklar:

- `PaperPayload`'a `doi: str | None = None` (`kind` ile aynı default'lu kalıp)
- `Paper`'a `doi` (indexed) + `title_hash` (indexed) kolonları + migration
- `upsert_paper` çözümleme sırası: `(source, external_id)` → normalize DOI → `title_hash`
- **Zenginleştirme:** eşleşen satırda `abstract`/`pdf_url`/`doi` **boşsa** doldur,
  dolu alanı asla ezme. Bu, çok kaynaklılığı zarardan kazanca çevirir.
- Mevcut 3 adaptöre DOI çıkarımı ekle

Aynı migration'da `Paper.url`/`pdf_url`'ü `String(512)` → `Text` yap.

### 5.2 Yeni akademik kaynaklar
5.1 bittikten sonra her biri küçük, bağımsız PR:

| Kaynak | Anahtar | Not |
|---|---|---|
| **OpenAlex** | Yok (`mailto` ile polite pool, ~100k/gün) | 250M+ eser, en yüksek getiri. Abstract `abstract_inverted_index` formatında gelir, çözücü gerekir |
| **Crossref** | Yok (`mailto`) | DOI otoritesi; abstract çoğu zaman JATS XML |
| DOAJ, bioRxiv/medRxiv | Yok | Aynı kalıp, ek karmaşıklık yok |

### 5.3 Conditional GET'i canlandır
`fetch_feed_conditional` etag/last_modified destekliyor ama hiçbir model saklamıyor →
304 yolu ölü kod, her gece her besleme tam indiriliyor. `UserFeed`'e `etag`,
`last_modified`, `last_fetched_at` kolonları ekle ve iki yutma yolunda da geçir.

### 5.4 RSS'siz sitelerden scrape + alan seçici
Kullanıcı URL verir, sistem sayfadaki alanları otomatik çıkarır, isterse CSS
seçiciyle override eder.

- **Önce ortak fetcher'ı çıkar:** redirect takibi + hop başına SSRF revalidation şu an
  `rss_source._get_with_redirects` içine gömülü. `app/modules/scrape/fetcher.py`'ye
  taşı, `rss_source` onu kullansın (davranış aynı kalır).
- **robots.txt uyumu ekle:** `app/modules/scrape/robots.py`, host başına
  `RobotFileParser`, Redis'te 24s cache, `Crawl-delay` okuma. Host başına rate limit
  için mevcut `acquire_slot(f"host:{netloc}", ...)` yeterli — yeni mekanizma yazma.
- **Discovery sırası:** ① RSS autodiscovery (`<link rel="alternate">`) — *"RSS'i yok"
  sanılan sitelerin çoğunda gizli RSS var, en ucuz kazanç burada* → ② JSON-LD →
  ③ tekrar eden blok sezgisi → ④ trafilatura ile tek makale.
- Yeni bağımlılıklar: `beautifulsoup4`, `lxml`, `trafilatura`. **Playwright/Selenium
  yok** — imajı şişirir; JS-render gerektiren siteler bilinçli olarak kapsam dışı.
- Yeni model `UserPage` (`mode`, `selectors` JSON, etag/last_modified), yeni kaynak
  `web_source.py` (`source="user_page"`, `kind="news"`).
- **XSS:** önizleme hedef siteden gelen **düz metni** gösterir, ham HTML'i asla.
  Jinja autoescape açık — hiçbir yerde `|safe` kullanma.

### 5.5 Sosyal beslemeler
**X/Twitter için ücretsiz yol yok.** 6 Şubat 2026'da pay-per-use'a geçtiler; okuma
$0.005/post, ücretsiz katman ~100 post/ay ve fiilen yazma için. Kazıma ToS ihlali
ve README'deki etik taahhütle çelişir. Yerine:

- **Bluesky** — `https://public.api.bsky.app` üzerinden `app.bsky.feed.getAuthorFeed`
  auth'suz ve ücretsiz; Jetstream (`wss://jetstream2.us-east.bsky.network/subscribe`)
  auth'suz JSON firehose. Akademik Twitter kitlesinin önemli kısmı orada.
- **Mastodon** — hesap başına yerleşik RSS (`https://sunucu/@kullanici.rss`).
  **Bugünkü altyapıyla zaten çalışıyor** — kullanıcı özel besleme olarak ekleyebilir.
  Kod değil, dokümantasyon işi.

### 5.6 Daha uzun vade
1. **pgvector + gerçek RAG.** README pgvector vaat ediyor, repoda tek satır yok.
   `ask_paper` "RAG chat" diye anılıyor ama başlık+abstract'ı prompt'a dolduruyor;
   `_get_similar_papers` de embedding tabanlı değil. Docker imajını
   `pgvector/pgvector:pg17`'ye çevirmek gerekir.
2. **Yazar takibi.** ORCID/Scopus/WoS kimlik modeli zaten var (PR #4-#6) ama gerçek
   bir özelliğe bağlanmadı. OpenAlex author id'si üzerinden "bu yazarın yeni
   yayınları" beslemesi doğal devam.
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
| [IMPROVEMENTS.md](../IMPROVEMENTS.md) | UI/UX punch list — ⚠️ dosya tablonun ortasında kesik, tamamlanmalı |
| [docs/HANDOVER.md](HANDOVER.md) | Bu dosya |
