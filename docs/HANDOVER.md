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

**Doğrulama durumu (12 Ağustos 2026):**

```
pytest tests/ -q      →  1004 passed in ~97s
ruff check app/ tests/  →  All checks passed!
black --check app/ tests/ →  208 files would be left unchanged
```

> ⚠️ `ruff`/`black`'i `migrations/` üzerinde çalıştırma — o klasörde eski lint borcu
> var ve formatlayıcı 21 eski migration'ı gereksizce yeniden yazar.

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

### 4.9 pgvector paylaşımlı Postgres'te yok — `create_all()` burada ölür
Faz 5.4 `Paper.embedding`'i `VECTOR(1536)` olarak ekledi. `tests/conftest.py`
her koşudan önce `CREATE EXTENSION IF NOT EXISTS vector` deniyor **ama
`try/except` ile yutuyor** — extension yoksa sessizce geçiyor, ardından
`_db.create_all()` şu satırda ölüyor:

```
sqlalchemy.exc.ProgrammingError: type "vector" does not exist
[SQL: CREATE TABLE papers (... embedding VECTOR(1536), ...)]
```

Bu "kod bozuk" gibi okunur, değildir. `docker/docker-compose.yml`'deki `db`
servisi `pgvector/pgvector:pg17`'ye sabitlendi, ama **bu makine o servisi
kullanmıyor** (bkz. üstteki paylaşımlı altyapı notu): Postgres, myoChtBt'nin
compose'unun kaldırdığı `myo_postgres17` container'ı ve o **`postgres:17-alpine`**
— stok imaj, pgvector içermiyor. Yani arkadaşının makinesinde geçen testler
burada toplu hâlde patlar; fark kodda değil, imajda.

Kontrol:
```bash
docker exec myo_postgres17 psql -U postgres -tAc   "SELECT name FROM pg_available_extensions WHERE name='vector';"
# boş dönüyorsa extension yok
```

İki çıkış yolu var, ikisi de bir bedelle geliyor — bu yüzden karar burada
verilmedi, bilinçli açık bırakıldı:

1. **ScrapeMind'a ayrı bir pgvector container'ı** (`SCRAPEMIND_DB_PORT=5433` +
   `docker compose -f docker/docker-compose.yml up -d db`). myoChtBt'ye
   dokunmaz, ama `scrapemind` dev veritabanındaki mevcut veri yeni container'a
   taşınmadıkça boş başlar.
2. **`myo_postgres17`'nin imajını `pgvector/pgvector:pg17` yapmak.** Volume
   korunur, PG major sürümü aynı. Ama alpine (musl) → debian (glibc) geçişi
   collation'ı değiştirir; myoChtBt'nin metin index'leri için `REINDEX`
   gerekebilir. Başkasının verisi, o yüzden onun kararı.

Ayrıca `TEST_DATABASE_URL` kökteki `.env`'de bulunmak **zorunda** (§4.5) ama
`.env.example`'da girdisi yoktu — eklendi. Yeni bir checkout'ta ilk yapılacak
şey odur.

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
4. **Açık erişim tam metin** — OpenAlex `best_oa_location`. Etik sınır net: sadece OA.
5. **Kayıtlı arama + uyarı** — bildirim altyapısı (`add_notification`) hazır.
6. **Zotero/Mendeley dışa aktarım** — BibTeX var, API entegrasyonu doğal devam.

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

## 6. Doküman Haritası

| Dosya | İçerik |
|---|---|
| [README.md](../README.md) | Proje tanıtımı, kurulum, yol haritası |
| [CLAUDE.md](../CLAUDE.md) | AI asistanı için bağlam — kurallar ve tuzaklar |
| [PROJECT.md](../PROJECT.md) | Detaylı tasarım dokümanı, faz planı |
| [docs/SCRAPING.md](SCRAPING.md) | Veri toplama mimarisi — **yeni kaynak eklemeden önce oku** |
| [docs/PHASE5.md](PHASE5.md) | Faz 5 planı — patentler, dergi kalitesi, yazar takibi, opsiyonel Scopus |
| [docs/API_V1.md](API_V1.md) | JSON API referansı |
| [docs/UI_REVIEW.md](UI_REVIEW.md) | UI inceleme notları |
| [docs/adr/](adr/) | Mimari karar kayıtları — neden **yapmadığımız** şeyler |
| [IMPROVEMENTS.md](../IMPROVEMENTS.md) | UI/UX punch list — ⚠️ dosya tablonun ortasında kesik, tamamlanmalı |
| [docs/HANDOVER.md](HANDOVER.md) | Bu dosya |
