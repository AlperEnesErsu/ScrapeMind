# Faz 8 — Patent Takibi (yuvarlanan tam metin penceresi)

> **Durum:** 8.1 bitti ve doğrulandı, 8.2–8.6 planlandı. 12 Eylül 2026.
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

`USPTO_ODP_API_KEY` **opsiyonel** tutulur: varsa ODP API'si, yoksa key'siz dizin
indirmesi denenir. Anahtar import'ta yakalanmaz, çağrı başına okunur
(`credentials_ok()` kalıbı).

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
  description_tsv    TSVECTOR      -- GIN
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
GIN `cpc_codes`, GIN `description_tsv`, GIN `to_tsvector(patent_claim.text)`,
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
2. **Tam metin** — `description_tsv` + istem metni.
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

### 9.3 İndirme + haftalık hat + admin paneli
`discover`/`fetch`, `TASK_ROUTES`, Beat girdisi, purge, `patent_ingest_run`.

**Kabul:** bir haftalık dosya uçtan uca yüklenir; ikinci koşu yeni satır yaratmaz;
purge pencere dışını siler.

### 9.4 Arama: FTS + filtre
### 9.5 Semantik + hibrit (RRF)
### 9.6 Okuma deneyimi: istem ağacı, jargon sadeleştirme

---

## 10. Config

| Değişken | Default | Ne yapar |
|---|---|---|
| `USPTO_ODP_API_KEY` | — | **Opsiyonel.** Varsa ODP API'si, yoksa key'siz indirme |
| `PATENT_BULK_DIR` | `./data/patent_bulk` | İndirilen XML'ler (gitignore) |
| `PATENT_WINDOW_WEEKS` | `3` | Pencere genişliği; purge bunu kullanır |
| `PATENT_AI_CPC_CODES` | `G06N` | Çekirdek filtre |
| `PATENT_AI_CPC_EXTENDED` | *(boş)* | `G06V,G10L,G06F40` ile genişletilir |
| `PATENT_FTS_ONLY` | `false` | Embedding'i tamamen kapatır |

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
