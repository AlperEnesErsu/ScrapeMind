# Devir Dokümanı

> **Tarih:** 10 Eylül 2026 · **Branch:** `main` · **Açık dal yok**
>
> Bu dosya projeyi devralan geliştirici için yazıldı. Sırayla oku: §1 durum → §2 kurulum
> → §3 commit geçmişi → §4 tuzaklar → §5 sıradaki iş.

---

## 1. Nerede Duruyoruz

**Faz 0-6 tamamı `main`'de, açık dal yok.** En son inen üç iş: Faz 6 (retrospektif
raporlar + yazar grupları, §5.5), tasarım sistemi (§5.6) ve CI'ın onarımı — pipeline
9 Eylül'den beri kırmızıydı ve bunu kimse fark etmemişti (§4.10).

Çalışan özellikler, kabaca:

| Alan | Durum |
|---|---|
| Auth (local + OAuth), RBAC, dinamik menü, audit, i18n (TR/EN) | ✅ |
| 2FA (TOTP + recovery kodları), oturum yönetimi, avatar upload | ✅ |
| API v1 (JWT, okuma + yazma, token revocation) | ✅ [docs/API_V1.md](API_V1.md) |
| Akademik kaynaklar: arXiv, Semantic Scholar, PubMed, **OpenAlex, Crossref** | ✅ |
| DOI normalizasyonu + `upsert_paper` zenginleştirmesi (boş alanı doldur, doluyu ezme) | ✅ |
| RSS: 4 küratörlü besleme + kullanıcının kendi beslemeleri | ✅ |
| Harici kaynak adaptörleri: YouTube Videos, GitHub Repos, Web Reader | ✅ (Agent-Reach'ten esinlenildi; bağımlılık yok, `net_guard`, `ratelimit`, `yt-dlp`, `gh` fallback) |
| **YouTube kanal aboneliği** + transkript özeti (admin panelinden limitli) | ✅ (kanal RSS'i + `yt-dlp` transkript + LLM özeti) |
| Konu sınıflandırma + ilgi-farkında kaynak seçici | ✅ |
| TR→EN anahtar kelime çevirisi | ✅ |
| Tarama geçmişi (`ScanRun`) + canlı durum paneli | ✅ |
| Günlük/haftalık LLM özeti (digest) | ✅ |
| Çok sağlayıcılı LLM (OpenRouter/Ollama/Anthropic) + kullanıcı bazlı şifreli anahtar | ✅ |
| Kimlik/admin kapılı kaynak altyapısı + haftalık kalıcı kota (Faz 5.1) | ✅ |
| **Patent kaynakları**: EPO OPS (dünya çapında) + PatentsView (ABD) (Faz 5.2) | ✅ anahtar + admin opt-in gerektirir |
| **Prior-art araması** + LLM yenilik değerlendirmesi (`/papers/patents`) | ✅ sonuçlar **saklanmaz** |
| **Dergi kalite katmanı**: SJR quartile rozeti + atıf sayısı + `?quartile=` filtresi (Faz 5.3) | ✅ `journals` tablosu **elle seed edilir** |
| **Yazar takibi** (ORCID → OpenAlex, gecelik) + opsiyonel **Scopus** (Faz 5.4) | ✅ Scopus discovery-only, varsayılan kapalı |
| pgvector + gerçek RAG, atıf grafiği (Faz 5.4) | ✅ `Paper.embedding` `VECTOR(1536)` — DB'nin pgvector olması **şart** (§4.9) |
| **Retrospektif raporlar** + yazar grupları + OpenAlex aralık hasadı (Faz 6) | ✅ on-demand, llm kuyruğunda (§5.5) |
| **Tasarım sistemi**: nar işareti, garnet palet, tip ölçeği, CVD-güvenli kategorik palet | ✅ [DESIGN.md](DESIGN.md) — **arayüze dokunmadan önce oku** |
| Dark mode | ❌ bilerek kaldırıldı — token'lar üzerinden geri gelebilir (§5.6) |

**Doğrulama durumu (10 Eylül 2026):**

```
pytest -q                        →  1193 passed in ~110s
pytest --cov=app                 →  %81.51  (CI eşiği 80)
ruff check app/ tests/ scripts/  →  All checks passed!
black --check app/ tests/ scripts/ →  225 files would be left unchanged
python scripts/mypy_ratchet.py   →  95 errors, baseline'da (yükselemez)
node scripts/audit_ui.mjs …      →  7 sayfa, 0 WCAG ihlali, 280/320/414px'te taşma yok
```

CI iki iş koşuyor: `lint-and-test` ve `ui-audit`. İkincisi tarayıcı indirdiği için
ayrı — erişilebilirlik ve reflow regresyonlarını `pytest` göremez.

> ⚠️ `ruff`/`black`'i `migrations/` üzerinde çalıştırma — o klasörde eski lint borcu
> var ve formatlayıcı 21 eski migration'ı gereksizce yeniden yazar. `scripts/` artık
> **kapsam içinde** (CI oradan üç script çalıştırıyor).

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
SCRAPEMIND_DB_PORT=5433 docker compose -f docker/docker-compose.yml -p scrapemind up -d db redis
pybabel compile -d translations
set FLASK_APP=wsgi.py
flask db upgrade
python scripts/seed.py
flask run --debug
```

Varsayılan admin: `admin` / `admin1234`.

> **Veritabanı 5433'te.** Faz 5.4'ten beri pgvector gerekiyor; paylaşımlı
> `myo_postgres17` (myoChtBt'nin, `postgres:17-alpine`) onu sağlayamıyor, o
> yüzden ScrapeMind kendi `pgvector/pgvector:pg17` container'ında. `.env`'de
> `DATABASE_URL` **ve** `TEST_DATABASE_URL` `localhost:5433`'e bakmalı.
> Gerekçe, geçmiş ve eski bir veritabanının nasıl onarılacağı: §4.9.

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

> **10 Eylül 2026.** Bu bölüm tarihsel: aşağıdaki tablo Faz 4'ün dalını anlatıyor ve
> o dal çoktan `main`'de. Sonraki turların commit'leri için `git log` ve PR
> açıklamaları daha güvenilir — #42-#45 (Faz 5), #53 (Faz 6), #54 (tasarım sistemi),
> #52/#56/#57 (CI ve düzeltmeler).
>
> **10 Ağustos 2026 güncellemesi.** Aşağıdaki dal (`feat/openalex-crossref-youtube-channels`)
> `main`'e merge edildi. Merge sırasında iki isim çakışması vardı — dal hâlâ
> `agent_reach_source` diyordu, `main` onu `external_sources`'a çevirmişti — ve merge
> `tests/conftest.py`'deki SQLite fallback'ini yanlışlıkla geri getirmişti (testler
> sessizce Postgres yerine SQLite'a düşüyordu). İkisi de düzeltildi.
> Güncel dal: **`feat/phase5-key-gated-sources`**, 4 commit, Faz 5.1 (bkz. §5.0).

Dal `main`'in **9 commit** önündeydi. Önceki devir turunun dalı
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


### 4.9 pgvector ve ScrapeMind'in kendi veritabani (bu makinede 5433)
Faz 5.4 `Paper.embedding`'i `VECTOR(1536)` yapti. Paylasimli `myo_postgres17`
(myoChtBt'nin compose'u, `postgres:17-alpine`) pgvector icermiyor ve alpine'in
hazir `postgresql-pgvector` paketi `postgresql18`'e bagli — PG 17.9 icin ise
yaramaz. Iki belirti ayni koke cikiyor:

- testlerde `conftest`'in `CREATE EXTENSION` denemesi `try/except` icinde
  yutuluyor, sonra `create_all()` `type "vector" does not exist` ile oluyor;
- uygulamada `Paper`'a dokunan her sorgu `papers_1.embedding does not exist`
  diyor (`papers_1` tablo degil, SQLAlchemy'nin join alias'i).

**Cozuldu:** ScrapeMind artik kendi Postgres'inde. `myo_postgres17` hic
degismedi, verisi yerinde; `scrapemind` DB'si oradan `pg_dump` ile kopyalandi.

```bash
SCRAPEMIND_DB_PORT=5433 docker compose -f docker/docker-compose.yml   -p scrapemind up -d db          # pgvector/pgvector:pg17, volume scrapemind_pg_data
```
`.env`: `DATABASE_URL` ve `TEST_DATABASE_URL` → `localhost:5433`
(kullanici/sifre `scrapemind`, compose'un tanimladigi gibi). Eski paylasimli
Postgres'teki `scrapemind` veritabani duruyor ama artik kullanilmiyor.

#### `development.bat` bu container'i baslatmaz
`development.bat`, `.env.local` varsa `:skip_docker`'a atliyor ve hicbir
container ayaga kaldirmiyor. Postgres myoChtBt'nin container'iyken bu dogruydu
— baskasi baslatiyordu. Artik Postgres ScrapeMind'in kendisinin, yani script
onu baslatmadigi halde ona baglanmaya calisiyor; container kapaliysa
`connection refused` alirsin ve script bunun sebebini soylemez.

Mevcut container'a `--restart unless-stopped` verildi, yani Docker Desktop
acildiginda kendisi geliyor:
```bash
docker update --restart unless-stopped scrapemind-db-1
```
Container yeniden yaratilirsa (`docker compose down` vb.) bu politika gider —
o zaman ya komutu tekrarla ya da `docker/docker-compose.yml`'deki `db`
servisine `restart: unless-stopped` ekle. Compose'a eklenmedi cunku o dosya
ayni zamanda deploy'da kullaniliyor ve orada politikayi kimin belirleyecegi
ayri bir karar.

#### Bunun altindaki asil tuzak: re-parent edilmis migration + damgali DB
Faz 6 zinciri (`4360c046a92e` → `7b3ce9d10a45`) ile main'in zinciri
(`08f12848f0d1` → `eb3c1118d2f5` → `f135d2517c0e`) ayni parent'tan,
`f4c1e8b52a76`'dan sarkiyordu. Merge sirasinda Faz 6'nin parent'i main'in
head'ine baglandi — repo icin dogru, **ama zaten eski zincirin ucunda damgali
bir veritabani icin degil**. O DB `alembic_version = 7b3ce9d10a45` diyor, kod
da ayni revizyonu head sayiyor, dolayisiyla:

```
flask db current   →  7b3ce9d10a45 (head)     # "yapacak is yok"
flask db upgrade   →  no-op
gercek             →  user_pages, user_bluesky, papers.embedding yok
```

Alembic uc migration'i **sessizce** atlanmis sayiyor. Hata vermiyor, bu yuzden
fark edilmesi zor. Kontrol: sema ile damgayi karsilastir, damgaya guvenme.

```bash
docker exec scrapemind-db-1 psql -U scrapemind -d scrapemind -tAc   "SELECT count(*) FROM information_schema.columns
   WHERE table_name='papers' AND column_name='embedding';"   # 0 ise damga yalan soyluyor
```

Onarim (bu makinede uygulanan yol — veri kaybi yok, `reports` tablosu
downgrade edilmeden kaliyor): eksik araligin SQL'ini offline uret ve uygula.

```bash
flask db upgrade f4c1e8b52a76:f135d2517c0e --sql > missing.sql
docker exec -i scrapemind-db-1 psql -U scrapemind -d scrapemind < missing.sql
```
Uretilen dosyadaki `UPDATE alembic_version ... WHERE version_num = '<eski>'`
satirlari eslesmez (`UPDATE 0`) — damga zaten dogru degerde oldugu icin
istenen davranis budur, duzeltmeye calisma.

Temiz bir checkout'ta ya da main tabanli bir DB'de bu sorun **yok**: sira
dogru islediginden `flask db upgrade` her seyi kendisi yapar. Tuzak yalnizca
merge'den once eski zincirin ucuna kadar upgrade edilmis veritabanlarinda.

### 4.10 Sürümsüz formatlayıcı = zamanlanmış CI arızası

CI 9 Eylül'den 10 Eylül'e kadar kırmızıydı — dört ardışık koşu, her biri ~45
saniyede. Hiçbiri test ettiği kodla ilgili değildi.

`black` `requirements.txt`'te sürümsüzdü, CI de her koşuda en yenisini çekiyordu.
Bir sürüm `scrape/ratelimit.py`'deki çok satırlı bir string'i yeniden akıttı ve
`black --check app/` o günden sonra her koşuda düştü. **Repo hiç değişmeden
pipeline kırmızıya döndü**, üstelik hata katkıcının suçu gibi göründü.

Arkasında ikinci bir arıza bekliyordu ve görünmüyordu, çünkü black'ten sonraki
her adım atlanıyordu: servis container'ı `postgres:17-alpine`'dı ve §4.9'un
anlattığı `CREATE EXTENSION vector` orada çalışmaz. Migration adımı bunu
söyleyecek kadar uzun yaşamadı.

İkisi de düzeltildi (PR #52): `ruff`, `black`, `mypy` sabit sürümde; servis imajı
`pgvector/pgvector:pg17`.

**Kural:** formatlayıcı ve linter sürümsüz bırakılmaz. Onlar kodu değil, kodun
nasıl yazılması gerektiğine dair fikri taşır ve o fikir kendi takvimlerinde
değişir.

**İkinci kural:** CI'ın kırmızı olduğunu fark eden bir şey yok. Dört koşu
boyunca kimse bakmadı. Bir dal açmadan önce `gh run list --branch main --limit 1`.

---

## 5. Sıradaki İş

Öncelik sırasıyla. Gerekçeler [SCRAPING.md](SCRAPING.md) §10'da.

> **6 Ağustos 2026 — bu bölüm baştan yazıldı.** Eski §5.1 (DOI tekilleştirmesi) ve
> §5.2 (OpenAlex + Crossref) tamamlandı, eski §5.3 (conditional GET) ise kısmen:
> aşağıda kalan kısmı yazıyor. Numaralar kaydı.

### 5.0 Faz 5 — planlandı, ayrı dokümanda

**[docs/PHASE5.md](PHASE5.md)** — patent kaynakları (EPO OPS + PatentsView), dergi
kalite katmanı (Scimago quartile + atıf sayısı), yazar takibi ve admin panelinden
açılan opsiyonel Scopus. Kapsam, 4 artımlı adım, doğrulama ve devir notları orada.

> **10 Ağustos 2026 — Faz 5.1 bitti** (`feat/phase5-key-gated-sources`). Diğer üç
> adımın ön koşulu olan altyapı hazır:
> - `SOURCE_META`'da `requires_key` + `credentials_ok()` ve `requires_admin_optin`
>   kapıları; `enabled_sources()` anahtarsız kaynağı hiç listelemiyor,
>   `effective_source_prefs` admin opt-in'i açık `UserSource` satırının **üstünde**
>   tutuyor (bkz. [SCRAPING.md](SCRAPING.md) §5).
> - `SourceQuotaUsage` + `ratelimit.consume_quota` — haftalık, Postgres'te, **fail-closed**,
>   tek statement'lık atomik harcama. Migration `c7e4b8d3a915`.
> - Core'da `toggle_registry.py`: modüller sistem ayarları sayfasına boolean
>   ekleyebiliyor, core hiçbir modülü import etmeden render ediyor. Scrape iki toggle
>   kaydediyor (`patents_enabled`, `scopus_enabled`), anahtar yoksa yanında uyarı çıkıyor.
> - Admin overview'da haftalık kota kartı.
>
> Kapıları kullanan ilk kaynaklar 5.2'de geldi. Mekanizmanın kendisi ayrıca sentetik
> bir kaynakla test ediliyor ([test_source_gating.py](../tests/modules/test_source_gating.py)),
> böylece onu ilk kullanan adaptörden bağımsız olarak sabit kalıyor.

> **12 Ağustos 2026 — Faz 5.2 bitti** (`feat/phase5-patent-sources`).
> - **`epo_ops`** — repodaki tek OAuth'lu adaptör (client-credentials, 20dk token,
>   401'de bir kez zorla yenileme). CQL `or` sayesinde tüm anahtar kelimeler tek
>   istekte, bu yüzden `_PER_KEYWORD_REQUEST_SOURCES`'a **girmez**. **Bant genişliği
>   ölçer**: istek öncesi nominal rezervasyon, yanıt sonrası gerçek boyutla kapanış.
> - **`patentsview`** — `X-Api-Key`, JSON DSL + `_or` (yine tek istek). 429'da
>   `Retry-After` bir kez ve yalnızca kısaysa beklenir. Hak sahibi `categories`'te
>   `assignee:` önekiyle.
> - **Gecelik `patents.ingest_for_all_users`** 03:05'te, ayrı `ScanRun.kind="patents"`.
>   Ayrı olmasının sebebi: tükenmiş bir patent kotası akademik taramayı `partial`
>   işaretlememeli. Anahtarı olmayan kurulum tek registry lookup'ıyla kısa devre yapar.
> - **`/papers/patents`** — prior-art araması + LLM yenilik değerlendirmesi.
>   **Hiçbir şey saklamaz** (`search_patents_live`); gerekçe [SCRAPING.md](SCRAPING.md) §11.
>   Değerlendirme cache'lenmez — cevap fikrin tam metnine bağlı.
> - Migration'lar: `c7e4b8d3a915` (5.1 kota tablosu), `d8a1c6e40f27` (nav girdisi).
>
> ⚠️ Doğrulama sınırı: sayfa **görsel olarak** kontrol edilmedi (admin/kullanıcı girişi
> gerekiyordu). Render, HTML gövdesini okuyan route testleriyle doğrulandı
> ([test_prior_art.py](../tests/modules/test_prior_art.py)).
>
> ⚠️ `tests/modules/test_scrape.py`'deki `clean` fixture'ı **tüm kullanıcıları siler ama
> `audit_logs`'a dokunmaz**. Audit satırı bırakan her yeni test, ilgisiz bir dosyada
> foreign-key ihlaline dönüşür. Yeni route testi yazarken kullanıcıyı audit satırlarıyla
> birlikte temizle (`test_prior_art.py` ve `test_system_settings.py` kalıbı).
>
> **12 Ağustos 2026 — Faz 5.3 bitti** (`feat/phase5-journal-quality`).
> - **`Journal` modeli** (Scimago SJR + DOAJ) + `papers.issn_l` / `papers.cited_by_count`.
>   `papers.issn_l` **foreign key değil**: bir makalenin ISSN'i, o dergiyi seed etmiş
>   olup olmadığımızdan bağımsız bir gerçek. Join kolon üzerinden yapılıyor.
> - **`_REFRESHABLE_FIELDS`** — `_enrich`'in ikinci kuralı. `cited_by_count` her
>   eşleşmede güncellenir (koşan toplam); `None` gelen değer mevcut sayıyı silmez, `0`
>   siler çünkü gerçek bir değerdir. Detay: [SCRAPING.md](SCRAPING.md) §8.
> - **`scripts/seed_journals.py`** — Scimago CSV (noktalı virgül + virgüllü ondalık) +
>   DOAJ CSV, idempotent. `parse_decimal` **`Decimal` döndürür**: string dönerse
>   SQLAlchemy'nin geri okuduğu `Decimal`'a eşit olmuyor ve her çalıştırma tüm satırları
>   "güncellendi" sayıyordu (test bunu yakaladı).
> - **UI**: kartta quartile rozeti + atıf sayısı, kütüphane aramasında `?quartile=Q1`.
>   `Paper.journal` viewonly ilişki, iki feed yolunda `joinedload` ile çekiliyor.
> - Migration: `e2b7c94d3f18`.
>
> ⚠️ **`journals` tablosu elle doldurulur.** Seed çalıştırılmamış bir kurulumda hiçbir
> kartta rozet çıkmaz — bu bozukluk değil. Scimago CSV'sini
> https://www.scimagojr.com/journalrank.php adresinden indirip
> `python scripts/seed_journals.py --scimago <dosya> --year 2025` çalıştır.
>
> ⚠️ **SJR verisi CC BY-NC ve atıf zorunlu.** Quartile'ın göründüğü her yerde Scimago
> kredilendirilmeli ([SCRAPING.md](SCRAPING.md) §11). Rozet tooltip'indeki ve filtre
> altındaki atıf metnini kaldırma — lisans şartı.
>
> **12 Ağustos 2026 — Faz 5.4 bitti** (`feat/phase5-author-tracking`). **Faz 5 tamam.**
> - **Yazar takibi** — `UserAuthor` genişletildi (yeni tablo yok): `openalex_id`, `orcid`,
>   `last_work_at`, `active`. Migration `f4c1e8b52a76`.
>   `last_work_at` bir **su seviyesi işareti**; olmasaydı üretken bir yazarın tüm kariyeri
>   her gece yeniden içeri alınırdı.
> - **Çözümleme takip anında yapılır**, gecelik koşuda değil: hatalı ORCID kullanıcının
>   gözü önünde patlar, bir gece sonra task log'una gömülmez.
> - Takip **kimlikle** yapılır, isimle değil — OpenAlex'te binlerce "J. Smith" var.
> - **`Followed Authors` profil sekmesi** (scrape modülünde, PHASE5'in dediği gibi
>   academic'te değil — veriyi sahiplenen modülde). Kullanıcının kayıtlı ORCID'i tek
>   tıkla takip olarak sunulur.
> - **`authors.ingest_for_all_users` 03:25**, kuyruk `scrape` (patent task'larının
>   aksine `io` değil — aynı OpenAlex bütçesini harcıyorlar).
> - **Scopus** — discovery-only, varsayılan kapalı, `abstract` sabit `None`.
>   Saklanabilir metadata `_hydrate_scopus_payloads` ile OpenAlex'ten gelir.
>
> ⚠️ **`scopus_source`'daki `abstract=None` bir lisans kısıtıdır, optimizasyon değil.**
> "Zaten API veriyor, alalım" diye değiştirme — gerekçe, reddedilen alternatifler ve
> kararın yeniden açılma koşulu [ADR-0002](adr/0002-elsevier-discovery-only.md)'de.
> Kalan risk (OpenAlex hidrasyonu tutmazsa Elsevier kaynaklı başlık saklanır) orada
> açıkça yazılı.
>
> ⚠️ Test tuzağı: bir test client'ının `_user_id`'sini değiştirmek Flask-Login'in
> çözdüğü kullanıcıyı **değiştirmiyor**. Sahiplik testi yazarken satırı doğrudan ikinci
> kullanıcıya ait yarat — ilk yazdığım hâli 200 dönerken 404 bekliyordu, yani hiçbir şey
> test etmiyordu.
>
> Doğrulama sınırı (5.2–5.4 boyunca aynı): sayfalar **görsel olarak** kontrol edilmedi,
> giriş gerektiriyor. Render, HTML gövdesini okuyan route testleriyle doğrulandı.

İki şey buradaki listeyi etkiliyor:
- Aşağıdaki **§5.4 ② (yazar takibi)** Faz 5.4'e taşındı, orada planlandı.
- Faz 5 "WoS Lite / Elsevier ücretsiz kotalarını kullanalım" sorusundan çıktı; araştırma
  sonucu **engelin kota değil lisans ve IP olduğu**. WoS Starter ücretsiz katmanı
  **50 istek/gün** ve atıf döndürmüyor; Elsevier sözleşmesi içeriğin **kalıcı
  saklanmasını** ve rekabet eden türev servisi yasaklıyor, Scopus anahtarı ayrıca kurum
  IP'sine bağlı. Gerekçe tablosu [PHASE5.md](PHASE5.md) §2'de — aynı soru tekrar
  gelirse oradan cevaplanır.

### 5.1 ✅ Beslemelerde conditional GET — bitti (16 Ağustos 2026, `feat/feed-conditional-get`)

Her iki yutma yolu da (`feed_tasks.ingest_all` ve `service.ingest_user_feeds`) artık
saklı `etag`/`last_modified`'ı geri gönderiyor; `not_modified` o beslemeyi sıfır
parse/upsert maliyetiyle kısa devre yapıyor. Kurallar ve saklama yerleri
[SCRAPING.md](SCRAPING.md) §7.1'de. Uygulamada çıkan üç şey:

- **`add_user_feed` bir tuzak taşıyordu.** Doğrulama fetch'inin etag'ini satıra
  yazıyordu ama payload'ları upsert etmiyordu. Conditional GET canlanınca ilk gecelik
  koşu 304 alıp beslemeyi **kalıcı olarak boş** gösterecekti. Artık doğrulama fetch'i
  validator saklamıyor; bir besleme aktifleştiğinde (`toggle_user_feed` ile devam,
  `add_user_feed` ile yeniden ekleme) validator'ları temizleniyor — duraklatılmışken
  yayınlananların üzerinden 304'le geçilmesin diye.
  Bunu doğrulayan eski test (`test_add_user_feed_stores_etag_and_last_modified`)
  **tersine çevrildi**, adı ve docstring'i nedenini anlatıyor.
- **Sıra önemli:** validator'lar upsert'lerden **sonra** yazılıyor. Ters sıra, döngü
  ortasında patlarsa hiç kalıcılaştırılmamış öğelerin üzerinden 304'le geçmek olurdu.
  `ok` olmayan durum (`timeout`, `http_error`) saklı validator'a hiç dokunmuyor.
- **`rss_source.fetch_feed` kaldırıldı.** Payload-only sarmalayıcı olarak durduğu sürece
  conditional GET'i sessizce atlamanın kolay yolu oydu — bu hata zaten bir kez tam
  böyle oluşmuştu. Tek doğru giriş noktası `fetch_feed_conditional`.

Küratörlü beslemelerin DB satırı olmadığı için validator'ları tek bir JSON blob'da:
`system_settings["feed_validators"]` (`feed_tasks.FEED_VALIDATORS_KEY`). Admin formu
sabit alan listesi render ettiği için bu makine-sahipli anahtar orada görünmüyor.

> ⚠️ Testte `system_settings`'i temizleyen bir fixture gerekiyor (`clean_validators`,
> [test_feeds.py](../tests/modules/test_feeds.py)) — `clean_user` o tabloya bilerek
> dokunmuyor, yoksa saklı bir etag sonraki testin ilk fetch'ine sızıyor.

**Doğrulama:** `pytest tests/ -q` → 927 passed · ruff + black temiz.
Sayfa görsel olarak kontrol edilmedi (bu değişiklik UI'a dokunmuyor).

### 5.2 ✅ RSS'siz sitelerden scrape + alan seçici — bitti (PR #47 + PR #48)
Kullanıcı URL verir, sistem sayfadaki alanları otomatik çıkarır, isterse CSS
seçiciyle override eder.

- **Ortak fetcher:** `app/modules/scrape/fetcher.py` ile hop başına SSRF revalidation
  ve redirect takibi sağlandı.
- **robots.txt uyumu:** `app/modules/scrape/robots.py` ile host bazında kurallar,
  Redis cache ve Crawl-delay okuma eklendi.
- **Discovery merdiveni:** `web_source.py` içinde 4 basamaklı merdiven (RSS autodiscovery ->
  JSON-LD -> tekrar eden blok sezgisi -> trafilatura) tamamlandı.
- **UserPage modeli & UI:** `UserPage` tablosu, kaynak yöneticisi modal/sekmesinde
  3. sekme (`_page_list.html`), filtreleme, anlık toggle/silme ve Celery zamanlanmış
  ingest entegrasyonu tamamlandı.

### 5.3 ✅ Sosyal beslemeler — bitti (PR #49, `feat/bluesky-social-source`)
- **Bluesky** — `https://public.api.bsky.app` üzerinden `app.bsky.actor.getProfile`
  ve `app.bsky.feed.getAuthorFeed` (filter=`posts_no_replies`) auth'suz ve ücretsiz.
  `bluesky_source.py` adaptörü, `UserBluesky` modeli, Celery `link_for_user`
  zamanlanmış görevi, `_bluesky_list.html` ve kaynak yöneticisi 4. sekme entegrasyonu tamamlandı.
  Gönderiler `kind="social"` olarak etiketlenir ve kartta `Social` rozeti alır.
- **Mastodon** — hesap başına yerleşik RSS (`https://sunucu/@kullanici.rss`).
  **Bugünkü altyapıyla zaten çalışıyor** — kullanıcı özel besleme (`UserFeed`) olarak ekleyebilir.

### 5.4 Daha uzun vade
1. ✅ **pgvector + gerçek RAG — bitti (PR #50, `feat/pgvector-rag-integration`).**
   - Docker Postgres servisi `pgvector/pgvector:pg17` imajına güncellendi.
   - `papers.embedding` kolonu (`vector(1536)`) ve HNSW cosine distance indeksi (`ix_papers_embedding_hnsw`) eklendi (Migration `f135d2517c0e`).
   - `embedding_service.py` modülü eklendi: OpenRouter, OpenAI ve Ollama uyumlu, testler için deterministik mock vektör desteği.
   - `_get_internal_similar` pgvector cosine distance ile çalışacak şekilde güncellendi, eşleşme yüzdesi (`similarity_score`) eklendi, boşluklarda kategori/anahtar kelimeye graceful fallback korundu.
   - Kütüphane araması (`/library/search`) ve keşif akışına (`/`) `semantic=1` parametresi ve arayüz toggle'ı eklendi.
   - `ask_paper` ("RAG chat") çok boyutlu gerçek bağlam aramasına yükseltildi: kullanıcının sorusu vektörleştirilerek hem makale içi yapılandırılmış analiz/notlardan hem de kütüphanedeki en yakın 2-3 ilişkili makaleden dinamik bağlam çekilir.
   - `embedding_tasks.py` Celery görevleri (`embed_paper`, `embed_pending_papers`) eklendi ve gece 03:55 zamanlamasına bağlandı.
2. ~~**Yazar takibi.**~~ → **Faz 5.4'e taşındı**, bkz. [PHASE5.md](PHASE5.md).
   ORCID/Scopus/WoS kimlik modeli zaten var (PR #4-#6) ama gerçek bir özelliğe
   bağlanmadı; OpenAlex adaptörü artık mevcut olduğu için author id üzerinden
   "bu yazarın yeni yayınları" beslemesi en yakın büyük kazanç.
3. ✅ **Atıf grafiği (Citation Graph) — bitti (PR #51, `feat/citation-graph`).**
   - OpenAlex API (`referenced_works` + `cites:{work_id}`) ve Semantic Scholar Graph API fallback desteği ile makale referans/atıf ağı çekme servisi (`citation_service.py`).
   - Redis 24 saat önbellekleme (`get_json`/`set_json`).
   - Kullanıcının kütüphane durumuyla dinamik zenginleştirme (`decorate_with_user_library`).
   - Makale detay sayfasına 5. mod olarak interaktif `vis-network` canvas'ı, lejant, filtreler (Referanslar / Atıflar) ve tek tıkla kütüphaneye ekleme (`/papers/<id>/citation-graph/add`).
4. ✅ **Açık erişim tam metin — bitti (PR #62).** Lisans kapılı saklama:
   yeniden dağıtıma izin veren lisanslarda metin saklanır ve aranır, diğerlerinde
   `VideoSummary` deseni sürer. [SCRAPING.md §11](SCRAPING.md).
5. **Kayıtlı arama + uyarı** → Faz 7.1, [PHASE7.md](PHASE7.md).
6. **Zotero'ya aktarım** → Faz 7.2, [PHASE7.md](PHASE7.md). Mendeley kapsam dışı.

---

### 5.7 [#58](https://github.com/AlperEnesErsu/ScrapeMind/issues/58) Hesap ayarları ile ürün yapılandırmasının ayrılması

birmstf'nin açtığı issue. Şikâyet yerinde ve ölçülebilir: `/settings/profile`
altında **12 sekme** var ve ikisi kavramsal olarak ayrı şey.

| Hesap — "kim olduğum, nasıl giriş yaptığım" | Ürün yapılandırması — "ürün benim için ne yapsın" |
|---|---|
| personal · email · password · security (2FA) | identifiers (ORCID/Scopus/WoS) |
| prefs · oauth · sessions · account | interests (ilgi alanları) |
| | ai (LLM sağlayıcı + şifreli anahtar) |
| | authors (takip edilen yazarlar) |

Sağdaki dördü `register_profile_tab()` ile modüllerden geliyor
([tab_registry.py](../app/core/settings/tab_registry.py)); soldaki sekizi
`CORE_TABS`. Yani ayrım kodda **zaten var**, sadece arayüzde tek bir sayfada
birleşiyorlar.

> ⚠️ Issue'nun gövdesi cümle ortasında bitiyor ("...mantıksal olarak ayırmak:") —
> önerilen çözüm yazılmamış. Aşağıdaki, mevcut yapıya bakarak çıkarılan bir okuma,
> issue'nun kendi kararı değil. Uygulamadan önce birmstf'ye sor.

**Dikkat edilecek nokta:** issue sidebar'ın sadeliğinden bahsediyor, ama sidebar
şu an sakin — 6 üst öğe artı bir admin grubu. Karmaşa profil *sayfasının içinde*.
Yani çözüm sidebar'a dört yeni öğe eklemek olamaz.

En küçük müdahale: modül sekmelerini kendi sayfasına taşıyıp sidebar'a **tek** bir
öğe eklemek (ör. "Araştırma Ayarlarım"), profili sekiz gerçek hesap sekmesine
indirmek. `_source_manager.html` da doğal olarak oraya ait. Bu, `tab_registry`'ye
sekmenin hangi gruba ait olduğunu söyleyen bir alan eklemeyi gerektirir —
`app/core/` hiçbir modülü import etmediği için kayıt yönü doğru zaten (CLAUDE.md
kural 1).

Karşı görüş: dört sekme için ayrı bir sayfa da bir tıklama daha demek. Alternatif,
profil sayfasında sekmeleri iki başlık altında gruplamak — sidebar hiç değişmez,
ama issue'nun "menü hiyerarşisi" şikâyetini tam karşılamaz.

---

### 5.5 ✅ Faz 6 — Retrospektif raporlar + yazar grupları (5 Eylül 2026)

İki branch, sırayla: `fix/llm-resilience` (dayanıklılık sertleştirmesi) →
`feat/phase6-reports` (özellik). İkincisi birincinin ucundan dallandı.

**Neden bu sırayla:** rapor hattı LLM-ağır ve çok sayfalı toplama yapıyor.
Timeout'suz/retry'siz bir `_call_llm` üzerine map/reduce kurmak, ilk 429'da
400 kayıtlık toplamayı çöpe atardı.

#### Sertleştirmede çıkan üç şey (hiçbiri planda yoktu)

1. **`celery_app.Task` süreç-global ve her `create_app()` onu yeniden bağlıyor.**
   `init_celery` içindeki `ContextTask`, `flask_app`'i closure'a alıp
   `celery_app.Task`'a atıyor → birden fazla app kuran bir süreçte **son
   `create_app()` tüm task'ların context'ini sahipleniyor**. Testlerde bu,
   config'e bağlı her task testini **sessizce toplama sırasına bağımlı**
   kılıyordu: bir test `app.config`'i değiştirip task çağırdığında, o config'in
   task'a ulaşıp ulaşmayacağı hangi dosyanın önce koştuğuna bakıyordu.
   Düzeltme: ortamda zaten pushlanmış app context'i kazanır (her iki context
   sınıfında). Worker'da ambient context olmadığı için üretim değişmedi.
2. **Her iki LLM SDK'sının kendi retry'ı var** (`max_retries=2`, backoff +
   Retry-After). Bizim retry'ımızla **toplanmaz, çarpılırdı**. İkisi de
   `max_retries=0` yapıldı; bütçe tek yerde.
3. **`response_format` fallback'i her istisnada tetikleniyordu** — 429 alınca
   rate-limit'li uç noktaya istek ikiye katlanıyor, eklenen geri çekilmeyi
   iptal ediyordu. Artık yalnız kalıcı hatalarda.

#### Faz 6'nın kendi tuzakları

- **Nav migration'ı ayrı** (`7b3ce9d10a45`), şemadan (`4360c046a92e`) bağımsız.
  `_sidebar.html` nav linklerini korumasız `url_for(item.endpoint)` ile kuruyor;
  route'suz bir menü satırı **her sayfayı BuildError'a çevirir**. Bu, geliştirme
  sırasında bir kez canlı olarak yaşandı — şema uygulandı, route henüz yoktu.
  Ayrı migration sayesinde tek adımda geri alındı, veri kaybı olmadı.
- **Gruba eklenen yazar `active=False`**, ve duraklatılmış bir yazar gruba
  eklenince **duraklatılmış kalır**. İlk uygulamada `follow_author`'ın mevcut-satır
  dalı koşulsuz `active=True` yapıyordu: kullanıcının bilinçli duraklatması
  gruba ekleme sırasında sessizce bozuluyordu. `activate` parametresi bu iki
  yolu ayırıyor; iki regresyon testi ikisini de kilitliyor.
- **Testlerde `flask db upgrade` çalışmaz** (`conftest` `create_all()` kullanır),
  bu yüzden elle yazılan migration'lar test paketinde hiç sınanmaz. Geçici bir
  veritabanında upgrade + downgrade koşularak ayrıca doğrulandı.
- **Ajanlar pytest koşamıyor** (paylaşımlı test DB + session teardown'da
  `drop_all()`), dolayısıyla test izolasyonu/sıralama hatalarını yapısal olarak
  göremiyorlar. Bu sınıfı orkestratörün yakalaması gerekiyor — yukarıdaki
  1. madde tam olarak böyle bulundu.

#### Kapsam dışı bırakılanlar

- Avesis profil URL'inden ORCID çıkarma (opsiyoneldi, kesildi) — public API'si
  yok, her üniversitenin şablonu farklı, robots.txt fail-closed. Kullanıcı
  ORCID'i elle yapıştırır ya da isimle arar.
- OpenAlex dışı kaynaklardan retrospektif toplama — atıf, konu taksonomisi ve
  yazar ayrıştırması tek yerde ve lisansı temiz olan tek kaynak o
  (`PHASE5.md §2`).
- Raporlar için zamanlanmış üretim yok (bilinçli: on-demand).

### 5.6 ✅ Tasarım sistemi + denetimler (10 Eylül 2026, PR #54 · #56 · #57 · #59)

Arayüzün hiç logosu, favicon'u ve renk sistemi yoktu; `theme.css`'te 51 hex ve 20
font boyutu birikmişti. İnen şey: nar işareti (elle yazılan tek kopya `logo.svg`,
gerisini `scripts/render_favicon.py` türetir), ondan türeyen garnet palet, IBM Plex
+ dokuz adımlı ölçek, ve CVD-güvenli kategorik palet. Tamamı [DESIGN.md](DESIGN.md).

**Arayüze dokunacaksan bilmen gereken tek kural:** marka kırmızı, tehlike de kırmızı.
Aralarında 17° var ve o dilimin tamamı tarandı — AA'yı geçip amber uyarıya da
çarpmayan bir tehlike rengi yok. Yani ayrım **renkle değil formla**: yıkıcı eylem
outline olur, dolu primary'nin yanında dolu danger durmaz, ikincil buton nötrdür.

#### Denetimler nerede
- `tests/core/test_design_system.py` — 7 test, tarayıcısız, normal suite'te.
- `tests/core/test_menu_invariants.py` — bir endpoint, bir menü öğesi.
- `tests/core/test_module_scaffold.py` — üretilen modül gerçekten parse ediyor mu.
- CI `ui-audit` işi — axe-core (WCAG 2.2 A/AA) + 280/320/414px reflow, 7 sayfa.

Hepsi mutasyonla doğrulandı: kural bozuldu, denetimin düştüğü görüldü, geri alındı.

#### Bu turda ortaya çıkan, planda olmayan altı şey
- **Dil seçimi hiç çalışmıyordu.** `init_babel` Flask-Babel **2.x** API'siyle bağlanmış,
  proje 4.0.0'da — `?lang=en` hiçbir şey yapmıyordu, dil çerezi okunmuyordu,
  `current_user.locale` yok sayılıyordu. Varsayılan zaten `tr` olduğu için kimse fark
  etmemiş. Erişilebilirlik denetimi bulmuştu: dil butonu `g.locale`'i basıyor, o da
  seçici çalışınca set ediliyor, dolayısıyla buton boş ve isimsiz render oluyordu.
- **CI 9 Eylül'den beri kırmızıydı** — sebep pgvector değil, sürümsüz `black`'ti (§4.10).
- **Her sayfa 320px'te yana kayıyordu** — flex çocuğunun `min-width: auto`'su.
- **60 etiketin `for`'u yoktu** — gözle etiketli, ekran okuyucuya anonim.
- **`create_module.py` parse edilemeyen şablon üretiyordu** (`{{% extends %}}`), yani
  CLAUDE.md'nin "ilk modülünü şununla oluştur" akışı bozuktu.
- **Isı haritası klavyeyle erişilemiyordu** — tıklanabilir `<div>`'lerdi, artık buton.

#### Bilinerek kabul edilen tek ihlal
Isı haritası hücreleri 10px, WCAG 2.2'nin istediği 24px hedefin altında; bir yıl
görünümü 24px'te çizilemez. Standart eşdeğer kontrole izin veriyor, yanına tarih
girdisi kondu. `scripts/audit_ui.mjs` bu muafiyeti **dar kapsamlı** tanımlıyor —
başka bir sayfada `target-size` çıkarsa gerçektir ve denetimi kırar.

### 5.8 ✅ Faz 7 + lokalde koşarken çıkanlar (11 Eylül 2026, PR #65 → #71)

**Faz 7.2 — Zotero'ya aktarım (PR #65).** Projenin dışarıya ilk yazma işlemi.
Her item ScrapeMind makale id'sini Zotero'nun `extra` alanında taşıyor; ikinci
aktarım işareti geri okuyup mevcut item key'i üzerinden **günceller**. Zotero
toplu yazmaya 200 + item bazlı döküm ile cevap verdiği için `ExportResult`
created/updated/failed sayıyor ve flash "40 üzerinden 37" diyor. Kimlik
bilgileri `UserSettings.settings["zotero"]` içinde Fernet ile şifreli; anahtar
türetmesi LLM anahtarınınkiyle **bayt bayt aynı** (`credentials.py`).

> ⚠️ **Gerçek bir Zotero hesabına karşı hiç koşulmadı.** Ağ yalnızca `requests`
> sınırında taklit edildi. Canlı doğrulama kullanıcının kendi API anahtarını
> ister.

Sonrasında uygulama **lokalde ayağa kaldırılıp elle gezildi**. Aşağıdakilerin
hepsi kodu okurken değil, **çalışan uygulamayı Türkçe okurken** bulundu.

#### Sağlık paneli kendini raporluyordu (PR #66)
Admin genel bakışı var olduğundan beri **her zaman** "Redis: disconnected,
Celery: offline" diyordu. `celery.current_app` thread-local: uygulamayı
oluşturan thread dışında broker'ı olmayan çıplak bir `Celery('default')`
dönüyor, ve threaded bir WSGI sunucusunda **her istek** böyle bir thread'e
düşüyor. `scrape/routes.py`'deki `_celery_task_finished` aynı tuzağa düşüp
orada düzeltilmişti; panelin iki çağrısı atlanmıştı.

> Neden bu kadar yaşadı: **bir şeyin çöktüğünü söyleyen sağlık paneli, panel
> hakkında haber değil sistem hakkında haber gibi okunuyor.**

Panel ayrıca rozet rengini `'active' in health_status.celery` ile seçiyordu —
İngilizce bir kelimeyi arayarak. Artık kod + çalışan sayısı dönüyor.

#### Çeviri katmanında üç ayrı kusur (PR #67 · #68 · #69 · #70)
| kusur | sayı | neden CI görmüyordu |
|---|---|---|
| `_()` ile sarılıp **hiçbir** katalogda olmayan string | 66 | CI iki kataloğu **birbirine** karşılaştırıyor; ikisinde de yoksa eşit kalıyorlar |
| aynısı, ama `_l` / `lazy_gettext` ile yazılmış | 34 | Babel'in varsayılan anahtar kelimelerinde `_l` yok — `_i18n_noop.py`'nin tamamı dahil |
| `{{ _(opt.desc) }}` gibi **veri üzerinden** gettext | 4 | statik çıkarım değişkeni göremez |
| başka bir string'e ait çeviri (fuzzy hasarı) | 21 | çeviri gayet normal bir etiket — başka şeyin etiketi |

Fuzzy hasarından örnekler: `Toggle favorite` → **"Tema Değiştir"** (karanlık mod
kaldırılırken çevirisi buraya düştü), `Save` → **"Aktif"**, `Delete this note?`
→ **"Bu rolü silmek istiyor musunuz?"**, `No notes yet.` → **"Henüz rol yok."**
Birkaçı `title`/`aria-label` içinde: **bir ekran okuyucu okuyana kadar kimse
görmüyor.**

`scripts/i18n_audit.py` üçünü de tutuyor ve CI'da koşuyor. **Kataloğa asla
yazmıyor** — hasarı yapan şey zaten `pybabel update`'in fuzzy eşlemesiydi.
Kaynak açıklamaları için elle liste tutmuyor, `SOURCE_META` ve `TOPICS`'i
doğrudan okuyor: elle tutulan liste (`_i18n_noop.py`) **zaten kaymış olan
şeydi**.

#### Faz 7.1 indiği hâlde hiç çalışmamıştı (PR #71)
Her kayıtlı arama uyarısı `RuntimeError` atıyordu: `notification_text`
içindeki `_()` locale seçicisine düşüyor, seçici `request.args`'ı okuyor,
uyarılar ise **beat'te** koşuyor. Hata arama başına `except Exception`'a
takılıp `alerts_search_failed` olarak loglanıyor ve dışarıdan **"yeni eşleşme
yok"** gibi görünüyordu.

İkinci ve daha kötü kusur bunu yaşayarak bulundu: `announce()` bildirilmiş
kümesini bildirimden **önce** commit ediyordu. Hata 150 makaleyi "bildirildi"
yapıp öldü; o makaleler o aramayla bir daha asla eşleşmeyecek ve kimseye bir
şey söylenmedi. Sıra artık **önce teslim et, sonra işaretle**.

#### Ve bunun neden test edilemediği — açık borç
`pytest-flask`, `app` fixture'ını kullanan **her** testin etrafına `GET /` için
bir istek bağlamı itiyor. Süit, "istek bağlamı yok" hatasını **yeniden
üretemiyor**; 7.1 tam bu yüzden ölü çıktı. `-p no:flask` ile **36 test
düşüyor**. Bu, sıradaki iş listesinde 2. madde — o 36'sının hangisinin gerçek
hata, hangisinin yalnızca test kolaylığı olduğunu ayırmak gerekiyor.

#### Lokal koşunun kendisi
Gerçek tarama uçtan uca çalıştı: ilgi alanı → "Şimdi tara" → Celery → **151
makale** (crossref, pubmed, openalex, web_reach). Kayıtlı arama kaydedildi ve
uyarı istek bağlamı olmadan bildirim üretti.

#### Sağlık paneli işçi ile zamanlayıcıyı ayıramıyordu (PR #76)
Bu bölüm önce "bilinçli ama yanıltıcı" diye yazılmıştı; sonra düzeltildi.

Panel canlılığı **Beat'in** zamanladığı `core.heartbeat`'ten okuyordu. Taze damga
ikisini birden kanıtlıyor, ama **bayat** damga hangisinin düştüğünü söylemiyor
ve panel her iki durumda da worker'ı suçluyordu. Beat'i durdurmak, sapasağlam
bir worker'ı olan makinede **"İşçi: kapalı"** yazdırıyordu — yani işe
yaramayacak düzeltmeyi işaret ediyordu.

Worker artık `worker_ready`'de başlattığı bir daemon thread'den **kendi**
anahtarını damgalıyor (`app/tasks/worker_liveness.py`). Onu hiçbir şey
zamanlamıyor, mesele de bu: **ayakta ama boşta** worker da "buradayım" diyor.
`core.heartbeat` diğer anahtarı damgalamaya devam ediyor ve artık adının
söylediğini ifade ediyor.

> ⚠️ Periyodik görev değil **thread**, çünkü periyodik görev Beat'e ihtiyaç
> duyar — kaldırılmak istenen bağımlılık tam olarak o.

Ayırma sırasında admin genel bakışındaki `celery inspect ping` de kalktı. O
RPC hiçbir şey cevap vermediğinde tüm zaman aşımı boyunca blokluyordu: sayfa,
admin **ne bozuk diye bakmak için açtığında** 12 saniye sürüyordu. `health.py`
bu tuzağı zaten yazmıştı; panel yine de içine düşmüştü. **Test süiti 121s →
86s.** İki panel artık aynı kaynaktan okuduğu için birbiriyle de çelişmiyor.

## 6. Doküman Haritası

| Dosya | İçerik |
|---|---|
| [README.md](../README.md) | Proje tanıtımı, kurulum, yol haritası |
| [CLAUDE.md](../CLAUDE.md) | AI asistanı için bağlam — kurallar ve tuzaklar |
| [PROJECT.md](../PROJECT.md) | Detaylı tasarım dokümanı, faz planı |
| [docs/SCRAPING.md](SCRAPING.md) | Veri toplama mimarisi — **yeni kaynak eklemeden önce oku** |
| [docs/PHASE5.md](PHASE5.md) | Faz 5 planı — patentler, dergi kalitesi, yazar takibi, opsiyonel Scopus |
| [docs/PHASE7.md](PHASE7.md) | Faz 7 planı — kayıtlı arama + uyarı, Zotero, #58 |
| [docs/API_V1.md](API_V1.md) | JSON API referansı |
| [docs/DESIGN.md](DESIGN.md) | Tasarım sistemi — **arayüze dokunmadan önce oku** |
| [docs/UI_REVIEW.md](UI_REVIEW.md) | UI inceleme notları |
| [docs/adr/](adr/) | Mimari karar kayıtları — neden **yapmadığımız** şeyler |
| [IMPROVEMENTS.md](../IMPROVEMENTS.md) | UI/UX punch list — ⚠️ dosya tablonun ortasında kesik, tamamlanmalı |
| [docs/HANDOVER.md](HANDOVER.md) | Bu dosya |
