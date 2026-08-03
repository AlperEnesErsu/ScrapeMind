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
| `openai_blog`, `google_ai_blog`, `deepmind_blog`, `huggingface_blog` | besleme | Hayır | Küratörlü, **global** yutulur |
| `youtube_reach` | besleme | Hayır | `yt-dlp` CLI/Python modülü (`sys.executable`), `kind="video"` |
| `github_reach` | besleme | Hayır (`gh` CLI opsiyonel) | `gh` CLI repo araması (`FileNotFoundError` korumalı), `kind="github"` |
| `web_reach` | besleme | Hayır | ScrapeMind `requests` + `net_guard` SSRF koruması, `kind="news"` |
| `user_feed` | besleme | Hayır | Kullanıcının eklediği özel RSS |

> ⚠️ `rss_source` sözleşmeyi **bilerek** kısmen uygular: `SOURCE_NAME` ve `search()`
> yoktur, `search_for_keywords` sabit `[]` döner. Beslemeler anahtar kelime aramasıyla
> değil, `feed_tasks.ingest_all` ile global olarak yutulur — `[]` dönüşü kaynak
> döngüsü oraya uğrarsa güvenli kalsın diye vardır.

Dağıtım `SCRAPE_SOURCES` env değişkeniyle listeyi kısabilir
(`SCRAPE_SOURCES=arxiv` gibi). Tanınmayan ad `scrape_source_unknown` olarak loglanıp atılır.

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

`service.effective_source_prefs(user)` üç kademeli çözer:

1. Açık `UserSource` satırı varsa **o kazanır**.
2. Satır yok + `category == "academic"` → **açık** (geniş kataloglar herkese uygun).
3. Satır yok + `category == "feed"` → kullanıcının sınıflandırılmış konularıyla
   kaynağın `topics`'i kesişiyorsa açık, yoksa kapalı.

**Satırın yokluğu "açık" demek** — bu sayede yeni bir kaynak eklendiğinde mevcut
kullanıcılar etkilenmez ve migration'da satır üretmek gerekmez.

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

`Paper` tekilleştirmesi **yalnızca** `UniqueConstraint("source", "external_id")`.
`service.upsert_paper` eşleşen satırı bulursa **dokunmadan** döndürür.

İki sonucu var, ikisi de bilinçli ama ikisi de sınır:
- Aynı makale hem arXiv hem Semantic Scholar'da varsa **iki ayrı `Paper` satırı** olur.
- arXiv'den abstract'sız gelen bir kayıt, başka kaynak abstract'ı taşısa bile
  sonsuza dek abstract'sız kalır.

DOI tabanlı tekilleştirme ve boş-alan zenginleştirmesi yeni kaynak eklemeden önce
yapılmalı — bkz. §10.

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
| 03:15 | `scrape.run_for_all_users` |
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
| 1 | **DOI tekilleştirmesi yok** | Yeni akademik kaynak eklemeden önce şart; yoksa aynı makale N satır olur |
| 2 | **`upsert_paper` zenginleştirme yapmıyor** | Boş abstract başka kaynaktan dolmuyor |
| 3 | **Conditional GET ölü kod** | Her gece her besleme tam indiriliyor (§7) |
| 4 | `Paper.url` / `pdf_url` `String(512)` | `external_id` genişletildi ama bunlar değil; uzun RSS permalink'i taşabilir |
| 5 | RSS'siz site scrape'i yok | Lab/enstitü haber sayfaları erişilemez |
| 6 | `ask_paper` "RAG" değil | Başlık+abstract prompt'a doldurulıyor; pgvector yok |

Detaylı yol haritası ve gerekçeler: [HANDOVER.md](HANDOVER.md).

---

## 11. Etik Sınırlar

README'deki taahhüt bu katmanın davranış sözleşmesidir:

- Hedef sitelerin `robots.txt`'ine uyulur *(genel sayfa scrape'i eklendiğinde
  zorunlu — şu an yalnızca RSS çekildiği için devrede değil)*
- Rate limit + dağıtık zamanlama ile hedef sunucular yorulmaz
- Mümkün olan her yerde **resmî API** tercih edilir
- Telifli içerik **yeniden yayımlanmaz** — yalnızca özet + kaynağa geri link
- X/Twitter kazınmaz: ücretsiz okuma API'si yok ve kazıma ToS ihlali olur

---

## 12. Config Referansı

Tümü `.env.example`'da açıklamalı. Özet:

| Değişken | Default | Ne yapar |
|---|---|---|
| `SCRAPE_SOURCES` | tüm 7 kaynak | Dağıtımın aktif kaynak listesi |
| `SEMANTIC_SCHOLAR_API_KEY`, `NCBI_API_KEY` | boş | Opsiyonel, daha yüksek kota |
| `SCRAPE_RATE_ARXIV_PER_MIN` / `_S2_PER_5MIN` / `_PUBMED_PER_SEC` | 20 / 100 / 3 | Dağıtım geneli bütçe |
| `MAX_USER_FEEDS` | 50 | Kullanıcı başına özel besleme |
| `FEED_FETCH_TIMEOUT` / `FEED_FETCH_MAX_BYTES` | 15 / 5 MiB | Besleme çekme sınırları |
| `FEED_ALLOW_PRIVATE_HOSTS` | false | SSRF guard kapatma — **prod'da asla** |
| `SCAN_RUN_RETENTION_DAYS` | 30 | 0 = sonsuza dek sakla |
| `SCAN_FANOUT_WINDOW_SECONDS` | 1800 | Gecelik dağıtım penceresi |
| `LLM_PROVIDER` | `openrouter` | `openrouter` \| `ollama` \| `anthropic` |

> ⚠️ `SCRAPE_SOURCES`, `SEMANTIC_SCHOLAR_API_KEY` ve `NCBI_API_KEY` **`BaseConfig`'i
> atlar**, doğrudan `os.getenv` ile okunur. Testte `monkeypatch.setitem(app.config, ...)`
> işe yaramaz; `monkeypatch.setenv` kullan.
