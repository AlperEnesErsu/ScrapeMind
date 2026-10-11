# Faz 9 — Öğrenme haritası (konu → kavram grafiği)

> **Durum:** 9.1 yazıldı (#127); 9.1b ile oklar **atıf akışından** geliyor (§3.2,
> §3.5); 9.3 anlatı (#128); 9.2 ile `learning_map` uçtan uca bir rapor türü ve
> sayfası erişilebilir sıralı liste (§5, §6). Sıradaki: 9.4 (canvas). 11 Ekim 2026.
> Dal başlangıcı: `main` (#118–#123 merge'lenmiş). Migration head'i: `a7c3e9d1f2b4`.

Kod yazmadan önce [SCRAPING.md](SCRAPING.md) §11 (etik sınırlar), Faz 6'nın rapor
hattı (`app/modules/scrape/report_service.py` başlık notu) ve arayüz için
[DESIGN.md](DESIGN.md) okunmalı.

---

## 1. Ne yapıyoruz

Bir araştırmacı yeni bir konuya girerken ilk sorusu "ne var?" değil, **"nereden
başlamalıyım?"**. Faz 6'nın konu raporu ilkine cevap veriyor: yıllara göre hacim,
büyüyen alt konular, en çok atıf alanlar. İkincisine cevap vermiyor.

Öğrenme haritası, bir konunun kavramlarını **ön koşul sırasına** dizen küçük bir
yönlü grafik (DAG) ve onun üzerinde adım adım gezilen kısa bir yol:

- **Düğüm** = bir kavram (ör. "Mesaj iletimi"), yanında külliyattaki makale sayısı
  ve rolü (ön koşul / çekirdek / yöntem / güncel).
- **Ok** = "B'yi anlamak için önce A". Okun yönü **hesaplanır**, LLM'e sorulmaz.
- **Adım kartı** = iki cümlelik açıklama, o kavramın külliyattaki anahtar makalesi
  (kaynağa link) ve bir kontrol sorusu.

Yeni bir rapor **türü** olarak: `Report.kind = "learning_map"`. Ayrı bir modül
değil, çünkü ihtiyaç duyduğu her şey (OpenAlex hasadı, kota, istek dışı üretim,
`partial` semantiği, rapor sayfası) Faz 6'da zaten var.

## 2. İlke: önce sayılar, sonra anlatı

Faz 6'nın kuralı burada daha da önemli. Bir LLM'e "bu konunun öğrenme yolunu çiz"
demek akıcı ama **doğrulanamaz** bir sıra üretir. Kimse yanlış bir ön koşul
okunu fark etmez; bir öğrenci fark etmeden yanlış sırayla öğrenir.

Bu yüzden iş ikiye ayrılır:

| Katman | Nereden | LLM? |
|---|---|---|
| Kavramlar, sayılar, oklar, sıra | OpenAlex `topics` + bu modülün aritmetiği | **Hayır** |
| Kavram adının sadeleştirilmesi, açıklama, kontrol sorusu | LLM, hesaplanmış grafiğin üzerine | Evet |

LLM yoksa ya da başarısız olursa harita yine çıkar: düğümler OpenAlex adlarıyla,
kartlar boş açıklamayla. Tıpkı Faz 6'daki gibi `status="partial"`.

## 3. Grafiğin hesaplanması

Girdi: konu raporuyla aynı sorgu, `works_in_range` (≤ `REPORT_MAX_WORKS` = 400
çalışma). Her çalışmanın 1–3 OpenAlex `topic`'i var (`openalex_source` bunları
zaten `categories`'e yazıyor) ve atıfladığı çalışmaların id'leri
(`PaperPayload.referenced_works`, 9.1b; aynı yanıtta geliyor, ek istek yok,
`papers`'a yazılmıyor).

### 3.1 Düğümler

En az `min_support` (öneri: 8) çalışmada geçen topic'ler. En sık **12**'si alınır,
çünkü 680 px'lik bir harita ve kısa bir öğrenme yolu için daha fazlası gürültüdür.
Sorgunun kendisiyle özdeş topic (her çalışmada geçen) düğüm değil, haritanın
başlığıdır.

### 3.2 Oklar: külliyat içi atıf akışı (9.1b)

> 9.1 okları subsumption ile çiziyordu (A, B'yi kapsıyorsa A önce). Canlı ölçüm
> (§3.5) OpenAlex topic'lerinin birbirini kapsamadığını gösterdi; kod kaldırıldı.

B'nin çalışmaları A'nın çalışmalarını atıflıyorsa B, A'nın **üzerine kurulu**,
yani önce A okunur. Yalnızca külliyatın içinde kalan atıflar sayılır:

```
akış(A→B) = #{(w, r) : w r'yi atıflıyor, r külliyatta, r ≠ w,
                       B ∈ kavramlar(w), A ∈ kavramlar(r)}

A → B   ⇔   akış(A→B) ≥ 5   ve   akış(A→B) ≥ 2 · akış(B→A)
```

Bir çalışmanın kendini atıflaması ve bir kavramın kendine akışı sayılmaz.
Külliyat dışına giden atıf yok sayılır. `min_flow` (5) ve `ratio` (2.0)
yapılandırılabilir. Üç ek kural grafiği okunur tutar:

- **Döngü kırma (açgözlü):** ikili "2 kat" kuralı A→B, B→C, C→A'yı engellemez.
  Adaylar güçlüden zayıfa sıralanır (akış ↓, ters akış ↑, sonra adlar) ve
  döngü kapatacak ok eklenmez. Böylece her zaman **en zayıf kanıt** düşer
  (yaklaşık minimum geri besleme kümesi; 12 düğümde yeterli).
- **Geçişli indirgeme:** A→B, B→C ve A→C varsa A→C silinir. Harita bir ağaç
  gibi okunur, kıl yumağı gibi değil.
- **Oksuz harita yol değildir:** düğüm sayısı yetse bile hiç ok kalmazsa sonuç
  `insufficient`, `reason="no_citation_flow"` (düğüm azsa `"too_few_concepts"`).
  Arayüz nedeni söyleyebilir.

### 3.3 Katman ve rol

- **Katman** = DAG'daki en uzun yol derinliği. Soldan sağa yerleşim bundan.
- **Rol**, sayılardan:
  - *ön koşul*: giriş derecesi 0 ve haritada en az bir kavramı kapsıyor. (İlk
    taslak "giriş derecesi 0 ve medyan yılı en eski çeyrekte" diyordu; 9.1'in
    testleri bunun tam hedeflediği kavramı kaçırdığını gösterdi: en genel kavram
    her yılda geçtiği için medyanı aralığın ortasına düşüyor.)
  - *güncel*: aralığın ikinci yarısındaki payı ilk yarıya göre en çok artan
    çeyrekte. Tanım Faz 6'nın `_growing_topics`'iyle aynı, ama o fonksiyon iki
    ek `aggregate_works` çağrısı yapıyor; burada zaten elde olan payload'ların
    yıllarından hesaplanır, ek istek yok;
  - *yöntem / çekirdek*: kalanlar, çıkış derecesine göre.

### 3.5 Canlı ölçüm: OpenAlex etiketleri yetmiyor (11 Ekim 2026)

9.1 yazıldıktan sonra fonksiyon gerçek veriyle koşuldu: konu başına 400 çalışma
(`cited_by_count` sıralı), "graph neural networks" ve "CRISPR gene editing".

| Girdi (`categories`) | Ok sayısı | Gözlem |
|---|---|---|
| `topics` (şu anki) | 1 / 0 | Topic'ler kardeş kümeler; birbirini kapsamıyor |
| `keywords` | 0 / 0 | Düz etiketler |
| `concepts` (skor ≥ 0.3) | 10 / 5 | Hiyerarşi çıkıyor ama yalnızca apaçık oklar: "Computer science → Artificial intelligence", "Biology → Genetics" |
| `concepts`, %30–50 üstü bağlam sayılınca | 0 / 1 | Genel kavramlar çıkınca orta seviye birbirini kapsamıyor |

**Sonuç:** subsumption doğru çalışıyor, ama OpenAlex etiketleri "önce bu" sinyali
taşımıyor. Kapsama ya yok ya da öğretici değil. `concepts` ayrıca OpenAlex'te
kullanımdan kalkmış durumda.

**Denenen alternatif — atıf akışı.** B'nin çalışmaları A'nın çalışmalarını
(külliyat içinde) tersine göre çok daha fazla atıflıyorsa, B A'nın üzerine
kuruludur: `A → B ⇔ akış(A→B) ≥ 5 ve akış(A→B) ≥ 2·akış(B→A)`. Aynı 400 çalışma,
düğümler yine düz `topics`:

| Konu | Külliyat içi atıf | Ok | Örnekler |
|---|---|---|---|
| graph neural networks | 864 | 16 | Neural networks → Advanced GNN (39'a 12); Domain adaptation → XAI |
| CRISPR gene editing | 691 | 18 | CRISPR → Nanoparticle drug delivery (34'e 12); CAR-T → Cancer immunotherapy (20'ye 10); Genomics & chromatin → RNA regulation (23'e 7) |

Bunlar bir öğrenme yoluna benziyor. Bedeli:

- **Döngü mümkün.** İkili "2 kat" kuralı A→B, B→C, C→A'yı engellemez.
  Subsumption'ın yapı gereği döngüsüzlüğü burada yok. En zayıf oku atan bir adım
  gerekir (ağırlıklı geri besleme kümesi; 12 düğümde açgözlü çözüm yeterli).
- **Veri.** `works_in_range` `referenced_works`'ü de istemeli (aynı istek, ek alan).
  Külliyat içi atıf sayısı küçük konularda düşük kalabilir; o zaman `insufficient`.
- **Anlam.** "B, A'ya dayanıyor" subsumption'ın "A, B'yi kapsıyor"undan daha
  doğrudan bir ön koşul iddiası. Arayüz okları "bunun üzerine kurulu" diye
  adlandırabilir.

**Karar (11 Ekim 2026): (a) atıf akışı.** Alternatifler (b) LLM'in özetlerden
çıkardığı kavramlar üzerinde subsumption (çalışma başına çağrı = pahalı; oklar
yine dolaylı bir "kapsama" iddiası) ve (c) ikisinin birleşimiydi. 9.1'in
iskeleti (katman, rol, sıra, anahtar makale, `insufficient`) aynen kaldı;
değişen yalnızca kenar kümesi. Subsumption kodu silindi, ölü kod tutulmuyor.

**9.1b uygulandıktan sonra aynı ölçüm** (`works_in_range`, 2015–2025, 400
çalışma, varsayılanlar `min_flow=5`, `ratio=2.0`):

| Konu | Atıflı çalışma | Külliyat içi atıf | Aday ok | Döngüden düşen | İndirgeme sonrası |
|---|---|---|---|---|---|
| graph neural networks | 388 / 400 | 840 | 17 | 0 | **12** |
| CRISPR gene editing | 400 / 400 | 674 | 16 | 0 | **14** |

Örnek oklar: "Domain Adaptation and Few-Shot Learning → Advanced Neural Network
Applications" (46'ya 23), "Neural Networks and Applications → Advanced Graph
Neural Networks" (39'a 12), "CRISPR and Genetic Engineering → Nanoparticle-Based
Drug Delivery" (34'e 12), "Genomics and Chromatin Dynamics → RNA regulation and
disease" (23'e 7), "CAR-T cell therapy research → Cancer Immunotherapy and
Biomarkers" (20'ye 10).

Varsayılanlar değiştirilmedi: `min_flow=3`'te GNN'de 24 aday çıkıyor ve ikisi
döngü kırmayla düşüyor (gürültü işareti); `min_flow=8`'de 10/10 ok kalıyor
ama ikinci sıradaki kavram bağları kayboluyor. 5 ikisinin arası. Eşiklerin
asıl kalibrasyonu 9.5'te.

Gözlem: iki konuda da `root` çıkmadı. Sorgu kelimesi topic'lerin %95'inde
geçmiyor; harita başlığı 9.2'de sorgunun kendisinden alınmalı.

### 3.4 Anahtar makale

Her düğüm için, o topic'i taşıyan çalışmalar arasında `cited_by_count`'u en
yüksek olan. Kullanıcının kütüphanesinde bir karşılığı varsa (DOI eşleşmesi) o
öne alınır ve "kütüphanende" diye işaretlenir.

## 4. Anlatı (LLM)

Tek çağrı, map/reduce yok: girdi metin değil, **yapılandırılmış grafik** (düğüm
adları, sayılar, oklar, her düğümün anahtar makalesinin başlık + özeti). Çıktı
şeması:

```json
{"nodes": [{"id": "T10123", "label": "Mesaj iletimi",
            "explain": "≤ 2 cümle", "check_question": "1 soru"}]}
```

- `ai_service.py`'ye yeni bir fonksiyon (`narrate_learning_map`), mevcut çok
  sağlayıcılı istemci ve `force_locale` kalıbıyla. Dil **raporu isteyenin** dili.
- Doğrulama: dönen `id` kümesi gönderilenle aynı değilse anlatı atılır, harita
  `partial` olur. Model ok ekleyemez, silemez; şemada ok alanı yok.
- Kontrol sorusunun cevabı saklanmaz ve gösterilmez. Amaç sınav değil, okuyucunun
  "bunu anladım mı" diye durması.

**Telif (§11):** girdi yalnızca başlık + özet, çıktı iki cümle. Tam metin
kullanılmaz; lisansı izin verse bile, çünkü kartın işi özet değil yönlendirme.

**Uygulanan hâli (9.3):**

- İmza: `narrate_learning_map(graph, *, topic, user=None) -> dict | None`. Dönüş
  **id'ye göre sözlük**, liste değil — çağıran kartı elindeki id ile arar:
  `{"nodes": {id: {"label", "explain", "check_question"}}}`.
- Girdi: düğüm başına `id, name, role, count, layer` + anahtar makalenin
  **yalnızca başlığı**. 9.1'in `key_work`'ünde özet yok; ileride gelirse
  `REPORT_ABSTRACT_CHARS`'a kırpılıp eklenir. Oklardan yalnızca `from`/`to`
  okunur, yani okların ağırlığı (subsumption → atıf akışı) değişse de fonksiyon
  değişmez.
- Dil: `flask_babel.get_locale()` — çağıranın `force_locale`'i belirler (sistem
  istemi "DAİMA Türkçe / İngilizce (English)"). Göndermediğimiz bir locale
  Türkçe'ye düşer. Mevcut rapor fonksiyonları dili sabit Türkçe tutuyor; bu
  fonksiyon ilk locale'e uyan rapor çağrısı. 9.2'nin `run_report` dalı çağrıyı
  `force_locale(<isteyenin locale'i>)` ile sarmalı — `report_tasks` şu an yalnızca
  bildirimi sarıyor.
- Kırpma: `label` 40, `explain` 360, `check_question` 200 karakter (kelime
  sınırında, "…" ile). Boş/eksik alan `""` olur, anlatıyı düşürmez.
- `None` döner (hiç çağrı yapılmadan): AI kapalı, `insufficient`, düğüm yok.
  Çağrıdan sonra: JSON nesnesi değil (bir onarım denemesinden sonra), `nodes`
  liste değil, öğe nesne değil, id eksik/fazla/tekrarlı ya da büyük-küçük harf
  farklı. Eşleme birebir; "yakın" id kabul edilmez.

## 5. Şema

Yeni tablo **yok**. `Report.stats`'e `graph` anahtarı:

```json
{"graph": {"root", "insufficient", "reason", "work_count", "citation_count",
           "nodes": [{"id", "name", "count", "layer", "order", "role", "median_year",
                      "growth", "key_work": {"title", "doi", "url", "cited_by_count",
                                             "in_library"}}],
           "edges": [{"from", "to", "flow", "reverse_flow"}],
           "params": {"min_flow": 5, "ratio": 2.0, "min_support": 8, "max_nodes": 12}}}
```

`flow` okun kanıtı (B'nin A'yı atıflayan çalışma çiftleri), `reverse_flow`
karşı yön; ikisi birlikte arayüzde "46'ya 23" diye gösterilebilir. `reason`
yalnızca `insufficient` iken dolu: `"too_few_concepts"` ya da
`"no_citation_flow"`. `citation_count` külliyat içinde kalan atıf sayısı.

`Report.sections`'a LLM çıktısı. `kind` sütunu `String(16)`;
`"learning_map"` (12 karakter) sığıyor, migration gerekmiyor. Faz 6'nın
`_RUNNABLE_KINDS` kademeli açılışı tam olarak bunun için var.

**Uygulanan hâli (9.2)** — `report_service._run_learning_map`:

```json
stats    = {"graph": <learning_map_graph çıktısı, yukarıdaki şema>,
            "query": "a OR b", "since_year": 2022, "until_year": 2026}
sections = {"narration": {"nodes": {node_id: {"label", "explain", "check_question"}}}}
           // anlatı yoksa {}
```

- OpenAlex'e tek harcama `works_in_range` hasadı (`_fetch_works`); konu
  raporunun `_build_stats` agregaları ve map/reduce **yok**. Çalışmalar konu
  raporu gibi `upsert_paper`'dan geçer (anahtar makale bağlanabilir bir satıra
  denk gelsin), kullanıcıya bağlanmaz.
- `library_dois`: sahibinin **gizlenmemiş** kütüphanesinden, yalnızca bu
  hasatta geçen DOI'ler — tek sorgu, `IN` listesi kütüphaneyle değil hasatla
  sınırlı.
- Anlatı `force_locale(recipient_locale(sahip))` içinde. `recipient_locale`
  `app/core/i18n/utils.py`'ye taşındı; `report_tasks`'ın bildirimi de onu
  kullanıyor.
- `status="partial"`: hasat kısıtlandıysa, grafik `insufficient` ise (çağrı
  yapılmaz), AI kapalıysa (çağrı yapılmaz) ya da anlatı `None` döndüyse. Harita
  her durumda saklanır. `item_count` = hasattaki çalışma sayısı. Beklenmeyen
  hata konu raporundaki gibi `error` + yeniden fırlatma.
- Başlık sorgudan: "Öğrenme haritası: <anahtar kelimeler>" (`root` canlıda hiç
  çıkmadı, §3.5).

## 6. Arayüz

- **Oluşturma:** rapor formunda üçüncü seçenek. Girdi konu raporuyla aynı
  (anahtar kelimeler + yıl aralığı); kota da aynı (`MAX_REPORTS_PER_DAY`).
- **Harita:** vis-network'ün `hierarchical` yerleşimi, soldan sağa. Atıf grafiği
  bunu zaten SRI'lı yüklüyor (#119); yeni CDN yok, CSP değişmiyor.
- **Adım kartı:** grafiğin altında; önceki/sonraki ve düğüme tıklama. Sıra =
  katmanlara göre topolojik sıra.
- **Erişilebilirlik:** canvas ekran okuyucuya bir şey söylemez. Aynı içerik bir
  **sıralı liste** olarak da basılır (her öğede ön koşulları). Canvas
  `aria-hidden`; asıl içerik liste. axe denetimine (`UI-UX/`) bu sayfa eklenir.
- **Renk:** rol kategorik bir değişken, DESIGN.md'nin CVD-güvenli kategorik
  paletinden; rol rengin yanında metin olarak da yazılır (renk tek başına
  taşımaz).

**9.2'de inen kısım:** oluşturma formu (`ReportForm.kind`, radyo grubu
`fieldset`/`legend` ile, her seçeneğin bir cümlelik açıklaması
`aria-describedby`) ve **sıralı liste** —
`reports/_learning_map_report.html`, JS'siz, inline script/style'sız. Liste
`learning_map.learning_map_steps(graph, narration)`'dan basılır (saf fonksiyon;
etiket = anlatı etiketi, yoksa OpenAlex adı; "üzerine kurulu olduğu" = gelen
okların etiketleri, okuma sırasında; anahtar makale linki yalnızca http(s) ya
da DOI). Her `<li>`: `id="lm-step-<order>"`, `data-node-id`, `data-order` —
9.4'ün canvas'ı bu işaretlemeyi değiştirmeden bağlanır. Rol rozetleri
`theme.css`'te `.lm-role--{prerequisite,core,method,current}` (mavi, çivit,
deniz mavisi, yeşil); canvas aynı dört aileyi kullanmalı. Sayfa bir cümleyle
okların AI değil atıf hesabı olduğunu ve "üzerine kurulu"nun her zaman "önce
oku" demek olmadığını söylüyor (§8). Yetersiz grafikte liste yerine `reason`'a
göre bir açıklama + "daha geniş anahtar kelime / daha uzun aralık" önerisi.
Sayfa `scripts/render_pages.py`'nin denetim listesinde
(`report-learning-map`, sentetik anlatılı bir rapor): axe 0 ihlal, 280/320/414
px'te taşma yok.

**9.4'te inen kısım:** listenin üstünde "Adım adım" bloğu — canvas + adım
kartı. **Aşamalı geliştirme:** blok `hidden` basılır, `static/js/learning_map.js`
açar; JS'siz sayfa yalnızca listedir.

- **Veri:** `learning_map.learning_map_canvas(graph, steps)` (saf) listenin
  basıldığı `steps`'ten kurulur — düğüm başına `id, label, role, layer, order`,
  ok başına `from, to, flow`; hiç ok kalmazsa `None` ve canvas da vis de
  yüklenmez. Sayfaya **tek tırnaklı `data-lm-graph` özniteliğinde `tojson`**
  ile gider: `<script type="application/json">` `test_csp_readiness`'in
  "src'siz `<script>` yok" kuralına takılır; ayrı bir JSON uç noktası ikinci
  bir sahiplik denetimi demek. `tojson` `< > & '`'yi kaçırır, yani `</script>`
  ya da tırnak içeren bir kavram adı öznitelikten çıkamaz (testli).
- **Canvas:** vis-network `hierarchical`, `LR`, seviye = `layer`, fizik kapalı,
  tekerlek sayfayı kaydırmaya devam eder (`zoomView: false`). Rol renkleri
  `.lm-role--*` ile aynı `--cat-*` token'larından `getComputedStyle` ile okunur;
  rol adı her düğüm etiketinin ikinci satırında ve ipucunda da yazar. Seçili
  düğüm **ters** çizilir (mürekkep dolgu, kalın kenar) — ton değil biçim.
  Kap `aria-hidden`; vis'in odaklanabilir çerçevesi sekme sırasından çıkarılır
  (axe `aria-hidden-focus`).
- **Adım kartı:** bir `<li>`'nin kendi çocuklarının kopyası, yani listede
  olmayan hiçbir şeyi söyleyemez. Önceki/Sonraki gerçek `<button>`'lar,
  "Adım n / N" `aria-live="polite"`; düğüme ya da liste öğesine tıklama aynı
  `select()`'e gider; seçili öğe `aria-current="step"` + marka kenar çizgisi.
  Uçtaki düğme devre dışı kalınca odak diğerine geçer; düğmelerde ←/→ da çalışır.
  `prefers-reduced-motion`'da animasyon yok.
- **vis yükleyici ortak:** SRI'lı URL'ler `citation_graph.js`'ten
  `static/js/vis_loader.js`'e taşındı (`window.ScrapeMindVis.load()`); atıf
  grafiği de onu kullanıyor.
- **Denetim:** `render_pages.py` artık modül blueprint'lerinin statik
  yollarını da göreli yapıyor — önceden `/papers/static/...` `file://`'da 404
  veriyordu ve denetim yalnızca JS'siz hâli görüyordu. Kart ve canvas açıkken
  axe 0 ihlal, 280/320/414 px'te taşma yok. CI'ın `ui-audit` işi uygulamanın
  JS dosyalarını `node --check`'ten geçiriyor.
- **Bilinçli eksik:** dar ekranda harita küçülerek sığar ve etiketler küçük
  kalır (yakınlaştırma kapalı); orada asıl yol kart + liste.

## 7. Aşamalar

| | İş | Bitti sayılır |
|---|---|---|
| 9.1 ✅ | `learning_map_graph(payloads)` — saf fonksiyon, IO yok (#127) | 42 test, %97 kapsam. Canlı ölçüm §3.5 |
| 9.1b ✅ | Oklar atıf akışından; `PaperPayload.referenced_works`; açgözlü döngü kırma | 50 test (25'i rastgele külliyat) + 6 adaptör testi, %99 kapsam; canlı: GNN 12, CRISPR 14 ok |
| 9.2 ✅ | `run_report` dalı + `stats.graph` + anlatı bağlantısı; form seçeneği; erişilebilir sıralı liste | `kind` `VALID_KINDS` + `_RUNNABLE_KINDS`'ta; anlatısız `partial` harita; anlatı sahibinin locale'inde. `tests/modules/test_learning_map_report.py` (31 test); sayfa axe denetiminde |
| 9.3 ✅ | `narrate_learning_map` + doğrulama | Sahte sağlayıcıyla şema / id kümesi testleri; locale testi. `tests/modules/test_learning_map_narration.py`; 9.2 ile `run_report`'a bağlandı |
| 9.4 ✅ | Sayfa: graf (canvas, `aria-hidden`) + adım kartı, 9.2'nin listesinin üstüne | `learning_map_canvas` + `learning_map.js` + ortak `vis_loader.js`; `data-lm-graph` özniteliği (§6). 10 yeni test (kaçış, gizli kart, ok yoksa canvas yok, `node --check`); kart açıkken axe 0 ihlal, 280/320/414 px taşma yok |
| 9.5 ✅ | Gerçek konularla kalibrasyon | **(a) Bağlantısız kavramlar** (#134) okuma yolundan çıkar, `graph.unconnected`'da raporlanır. **(b) Anahtar makale** önce kütüphaneden, sonra **külliyat içi atıfa** göre seçilir; global `cited_by_count` yalnızca eşitliği bozar. Neden: CRISPR'da OpenAlex'in birleşik kaydı (2019 cannabis makalesi, 4.224 atıf) seçiliyordu; şimdi Komor 2016 baz düzenleme (külliyatta 23 atıf). **(c) Eşikler** 6 konuda (CRISPR, GNN, LLM, perovskit, mikrobiyom, RL robotik; 2016–2026, 400 çalışma) `min_flow`∈{3,5,8} × `ratio`∈{1.5,2,3} tarandı: varsayılan **5 / 2.0** altısında da harita çiziyor (8–11 düğüm, 7–13 ok); 8 / 3.0 CRISPR'da yetersiz kalıyor, 3 / x gürültüyü artırıyor. Varsayılanlar korundu. **Açık:** AI açıklamaları gerçek bir modelle henüz denenmedi (yerelde LLM anahtarı yok) |

9.1 tek başına değerli ve risksiz: hiçbir yere bağlanmadan test edilebilir.

## 8. Riskler ve açık sorular

- **Topic tanecikliği.** OpenAlex topic'leri (~4.500) bazı alanlarda kaba olabilir:
  "Graph neural networks" sorgusunda düğümlerin yarısı aynı topic'e düşebilir. Plan
  B: çalışmaların `keywords` alanı (daha ince) aynı hesapla. 9.5'te karar.
- **Küçük külliyat.** Az çalışmada külliyat içi atıf da az kalır (400'de
  ~700–850). `min_support` düğüm sayısını düşürür; 4'ten az düğüm ya da hiç ok
  kalmazsa harita yerine "bu konu için yeterli veri yok" denir (`reason`).
  Yanlış bir harita göstermekten iyidir.
- **Sıra ≠ pedagoji.** Atıf akışı "B, A'nın üzerine kurulu"yu bulur, "önce
  öğrenilmeli"yi değil; çoğu zaman örtüşüyor, her zaman değil. Arayüz okları
  "önce bunu oku" diye değil **"bunun üzerine kurulu"** diye adlandırmalı ve bu
  sınır sayfada bir cümleyle söylenmeli.
- **Maliyet.** Rapor başına bir LLM çağrısı (Faz 6'nın map/reduce'undan ucuz).

## 9. Kapsam dışı (bilinçli)

- Kullanıcının haritayı elle düzenlemesi. Önce hesaplanan haritanın işe yaradığını
  görmek gerek.
- Makale başına mini harita. Konuşuldu; konu haritası çalışırsa aynı fonksiyon
  bir makalenin atıf komşuluğuna uygulanabilir.
- İlerleme takibi ("bu adımı tamamladım"). Durum tutar, ayrı bir tasarım ister.
