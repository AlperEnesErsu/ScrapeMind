# Faz 8 — Patent Takibi (yuvarlanan tam metin penceresi)

> **Durum:** 8.1–8.5 bitti ve doğrulandı (8.1–8.3b PR #98, 8.4 PR #103). 8.6 planlandı. 12 Eylül 2026.
> Dal: `feat/patent-fulltext`, taban `c7c3e3f` (main).
> Migration zinciri head'i: **`c3f9a17d40be`** (bu fazın şeması; ebeveyni
> `e7b204c9f83a`, tek head, 38 revizyon).
>
> **Kapsam:** `app/modules/patent/` + `app/tasks/patent_bulk_tasks.py`
>
> **Önce oku:** [SCRAPING.md](SCRAPING.md) §8 (kalıcılık), §9 (zamanlama), §11 (etik
> sınırlar) · [HANDOVER.md](HANDOVER.md) §4.9 (pgvector + damgalı DB tuzağı) ·
> arayüze dokunulacaksa [DESIGN.md](DESIGN.md) (kırmızı marka / kırmızı tehlike kuralı).

---

## 1. Ne yapıyoruz

Kendi nav bölmesi olan (**"Patent Takibi"**, `/patents`) bağımsız bir modül: USPTO'nun
haftalık tam metin yayınlarından **son ~3 haftayı** yerelde tutar, içeriğinde — istem
ve tarifname düzeyinde — arama yaptırır, pencereden düşeni siler.

Bu bir **korpus** değil, bir **takip** aracı. Fark önemli: "AI'da son üç haftada ne
patentlendi ve tam olarak neyi talep ediyor" sorusunu cevaplar; "2015'ten beri tüm AI
patentleri" sorusunu cevaplamaz.

### Neden bu sınır

[SCRAPING.md](SCRAPING.md) §10 madde #10 şunu kaydetmiş: *"Patent araması yalnızca
başlık+özet tarar — tarifname (claims) metni taranmıyor. Prior-art için asıl metin
orada."* Bu faz o boşluğu kapatır, ama **sabit maliyetle**.

| | 10 yıllık korpus (terk edildi) | 3 haftalık pencere |
|---|---|---|
| Doküman | 400–600 bin | ~20 bin ham → CPC filtresinden sonra ~3–5 bin |
| Disk | ~20 GB | < 1 GB |
| Vektör | ~500 bin | ~5 bin |
| ETL | resume edilebilir çok yıllık hat | haftada bir dosya + purge |
| `halfvec`, ertelenmiş HNSW inşası | zorunluydu | **gereksiz** — düz `vector` yeter |

Terk edilen tasarımın en karmaşık üç parçası (çok yıllık resume mantığı, `halfvec`
tip ayrımı, indeks inşa sırası) bu sınırla birlikte ortadan kalkıyor.

---

## 2. Mimari karar: yeni modül, yeni tablolar

Tam metin `papers` tablosuna **girmez**. Bir arXiv abstract satırı ~2 KB, bir patent
tarifnamesi 30–100 KB; yazma deseni de farklı (haftalık toplu yükleme vs. gecelik
kullanıcı taraması).

- `app/modules/patent/` kendi tablolarıyla gelir.
- `scrape` modülünün patent adaptörleri (`epo_ops_source`, `patentsview_source`)
  **olduğu gibi kalır**. Faz 5.2'nin prior-art araması canlı, dünya çapında ve
  saklamayan yol olarak yerinde durur; bu modül yerel, ABD ve derin olan yoldur.
  Kullanıcı ikisinin farkını görür.
- Bağ tek yönlü ve opsiyonel: `patent_document.paper_id → papers.id` (nullable).
  Kullanıcı bir patenti kütüphanesine alırsa `papers` satırı doğar; korpus dokümanının
  varlığı tek başına `papers`'a satır **eklemez**.
- Mimari kural 1 korunur: `app/core/` bu modülden import etmez.

---

## 3. Veri kaynağı

### 3.1 Seçim

**USPTO haftalık tam metin (verilmiş patentler), sadece ABD.**

1. **Hukuki:** ABD patent tarifnamesi ve istemleri kamu malı — §11'in "yeniden
   yayımlama" sınırıyla çatışmaz.
2. **Sözleşme:** EPO OPS bu verinin kaynağı **olamaz**. §11, EPO fair-use'u için
   prior-art aramasının hiçbir şey saklamamasını kural koymuş; toplu indirip saklamak
   o kararı bozar. EPO canlı arama yolunda kalır.
3. **Teknik:** tek dil → embedding'ler tutarlı, çeviri katmanı yok.

### 3.2 Endpoint durumu (10 Eylül 2026'da ölçüldü)

Eski `bulkdata.uspto.gov` yerine **Open Data Portal**:

| Ne | Nerede | Not |
|---|---|---|
| Ürün arama | `https://api.uspto.gov/api/v1/datasets/products/search` | Key'siz **401** (ölçüldü) |
| Doküman | `https://data.uspto.gov/apis/bulk-data/{search,product,download}` | Swagger UI |
| Dizin | `https://data.uspto.gov/bulkdata/datasets` | Site üzerinden key'siz indirme de mümkün |

> **Güncelleme (13 Eylül 2026) — anahtar ZORUNLU, anahtarsız yol yok.** İlk tasarım
> anahtarı opsiyonel tutuyordu: anahtar yoksa `bulkdata.uspto.gov` URL'si tarihten
> türetiliyordu. Sandbox dışından ölçüldü: **`bulkdata.uspto.gov`'un adres kaydı yok**
> (host emekli), ve **18 Haziran 2026'dan beri** Open Data Portal'ın kendisi
> **USPTO.gov hesabıyla (MFA zorunlu) giriş** istiyor; dataset sayfası kayıt ekranına
> yönleniyor, API anahtarsız 401 dönüyor. 18 Ağustos 2026'dan beri profilde dört ek alan
> da zorunlu, yoksa anahtar erişimi kesiliyor.
>
> Sonuç: türetilmiş URL kaldırıldı. Anahtar yoksa haftalık yükleme **gerekçesiyle
> atlanıyor** (`no_api_key`), purge çalışmaya devam ediyor; panel durumu açıkça
> söylüyor ve yükleme düğmesi (sunucu tarafında da) kapalı. ODP keşfi başarısız olursa
> artık ölü bir adrese düşülmüyor, hata doğrudan koşu kaydına yansıyor.
> Anahtar: giriş yaptıktan sonra `https://data.uspto.gov/apikey`.

Anahtar import'ta yakalanmaz, çağrı başına okunur (`credentials_ok()` kalıbı).

> `data.uspto.gov` bir Angular SPA — sunucudan metin dönmez, HTML kazımaya çalışma.

### 3.3 AI filtresi — AIPD **kullanılmıyor**

İlk taslak USPTO'nun AI Patent Dataset'ini (AIPD) birincil sinyal yapıyordu.
**Yuvarlanan pencerede bu işe yaramaz:** AIPD yıllık bir araştırma yayını ve bir yıldan
fazla gecikmeli; üç haftalık taze veriyi hiçbir zaman kapsamaz. Karar, pencere
daralınca değişti.

Dolayısıyla tek filtre **CPC kodları**:

| Küme | Kodlar | Varsayılan |
|---|---|---|
| Çekirdek | `G06N` (makine öğrenmesi / nöral ağlar) | açık |
| Genişletilmiş | `G06V` (görüntü), `G10L` (konuşma), `G06F40` (NLP) | opsiyonel |

`patent_document.ai_source` hangi kuralın etiketlediğini saklar — bir sayı raporlarken
tanımın ne olduğunu söyleyebilmek için. ("2022'de 100.000+ AI başvurusu" gibi
rakamların tamamen tanıma bağlı olmasının sebebi tam olarak bu.)

---

## 4. Şema

Üç tablo + koşu kaydı. Tarifname ayrı tabloya alınmadı — bu ölçekte (~5 bin satır)
TOAST zaten yeterli, dördüncü tablo karşılıksız karmaşıklık olurdu.

```
patent_document
  id                 PK
  paper_id           FK papers.id, nullable, ondelete SET NULL
  doc_number         TEXT UNIQUE   -- "US11234567B2"
  kind_code          TEXT          -- B1/B2
  country            TEXT          -- v1: "US"
  title, abstract, description   TEXT
  search_tsv         TSVECTOR      -- GIN; ağırlıklı: başlık A, özet B, tarifname C (8.4)
  filing_date, grant_date, priority_date   DATE
  assignees, inventors, cpc_codes          JSONB
  ai_source          TEXT          -- "cpc_core" | "cpc_extended"
  claim_count        INT
  source_file        TEXT          -- hangi haftalık dosyadan geldi
  raw_sha256         TEXT          -- yeniden parse tespiti
  ingested_at        TIMESTAMPTZ

patent_claim
  id, patent_document_id (CASCADE)
  number INT, is_independent BOOL, depends_on INT NULL, text TEXT
  text_tsv TSVECTOR   -- GIN, saklanan (8.4)
  UNIQUE (patent_document_id, number)

patent_chunk
  id, patent_document_id (CASCADE)
  kind TEXT            -- v1: yalnızca "claim1"
  ref TEXT, text TEXT
  embedding vector(1536)          -- papers.embedding ile aynı tip
  UNIQUE (patent_document_id, kind, ref)

patent_ingest_run     -- haftalık koşu kaydı; ScanRun DEĞİL (§6)
  id, source_file, status, documents_seen, documents_kept,
  started_at, finished_at, error
```

`grant_date` pencerenin kaydığı kolon. `depends_on` istem ağacının kaynağı.

**İndeksler:** `patent_document(grant_date)`, `(ai_source, grant_date)`,
GIN `cpc_codes`, GIN `search_tsv`, GIN `patent_claim.text_tsv`,
HNSW cosine `patent_chunk.embedding`. Bu ölçekte HNSW'yi migration'da yaratmak
güvenli — 5 bin vektörde inşa anlık.

---

## 5. Embedding

**Yalnızca bağımsız istem 1 gömülür.** Bir patentin hukuki kapsamı istem 1'dir;
semantik aramanın karşılığının büyük kısmı orada ve bu, mevcut hiçbir arayüzde birinci
sınıf vatandaş değil. Tarifname gömülmez, FTS'te aranır.

~5 bin vektör × 6 KB ≈ 30 MB. `embedding_service.get_embeddings_batch` olduğu gibi
yeniden kullanılır; `papers.embedding` ile aynı `vector(1536)` tipi.

`PATENT_FTS_ONLY=true` embedding'i tamamen kapatır (anahtarsız / düşük disk kurulumu).

> Faz 7.0'ın "parçalanmış gömme yapılmadı" kararıyla çelişmez: orada bir **makaleyi**
> parçalayıp ortalamak bulanıklaştırıyordu. Burada parçalama yok — tek, hukuken
> tanımlı bir birim (istem 1) gömülüyor.

---

## 6. Haftalık hat

```
patents_bulk.refresh_window()            ← Beat, haftada bir
   ├─ discover()   son yayınlanan haftalık dosyayı bul
   ├─ fetch()      indir (io kuyruğu), sha256
   ├─ parse()      XML stream (iterparse) → doküman + istem satırları
   ├─ filter()     CPC kuralı — eşleşmeyen dokümanı **hiç yazma**
   ├─ upsert()     patent_document + patent_claim
   ├─ embed()      istem 1 → vector
   └─ purge()      grant_date < now - PATENT_WINDOW_WEEKS → sil
```

Kurallar:

- **`TASK_ROUTES`'a eklenmeyen task varsayılan `celery` kuyruğundan tüketilir**
  ([SCRAPING.md](SCRAPING.md) §9). Hepsi açıkça yönlendirilir: `fetch` → `io`,
  gerisi → `celery`. Prod'da bir kez yaşanmış hata tam olarak buydu.
- **Beat saatleri Europe/Istanbul.** USPTO salı günleri yayınlıyor → **Çarşamba 04:45**;
  04:00–04:30'daki üç purge task'ının ardına denk gelir, gecelik yoğunluğun dışında.
- **`ScanRun` üretmez.** `ScanRun` kullanıcı taramasının kaydı; bu sistem işi.
  Kendi tablosu var (`patent_ingest_run`). Faz 5.2'nin `patents.ingest_for_*`
  task'larına ve `ScanRun.kind="patents"` semantiğine **dokunulmaz**.
- **Filtre yazmadan önce uygulanır.** Haftalık dosyadaki 6–8 bin patentin CPC
  eşleşmeyen ~%95'i veritabanına hiç girmez. Sonra silmek değil, baştan almamak.
- XML **stream** parse (`iterparse`) — dosya belleğe alınmaz.
- Tekilleştirme `doc_number` UNIQUE. `upsert_paper`'ın fill-only kuralı burada geçerli
  **değil**: USPTO tek yetkili kaynak, yarışan ikinci kaynak yok; yeniden parse taze
  veriyi yazar (`raw_sha256` değiştiyse). Bilinçli ayrım.
- **Purge geri döndürülemez.** Kullanıcının kütüphanesine aldığı patent `papers`
  satırı olarak yaşamaya devam eder — silinen yalnızca korpus kopyasıdır.

---

## 7. Arama

Üç yol, tek sonuç listesi:

1. **Yapılandırılmış filtre** — tarih, CPC, hak sahibi, istem sayısı. Düz SQL.
2. **Tam metin** — `search_tsv` (başlık/özet/tarifname) + istemlerin `text_tsv`'si.
3. **Semantik** — doğal dil → embedding → `patent_chunk` komşuluğu.

Filtre her zaman `WHERE`. FTS ve semantik **Reciprocal Rank Fusion** ile karıştırılır
(skorları aynı ölçekte değil; ağırlıklı toplam kalibrasyon borcu yaratır). Arayüz
`/library/search?semantic=1` toggle kalıbını izler.

---

## 8. Arayüz

- `/patents` — bölmenin girişi: bu haftanın yeni AI patentleri + pencere durumu.
- `/patents/search` — filtre paneli + sonuç listesi + semantik toggle.
- `/patents/<doc_number>` — okuma görünümü:
  - **İstem 1 en üstte, ayrı kutuda.** Kapsam orada.
  - **İstem ağacı** — `depends_on` hiyerarşisi, bağımlılar katlanır.
  - **Jargon sadeleştirme** — istem başına, istek üzerine, mevcut çok sağlayıcılı LLM
    servisiyle; prompt kaynak metne bağlı kalmaya zorlar.
  - Tarifname + kaynağa (Google Patents) link.
- `/admin/patents` — pencere durumu, son koşular, elle tetikleme.

[DESIGN.md](DESIGN.md) kuralı: elle tetikleme birincil **dolu** garnet, purge/temizle
**outline kırmızı** (asla dolu), ikisi yan yana durmaz.

Menü girdisi **ayrı migration'da** (§9.2).

**Hukuki uyarı korunur:** yenilik değerlendirmesi hukuki tavsiye değildir, sonuç bir
patent vekiliyle doğrulanmalıdır. [SCRAPING.md](SCRAPING.md) §11 bu ifadenin
kaldırılmamasını açıkça yazıyor — yeni sayfalarda da duracak.

---

## 9. Aşamalar

Her aşama tek başına test-yeşil ve commit'lenebilir (`git bisect` güvenilir kalsın).

### 9.1 ✅ Şema + modül iskeleti + parser — bitti (12 Eylül 2026)
Modeller, migration `c3f9a17d40be` (`down_revision = "e7b204c9f83a"`), CPC
sınıflandırıcı ve **ağ gerektirmeyen** XML parser + fixture testi.

**Doğrulandı:**

- Tam paket **1319 test yeşil** (migration'ın eklediği tablolar her testin
  `create_all()`'una giriyor, yani computed `tsvector` ve fonksiyonel GIN indeksi
  orada da kuruluyor).
- Migration sıfırdan kurulan geçici bir DB'de **upgrade → downgrade → upgrade**
  round-trip'ini geçti; downgrade geriye tablo da indeks de bırakmıyor (0/0).
- Şema davranışı SQL ile sınandı: generated `description_tsv` `UPDATE` sonrası
  kendini yeniliyor, `jsonb` containment çalışıyor, `CASCADE` istemleri alıyor,
  `paper_id` FK'si `SET NULL`.
- `ruff` + `black` temiz.

**Hâlâ doğrulanmadı:** parser gerçek bir haftalık USPTO dosyası görmedi — belgelenmiş
grant DTD'sine (v4.x) göre yazıldı ve elle hazırlanmış fixture'da çalışıyor. 8.3'ün
kapısı bu; alan düzeyinde sürpriz beklenir, yapısal değil.

> Dev veritabanına **uygulanmadı** (damga `e7b204c9f83a`'da bırakıldı): dal merge
> edilmeden şemayı ilerletmek, `main`'den çalışan uygulamayı kodunun tanımadığı bir
> revizyonla karşı karşıya bırakırdı.

### 9.2 Nav girdisi (ayrı migration)
`_sidebar.html` nav linklerini korumasız `url_for(item.endpoint)` ile kuruyor;
**route'suz bir menü satırı her sayfayı BuildError'a çevirir** — Faz 6'da bir kez canlı
yaşandı. Nav seed'i şemadan ayrı, route'lar hazır olduktan sonra.

### 9.3a ✅ İndirme + haftalık hat — bitti (12 Eylül 2026)
`uspto.py` (keşif + indirme), `ingest.py` (parse → filtre → upsert → purge),
`patent_bulk_tasks.py`, `TASK_ROUTES` girdileri, Beat girdileri.

**Tasarım kararları:**

- ~~**Anahtar opsiyonel, iki rota var.**~~ **Geçersiz — 13 Eylül 2026'da kaldırıldı**
  (§3.2). Anahtarsız rota `bulkdata.uspto.gov`'dan tarihe göre URL türetiyordu; o host
  emekli ve ODP giriş istiyor. Artık anahtar yoksa yükleme gerekçesiyle atlanıyor.
  Ders: bu karar gerçek servise hiç istek atılamadan verilmişti ve "varsayım" diye
  işaretliydi — ilk gerçek ağ denemesi onu çürüttü.
- **Filtre yazmadan önce.** Haftalık dosyadaki 6–8 bin patentin CPC eşleşmeyeni
  veritabanına hiç girmiyor.
- **`raw_sha256` ile değişmeyen doküman yeniden yazılmıyor.** İstemler ise
  birleştirilmiyor, **tamamen değiştiriliyor**: düzeltilmiş bir patent istemleri
  yeniden numaralandırabilir ve numaraya göre birleştirmek iki farklı sürümden
  dikilmiş bir ağaç bırakır.
- **Purge ayrı ve günlük.** İndirme bozuksa bile pencere küçülmeye devam etmeli;
  bayat korpus kabul edilebilir, sınırsız büyüyen korpus değil.
- **Redis kilidi** aynı anda iki yüklemeyi engelliyor; Redis yoksa yükleme yine
  çalışıyor (kilit best-effort).
- Beat: **Çarşamba 04:45** yükleme, **her gün 04:50** purge (Europe/Istanbul).

**Doğrulandı:** 21 yeni test, tam paket **1354 yeşil**, ruff + black temiz.
Ağ yok — modülün kendi `requests`'i monkeypatch'leniyor (`SCRAPING.md` §2 sözleşmesi).

**Hâlâ doğrulanmadı — 9.3'ün asıl kapısı:** hiçbir istek gerçek servise gitmedi.
Bu ortam `bulkdata.uspto.gov`'u çözemiyor, `data.uspto.gov` bağlantıyı resetliyor ve
ortada anahtar yok. Dolayısıyla **ODP yanıt şeması varsayım** (bu yüzden
`_extract_files` şekli sabitlemek yerine tolere ediyor) ve **parser gerçek bir
haftalık dosya görmedi**. İlk gerçek koşu bu iki şeyi birden sınayacak.

### 9.3b ✅ Admin paneli — bitti (13 Eylül 2026)
`/patents/admin`, `patents.manage` izniyle. Pencere durumu, sonraki yüklemenin ne
yapacağı (hangi rota, hangi dosya, hangi CPC filtresi), son 10 koşu ve hatası,
iki tetikleyici.

- **Sonraki dosya önceden gösteriliyor** — anahtar yoksa dosya adı tarihin saf
  fonksiyonu olduğu için tam olarak; anahtar varsa "yükleme çalışınca bulunur".
  Bir yükleme başarısız olunca yöneticinin asıl sorusu "ODP mi, türetilmiş URL mi?"
  — ikisi farklı sebeplerle bozulur ve farklı yerde düzeltilir.
- **Worker yoksa söyleniyor ve butonlar pasif.** Worker'ı olmayan kuyruğa alınmış
  bir iş, çalışan bir işle aynı görünür.
- **Tarayıcı task adı değil aksiyon adı gönderir** (`tasks_admin._TRIGGERS` kalıbı);
  task adı post etmek hiçbir şey tetiklemiyor, testli. Her tetikleme audit'e yazılıyor.
- **Panel ayrı bir menü satırı** (`required_permission="patents.manage"`), takip
  sayfasında şablon içinde koşullu bir link değil: kimin göreceğine şablonda karar
  vermek `permission_required` dışında bir izin kontrolü yazmak demekti — superuser
  bypass'ının yaşadığı ve yalnızca orada yaşaması gereken yer (CLAUDE.md kural 2).
- DESIGN.md: yükleme **dolu** garnet, temizleme **outline kırmızı**. Onay CSP-uyumlu
  `data-confirm` ile, inline script yok.

**Doğrulandı:** 8 yeni test, tam paket **1397 yeşil**. Geçici bir DB'de sıfırdan
migration + gerçek istekler: admin 200, yetkisiz kullanıcı 403, panel linki yalnızca
admin'in sidebar'ında.

> **Doğrulama tuzağı (kod değil, betik):** birden fazla kullanıcının isteklerini
> tek bir dış `app.app_context()` içinde yapmak yanlış sonuç verir — Flask açık
> app context'i istek sırasında yeniden kullanır, Flask-Login yüklenen kullanıcıyı
> `g`'ye koyar, ve ikinci kullanıcının istekleri **birincisi olarak** çalışır.
> Yetkisiz kullanıcı için sahte bir 200 bu şekilde üretildi ve ayrı context'lerle
> tekrarlanınca 403'e döndü. Elle doğrulama betiklerinde her istemciyi dış context
> olmadan kullan.

> **Dev DB'ye karşı doğrulama yapılmadı, bilerek:** worktree kodunu dev DB'ye karşı
> başlatmak manifest sync'iyle `patent.admin`'e bakan menü satırını oraya yazardı.
> `main`'den çalışan uygulamada o endpoint yok, ve korumasız `url_for` admin
> kullanıcı için her sayfayı BuildError'a çevirirdi. Bu satır, kod `main`'e girdikten
> sonra uygulama açılışında kendiliğinden gelir.

### 9.4 ✅ Arama: FTS + filtre — bitti (13 Eylül 2026)
`/patents/search`; takip sayfasında da arama kutusu var.

- **Gerçek Postgres FTS, `LIKE` değil.** Kütüphane araması `LIKE` kullanıyor ve orada
  sorun değil (başlık + özet); 50 KB'lık tarifnamede GIN indekslerini boşa çıkarır.
- **`websearch_to_tsquery`** — tırnak, `or`, `-hariç` kabul eder, bozuk girdide hata
  vermez. `to_tsquery` başıboş bir `&`'ı 500'e çevirir.
- **İstemler ayrı bir kapsam.** Tarifname bir şeyden *bahseder*, istem onu *talep
  eder*; "bu fikir alınmış mı" sorusu yalnızca ikincisine bakar. İstemde eşleşme her
  zaman üstte: `ts_rank` 32 normalizasyonuyla [0,1)'e sınırlı, istem eşleşmesi tam bir
  puan ekliyor — sıralama garantisi verinin uysal olmasına değil yapıya dayanıyor.
- **Yalnızca stopword'den oluşan sorgu "sonuç yok" gibi gösterilmiyor**, yok sayıldığı
  söyleniyor. "Sonuç yok" da pencereyle sınırlanarak yazılıyor: üç haftalık ABD
  verisindeki bir boşluk dünya hakkında bir şey söylemez.
- **Snippet önce escape, sonra `<mark>`** — sıra güvenliğin kendisi; ayrıca parser
  davranışına bağlı olmayan birim testiyle kilitli.
- **Kullanıcı metni LIKE joker karakteri olamaz** (`100%` her hak sahibini eşleştirmez),
  CPC girdisi `[A-Z0-9/]` dışını atar, geçersiz tarih filtre daraltmaz.
- **Ortak `_pagination.html` düzeltildi:** değerleri HTML-escape ediyor ama URL-encode
  etmiyordu; `R&D` araması 2. sayfada `q=R`'ye bölünüyordu. Kütüphane araması da düzeldi.

**Performans — ölçülerek iki kez yeniden tasarlandı.** 3000 doküman, ortalama 56 KB
tarifname, 60 bin istem (pencerenin gerçekçi üst sınırı), uçtan uca HTTP:

| Senaryo | İlk tasarım | Ara adım | Son |
|---|---|---|---|
| Geniş terim, her yerde | 512 ms | 4266 ms | **73 ms** |
| Geniş terim, yalnızca istemler | 403 ms | 1561 ms | **68 ms** |
| Eşleşme yok | 268 ms | 31 ms | **38 ms** |
| Özgül terim, istemler | — | 14 ms | **26 ms** |

1. *İlk tasarım:* başlık+özet vektörü sorgu anında hesaplanıyordu (tek başına 117 ms,
   sıfır eşleşmede bile) ve istem eşleşmesi satır başına korelasyonlu `EXISTS`'ti.
2. *Ara adım:* ağırlıklı saklanan `search_tsv` sıfır/özgül sorguları çözdü, ama geniş
   terimi **kötüleştirdi**. `EXPLAIN` sebebini gösterdi: terim 60 bin istemin 49 bininde
   geçtiği için planlayıcı haklı olarak indeksi bırakıp taradı — ve ifade indeksi
   taramaya hiçbir şey kazandırmadığı için her istem **yeniden tokenize edildi**
   (~1,4 s), üstelik filtre, sıralama ve sayım için ayrı ayrı. `ts_rank` masumdu (11 ms).
3. *Son:* istemlere de saklanan `text_tsv`, ve eşleşen doküman kümesi tek bir CTE'de
   (iki kez referans verilen CTE Postgres'te bir kez materialize edilir).

Migration `a8d3e6f1b2c4` (ebeveyni merge revizyonu `b1e4c7a90d2f`; `description_tsv` → `search_tsv`, istem ifade indeksi →
`text_tsv`); geçici DB'de upgrade → downgrade → upgrade, downgrade eski kolon ve iki
eski indeksi birebir geri kuruyor.

**Doğrulandı:** 27 yeni test, tam paket **1424 yeşil**.

> Sentetik veri tüm terimleri tüm dokümanlara yayan 23 kelimelik bir sözlükle üretildi
> — geniş terim senaryosu bu yüzden gerçekten en kötü durum. Gerçek korpus dağılımı
> 8.3'ün ilk gerçek yüklemesinden sonra yeniden ölçülmeli.
### 9.5 ✅ Semantik + hibrit (RRF) — bitti (13 Eylül 2026)
Arama sayfasında "Anlamca da eşleştir" anahtarı.

- **Doküman başına tek vektör, istem 1'den.** Parçalama ve ortalama yok (Faz 7.0'ın
  reddettiği şey değil): hukuken tanımlı tek bir birim bütün olarak gömülüyor.
- **Her vektör hangi modelden geldiğini taşıyor** (`patent_chunks.embedding_model`,
  migration `d2f6b8c31a57`). Farklı modellerin vektörleri arasındaki kosinüs mesafesi
  bir sayıdır, ölçüm değil; `EMBEDDING_MODEL` değişince eski vektörler hâlâ aynı
  güvenle "en yakın komşu" döndürür ve hiçbir yerde hata çıkmaz. Arama yalnızca
  geçerli modelin vektörlerini okuyor, `embed_pending` gerisini yeniden üretiyor.
  `papers` tablosunda bu koruma yok.
- **Füzyon RRF, skor harmanlama değil** — `ts_rank` ile kosinüs mesafesi ilgisiz
  ölçeklerde; ağırlıklı toplam, verinin sessizce bozacağı bir kalibrasyon ister. RRF
  yalnızca sıra kullanır (k=60).
- **Anlamsal eşleşme yoksa sayfa bunu söylüyor.** Sağlayıcı yok / çağrı başarısız /
  yalnızca tam metin ayarlı ise sonuçlar tam metinden gelir ve uyarı gösterilir —
  sözcüksel sonuçlar anlamsal diye sunulmaz.
- **Mesafe tabanı** (`PATENT_SEMANTIC_MAX_DISTANCE`, 0.70): vektör araması her zaman
  bir komşu döndürür; taban olmazsa gerçek eşleşmesi olmayan sorgu sayfayı gürültüyle
  doldurur.
- **Aday sınırı dürüstçe gösteriliyor.** Her liste ilk 100 adayla füzyona girer; geniş
  bir terimde "100 sonuç" demek "yalnızca 100 eşleşme var" diye okunurdu. Toplam tam
  metin eşleşmesi ayrıca yazılıyor.
- **Embedding faturalı iş:** `patents_bulk.embed_pending` `llm` kuyruğunda; haftalık
  yüklemenin ardından kuyruğa giriyor, ayrıca her gün 04:55 telafi koşusu (sağlayıcı
  kesintisinde o haftanın patentleri dışarıda kalmasın). Yeniden yazılan patentin eski
  vektörü siliniyor. Yükleme panelinde kapsama: "N patentin M tanesi hazır (model)".

**Performans** (3000 doküman + 1536 boyutlu vektör): vektör adayları 11–20 ms (bu
boyutta planlayıcı HNSW yerine düz taramayı seçiyor, 9 ms), hibrit arama uçtan uca
~85 ms.

**Doğrulandı:** 23 yeni test, tam paket **1462 yeşil** (%82.63). Dört kritik koruma
mutasyonla sınandı — model filtresi, yeniden yazımda vektör silme, yapısal filtrenin
vektör adaylarına uygulanması, anlamsal kullanılamadığında bayrak — her biri
kaldırıldığında ilgili test kırılıyor.

> **Testlerin kanıtlayamadığı:** test embedding'leri hash tabanlı; yalnızca aynı metin
> aynı vektörü verir. Yani "sorguyla ortak kelimesi olmayan patenti anlamca bulma"
> özelliği gerçek modelin özelliğidir ve mock'larla gösterilemez. Testler tesisatı ve
> füzyon matematiğini doğruluyor; anlam kalitesi gerçek sağlayıcıyla ilk kullanımda
> gözden geçirilmeli.
### 9.5b ✅ Patenti saklamak + prior-art bağlantısı — bitti (13 Eylül 2026)
Değerlendirmede (F2, F3) planlanıp yapılmamış olarak bulunan iki eksik.

- **"Kütüphaneye ekle"** (detay sayfası). Pencere bir patenti birkaç hafta sonra
  unutuyor; bu, kullanıcının onu tutma yolu. `patent_documents.paper_id` köprüsü ve
  purge'ün `SET NULL`'u baştan bunun için vardı, ama köprüyü dolduran kod yoktu.
- **Çift kayıt önleniyor.** Patentlerin DOI'si yok, `upsert_paper` PatentsView'in
  `US11123456`'sı ile EPO'nun/bu korpusun `US11123456B2`'sinin aynı patent olduğunu
  göremez. İki biçim de aranıyor, varsa mevcut satır yeniden kullanılıyor; numara önek
  olarak değil bütün olarak karşılaştırılıyor (`US1112345` ≠ `US11123456`).
- **Gizlenmiş patent geri geliyor.** Akıştan gizlenmiş (`dismissed_at`) bir patenti
  açıkça saklamak istemek ters talimattır ve kazanmalı; yoksa düğme görünür hiçbir şey
  yapmaz.
- **Prior-art sayfası patent aramasına bağlanıyor** — iki arama farklı soruları
  cevaplıyor (canlı/dünya/saklamayan vs yerel/ABD/derin) ve birbirinden habersizdi.
  Bağlantı yalnızca `patent.search` gerçekten kayıtlıysa çıkıyor, ve patent kaynağı
  anahtarı olmayan kurulumda da (en çok orada işe yarıyor) görünüyor.

**Doğrulandı:** 12 test; üç koruma mutasyonla sınandı.

### 9.6 Okuma deneyimi: istem ağacı, jargon sadeleştirme

---

## 10. Config

| Değişken | Default | Ne yapar |
|---|---|---|
| `USPTO_ODP_API_KEY` | — | **Zorunlu** (haftalık yükleme için). Yoksa yükleme atlanır, purge çalışır |
| `PATENT_BULK_DIR` | `./data/patent_bulk` | İndirilen XML'ler (gitignore) |
| `PATENT_WINDOW_WEEKS` | `3` | Pencere genişliği; purge bunu kullanır |
| `PATENT_AI_CPC_CODES` | `G06N` | Çekirdek filtre |
| `PATENT_AI_CPC_EXTENDED` | *(boş)* | `G06V,G10L,G06F40` ile genişletilir |
| `PATENT_FTS_ONLY` | `false` | Embedding'i tamamen kapatır |
| `PATENT_SEMANTIC_MAX_DISTANCE` | `0.70` | Semantik eşleşme için azami kosinüs mesafesi (8.5) |

Hepsi `.env.example`'a açıklamalı girer.

---

## 11. Tuzaklar (dokümanlardan devralınan)

1. **`pybabel extract`/`update` KULLANMA** — bir kez ~300 TR çeviriyi sildi. Yeni
   string'ler Babel API ile tek tek eklenir, sonra `pybabel compile`. TR/EN msgid key
   set'leri eşit olmalı, CI kontrol ediyor.
2. **`ruff`/`black`'i `migrations/` üzerinde çalıştırma.**
3. **Beat saatleri Europe/Istanbul, UTC değil.**
4. **`TASK_ROUTES`'ta adı geçmeyen task `celery` kuyruğuna düşer.**
5. **Damgalı DB yalan söyleyebilir** — [HANDOVER.md](HANDOVER.md) §4.9; şemayı damgayla
   karşılaştır, damgaya güvenme.
6. **Testlerde `flask db upgrade` çalışmaz**; migration'lar elle sınanır.
7. **Modül seviyesinde `requests`** — testler modülün kendi `requests`'ini
   monkeypatch'liyor, ortak wrapper arkasına saklama.
8. Kullanıcı URL'si alan yol yok (host sabit USPTO), bu yüzden `net_guard`/SSRF yüzeyi
   eklenmiyor — bilinçli.

---

## 12. Kapsam dışı (bilinçli)

- **10 yıllık korpus** — §1'deki maliyet tablosu. Pencere sabit maliyetli, korpus değil.
- **AIPD** — §3.3; yıllık ve gecikmeli, taze pencereye yetişmez.
- **EPO/WO/TR toplu tam metni** — §3.1'deki lisans gerekçesi.
- **Pre-grant publications (başvuru yayınları)** — v1 yalnızca verilmiş patentler.
  Pencereyi ikiye katlıyor; karşılığı ikinci aşamada değerlendirilir.
- **Prior-art aramasını persist etmek** — [SCRAPING.md](SCRAPING.md) §10 madde #8
  bilinçli karar. İhtiyaç doğarsa çözüm "otomatik kaydet" değil, açık bir
  "kütüphaneme ekle" butonu.
- **Headless browser** — [ADR-0001](adr/0001-headless-browser-yok.md) kapatmış.
