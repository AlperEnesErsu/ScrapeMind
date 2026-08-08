# Faz 5 — Patentler, Dergi Kalite Katmanı, Yazar Takibi (+ opsiyonel Scopus)

> **Durum:** planlandı, uygulanmadı. 8 Ağustos 2026.
> Branch başlangıcı: `feat/openalex-crossref-youtube-channels` (Faz 4).
> Migration zinciri head'i: `b3d9f2a1c4e6`.

Bu doküman Faz 5'in kapsamını, gerekçesini ve uygulama sırasını tutar. Kod yazmadan
önce [SCRAPING.md](SCRAPING.md) §2 (kaynak adaptörü sözleşmesi) ve §11 (etik sınırlar)
okunmalı; devralan geliştirici için ayrıca en sondaki **Devir notları** bölümü var.

---

## 1. Neden

Faz 4 akademik kaynak sayısını 5'e çıkardı (arXiv, Semantic Scholar, PubMed, OpenAlex,
Crossref) ama hepsi aynı şeyi yapıyor: anahtar kelimeye göre makale metadata'sı. Üç
gerçek boşluk kaldı:

1. **Kalite sinyali yok.** Kullanıcı 100 makale görüyor; hangisi Q1 dergide, hangisi
   50 atıf almış — bilmiyor. Türk akademisinde terfi/teşvik kriteri doğrudan bu.
2. **Bilim–sanayi köprüsü yok.** Aynı konuda kim patent alıyor, kullanıcının fikri
   daha önce patentlenmiş mi — hiç görünmüyor.
3. **Kimlik modeli ölü.** `UserIdentifier` (ORCID/Scopus ID/WoS ID) PR #4-#6'dan beri
   duruyor, hiçbir özelliğe bağlı değil (eski [HANDOVER](HANDOVER.md) §5.4 ②).

**Beklenen sonuç:** kullanıcı akışında "Q1 · 87 atıf" rozeti taşıyan makaleler, ilgi
alanına giren yeni patentler ve takip ettiği yazarların yeni yayınları görür; ayrı bir
sayfada kendi buluş fikri için prior-art araması yapıp LLM'den yenilik değerlendirmesi alır.

---

## 2. Elsevier / WoS araştırması — neden ana yol değil

Faz 5 "WoS Lite ve Elsevier'in ücretsiz haftalık kotalarını kullanalım" sorusundan
çıktı. Araştırma sorunun **kota olmadığını** gösterdi:

| Servis | Ücretsiz limit | Gerçek engel |
|---|---|---|
| **WoS Starter** (WoS Lite'ın yerini aldı) | **50 istek/gün**, atıf sayısı dönmüyor | Çok kullanıcılı gecelik tarama için kullanılamaz. Kurumsal plan 5.000/gün ama WoS aboneliği şart |
| **Scopus Search** | 20.000/hafta, 9 req/s | Anahtar **kurum IP'sine bağlı**; kampüs dışından anlamlı sonuç yok (`X-ELS-Insttoken` gerekir) |
| Scopus Abstract Retrieval | 10.000/hafta | Tam abstract kurumsal entitlement istiyor |
| **Elsevier API sözleşmesi** | — | İçeriğin **kalıcı saklanması yasak** ("sözleşme biterse tüm kopyaları sil"), Elsevier ürünleriyle **rekabet eden türev servis** üretmek yasak |

Son satır ScrapeMind'in mimarisiyle doğrudan çatışıyor: tüm model kalıcı `papers`
tablosu + çok kullanıcıya sunum.

**Elsevier'in asıl kıymetli olduğu üç şeyden ikisi zaten ücretsiz elimizde:** atıf
sayısı ve yazar disambiguation → OpenAlex, hem de daha iyi lisansla. Üçüncüsü, dergi
quartile/indeks bilgisi, Scimago'nun ücretsiz CSV'siyle karşılanıyor (§5.3).

**Karar:** ana yatırım ücretsiz ve lisansı temiz kaynaklara yapılır. Scopus,
**admin panelinden açılan, varsayılan kapalı, "discovery-only"** opsiyonel bir kaynak
olarak eklenir (§5.4) — kurum IP'sinden çalıştırılacağı senaryo için. WoS Starter aynı
altyapıyla sonradan eklenebilir ama 50/gün limiti ve abstract dönmemesi nedeniyle bu
faza alınmıyor.

### Patent tarafı

| Kaynak | Limit | Kapsam |
|---|---|---|
| **EPO OPS v3.2** | **4 GB/hafta** ücretsiz | Dünya çapında (DOCDB), **TR yayınları dahil**, abstract + patent ailesi + hukuki durum |
| **PatentsView** (USPTO) | 45 istek/dk, ücretsiz key | Sadece ABD, ama çok zengin yapılandırılmış veri (mucit, hak sahibi, CPC, atıflar) |

Patent tarifnamesi/özeti çoğu yargı alanında **resmî yayın → telif dışı**, yani
[SCRAPING.md](SCRAPING.md) §11 etik sınırıyla hiç çatışmıyor. Şema uyumu da birebir:
`PaperPayload` + `kind="patent"` — `papers` tablosunda **migration gerekmiyor**.

Değerlendirilip elenenler: **Lens.org** (14 gün trial, sonrası ücretli),
**TÜRKPATENT** (public API yok — TR yayınları EPO OPS/DOCDB üzerinden zaten geliyor),
**Google Patents Public Data** (BigQuery, GCP faturası gerekiyor).

---

## 3. Uygulama sırası — 4 artımlı adım

Her adım tek başına test-yeşil ve shippable (Faz 4'ün commit disiplini korunur,
`git bisect` bozulmaz).

**Sıra bağlayıcı:** 5.1 diğer üçünün ön koşulu.

---

### 5.1 — Kimlik gerektiren kaynak altyapısı

Bugün `effective_source_prefs` ([service.py](../app/modules/scrape/service.py))
`category == "academic"` olan her kaynağı **satır yoksa açık** kabul ediyor. Anahtarı
olmayan bir kaynak bu modelde her kullanıcı için her gece `-1` sentinel'i ve kalıcı
`status="partial"` üretir. Faz 5'in tüm kaynakları anahtar gerektirdiği için bu ilk sırada.

**[`sources/__init__.py`](../app/modules/scrape/sources/__init__.py)** — `SOURCE_META`'ya
iki ortogonal alan:
- `requires_key: True` + `credentials_ok: Callable[[], bool]` — env değişkenleri yoksa
  kaynak `enabled_sources()`'a hiç girmez (kullanıcı toggle'ı da görmez).
- `requires_admin_optin: "scopus_enabled"` — ek olarak `SystemSettings`'te o anahtar
  `True` olmalı.
- Yeni `category: "patent"` (kaynak seçici kartında gruplama + `topics` eşleşmesi için).

`effective_source_prefs`'e dördüncü kural: `requires_key` olan kaynak, kimlik bilgisi
yoksa hiç listelenmez; `requires_admin_optin` olan kaynak admin açmadıkça **varsayılan
kapalı** (`UserSource` satırı olsa bile).

**Kimlik bilgileri nerede durur:** mevcut `os.getenv` konvansiyonu korunur
(bkz. `semantic_scholar_source.py`) — `SystemSettings.value` düz JSON olduğu için oraya
sır yazılmaz. Admin panelindeki ayar sadece **boolean toggle**:
- `.env`: `EPO_OPS_KEY`, `EPO_OPS_SECRET`, `PATENTSVIEW_API_KEY`, `SCOPUS_API_KEY`,
  `SCOPUS_INSTTOKEN` (+ `.env.example` placeholder'ları)
- `SystemSettings`: `scopus_enabled`, `patents_enabled`

**Admin UI:** [`app/core/settings/system_forms.py`](../app/core/settings/system_forms.py)
+ [`routes.py`](../app/core/settings/routes.py) — mevcut 5 alanlı `SystemSettingsForm`'a
iki `BooleanField` eklenir; `max_user_channels` girdisi birebir örnek. Her toggle'ın
yanında "anahtar yapılandırılmamış" uyarısı (`credentials_ok()` sonucu) gösterilir —
aksi halde admin açar ama hiçbir şey olmaz.

**Kalıcı kota sayacı — yeni.** [`ratelimit.py`](../app/modules/scrape/ratelimit.py)'deki
Redis sabit pencere "haftada 20.000" veya "haftada 4 GB" ifade edemez, ayrıca
**fail-open** (Redis düşerse sınırsız istek). Kotalı/lisanslı kaynak için bu yanlış yön.

- Yeni model `SourceQuotaUsage(source_name, window_start, requests_used, bytes_used)`,
  `uq(source_name, window_start)`, haftalık pencere.
- `ratelimit.consume_quota(name, *, cost=1, bytes_=0) -> bool` — Postgres'te atomik
  `UPDATE ... RETURNING`, limit `SCRAPE_QUOTA_<NAME>_WEEKLY` config'inden
  (`app/config.py`'deki `SCRAPE_RATE_*` kalıbı). **Fail-closed**: hata olursa `False`.
  Mevcut Redis `<name>_slot()` saniye-bazlı throttle için yerinde kalır; ikisi birlikte
  çağrılır (`slot` = anlık hız, `consume_quota` = kümülatif bütçe).
- Kota bitince adaptör `SourceThrottledError` fırlatır → `ScanRun` otomatik
  `status="partial"` olur (`apply_scan_result` zaten böyle çalışıyor). **Boş liste
  dönmek yanlış** — kullanıcıya "0 sonuç" diye yalan söyler, bkz. `ratelimit.py` docstring.
- Admin health paneline "Scopus: 3.240 / 20.000 (hafta X'te sıfırlanır)" satırı.

**Migration:** `source_quota_usage` tablosu (`b3d9f2a1c4e6_video_summaries.py` şablonu:
açık `created_at`/`updated_at` `server_default`, `deleted_at` + index, adlandırılmış
`UniqueConstraint`, simetrik `downgrade()`).

---

### 5.2 — Patent kaynakları + prior-art arama

**`app/modules/scrape/sources/epo_ops_source.py`**
- Modül seviyesinde `requests` — `python-epo-ops-client` SDK'sı **kullanılmaz**
  (`CLAUDE.md` kuralı 7: testler modülün kendi `requests`'ini monkeypatch'liyor).
- OAuth2 client-credentials: `POST https://ops.epo.org/3.2/auth/accesstoken`
  (Basic auth = key:secret) → bearer token, **20 dk TTL**. Token modül seviyesinde bir
  `_token_cache` dict'inde tutulur (süre + değer), 401'de bir kez yenilenip istek
  tekrarlanır. Bu repoda **ilk OAuth'lu adaptör** — token yenileme mantığı adaptörün
  içinde kalır, ortak wrapper'a çıkarılmaz.
- Arama: `GET /3.2/rest-services/published-data/search/biblio` + CQL sorgusu.
  **CQL boolean destekliyor** → `ti,ab any "kw1" or ti,ab any "kw2"` tek istekte; yani
  `_PER_KEYWORD_REQUEST_SOURCES` setine **eklenmez** (bu set maliyet tahminini de besliyor).
- `PaperPayload(source="epo_ops", external_id=<publication number>, kind="patent",
  authors=<mucitler>, categories=<CPC/IPC kodları>, url=<Espacenet linki>, doi=None)`.
- Yanıt boyutu `consume_quota(..., bytes_=len(resp.content))` ile 4 GB bütçesinden düşülür.

**`app/modules/scrape/sources/patentsview_source.py`**
- `X-Api-Key` header, `GET https://search.patentsview.org/api/v1/patent/` + JSON
  `q`/`f`/`o` parametreleri. 429 + `Retry-After` işlenir.
- `source="patentsview"`, `kind="patent"`.

Her ikisi için: `ratelimit.py`'de `epo_ops_slot()` / `patentsview_slot()` +
`SCRAPE_RATE_*` config; `sources/__init__.py`'de 3 satır (import · `AVAILABLE_SOURCES` ·
`SOURCE_META` with `category="patent"`, `requires_key=True`); `_DEFAULT` ve
`.env.example` `SCRAPE_SOURCES` güncellemesi.

**Gecelik tarama.** `app/tasks/patent_tasks.py` — `patents.ingest_for_user` /
`patents.ingest_for_all_users`. [`channel_tasks.ingest_for_user`](../app/tasks/channel_tasks.py)
altı adımlı kalıbı birebir kopyalanır (user guard → `acquire_user_lock` →
`record_scan_run` → `apply_scan_result` → `self.retry` → `release_user_lock`).
- ⚠️ Modül [`app/tasks/__init__.py`](../app/tasks/__init__.py)'deki import tuple'ına
  **eklenmezse worker task'ı hiç görmez, hata da vermez**.
- `TASK_ROUTES`: `patents.*` → `io` kuyruğu.
- `BEAT_SCHEDULE` ([`app/tasks/schedule.py`](../app/tasks/schedule.py)): **03:05** —
  `channels` (02:55) ile `scrape` (03:15) arasına. Saatler **UTC değil**, `Europe/Istanbul`.
- `ScanRun.kind` dördüncü değeri `"patents"` — String(16), migration gerekmez, ama
  `service.scan_status_context` / `_estimate_scan_seconds` ve
  `library/_timeline.html`'deki `kind` branch'i bilmeli.

**Prior-art / yenilik arama sayfası.** `scrape_bp` altında yeni route `/papers/patents`
([`routes.py`](../app/modules/scrape/routes.py)) + `templates/scrape/patents.html`:
- Sorgu kutusu → adaptörler canlı çağrılır, sonuçlar **`papers`'a yazılmaz**
  (tek seferlik prior-art sorguları kalıcı tabloyu kirletmesin; EPO fair-use da bunu
  destekliyor). Sadece gecelik anahtar-kelime taraması persist eder.
- Sonuç listesi + "aileyi göster" (EPO `family` servisi).
- LLM yenilik değerlendirmesi: [`ai_service`](../app/modules/scrape/ai_service.py)'e
  `analyze_novelty(query, patents)` — mevcut `_resolve_llm` ve `is_ai_enabled(user)`
  aynen kullanılır, yeni sağlayıcı mantığı yazılmaz.
- Nav girdisi: `f3b1a0c2d8e7_seed_user_nav_items.py` kalıbında idempotent migration.

**UI:** `templates/scrape/_paper_card.html`'deki hardcoded `kind` if/elif zincirine
`patent` dalı + `source-epo_ops` / `source-patentsview` CSS sınıfı. Kaynak seçici kartı
ve library search'ün kaynak dropdown'ı (`distinct_user_sources`) **kendiliğinden** yeni
kaynakları alır — orada değişiklik yok.

---

### 5.3 — Dergi kalite katmanı (Scopus/WoS'un asıl değeri, lisanssız)

**Yeni model `Journal`** — `issn_l` String(9) unique idx, `title`, `publisher`,
`sjr` Numeric, `sjr_quartile` String(2) (`Q1`..`Q4`), `sjr_year` Int, `h_index` Int,
`is_doaj` Bool, `is_oa` Bool.

**`Paper`'a iki kolon:** `issn_l` String(9) idx, `cited_by_count` Integer.
`PaperPayload`'a aynı iki alan `None` default'uyla (`kind` alanının precedent'i,
[SCRAPING.md](SCRAPING.md) §2).

> ⚠️ **`cited_by_count` overwrite semantiği ister.** `_ENRICHABLE_FIELDS` "boşsa doldur,
> doluysa asla ezme" mantığında çalışıyor; atıf sayısı zamanla artan bir değer olduğu
> için orada donup kalır. Ayrı bir `_REFRESHABLE_FIELDS = ("cited_by_count",)` seti
> eklenip `_enrich` içinde **her zaman güncellenir**. `issn_l` normal enrichable.

**Seed script `scripts/seed_journals.py`:**
- Scimago `journalrank.php?out=xls` CSV'si (alanlar: Title, Type, ISSN, Publisher, SJR,
  **SJR Best Quartile**, H-index, Open Access) → `Journal` upsert.
- DOAJ dizin CSV'si → `is_doaj`.
- Idempotent, `--year` parametresi.
- **Lisans: CC BY-NC + atıf zorunlu.** UI'da Scimago atfı verilir
  ("SCImago, (n.d.). SJR — SCImago Journal & Country Rank"), [SCRAPING.md](SCRAPING.md)
  §11'e bir satır eklenir. ScrapeMind ticari olmadığı için NC şartı sorun değil —
  bu, projenin ticarileşmesi hâlinde yeniden değerlendirilmesi gereken bir bağımlılık.

**Adaptör güncellemeleri:**
- `openalex_source.py`: `primary_location.source.issn_l` → `issn_l`,
  `cited_by_count` → `cited_by_count`.
- `crossref_source.py`: `ISSN[0]`, `is-referenced-by-count`.
- Diğer adaptörler dokunulmaz; DOI eşleşmesi üzerinden `upsert_paper` enrichment'ı
  değeri zaten taşır.

**UI:** `_paper_card.html`'de ISSN → `Journal` join'inden **Q1/Q2** rozeti + atıf sayısı.
N+1'i önlemek için `list_user_papers` sorgusuna `Journal` join'i eklenir (`VideoSummary`
`joinedload`'ının gerekçesiyle aynı sebep). Library search'e `?quartile=Q1` filtresi
(`search_user_papers_query`).

**Migration:** `journals` tablosu + `papers`'a iki kolon + index'ler.

---

### 5.4 — Yazar takibi + opsiyonel Scopus

**Yazar takibi.** Mevcut `UserAuthor` (`user_id`, `author_name`) **yeniden kullanılır**,
yeni tablo açılmaz — genişletilir: `openalex_id` String(32) idx, `orcid` String(19),
`last_work_at` DateTime(tz), `active` Bool.

- ORCID → OpenAlex çözümlemesi: `GET api.openalex.org/authors/https://orcid.org/{orcid}`.
  Yeni fonksiyonlar `openalex_source.fetch_author(orcid_or_id)` ve
  `works_by_author(author_id, since)`.
- Giriş noktası mevcut kimlik sekmesi
  ([`_tab_identifiers.html`](../app/modules/academic/templates/settings/_tab_identifiers.html))
  — kullanıcının ORCID'i varsa "kendi yayınlarımı takip et", ayrıca serbest yazar arama.
  Servis fonksiyonları [`academic/service.py`](../app/modules/academic/service.py)'de
  hazır (`list_user_identifiers(user, type_code="orcid")`).
- `app/tasks/author_tasks.py` → `authors.ingest_for_all_users`, kuyruk `scrape`,
  `BEAT_SCHEDULE` **03:25**, `ScanRun.kind="authors"`. 5.2'deki task kalıbının aynısı.
- Yan düzeltme: [`scripts/seed.py`](../scripts/seed.py)'deki `wos_id` regex'i
  `^[A-Z]-\d{4}-\d{4}$` yeni `ABC-1234-2020` formatını kabul etmiyor — genişletilir.

**Scopus (opsiyonel, discovery-only).** `app/modules/scrape/sources/scopus_source.py`:
- `GET api.elsevier.com/content/search/scopus`, header `X-ELS-APIKey` + varsa
  `X-ELS-Insttoken`. `TITLE-ABS-KEY(a OR b)` boolean destekli → tek istek.
- **Kritik tasarım: abstract saklanmaz.** Payload sadece `doi` + `title` +
  `published_at` + `url` (Scopus link-out) taşır, `abstract=None`. Scopus "ne var"
  bilgisini verir; **storable metadata'yı OpenAlex/Crossref doldurur** — mevcut DOI-first
  `upsert_paper` + `_enrich` mekanizması bunu bedava yapıyor. Sonuç: Elsevier lisanslı
  metni hiç persist edilmez, kaynağa link verilir ([SCRAPING.md](SCRAPING.md) §11
  "özet + kaynağa link" kuralıyla hizalı).
- Scopus'un bulduğu ama OpenAlex aramasının döndürmediği DOI'lar için hidrasyon: yeni
  `openalex_source.fetch_by_doi(doi)`, ayrı bir task değil — `scrape_for_user` içinde
  `source == "scopus"` payload'ları için tek toplu çağrı.
- 5.1'deki `requires_admin_optin="scopus_enabled"` + `consume_quota` (20.000/hafta)
  ile kapılanır. Varsayılan **kapalı**.

> **Kalan risk, açıkça:** Elsevier sözleşmesi "türev servis" ve "kalıcı kopya"
> maddelerini geniş yazmış. Yukarıdaki tasarım abstract/tam metin saklamayarak riski
> minimize ediyor ama DOI + başlık da teknik olarak API'den gelen içerik. Repo halka
> açık olduğu için `docs/adr/0002-elsevier-discovery-only.md` yazılıp gerekçe,
> reddedilen alternatifler ve **kararın yeniden açılma koşulu** kaydedilir
> ([ADR-0001](adr/0001-headless-browser-yok.md) formatı). ADR, 5.4 uygulanırken yazılacak.

---

## 4. Dokümantasyon işleri

- [SCRAPING.md](SCRAPING.md): §3 kaynak tablosuna 3 yeni satır; §5'e dördüncü kural
  (kimlik gerektiren / admin-opt-in kaynaklar); §6.2'ye kalıcı kota bölümü; §11'e patent
  telif notu + Scimago atfı; §12 config tablosuna yeni env değişkenleri.
  Bu arada **§8 bayat** — hâlâ dedup'ın sadece `(source, external_id)` olduğunu yazıyor,
  kod DOI-first + `_enrich` yapıyor; bu fazda düzeltilir.
- `docs/adr/0002-elsevier-discovery-only.md` — yeni (5.4 ile birlikte).
- [HANDOVER.md](HANDOVER.md) §3 commit listesi + §5 sıradaki iş güncellenir.
- Çeviriler: **`pybabel extract`/`update` KULLANMA** (aşağıda, Devir notları).

---

## 5. Doğrulama

**Testler.** Adaptörler `tests/modules/test_scrape_sources.py` kalıbıyla: konserve
yanıtlar inline literal olarak, modülün **kendi** `requests`'i monkeypatch'lenir,
`<name>_slot` `lambda: True` ile etkisizleştirilir.

- ⚠️ `test_registry_has_academic_adapters_and_feeds` ve `test_enabled_sources_defaults_to_all`
  **set eşitliği** kontrol ediyor — yeni `_PATENT_KEYS` / `_LICENSED_KEYS` setleri
  eklenmezse kırılır.
- EPO OAuth: token cache testi (ilk çağrı token alır, ikinci almaz, 401'de bir kez yeniler).
- Kota: `consume_quota` limitte `False` döner, DB hatasında `False` (fail-closed),
  pencere sınırında sıfırlanır.
- `effective_source_prefs`: anahtar yoksa kaynak listede yok; anahtar var + admin kapalı
  → kapalı; ikisi de var → varsayılan **kapalı** ama toggle edilebilir.
- `upsert_paper`: `cited_by_count` **ezilir**, `abstract` ezilmez.
- Task'lar `tests/tasks/test_channel_tasks.py` kalıbıyla (`clean_user` fixture, servis
  fonksiyonu task'ın import ettiği modülde monkeypatch'lenir).

```bash
venv/Scripts/python.exe -m pytest tests/ -q          # TEST_DATABASE_URL kökteki .env'de olmalı
venv/Scripts/python.exe -m pytest tests/modules/test_scrape_sources.py tests/tasks/ -q
```

**Uçtan uca** (Docker Desktop açık, `myo_postgres17` + `shared_redis` ayakta):

1. `flask db upgrade` → 3 yeni migration sorunsuz, `downgrade` de çalışıyor.
2. `venv/Scripts/python.exe scripts/seed_journals.py --year 2025` → `journals` satır
   sayısı kontrol.
3. `.env`'e EPO/PatentsView anahtarları; `/admin/settings`'ten `patents_enabled` aç.
   Anahtar yokken toggle yanında uyarı çıktığı görülür.
4. Ana sayfadan "Scrape now" → `/papers` akışında `kind="patent"` kartları ve Q1 rozeti
   + atıf sayısı görünür; `ScanRun` `status="ok"`.
5. `/papers/patents` → sorgu gir, sonuç + LLM yenilik analizi; sonrasında
   `SELECT count(*) FROM papers WHERE source='epo_ops'` **artmamış** olmalı
   (prior-art sonuçları persist edilmiyor).
6. Kimlik sekmesinden ORCID takibi ekle → `authors.ingest_for_user` elle tetikle, yeni
   yayın akışa düşüyor.
7. Kota paneli: admin health'te "EPO OPS: x MB / 4 GB" doğru artıyor.
8. Scopus (varsa kurumsal IP): admin'den aç → taramada DOI'lar geliyor, `papers`
   satırlarında `source='scopus'` olanların `abstract`'ı **boş değil** (OpenAlex
   hidrasyonu çalıştı) ama abstract Scopus'tan değil OpenAlex'ten geldi.

**Kapsam dışı:** WoS Starter adaptörü, Lens.org, TÜRKPATENT, pgvector/gerçek RAG,
besleme conditional GET ([HANDOVER](HANDOVER.md) §5.1 — bağımsız küçük iş, bu fazla
çakışmıyor).

---

## 6. Devir notları (devralan geliştirici için)

Plan dışında, bu repoda çalışırken bilinmesi gereken ve koddan/git geçmişinden
anlaşılmayan şeyler.

**Sıra bağlayıcı.** 5.1 diğer üçünün ön koşulu. Atlanırsa `effective_source_prefs`
`category="academic"` olan her kaynağı "satır yoksa açık" kabul ettiği için, anahtarı
olmayan yeni kaynaklar her kullanıcıda her gece `-1` sentinel'i ve kalıcı
`status="partial"` üretir.

**İki sessiz tuzak:**
- Yeni Celery task modülü `app/tasks/__init__.py`'deki import tuple'ına eklenmezse
  **worker task'ı hiç görmez ve hata da vermez.** `TASK_ROUTES` girdisi de gerekir;
  `-Q` ile başlatılan worker'ın kuyruk listesinde `celery` de bulunmalı (Faz 4'teki
  `fix(deploy)` commit'inin sebebi tam buydu — prod'da routed task'ların hiçbiri
  tüketilmiyordu).
- `tests/modules/test_scrape_sources.py`'deki iki registry testi **set eşitliği**
  kontrol ediyor; yeni kaynak beklenen setlere yazılmadan testler kırılır.

**Çeviri akışı.** `pybabel extract` / `pybabel update` **kullanılmayacak** — bu akış bir
kez mevcut TR çevirilerin ~300'ünü sildi (fuzzy-match kazası). Yeni string'ler Babel
API'siyle tek tek eklenir (snippet `CLAUDE.md`'de), sonra `pybabel compile -d translations`.
TR ve EN kataloglarının msgid key set'leri eşit olmalı — CI kontrol ediyor.

**Yerel ortam.** Bu makine paylaşımlı altyapı kullanıyor: `myo_postgres17` **5432**'de,
`shared_redis` **6379**'da (myoChtBt projesinin compose'u ayağa kaldırıyor).
`TEST_DATABASE_URL` kökteki `.env`'de **bulunmak zorunda** — yoksa testlerin tamamı
`psycopg2.OperationalError` verir ve bu "kod bozuk" gibi okunur; ilk bakılacak yer
burasıdır. `venv`'i `requirements.txt` ile senkron tutun.

**Test config'i bilinçli "kırık":** `FEED_ALLOW_PRIVATE_HOSTS=True` (CI'da dışa DNS yok),
`REDIS_URL` kapalı porta bakar (kilit/rate limit fail-open olsun), LLM anahtarları
boşaltılır (yanlışlıkla faturalı çağrı olmasın). Faz 5'in kalıcı kota sayacı bunun
**tersine fail-closed** tasarlanıyor — testlerde bu farkı bilerek kurgulayın.

**Mimari kurallar** (`CLAUDE.md`): `app/core/` asla `app/modules/`'dan import etmez;
adaptörler modül seviyesinde `requests` kullanır (testler modülün kendi `requests`'ini
monkeypatch'liyor — ortak wrapper'ın arkasına saklamayın, bu yüzden EPO için hazır SDK
kullanılmıyor); `BEAT_SCHEDULE` saatleri UTC değil `Europe/Istanbul` — kullanıcıya
gösterilen zamanı elle hesaplamayın, `app/tasks/schedule_info.py` var; `beat` tek replika.

**Tarayıcı otomasyonu yok.** Scrapy/Playwright/Selenium kurulu değil ve kurulmayacak —
gerekçe ve kararın yeniden açılma koşulu [ADR-0001](adr/0001-headless-browser-yok.md).

**Commit disiplini.** Faz 4'te her commit tek başına test-yeşil tutuldu, `git bisect`
güvenilir. 5.1–5.4 adımları bu granülerlikte ilerleyecek şekilde bölündü.

**Repo halka açık** — commit/PR/dokümana gizli bilgi (şifre, API key, gerçek e-posta)
yazılmaz, `.env` commit'lenmez, yeni config değişkeni eklenirken `.env.example`
placeholder ile güncellenir, örneklerde `example.com` / `example.test` kullanılır.

---

## 7. Kaynaklar

- [Elsevier API quotas & throttling](https://dev.elsevier.com/api_key_settings.html) ·
  [API service agreement](https://dev.elsevier.com/api_service_agreement.html)
- [Web of Science Starter API](https://developer.clarivate.com/apis/wos-starter)
- [EPO OPS kurulum / fair use](https://docs.ip-tools.org/patzilla/configure/epo-ops.html)
- [PatentsView Search API reference](https://search.patentsview.org/docs/docs/Search%20API/SearchAPIReference/) ·
  [rate limits](https://patentsview.org/forum/7/topic/781)
- [Scimago Journal & Country Rank — kullanım koşulları](https://www.scimagojr.com/help.php)
