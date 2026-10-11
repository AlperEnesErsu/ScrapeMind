# Faz 9 — Öğrenme haritası (konu → kavram grafiği)

> **Durum:** tasarım, kod yok. 11 Ekim 2026.
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
zaten `categories`'e yazıyor).

### 3.1 Düğümler

En az `min_support` (öneri: 8) çalışmada geçen topic'ler. En sık **12**'si alınır,
çünkü 680 px'lik bir harita ve kısa bir öğrenme yolu için daha fazlası gürültüdür.
Sorgunun kendisiyle özdeş topic (her çalışmada geçen) düğüm değil, haritanın
başlığıdır.

### 3.2 Oklar: subsumption

Klasik ve açıklanabilir bir yöntem (Sanderson & Croft, 1999): A kavramı B'yi
**kapsıyorsa** A daha geneldir ve ondan önce gelir.

```
A → B   ⇔   P(A | B) ≥ 0.8   ve   P(B | A) < 1
```

`P(A | B)` = B'nin geçtiği çalışmaların yüzde kaçında A da geçiyor. Eşik
yapılandırılabilir. İki ek kural grafiği okunur tutar:

- **Geçişli indirgeme:** A→B, B→C ve A→C varsa A→C silinir. Harita bir ağaç
  gibi okunur, kıl yumağı gibi değil.
- **Döngü yok:** eşitlik durumunda (karşılıklı kapsama) ok çizilmez; iki kavram
  aynı katmana düşer.

### 3.3 Katman ve rol

- **Katman** = DAG'daki en uzun yol derinliği. Soldan sağa yerleşim bundan.
- **Rol**, sayılardan:
  - *ön koşul*: giriş derecesi 0 ve medyan yayın yılı en eski çeyrekte;
  - *güncel*: aralığın ikinci yarısındaki payı ilk yarıya göre en çok artan
    çeyrekte. Tanım Faz 6'nın `_growing_topics`'iyle aynı, ama o fonksiyon iki
    ek `aggregate_works` çağrısı yapıyor; burada zaten elde olan payload'ların
    yıllarından hesaplanır, ek istek yok;
  - *yöntem / çekirdek*: kalanlar, çıkış derecesine göre.

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

## 5. Şema

Yeni tablo **yok**. `Report.stats`'e `graph` anahtarı:

```json
{"graph": {"nodes": [{"id", "name", "count", "layer", "role",
                      "median_year", "key_work": {"title", "doi", "url", "in_library"}}],
           "edges": [{"from", "to", "p_a_given_b"}],
           "params": {"min_support": 8, "threshold": 0.8, "max_nodes": 12}}}
```

`Report.sections`'a LLM çıktısı. `kind` sütunu `String(16)`;
`"learning_map"` (12 karakter) sığıyor, migration gerekmiyor. Faz 6'nın
`_RUNNABLE_KINDS` kademeli açılışı tam olarak bunun için var.

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

## 7. Aşamalar

| | İş | Bitti sayılır |
|---|---|---|
| 9.1 | `learning_map_graph(payloads)` — saf fonksiyon, IO yok | Sabit girdilerle deterministik; subsumption, geçişli indirgeme, döngüsüzlük testleri |
| 9.2 | `run_report` dalı + `stats.graph`, LLM'siz | `kind` `VALID_KINDS` + `_RUNNABLE_KINDS`'ta; anlatısız `partial` harita |
| 9.3 | `narrate_learning_map` + doğrulama | Sahte sağlayıcıyla şema / id kümesi testleri; locale testi |
| 9.4 | Sayfa: graf + liste + adım kartı | Tarayıcıda; axe 0 ihlal; 320 px'te taşma yok |
| 9.5 | Gerçek konularla kalibrasyon | 3 konuda eşik ve `max_nodes` elle değerlendirilir, karar buraya yazılır |

9.1 tek başına değerli ve risksiz: hiçbir yere bağlanmadan test edilebilir.

## 8. Riskler ve açık sorular

- **Topic tanecikliği.** OpenAlex topic'leri (~4.500) bazı alanlarda kaba olabilir:
  "Graph neural networks" sorgusunda düğümlerin yarısı aynı topic'e düşebilir. Plan
  B: çalışmaların `keywords` alanı (daha ince) aynı hesapla. 9.5'te karar.
- **Küçük külliyat.** 400'den az çalışmada `P(A|B)` gürültülü. `min_support`
  düğüm sayısını düşürür; 4'ten az düğüm kalırsa harita yerine "bu konu için
  yeterli veri yok" denir. Yanlış bir harita göstermekten iyidir.
- **Sıra ≠ pedagoji.** Subsumption "daha genel"i bulur, "önce öğrenilmeli"yi değil.
  Çoğu zaman örtüşüyor, her zaman değil. Arayüz okları "önce bunu oku" diye değil
  **"bunu kapsar"** diye adlandırmalı ve bu sınır sayfada bir cümleyle söylenmeli.
- **Maliyet.** Rapor başına bir LLM çağrısı (Faz 6'nın map/reduce'undan ucuz).

## 9. Kapsam dışı (bilinçli)

- Kullanıcının haritayı elle düzenlemesi. Önce hesaplanan haritanın işe yaradığını
  görmek gerek.
- Makale başına mini harita. Konuşuldu; konu haritası çalışırsa aynı fonksiyon
  bir makalenin atıf komşuluğuna uygulanabilir.
- İlerleme takibi ("bu adımı tamamladım"). Durum tutar, ayrı bir tasarım ister.
