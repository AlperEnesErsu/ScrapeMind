# Faz 7 — Kütüphane bir varış noktası olmaktan çıkıp iş akışının parçası oluyor

> **Durum:** 7.0 bitti (PR #62), 7.1–7.3 planlandı. 10 Eylül 2026.
> Dal başlangıcı: `main` (Faz 6 + tasarım sistemi + OA tam metin merge'lenmiş).
> Migration zinciri head'i: `d51a7c2e04b9`.

Kod yazmadan önce [SCRAPING.md](SCRAPING.md) §2 (kaynak adaptörü sözleşmesi) ve §11
(etik sınırlar) okunmalı. Arayüze dokunulacaksa ayrıca [DESIGN.md](DESIGN.md) —
özellikle kırmızı marka / kırmızı tehlike kuralı, çünkü o kural kendini renkle
koruyamıyor.

---

## 1. Neden

Faz 2–6 külliyatı kurdu: 14 kaynak, DOI tekilleştirme, dergi kalitesi, atıf grafiği,
gerçek RAG, retrospektif raporlar. Faz 7'nin başında külliyat **geniş, zengin ve
aranabilir**. Eksik olan şey veri değil, **konum**.

Ürün şu an tek bir modda çalışıyor: kullanıcı gelir ve bakar. Üç somut boşluk:

1. **"Şu çıkarsa bana söyle" diyemiyorsun.** Gecelik tarama koşuyor, digest de
   pencere içindeki *her şeyi* özetliyor — ama bu bir bülten, sorgu değil. Belirli
   bir konuyu izlemek isteyen kullanıcı her gün elle arıyor.
2. **Kütüphane çıkışsız.** BibTeX ve CSV indirilebiliyor
   ([library_routes.py](../app/modules/scrape/library_routes.py)), ama kullanıcı
   yazarken Zotero'da. Dosya indirip elle içe aktarmak, "kütüphanem" iddiasının
   bittiği yer.
3. **Ayarlar ile ürün yapılandırması aynı sayfada.** [#58](https://github.com/AlperEnesErsu/ScrapeMind/issues/58),
   birmstf. Profilde 12 sekme var; sekizi hesap, dördü ürünün çalışma yapılandırması.

Faz 7'nin tezi: **külliyat kullanıcıya gitsin, ve kullanıcının yazdığı yere aksın.**

---

## 2. Uygulama sırası

### 7.0 ✅ Açık erişim tam metni — bitti (PR #62)

`best_oa_location` üzerinden getirme, `pypdf`/`trafilatura` ile çıkarım, **lisans
kapılı** saklama. Ayrıntı ve gerekçe: [SCRAPING.md §11](SCRAPING.md).

Bu adımın 7.1'i doğrudan etkileyen bir sonucu var: **artık aranabilir gövde metni
var**, ama yalnızca lisansın saklamaya izin verdiği alt kümede. Kayıtlı arama bunun
üstünde çalışacak, yani bir aramanın kapsamı külliyatın tamamı değil.

#### Kasten yapılmayan: parçalanmış gömme

Tam metin tek bir gömmeye giriyor, ilk 20 bin karakter
(`FULLTEXT_EMBEDDING_CHARS`). Bölüm bazlı parçalama yapılmadı çünkü bir makaleyi
parçalara bölüp ortalamak onu **bulanıklaştırır** — "şu yöntemi kullanan makale"
sorgusunu kolaylaştırmaz, zorlaştırır. Ayrı bir tablo ve ayrı bir karar.

> **Yeniden açılma koşulu:** kullanıcılar "yöntem bölümünde X geçen makaleler" gibi
> bölüm-bilinçli sorgular sormaya başlarsa. O noktada `paper_chunks` tablosu +
> bölüm etiketi + parça başına gömme gerekir; tek gömme bunu asla veremez.

---

### 7.1 Kayıtlı arama + uyarı

**Boşluk:** kullanıcı bir sorguyu izleyemiyor.

**Model:** `SavedSearch(user_id, name, q, filters JSON, semantic bool, cadence,
last_notified_at, is_active)`. `filters` mevcut `search_user_papers_query`
imzasındaki her şeyi taşır (source, quartile, date aralığı, has_notes) — böylece
kayıtlı arama, kütüphane aramasının kaydedilmiş hâli olur, ikinci bir arama motoru
değil.

#### Buradaki asıl zorluk: "yeni" nedir

Digest bunu bir **zaman penceresi** ile çözüyor (`_window_bounds`). Kayıtlı arama
çözemez, çünkü bir makale sonradan eşleşir hâle gelebilir: DOI eşleşmesi boş
`abstract`'ı doldurur, ya da OA tam metni gelir ve gövdede arama terimi geçer. O
makale "yeni" değildir ama kullanıcı için **yeni görünür**.

İki seçenek ve tercih:

| | |
|---|---|
| **Zaman damgası** (`last_notified_at`'ten yeni olanlar) | Basit, ama zenginleşerek eşleşen makaleyi kaçırır — ki bu ürünün en değerli anıdır |
| **Bildirilmiş kümesi** (`saved_search_notifications` join tablosu) ✅ | Bir satır fazla, ama "bu aramayı bu makale için bildirdik" tam olarak söylenen şey |

Join tablosu seçilmeli. Zaman damgası ucuz görünüyor ama yanlış soruyu cevaplıyor:
soru "ne zaman baktık" değil, "neyi zaten söyledik".

#### İkinci zorluk: gürültü

Geniş bir sorgu gecede 200 makale eşleyebilir. Makale başına bildirim, posta
kutusunu doldurup özelliği kapattırır. Kurallar:

- Bildirim **toplu**: "kayıtlı aramanız *transformer ısı yönetimi* için 12 yeni
  makale", tek bildirim, kütüphane aramasına link.
- Koşu başına **üst sınır** (öneri: 50). Aşılırsa bildirim sayıyı söyler ama
  listelemez — ve bu, aramanın fazla geniş olduğunun kullanıcıya sinyalidir.
- `cadence`: `daily` | `weekly` | `off`. Digest'in `digest` tercihiyle **aynı
  vokabüler**, çünkü kullanıcı için aynı kavram.

#### Altyapı

`add_notification` ([notification.py](../app/core/models/notification.py)) hazır ve
uygulama içi. E-posta yolu gerekiyorsa digest'in şablon/gönderim hattı yeniden
kullanılır — ikinci bir gönderici yazılmaz.

Fan-out `app/tasks/fanout.py` desenini izler; task `alerts.run_for_all_users` →
`alerts.run_for_user`, kuyruk `io` (LLM çağrısı yok, sadece sorgu).

> ⚠️ Yeni task modülü **üç yere** yazılmalı: `app/tasks/__init__.py` import satırı,
> `TASK_ROUTES` ve `BEAT_SCHEDULE`. Biri eksikse worker task'ı hiç görmez ve hata
> da vermez (CLAUDE.md).

#### Doğrulama
- Zenginleşerek eşleşen makale bildirilir (join tablosunun var olma sebebi).
- Aynı makale iki kez bildirilmez.
- Üst sınır aşıldığında tek bildirim çıkar ve sayıyı doğru söyler.
- `cadence="off"` hiç koşmaz — LLM'siz bile boş iş yapılmaz.

---

### 7.2 Zotero'ya aktarım

**Boşluk:** kütüphane çıkışsız.

**Neden Zotero, neden Mendeley değil.** Zotero açık kaynak, Web API v3 belgelenmiş
ve kullanıcı kendi API anahtarını üretebiliyor. Mendeley Elsevier'in ve OAuth
istiyor; [ADR-0002](adr/0002-elsevier-discovery-only.md) Elsevier bağımlılığı
konusunda zaten net bir duruş almış durumda. **Mendeley bu fazın kapsamında değil**
— istenirse ayrı bir karar olarak açılır.

#### Bu, uygulamanın dışarıya ilk *yazma* işlemi

Şimdiye kadar her harici çağrı okumaydı. Zotero'ya öğe yaratmak farklı bir risk
sınıfı: başarısız bir toplu aktarım kullanıcının kendi kütüphanesinde yarım kalmış
kayıtlar bırakır. Bunun sonuçları:

- Aktarım **idempotent** olmalı. Zotero öğesinin `extra` alanına ScrapeMind paper
  id'si yazılır; tekrar aktarımda mevcut öğe güncellenir, ikinci kopya yaratılmaz.
- **Kısmi başarı raporlanır.** "40 öğeden 37'si aktarıldı, 3'ü başarısız" —
  sessizce yutulmaz.
- Aktarım **kullanıcı tetikli**, zamanlanmış değil. Kimsenin Zotero kütüphanesine
  arka planda yazılmaz.

#### Kimlik altyapısı zaten var

Faz 5.1'in `requires_key` + `credentials_ok()` kapıları ve kullanıcı bazlı şifreli
anahtar saklama mekanizması ([SCRAPING.md §5](SCRAPING.md)) bu iş için yazılmıştı.
Zotero anahtarı aynı yoldan geçer; yeni bir sır saklama yolu **açılmaz**.

#### Doğrulama
- Aynı makale iki kez aktarılınca Zotero'da tek öğe kalır.
- Anahtar yokken özellik hiç görünmez (kapı çalışıyor).
- Kısmi başarı kullanıcıya sayıyla bildirilir.

---

### 7.3 [#58](https://github.com/AlperEnesErsu/ScrapeMind/issues/58) Hesap ayarları / ürün yapılandırması ayrımı

Tam çalışma: [HANDOVER.md §5.7](HANDOVER.md). Özet:

Ayrım **kodda zaten var** — dört modül sekmesi `register_profile_tab()` ile geliyor,
sekiz hesap sekmesi `CORE_TABS`. Birleştiren tek şey arayüz.

> ⚠️ **Issue'nun gövdesi cümle ortasında bitiyor**, önerilen çözüm yazılmamış.
> Uygulamadan önce birmstf'ye sorulmalı. §5.7'deki okuma bir çıkarımdır, issue'nun
> kararı değil.

**Dikkat:** issue sidebar'ın sadeliğinden bahsediyor ama sidebar zaten sakin (6 üst
öğe + admin grubu). Karmaşa profil sayfasının *içinde*. Sidebar'a dört öğe ekleyen
bir çözüm yanlış şikâyeti cevaplar.

Bu adım en sona konuldu çünkü tek bloke olan iş bu ve teknik riski en düşük olanı.

---

## 3. Sıra neden bu

7.1 önce, çünkü 7.0'ın açtığı gövde metni aramasını **gerçekten kullanan** ilk
özellik o — tam metin arama var ama onu izleyecek bir mekanizma yok, yani şu an
kimse fark etmiyor.

7.2 sonra, çünkü dışarıya yazma bu projedeki ilk örneği ve tek başına
değerlendirilmeyi hak ediyor; başka bir işin içine gömülmemeli.

7.3 en sonda, çünkü cevap bekliyor.

---

## 4. Dokümantasyon işleri

- `SCRAPING.md` — Zotero'nun **yazma** olduğu ve idempotentlik kuralı §11'e eklenir.
  Bu, "telifli içerik yeniden yayımlanmaz" ile aynı sınıf bir davranış sözleşmesi.
- `CLAUDE.md` — sıradaki iş listesi güncellenir (madde tamamlandıkça **silinir**;
  bitmiş maddeyi listede bırakmak bu repoda iki kez oldu).
- `HANDOVER.md` — §5.7 #58 uygulanınca kapatılır, §5.8 olarak Faz 7 devir notu.
- Mendeley'in kapsam dışı bırakılması ADR'a değecek kadar önemliyse
  `docs/adr/0003-mendeley-kapsam-disi.md`.

---

## 5. Doğrulama

Her adım kendi başına test-yeşil inmeli (`git bisect` bu repoda güvenilir tutuluyor).
İnmeden önce:

```
pytest -q                          # 1226+ passed
pytest --cov=app --cov-fail-under=80
ruff check app/ tests/ scripts/
black --check app/ tests/ scripts/
python scripts/mypy_ratchet.py     # baseline yükselemez
```

Arayüze dokunulduysa ayrıca:

```
python scripts/render_pages.py ui-audit-pages
node scripts/audit_ui.mjs ui-audit-pages   # 0 ihlal, 280/320/414px'te taşma yok
```

CI ikisini de koşuyor (`lint-and-test`, `ui-audit`).

---

## 6. Devir notları

**Kayıtlı aramada zaman damgası kullanma.** Ucuz görünür ve ürünün en değerli anını
kaçırır: bir makale zenginleşerek eşleşir hâle geldiğinde. Join tablosu bir satır
fazla, doğru soruyu cevaplıyor.

**Tam metin araması külliyatın tamamını kapsamıyor ve kapsayamaz.** Lisans sınırı,
eksik değil. Kayıtlı arama sonuçları "hiç sonuç yok" derken bunun sebebi makalenin
olmaması değil, gövdesinin saklanmamış olması olabilir — kullanıcıya gösterilen
metin bunu ima etmemeli.

**Zotero yazma yönünde.** Bu projedeki her harici çağrı bugüne kadar okumaydı;
başarısız bir toplu aktarım kullanıcının kendi verisinde iz bırakır. Idempotentlik
bir optimizasyon değil, doğruluk şartı.

**#58 bloke.** Issue tamamlanmamış. Kendi okumanı issue'nun kararı sanma.

---

## 7. Kaynaklar

| | |
|---|---|
| Zotero Web API v3 | https://www.zotero.org/support/dev/web_api/v3/start |
| Zotero item types/fields | https://www.zotero.org/support/dev/web_api/v3/types_and_fields |
| OpenAlex `best_oa_location` | https://docs.openalex.org/api-entities/works/work-object#best_oa_location |
| Creative Commons lisans karşılaştırması | https://creativecommons.org/share-your-work/cclicenses/ |
