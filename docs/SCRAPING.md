# Scraping Mimarisi

> ScrapeMind'ın veri toplama katmanı: kaynak adaptörleri, besleme yutma, güvenlik
> katmanları ve zamanlama. PROJECT.md'de "ayrı `SCRAPEMIND.md`'de planlanacak"
> denen doküman budur.

**Kapsam:** `app/modules/scrape/` + `app/tasks/{scrape,feed,digest}_tasks.py`

---

## 1. Genel Akış

```
Beat (schedule.py)
   ├─ 02:45  feeds.ingest_all ──────────► küratörlü RSS → Paper (kind="news")
   ├─ 03:15  scrape.run_for_all_users ─┐
   ├─ 03:45  feeds.link_for_all_users ─┤ fan_out() → kullanıcı başına 1 task
   └─ 07:00  digest.run_for_all_users ─┘
                    │
                    ▼
        scrape.run_for_user(user_id)
                    │
   ┌────────────────┴─────────────────┐
   │ 1. kilit al (Redis SET NX EX)     │
   │ 2. ScanRun aç (record_scan_run)   │
   │ 3. service.scrape_for_user        │
   │ 4. ScanRun kapat + bildirim       │
   └───────────────────────────────────┘
```

`service.scrape_for_user` ([service.py:600](../app/modules/scrape/service.py#L600)) adım adım:

1. `list_user_keywords(user)` — anahtar kelime yoksa `reason="no_keywords"` ile çıkar.
2. `user_enabled_sources(user)` — aktif kaynak yoksa `reason="no_sources"` ile çıkar.
3. `ensure_keyword_translations(...)` — TR terimleri İngilizceye çevirir (aşağıda).
4. Her kaynak için `search_for_keywords(terms, max_results=...)` — **try/except içinde**;
   patlayan kaynak `per_source[name] = -1` sentinel'i alır ve tarama devam eder.
5. `upsert_paper(payload)` → `link_user_paper(user, paper, matched_keyword=...)`.

Dönüş: `{"hits": int, "linked": int, "sources": {name: count}}`.

---

## 2. Kaynak Adaptörü Sözleşmesi

**ABC yok, duck-typed modül var.** Bir adaptör = bir Python modülü:

```python
SOURCE_NAME: str
search(query, *, max_results) -> list[PaperPayload]
search_for_keywords(keywords, *, max_results) -> list[PaperPayload]
```

Orkestratör yalnızca `search_for_keywords`'ü çağırır; `search` tekil sorgu kolaylığıdır.

### `PaperPayload` — tüm kaynakların ortak çıktısı
[`sources/payload.py`](../app/modules/scrape/sources/payload.py) — frozen dataclass:

| Alan | Not |
|---|---|
| `source` | registry anahtarı |
| `external_id` | kaynağa özel id — arXiv `2401.12345v2`, S2 paperId, PMID, RSS GUID |
| `title`, `abstract`, `authors`, `url`, `pdf_url`, `published_at`, `categories` | |
| `kind` | `"news"` (RSS) veya `None` (makale). Default'lu — yeni alan eklerken bu kalıbı izle |

`as_dict()` doğrudan `Paper(**data)`'ya açılır, bu yüzden alan adları modelle birebir olmalı.

### Yeni kaynak ekleme reçetesi

1. `sources/<ad>_source.py` yaz — modül seviyesinde `requests` kullan
   (**testler modülün kendi `requests`'ini monkeypatch'liyor**, bu yüzden ortak bir
   HTTP wrapper'ın arkasına saklama).
2. Rate limit gate'i ekle: `ratelimit.py`'de `<ad>_slot()` + `SCRAPE_RATE_<AD>_*` config.
3. [`sources/__init__.py`](../app/modules/scrape/sources/__init__.py)'de üç satır:
   import · `AVAILABLE_SOURCES[...]` · `SOURCE_META[...]`.
4. `_DEFAULT` string'ine adı ekle, `.env.example`'daki `SCRAPE_SOURCES` satırını güncelle.
5. Test yaz — `tests/modules/test_scrape_sources.py` kalıbı.

**UI değişikliği gerekmiyor.** `source_options()` registry'yi otomatik topluyor,
kaynak seçici kartı kendiliğinden yeni satırı gösteriyor.

### Adaptör metadata'sı neden modülde değil?
`SOURCE_META` registry'de tutuluyor ki adaptörler saf I/O kalsın. Her satır:

```python
{"label", "icon", "desc", "url", "topics": [...], "category": "academic"|"feed"}
```

`topics`, `TOPICS` taksonomisinden (9 anahtar: ai, ml, cs, physics, math, biomed,
social, humanities, general) seçilir ve ilgi-farkında kaynak seçiciyi besler.
`category` de varsayılan davranışı belirler (§5).

---

## 3. Mevcut Kaynaklar

| Anahtar | Tür | Anahtar gerekir mi | Not |
|---|---|---|---|
| `arxiv` | akademik | Hayır | `arxiv` SDK, client-side 3sn gecikme |
| `semantic_scholar` | akademik | Opsiyonel `SEMANTIC_SCHOLAR_API_KEY` | **OR operatörü yok** → anahtar kelime başına 1 istek |
| `pubmed` | akademik | Opsiyonel `NCBI_API_KEY` | esearch→efetch iki adım, OR ile tek sorgu |
| `openalex` | akademik | Hayır (opsiyonel `OPENALEX_MAILTO` → polite pool) | OR ile tek sorgu; abstract `abstract_inverted_index` (kelime→pozisyon) olarak gelir, düz metne geri çevrilir. **Dergi kalitesinin birincil kaynağı**: gerçek `issn_l` + `cited_by_count` (Faz 5.3) |
| `crossref` | akademik | Hayır (opsiyonel `CROSSREF_MAILTO` → polite pool) | **OR operatörü yok** → anahtar kelime başına 1 istek; DOI zorunlu (yoksa kayıt atlanır); abstract JATS XML (`<jats:p>...`) olarak gelir, düz metne çevrilir ve **kısmi** — çoğu yayıncı abstract yüklemiyor, bu kaynağın asıl değeri DOI + metadata zenginleştirme |
| `openai_blog`, `google_ai_blog`, `deepmind_blog`, `huggingface_blog` | besleme | Hayır | Küratörlü, **global** yutulur |
| `youtube_reach` | besleme | Hayır | `yt-dlp` CLI/Python modülü (`sys.executable`), `kind="video"` |
| `github_reach` | besleme | Hayır (`gh` CLI opsiyonel) | `gh` CLI repo araması (`FileNotFoundError` korumalı), `kind="github"` |
| `web_reach` | besleme | Hayır | ScrapeMind `requests` + `net_guard` SSRF koruması, `kind="news"` |
| `youtube_channel` | besleme | Hayır | Abonelik tabanlı — YouTube'un ücretsiz, anahtarsız kanal RSS beslemesi (`feeds/videos.xml?channel_id=`), `rss_source.fetch_feed_conditional` üzerinden; `kind="video"`. Transkript ayrı bir adımda `yt-dlp` ile (video başına bir kez, taramaya dahil değil) çekilir — yt-dlp kurulu değilse veya YouTube isteği engellerse (özellikle datacenter IP'lerinden yaygın) özetsiz zarifçe devam eder |
| `user_feed` | besleme | Hayır | Kullanıcının eklediği özel RSS |
| `scopus` | akademik | **Evet** — `SCOPUS_API_KEY` (+ kampüs dışı `SCOPUS_INSTTOKEN`) | **Yalnızca keşif, varsayılan kapalı.** `abstract` sabit `None` — Elsevier lisanslı içerik saklanmaz; saklanabilir metadata `openalex_source.fetch_by_doi` ile OpenAlex'ten gelir. DOI'si olmayan kayıt atlanır. Anahtar kurum IP'sine bağlı (401/403 ayrı loglanır). Gerekçe ve kalan risk: [ADR-0002](adr/0002-elsevier-discovery-only.md) |
| `epo_ops` | **patent** | **Evet** — `EPO_OPS_KEY` + `EPO_OPS_SECRET` | Dünya çapında (DOCDB), TR dahil. Repodaki **tek OAuth'lu** adaptör: client-credentials → 20dk bearer token, modül seviyesinde cache, 401'de bir kez zorla yenilenip istek tekrarlanır. CQL `or` destekler → tüm anahtar kelimeler **tek istekte**. **Bant genişliği ölçer** (4 GB/hafta), çağrı değil: istek öncesi nominal rezervasyon, yanıt sonrası gerçek boyutla kapanış. 404 = "eşleşme yok" (hata değil). `kind="patent"`, `doi=None` |
| `patentsview` | **patent** | **Evet** — `PATENTSVIEW_API_KEY` | Yalnızca ABD ama çok daha yapılandırılmış: mucit, **hak sahibi**, CPC ayrı nesneler. JSON DSL + `_or` → tek istek. 429'da `Retry-After` **bir kez** ve yalnızca kısaysa beklenir. Hak sahibi `categories`'te `assignee:` önekiyle taşınır (kurum mucit değildir). `kind="patent"`, `doi=None` |

> ⚠️ `rss_source` sözleşmeyi **bilerek** kısmen uygular: `SOURCE_NAME` ve `search()`
> yoktur, `search_for_keywords` sabit `[]` döner. Beslemeler anahtar kelime aramasıyla
> değil, `feed_tasks.ingest_all` ile global olarak yutulur — `[]` dönüşü kaynak
> döngüsü oraya uğrarsa güvenli kalsın diye vardır.

Dağıtım `SCRAPE_SOURCES` env değişkeniyle listeyi kısabilir
(`SCRAPE_SOURCES=arxiv` gibi). Tanınmayan ad `scrape_source_unknown` olarak loglanıp atılır.

> **Patent kaynakları `SCRAPE_SOURCES`'un varsayılanında yer alır ama bu tek başına
> hiçbir şey açmaz.** İki kapı daha var (§5): anahtar yoksa `enabled_sources()` kaynağı
> zaten listelemez, admin `patents_enabled`'ı açmadıysa `effective_source_prefs` her
> kullanıcı için kapalı tutar. Yani anahtarları tanımlamamış bir kurulumda bu iki satır
> hiçbir davranış değiştirmez.

### Patentler neden ayrı bir gecelik tarama?

`patents.ingest_for_all_users` (03:05) akademik taramadan (`scrape.run_for_all_users`,
03:15) **ayrı** bir task ve ayrı bir `ScanRun.kind="patents"` üretir. Gerekçe: patent
kaynakları haftalık bütçeyle ölçülüyor. Tek bir `ScanRun` paylaşılsaydı tükenmiş bir
patent kotası kullanıcının **akademik** taramasını da `partial` işaretlerdi ve durum
satırı anlamını kaybederdi. Patent anahtarı olmayan kurulum tek registry lookup'ıyla
kısa devre yapar, kullanıcı başına task kuyruğa atmaz.

---

## 4. Anahtar Kelime Çevirisi

Kullanıcı ilgi alanını Türkçe yazıyor, kaynaklar İngilizce korpus — "kalp yetmezliği"
hiçbir şey bulmuyordu. `Keyword` ([academic/models.py](../app/modules/academic/models.py))
üç kolon taşır:

- `value_en` — kanonik İngilizce karşılık
- `variants` — ek eşanlamlılar (`["cardiac failure"]`), OR sorgusunu genişletir
- `translated_at` — "zaten İngilizce" no-op'unda bile set edilir, böylece çevrilemeyen
  terim her taramada yeniden denenmez

`Keyword` **global ve tekilleştirilmiş**: bir terim tüm dağıtım için bir kez çevrilir,
sonraki kullanıcı hiçbir maliyet ödemez. Doldurma `service.ensure_keyword_translations`
ile **tarama anında tembel** yapılır — ilgi ekleme senkron yolunda asla LLM çağrısı yok.

---

## 5. Kaynak Seçimi (opt-out modeli)

`service.effective_source_prefs(user)` dört kademeli çözer:

0. Kaynak `requires_admin_optin` taşıyor ve admin açmamışsa → **kapalı**, üstelik
   1. maddedeki açık satır bunu **ezemez** (Faz 5.1).
1. Açık `UserSource` satırı varsa **o kazanır**.
2. Satır yok + `category == "academic"` / `"patent"` → **açık** (geniş kataloglar
   herkese uygun). İstisna: admin opt-in'i olan kaynak, açılmış olsa bile
   **varsayılan kapalı**.
3. Satır yok + `category == "feed"` → kullanıcının sınıflandırılmış konularıyla
   kaynağın `topics`'i kesişiyorsa açık, yoksa kapalı.

**Satırın yokluğu "açık" demek** — bu sayede yeni bir kaynak eklendiğinde mevcut
kullanıcılar etkilenmez ve migration'da satır üretmek gerekmez.

### Kimlik gerektiren ve admin onaylı kaynaklar (Faz 5.1)

Faz 4'e kadar hiçbir kaynak anahtar istemiyordu; "enabled" sadece "dağıtım listeledi"
demekti. Faz 5'in kaynakları (EPO OPS, PatentsView, Scopus) anahtarsız **cevap
veremez** ve cevap veremeyen kaynak `scrape_for_user`'ın `-1` sentinel'ini tetikler —
yani her kullanıcının her gecelik taraması kalıcı olarak `status="partial"` olur.
Anlamı olmayan bir uyarı ikonu, uyarı ikonu olmaktan çıkar.

`SOURCE_META`'da iki **bağımsız** kapı bunu engeller:

| Alan | Etkisi |
|---|---|
| `requires_key: True` + `credentials_ok: Callable[[], bool]` | Anahtar yoksa kaynak `enabled_sources()`'a **hiç girmez** — seçicide, kütüphane filtresinde, `user_enabled_sources`'ta aynı anda görünmez olur |
| `requires_admin_optin: "<SystemSettings anahtarı>"` | Kaynak **listelenir** (admin neyi açtığını görmeli) ama admin açana kadar herkes için zorla kapalı; açıldıktan sonra da varsayılan kapalı |

`credentials_ok` bilinçli olarak **callable**, bool değil: env değişkenleri her çağrıda
okunur (`SCRAPE_SOURCES` presedansı), böylece anahtar ekleyen dağıtımın restart'a
ihtiyacı olmaz. Probe patlarsa "anahtar yok" sayılır — cevap veremeyen bir kontrol,
anahtarın var olduğunun kanıtı değildir.

Admin toggle'ları core'un [`toggle_registry.py`](../app/core/settings/toggle_registry.py)'sine
kaydedilir ([`scrape/routes.py:_register_system_toggles`](../app/modules/scrape/routes.py)).
`app/core/` asla `app/modules/`'dan import etmediği için (CLAUDE.md kuralı 1) core yalnızca
adını bilmediği boolean'ları saklar ve render eder; anlamı — hangi API'yi kapıladığı,
hangi env değişkenine baktığı — modülde durur. **Anahtarın kendisi asla
`SystemSettings`'e yazılmaz** (`value` düz JSON, admin'in okuyabildiği bir tablo);
orada yalnızca boolean vardır.

Konu sınıflandırması `ai_service.classify_user_topics(user)`: önce sözlük hızlı yolu
(`_TOPIC_LEXICON`, kelime sınırı regex'i), tutmazsa tek LLM çağrısı. Sonuç
`UserSettings.settings["topics"]` içinde anahtar kelime seti hash'ine göre cache'lenir.

UI: [`dashboard/_sources_card.html`](../app/modules/dashboard/templates/dashboard/_sources_card.html)
— HTMX `outerHTML` swap, "Sana önerilenler" / "Diğer kaynaklar" gruplaması.

---

## 6. Güvenlik Katmanları

### 6.1 SSRF guard — `net_guard.py`
Kullanıcının verdiği URL'i **sunucu** çekiyor; bu bir request-forgery primitifidir.
`is_public_http_url(url, *, allow_private=False)`:

- şema ∈ {http, https}, port ∈ {80, 443}
- bulut metadata host adları isimle reddedilir (`metadata.google.internal` vb.)
- `getaddrinfo` ile **çözülen tüm adresler** denetlenir: private / loopback /
  link-local / reserved / multicast / unspecified
- `::ffff:127.0.0.1` ve 6to4 gibi IPv6 geçiş adresleri açılıp tekrar denetlenir
- **çözülemeyen host reddedilir** — "unresolvable" asla "sorun yok" demek değil
- hata mesajı bilinçli olarak muğlak (`BLOCKED_MESSAGE`); "bu host 10.0.0.5'e çözüldü"
  demek guard'ı ağ tarayıcısına çevirir

**İki kez çalışır:** besleme eklenirken (hızlı geri bildirim) ve fetch sırasında
**her redirect hop'unda** — DNS arada yeniden yönlendirilebilir. Bu yüzden
`rss_source._get_with_redirects` `allow_redirects=False` ile manuel zincir takip eder.

`FEED_ALLOW_PRIVATE_HOSTS=true` guard'ı tamamen kapatır — **yalnızca yerel geliştirme**.
Test config'i bunu `True` yapar (CI'da dışa DNS yok); guard'ın kendisi çözüm
gerektirmeyen literal IP'lerle ayrıca test edilir.

### 6.2 Rate limit — `ratelimit.py`
Adaptörlerin kendi client-side gecikmeleri worker sayısıyla **çarpılıyor**, bu yüzden
limit dağıtım genelinde Redis'te tutulur: `acquire_slot(bucket, limit, per_seconds,
max_wait=30)` — `INCR` + `EXPIRE` ile sabit pencere.

**Her yerde fail-open**: Redis yoksa veya patlarsa `True` döner. Rate limit bir nezaket
mekanizmasıdır; Redis arızası taramayı tamamen durdurmamalı.

Bucket adı serbest string — host başına limit gerekirse yeni mekanizma yazmaya gerek yok.

### 6.2b Kalıcı haftalık kota — `ratelimit.consume_quota` (Faz 5.1)

Yukarıdaki Redis sayacı **saniye/dakika bazlı bir hız** limitidir ve **fail-open**'dır.
Lisanslı kaynakların kotası ikisine de uymaz:

- **Pencere hafta.** Scopus 20.000 istek/hafta, EPO OPS 4 GB/hafta. Yedi gün yaşaması
  gereken bir Redis anahtarı, Redis'ten istenmeyen bir dayanıklılık sözüdür — burada
  broker olarak yapılandırılmış, restart bütçeyi sessizce sıfırlar.
- **Fail-open yanlış yön.** Cache düştü diye sözleşmeli kotayı aşmak, bir gecelik
  taramayı atlamaktan kötüdür.

Bu yüzden bütçe Postgres'te (`SourceQuotaUsage`, kaynak başına haftada bir satır) durur
ve `consume_quota(name, *, cost=1, bytes_=0)` **fail-closed**'dır: herhangi bir DB
hatasında `False` döner.

- Harcama ve limit kontrolü **tek statement**: `UPDATE ... WHERE used + cost <= limit
  RETURNING id`. Son slot için yarışan iki worker'ın ikisi birden kazanamaz.
- Haftanın ilk harcaması satırı `ON CONFLICT DO NOTHING` ile yaratır.
- İstek ve byte **ayrı eksenler** (EPO bant genişliği, Scopus çağrı ölçer). Bir eksende
  limit `0` ise "burada tavan yok" demektir, "hiç izin yok" değil.
- Pencere **Pazartesi 00:00 UTC** — bilerek `BABEL_DEFAULT_TIMEZONE` değil (§9'daki
  `BEAT_SCHEDULE`'ın aksine): sıfırlama sınırı sağlayıcıya ait ve her worker için aynı
  an olmalı.
- Kota bitince adaptör **`SourceThrottledError` fırlatır**, boş liste dönmez —
  `scrape_for_user` bunu `-1` sentinel'ine çevirir, `ScanRun` `status="partial"` olur.
  Boş liste dönmek kullanıcıya "0 sonuç" diye yalan söyler.

`quota_usage(name)` admin overview'daki kota kartını besler ve **asla patlamaz**.
Bütçesi olmayan kaynaklar panelde hiç listelenmez — anlamsız "0 / 0" satırları admin'e
paneli görmezden gelmeyi öğretir.

### 6.3 Boyut ve süre sınırları
`FEED_FETCH_TIMEOUT` (15sn), `FEED_FETCH_MAX_BYTES` (5 MiB, streaming olarak kesilir),
`_MAX_REDIRECTS=3`. Celery tarafında `CELERY_TASK_SOFT_TIME_LIMIT`/`_TIME_LIMIT` —
soft yakalanabilir, task elindekini commit edebilir.

---

## 7. Besleme Yutma

`fetch_feed_conditional(feed, *, max_entries=40, etag=None, last_modified=None)`
→ `FeedFetchResult(payloads, status, etag, last_modified, http_status, title)`

`status` sözlüğü: `ok | not_modified | blocked | http_error | timeout | too_large | parse_error`.

İki ayrı yol:
- **Küratörlü beslemeler** → `feed_tasks.ingest_all`, global, kullanıcıdan bağımsız.
- **Kullanıcı beslemeleri** → `service.ingest_user_feeds(user)`, `feeds.link_for_user` içinde.

Yutulan haberler otomatik olarak kullanıcıya bağlanmaz; `link_relevant_feed_items`
en fazla 50 bağlanmamış `kind="news"` makaleyi **tek** `ai_service.score_feed_relevance`
çağrısında puanlatır ve ≥60 skorluları bağlar.

> 🐞 **Bilinen açık:** `fetch_feed_conditional` etag/last_modified alıp döndürüyor ama
> **hiçbir model bunları saklamıyor** — `UserFeed`'de böyle kolon yok ve her iki yutma
> yolu da bu değerleri geçirmeyen `fetch_feed` sarmalayıcısını çağırıyor. Yani 304
> yolu şu an ölü kod ve her gece her besleme tam indiriliyor. Bkz. §10.

---

## 8. Kalıcılık ve Tekilleştirme

`service.upsert_paper` **DOI-first** çözer (Faz 4):

1. Normalize edilmiş DOI (`doi.normalize_doi`) ile arar. DOI **yazarken** normalize
   edilir, böylece saklanan biçim her zaman kanonik ve arama düz bir
   `filter_by(doi=...)` — `ilike` taraması değil. Önceki sürüm kaynağın verdiği
   string'i olduğu gibi saklayıp `ilike` ile eşleştiriyordu; bu büyük/küçük harfe
   duyarsızdı ama **prefix'e duyarsız değildi**, yani bir kaynaktan gelen
   `https://doi.org/10.X/Y` ile diğerinden gelen `10.X/Y` yine iki satır üretiyordu.
2. DOI yoksa/tutmazsa `UniqueConstraint("source", "external_id")`.

Eşleşen satır **fill-only** zenginleştirilir (`_enrich`): boş alanlar
(`abstract`, `pdf_url`, `url`, `doi`, `published_at`, `categories`, `authors`) yeni
payload'dan doldurulur, **dolu alan asla ezilmez** — iki kaynağın ikisinde de değer
varken "hangisi doğru" diye karar vermek için elimizde bir dayanak yok, o yüzden ilk
gelen kalır. Boş sayılan şekiller: `None`, boş string, boş liste (`0`/`False` değil).

Sınır olarak kalan durum: gelen DOI bir satırla, gelen `(source, external_id)` ise
**başka** bir satırla eşleşirse DOI kazanır ve zenginleştirilen o olur; diğer satıra
dokunulmaz. İki satırı birleştirmek (`UserPaper` bağlarını taşımak dahil) upsert'in
işi değil — gerçek bir migration ister.

### İki farklı birleştirme kuralı (Faz 5.3)

`_enrich` iki set üzerinde **bilerek farklı** davranır:

| Set | Kural | Neden |
|---|---|---|
| `_ENRICHABLE_FIELDS` | **Fill-only** — boşsa doldur, doluyu asla ezme | İki kaynak da dolu değer veriyorsa hangisinin doğru olduğuna karar verecek dayanağımız yok; ilk gelen kalır |
| `_REFRESHABLE_FIELDS` | **Her zaman güncelle** | Alan zamanla değişen bir şeyi izliyor |

Bugün `_REFRESHABLE_FIELDS` yalnızca `cited_by_count` içeriyor. Atıf sayısı bir
**koşan toplam**; 2019 tarihli bir makale yıllarca atıf almaya devam eder. Fill-only
kuralında ilk bildirilen rakamda donardı — bugün 300 atıfı olan bir makale için sonsuza
kadar "12 atıf" göstermek, hiçbir şey göstermemekten **kötüdür**, çünkü kesin görünür.

İki koruma var: gelen değer `None` ise "bu kaynak söylemedi" demektir ve mevcut gerçek
sayıyı silmez; `0` ise gerçek bir değerdir ve bayat bir sayının yerini alabilir.

`issn_l` fill-only tarafta kalır — bir makalenin dergisi değişmez.

> Bu sete yeni alan eklerken dikkat: yalnızca **açıkça zamana bağlı** alanlar buraya
> girer. Geri kalan her şey için "iki dolu değer arasında galip seçemeyiz" doğru
> varsayılandır.

`ScanRun` her taramayı kaydeder (`status`: `running|ok|partial|skipped|error`;
negatif kaynak sayacı → `partial`, `reason` → `skipped`). UI Celery'yi yoklamak yerine
bu tabloyu okur.

---

## 9. Zamanlama

`BEAT_SCHEDULE` ([app/tasks/schedule.py](../app/tasks/schedule.py)) —
**saatler `BABEL_DEFAULT_TIMEZONE` (Europe/Istanbul) yereldir, UTC değil.**
`enable_utc=True` yalnızca mesaj zaman damgalarını etkiler.

| Saat | Task |
|---|---|
| her dakika | `core.heartbeat` |
| 02:45 | `feeds.ingest_all` |
| 02:55 | `channels.ingest_for_all_users` |
| 03:05 | `patents.ingest_for_all_users` |
| 03:15 | `scrape.run_for_all_users` |
| 03:25 | `authors.ingest_for_all_users` |
| 03:45 | `feeds.link_for_all_users` |
| 04:00 / 04:15 / 04:30 | audit / revoked token / scan run purge |
| 07:00 · Pzt 07:30 | `digest.run_for_all_users` (daily / weekly) |

`fan_out()` kullanıcıları aynı anda kuyruğa atmaz: `countdown = (user_id * 2654435761)
% SCAN_FANOUT_WINDOW_SECONDS` (Knuth hash). **Rastgele değil deterministik** — çünkü
kullanıcıya "sıradaki tarama 03:27" diyoruz ve bunun doğru olması gerekiyor.

Worker ayrımı ([docker-compose.yml](../docker/docker-compose.yml)):
- `worker` — `-Q celery,scrape,llm -c 4`, prefork. `celery` kuyruğu listede **kalmalı**:
  varsayılan kuyruk odur, `TASK_ROUTES`'ta adı geçmeyen her task oradan tüketilir.
- `worker-io` — `-Q io -P threads -c 16`. Besleme çekmek saf ağ beklemesi; hem `requests`
  hem `psycopg2` beklerken GIL'i bırakır, 16 thread 16 yavaş beslemeyi tek proseste
  bindirir. (gevent değil: psycopg2 C eklentisi hub'ı bloklar.)
- `beat` — **tek replika**. Zamanlama yerel dosyada, ikinci beat her şeyi çift tetikler.

---

## 10. Bilinen Sınırlar / Sıradaki İş

| # | Konu | Neden önemli |
|---|---|---|
| 1 | **`doi` üzerinde UNIQUE constraint yok** | Sadece index var. İki worker aynı DOI'yi eşzamanlı ekleyebilir. Bugünkü tek gecelik worker'da pratikte imkânsız; constraint eklemek önce mevcut duplicate'leri temizleyen ayrı bir migration ister |
| 2 | **Satır birleştirme yok** | DOI bir satırla, `(source, external_id)` başka bir satırla eşleşirse DOI satırı kazanır, diğeri olduğu gibi kalır. Birleştirmek `UserPaper` linklerini taşımayı gerektirir — upsert'in işi değil |
| 3 | **Conditional GET beslemelerde hâlâ ölü** | `UserFeed.etag` kolonu var ve `add_user_feed` dolduruyor, ama iki yutma yolu da `fetch_feed`'i çağırıyor ve etag'i **geçirmiyor** → 304 yolu ölü. `ingest_user_channels` geçiriyor (§7) |
| 3b | YouTube kanal feed'i validator göndermiyor | Ölçüldü: `feeds/videos.xml` yalnızca `Cache-Control: max-age=900` dönüyor, **ETag ve Last-Modified yok**. `UserChannel.etag` bu yüzden hep NULL kalır ve her koşu ~15 girdiyi yeniden indirir. Kolonun boş olması hata değil — düzeltmeye çalışma |
| 4 | RSS'siz site scrape'i yok | Lab/enstitü haber sayfaları erişilemez. Yol haritası: [ADR-0001](adr/0001-headless-browser-yok.md) + HANDOVER §5 |
| 5 | Kanal RSS'i son ~15 videoyu verir | Yeni eklenen kanalın geçmişi geri doldurulmaz. Geçmiş için `youtube_reach` anahtar kelime araması var |
| 6 | Video özeti dil başına cache'lenmez | `VideoSummary` `paper_id` üzerinde tekil (feed'de N+1 olmasın diye) — `PaperAnalysis`'in aksine dil başına satır yok |
| 7 | `ask_paper` "RAG" değil | Başlık+abstract prompt'a dolduruluyor; pgvector yok |
| 8 | Prior-art araması **hiçbir şey saklamaz** | Tasarım kararı, eksik değil: tek seferlik fikir kontrolleri `papers`'ı kirletmesin (§11). Sonuç kaydedilemez — kullanıcı ilgilendiği patenti kütüphanesine alamaz. İhtiyaç doğarsa çözüm "otomatik kaydet" değil, açık bir "kütüphaneme ekle" butonudur |
| 9 | EPO patent ailesi / hukuki durum kullanılmıyor | OPS bunları veriyor, adaptör yalnızca biblio araması yapıyor. "Aileyi göster" için ayrı bir servis çağrısı gerekir |
| 10 | Patent araması yalnızca başlık+özet tarar | EPO `ti,ab any`, PatentsView `_text_any` — tarifname (claims) metni taranmıyor. Prior-art için asıl metin orada; ikisi de ayrı endpoint ister |
| 11 | **Quartile yalnızca seed edilmiş dergiler için var** | `journals` tablosu elle yüklenir (`scripts/seed_journals.py`). Seed çalıştırılmamış bir kurulumda hiçbir kartta rozet çıkmaz — bu bozukluk değil, veri yokluğu. `?quartile=Q1` filtresi de bilinmeyen dergili makaleleri **dışarıda bırakır** (bilerek: "Q1 göster" derginin iddiasıdır) |
| 12 | Crossref'in `issn_l`'i yok | Crossref bir derginin basılı ve elektronik ISSN'ini hangisi bağlayıcı demeden listeler; adaptör ilk geçerli olanı alır. Print/online ayrımı olan dergi iki farklı ISSN altına düşebilir. OpenAlex gerçek `issn_l` verdiği ve fill-only yarışını kazandığı için bu yalnızca Crossref-only kuyruğu etkiler |
| 13a | **Scopus başlığı saklanıyor** | OpenAlex hidrasyonu tutmazsa Elsevier kaynaklı başlık `papers`'ta kalır. Bilinçli, sınırlı ve kayıtlı bir risk — [ADR-0002](adr/0002-elsevier-discovery-only.md) |
| 13b | Yazar takibi yalnızca OpenAlex | OpenAlex'te olmayan bir yazar takip edilemez; ORCID'i olmayan araştırmacı için giriş yolu yok |
| 13 | Atıf sayısı kaynağa göre değişir | `cited_by_count` "en son yazan kaynak kazanır" — OpenAlex ve Crossref farklı sayılar bildirir ve ikisi de kendi içinde doğrudur. Kartta tek bir rakam görünür; hangi kaynaktan geldiği gösterilmez |

Detaylı yol haritası ve gerekçeler: [HANDOVER.md](HANDOVER.md).

---

## 11. Etik Sınırlar

README'deki taahhüt bu katmanın davranış sözleşmesidir:

- Hedef sitelerin `robots.txt`'ine uyulur *(genel sayfa scrape'i eklendiğinde
  zorunlu — şu an yalnızca RSS çekildiği için devrede değil)*
- Rate limit + dağıtık zamanlama ile hedef sunucular yorulmaz
- Mümkün olan her yerde **resmî API** tercih edilir
- Telifli içerik **yeniden yayımlanmaz** — yalnızca özet + kaynağa geri link.
  Video transkriptleri bu yüzden **saklanmaz**: `VideoSummary` yalnızca LLM özetini ve
  `transcript_chars` sayacını tutar, ham metni değil
- X/Twitter kazınmaz: ücretsiz okuma API'si yok ve kazıma ToS ihlali olur
- **Tarayıcı otomasyonu kullanılmaz** — gerekçe ve kararın yeniden açılma koşulu:
  [ADR-0001](adr/0001-headless-browser-yok.md)

### Dergi kalite verisi (Faz 5.3)

- **SJR verisi CC BY-NC ve atıf zorunlu.** Quartile'ın göründüğü her yerde Scimago
  kredilendirilir — rozet tooltip'inde ve kütüphane filtresinin altında. Bu bir nezaket
  değil **lisans şartıdır**; kaldırma.
  Atıf metni: "SCImago, (n.d.). SJR — SCImago Journal & Country Rank".
- **NC şartı canlı bir kısıt.** ScrapeMind ticari olmadığı sürece sorun yok. Proje
  ticarileşirse bu bağımlılık yeniden değerlendirilmeli — buradaki tek veri parçası ki
  ticari bir lisans onu otomatik kapsamaz.
- Veri dosyaları (Scimago CSV, DOAJ CSV) **elle** indirilir, `scripts/seed_journals.py`
  ile yüklenir. Gecelik bir task bunları çekmez: yıllık anlık görüntüler ve ikisinin de
  bir scraper'a gömülmeye değer sabit sürümlü URL'i yok.

### Scopus (Faz 5.4)

- **Elsevier lisanslı içerik kalıcı olarak saklanmaz.** `scopus_source` payload'ı
  yalnızca DOI + başlık + tarih + Scopus'a link taşır; `abstract` sabit `None`'dır.
  Bu bir optimizasyon değil, **lisans kısıtıdır** — "zaten API veriyor, alalım" diye
  değiştirme.
- Saklanabilir metadata `service._hydrate_scopus_payloads` üzerinden OpenAlex'ten gelir.
- **Kalan risk açıkça kayıtlı**: OpenAlex hidrasyonu tutmazsa satırda Elsevier kaynaklı
  bir başlık kalır. Gerekçe, reddedilen alternatifler ve kararın yeniden açılma koşulu:
  [ADR-0002](adr/0002-elsevier-discovery-only.md).

### Yazar takibi (Faz 5.4)

- `UserAuthor.last_work_at` bir **su seviyesi işareti**: gecelik koşu OpenAlex'ten
  yalnızca bu tarihten yeni işleri ister. Olmasaydı üretken bir yazarın tüm kariyeri her
  gece yeniden içeri alınırdı.
- Duraklatmak satırı ve işareti korur; silip yeniden eklemek ikisini de kaybettirir.
- Takip **kimlikle** yapılır (ORCID / OpenAlex id), isimle değil: OpenAlex'te binlerce
  "J. Smith" var ve yanlış eşleşme akışı sessizce başkasının yayınlarıyla doldurur.

### Patentler (Faz 5.2)

- Patent tarifnamesi ve özeti çoğu yargı alanında **resmî yayındır, telif dışıdır** —
  yukarıdaki "yeniden yayımlama" sınırıyla çatışmaz. Yine de kart kaynağa link verir.
- **Prior-art araması hiçbir şey saklamaz** (`service.search_patents_live`). İki gerekçe:
  tek seferlik fikir sorguları kalıcı `papers` tablosunu kirletmemeli, ve EPO'nun
  fair-use şartları hiçbir şey saklamayan bir aramadan memnun. Yalnızca gecelik anahtar
  kelime taraması persist eder.
- **Yenilik değerlendirmesi hukuki tavsiye değildir.** Prompt modele yalnızca verilen
  listeye dayanmasını ve listede olmayan patenti anmamasını söylüyor; sayfa da sonucun
  bir patent vekiliyle doğrulanması gerektiğini açıkça yazıyor. Bu ifadeyi kaldırma —
  kullanıcı bu çıktıya dayanarak başvuru kararı verebilir.

---

## 12. Config Referansı

Tümü `.env.example`'da açıklamalı. Özet:

| Değişken | Default | Ne yapar |
|---|---|---|
| `SCRAPE_SOURCES` | tüm kaynaklar | Dağıtımın aktif kaynak listesi |
| `SEMANTIC_SCHOLAR_API_KEY`, `NCBI_API_KEY` | boş | Opsiyonel, daha yüksek kota |
| `OPENALEX_MAILTO`, `CROSSREF_MAILTO` | boş | Anahtar değil — "polite pool" için iletişim adresi |
| `SCRAPE_RATE_ARXIV_PER_MIN` / `_S2_PER_5MIN` / `_PUBMED_PER_SEC` | 20 / 100 / 3 | Dağıtım geneli bütçe |
| `SCRAPE_RATE_OPENALEX_PER_SEC` / `_CROSSREF_PER_SEC` | 8 / 5 | Aynı |
| `SCRAPE_RATE_WEB` / `_YOUTUBE` / `_GITHUB` / `_YT_CHANNEL_PER_MIN` | 30 | Aynı |
| `MAX_USER_FEEDS` | 50 | Kullanıcı başına özel besleme |
| `MAX_USER_CHANNELS` | 10 | Kullanıcı başına YouTube kanalı — **yalnızca fallback**, geçerli değer admin panelindeki `max_user_channels` sistem ayarı |
| `CHANNEL_SUMMARY_MAX_PER_RUN` | 5 | Kullanıcı başına gecelik özetlenecek video tavanı |
| `FEED_FETCH_TIMEOUT` / `FEED_FETCH_MAX_BYTES` | 15 / 5 MiB | Besleme çekme sınırları |
| `FEED_ALLOW_PRIVATE_HOSTS` | false | SSRF guard kapatma — **prod'da asla** |
| `SCAN_RUN_RETENTION_DAYS` | 30 | 0 = sonsuza dek sakla |
| `SCAN_FANOUT_WINDOW_SECONDS` | 1800 | Gecelik dağıtım penceresi |
| `LLM_PROVIDER` | `openrouter` | `openrouter` \| `ollama` \| `anthropic` |
| `SCRAPE_QUOTA_<KAYNAK>_WEEKLY` | 0 | Haftalık **istek** bütçesi. 0 = ölçümsüz |
| `SCRAPE_QUOTA_EPO_OPS_WEEKLY_BYTES` | 3 GiB | Haftalık **byte** bütçesi — EPO'nun 4 GB'lık katmanına pay bırakır (aynı anahtar başka bir araçla paylaşılıyor olabilir) |
| `SCRAPE_RATE_EPO_OPS_PER_MIN` / `_PATENTSVIEW_PER_MIN` | 10 / 45 | EPO saniyelik rakam yayınlamıyor; gerçek tavan byte bütçesi |
| `EPO_OPS_KEY` / `EPO_OPS_SECRET` | boş | OAuth2 client-credentials. İkisi birden gerekli |
| `PATENTSVIEW_API_KEY` | boş | `X-Api-Key` header |
| `SCOPUS_API_KEY` / `SCOPUS_INSTTOKEN` | boş | Faz 5.4 — kurum IP'sine bağlı, varsayılan kapalı |

> ⚠️ `SCRAPE_SOURCES`, `SEMANTIC_SCHOLAR_API_KEY` ve `NCBI_API_KEY` **`BaseConfig`'i
> atlar**, doğrudan `os.getenv` ile okunur. Testte `monkeypatch.setitem(app.config, ...)`
> işe yaramaz; `monkeypatch.setenv` kullan. Kota değişkenleri (`SCRAPE_QUOTA_*`) bunun
> **tersine** `_cfg` üzerinden `app.config`'ten okunur — testte `setitem` doğru yoldur.
>
> Anahtar gerektiren kaynakların açık/kapalı durumu env'de değil, admin panelindeki
> `patents_enabled` / `scopus_enabled` sistem ayarlarındadır (§5). Env yalnızca
> **anahtarı** taşır; anahtar yoksa kaynak zaten listelenmez.
