# ADR-0002 — Scopus yalnızca "keşif" için, varsayılan kapalı

**Durum:** Kabul edildi · **Tarih:** 12 Ağustos 2026
**Bağlam:** Faz 5.4 uygulanırken verildi ([PHASE5.md](../PHASE5.md) §5.4).

---

## Neden bu dosya var

Bu repo halka açık ve bu karar bir **lisans** kararı. Kodun neden böyle
yazıldığı — özellikle `scopus_source._to_payload`'daki `abstract=None` satırının
neden bir optimizasyon değil bir kısıt olduğu — dosyaya bakan birine belli
olmuyor. Birinin iyi niyetle "abstract'ı da alalım, zaten API veriyor" demesi
işten değil. Bu ADR o değişikliğin neden yapılmaması gerektiğini ve hangi
koşulda yeniden tartışılabileceğini yazıyor.

## Karar

**Scopus, ScrapeMind'da yalnızca bir keşif kaynağıdır.**

1. Elsevier lisanslı hiçbir içerik **kalıcı olarak saklanmaz**. Payload yalnızca
   DOI + başlık + tarih + Scopus'a geri link taşır. `abstract` **sabit
   `None`** — "genelde boş" değil, "kırpılmış" değil, `None`.
2. Saklanabilir metadata **OpenAlex'ten** gelir: `service._hydrate_scopus_payloads`
   her DOI için `openalex_source.fetch_by_doi` çağırır ve mevcut DOI-first
   `upsert_paper` ikisini birleştirir. Yani Scopus "ne var" der, OpenAlex "ne
   sakladığımızı" verir.
3. Kaynak **varsayılan kapalıdır** ve iki kapıdan geçer (Faz 5.1):
   `SCOPUS_API_KEY` yoksa hiç listelenmez, admin `scopus_enabled`'ı açmadıkça
   her kullanıcı için kapalıdır.
4. Haftalık 20.000 istek kotasının altında, **fail-closed** bir sayaçla
   ölçülür.

## Gerekçe

### Elsevier sözleşmesinin iki maddesi

- **Kalıcı kopya yasağı.** Sözleşme, içeriğin kalıcı saklanmasını yasaklıyor ve
  sözleşme biterse tüm kopyaların silinmesini şart koşuyor. ScrapeMind'in tüm
  mimarisi kalıcı bir `papers` tablosu — yani abstract saklamak doğrudan
  çatışma demek.
- **Rekabet eden türev servis yasağı.** Elsevier ürünleriyle rekabet eden bir
  türev servis üretmek yasak. Çok kullanıcılı bir keşif akışı bu tarife
  yaklaşabilir; içeriği saklamamak aradaki mesafeyi koruyan şey.

### Neden tamamen dışarıda bırakmadık

Scopus, açık kataloğun kaçırdığı işleri — özellikle İngilizce dışı yayıncılıkta
— indeksliyor. Kurum aboneliği olan bir kullanıcı için bu gerçek bir kazanç.
Kaynağı hiç eklememek, aboneliği olan kurumlardan bu değeri sebepsiz esirgemek
olurdu.

### Neden anahtar tek başına yetmiyor

Scopus anahtarı **kurum IP aralığına bağlı**. Anahtarı olan ama o ağda olmayan
bir kurulum, geçerli anahtarla bile anlamlı sonuç alamaz (`SCOPUS_INSTTOKEN`
kampüs dışı erişim için var). Bu yüzden 401/403 ayrı loglanıyor: "anahtar
geçerli ama ağ yanlış" buradaki en kafa karıştırıcı hata.

## Reddedilen alternatifler

| Alternatif | Neden reddedildi |
|---|---|
| **Scopus'u hiç eklememek** | Abonelikli kurumlar için gerçek bir kayıp; §"neden tamamen dışarıda bırakmadık" |
| **Abstract'ı da saklamak** | Sözleşmenin kalıcı kopya maddesiyle doğrudan çatışıyor |
| **Abstract'ı saklamayıp gösterimde canlı çekmek** | Her kart render'ında Scopus isteği — kota ve gecikme bir yana, "kalıcı olmayan kopya" ayrımı hukuken güvenilir değil |
| **Yalnızca DOI saklayıp başlığı da OpenAlex'ten beklemek** | OpenAlex'te olmayan DOI için kart tamamen boş kalır; kullanıcı neyi kaçırdığını göremez |

## Kalan risk — açıkça

**Başlık saklanıyor.** OpenAlex hidrasyonu tutmazsa satırda Elsevier
kaynaklı bir başlık kalır. Bu teknik olarak API'den gelen içeriktir.

Riski üç şey sınırlıyor: yalnızca başlık (özet/tam metin yok), kaynağa link
veriliyor, ve kaynak varsayılan kapalı — yani hiçbir kurulum bu satırı istemeden
üretmiyor. Yine de bunun **sıfır risk olmadığını** burada yazıyoruz, çünkü
"farkında değildik" savunması halka açık bir repoda geçerli değil.

## Kararın yeniden açılma koşulu

Şunlardan biri olursa bu ADR yeniden değerlendirilir:

1. **Elsevier lisans şartları değişirse** — kalıcı saklamaya izin veren bir
   kurumsal sözleşme, 2. maddedeki hidrasyon mekanizmasını gereksiz kılar.
2. **ScrapeMind ticarileşirse** — "rekabet eden türev servis" maddesi o noktada
   çok daha yakın bir risk hâline gelir ve mevcut tasarım yeterli olmayabilir.
   (Aynı koşul Scimago'nun CC BY-NC verisi için de geçerli, bkz.
   [SCRAPING.md](../SCRAPING.md) §11.)
3. **Hukuki görüş alınırsa** — bu ADR mühendislik muhakemesidir, hukuki görüş
   değildir. Kurumsal bir dağıtım için avukat görüşü alınırsa sonucu buraya
   yazılmalı.

## İlgili

- [PHASE5.md §2](../PHASE5.md) — WoS/Elsevier araştırması ve neden ana yol olmadığı
- [SCRAPING.md §11](../SCRAPING.md) — etik sınırlar, Scimago lisansı
- [ADR-0001](0001-headless-browser-yok.md) — bu formatın kaynağı
