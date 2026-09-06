# ADR-0003 — Yazar isimle aranabilir, ama seçimi kullanıcı yapar

**Durum:** Kabul edildi · **Tarih:** 5 Eylül 2026
**Bağlam:** Faz 6 (grup dosyası raporu) uygulanırken verildi.

---

## Neden bu dosya var

Bu ADR **bir kararı geri alıyor**. Faz 5.4'te yazar takibi bilinçli olarak
isimle aramayı reddetmişti ve bu, koda yorum olarak yazılmıştı —
`app/modules/scrape/forms.py`, `FollowAuthorForm`:

> *"Deliberately not a free-text name search"* — OpenAlex'te binlerce "J.
> Smith" var, yanlışını seçmek feed'i bir yabancının makaleleriyle doldurur.

O gerekçe **hâlâ geçerli**. Değişen, gerekçeyi ortadan kaldıran bir karşı
önlemin eklenmiş olması. Bu dosya olmasa, kodu sonradan okuyan biri iki
çelişkili sinyal görürdü: bir yanda "isimle arama yok" diyen bir docstring,
öbür yanda isimle arayan bir endpoint. Hangisinin güncel olduğunu ve neyin
değiştiğini bilemezdi.

## Karar

**Yazar isimle aranabilir. Hangi yazar olduğuna her zaman kullanıcı karar
verir; sistem asla kendisi eşleştirmez.**

1. `openalex_source.search_authors(name, *, limit)` yalnızca bir **aday
   listesi** döndürür. Hiçbir çağrı yolu "en iyi eşleşmeyi" seçip takibe
   almaz.
2. Her aday, ayırt etmeye yarayan meta veriyle birlikte gösterilir: **kurum,
   yayın sayısı, atıf sayısı, başlıca konular**. Doğru "J. Smith"i seçmenin
   tek yolu budur; bu alanlar süs değil, kararın kendisidir.
3. Seçim, mevcut `service.follow_author(user, raw)` akışına **OpenAlex id ile**
   girer. Yani çözümleme, kota kontrolü ve isim çakışması ayrıştırması
   Faz 5.4'te yazıldığı yerde kalır, ikinci bir kopyası çıkmaz.
4. ORCID ve OpenAlex id ile doğrudan ekleme **aynen korunur**. İsim araması
   bir alternatif giriş yoludur, mevcut yolun yerine geçmez.

## Gerekçe

### Kararı geri aldıran şey: kullanım senaryosu değişti

Faz 5.4'te yazar takibi **ileriye dönük bir besleme** özelliğiydi: takip
ettiğin yazarın yeni yayınları gecelik olarak feed'ine düşer. O bağlamda
yanlış kişiyi seçmenin bedeli kalıcı ve sinsi — feed her gece biraz daha
kirlenir ve kullanıcı nedenini fark etmeyebilir. Kimliği zorunlu kılmak
doğru karardı.

Faz 6'nın grup dosyası ise **geriye dönük ve tek seferlik**: kullanıcı bir
grup adı altında birkaç yazar toplar ve onların geçmiş üretiminin bir
değerlendirmesini ister. Burada:

- Kullanıcının elinde genelde **yalnızca ad-soyad** vardır. Bunu ORCID'e
  çevirmesini istemek, özelliği kullanılamaz kılar — kaynak senaryoda
  (akademik değerlendirme hazırlığı) kişinin ORCID'ini bulmak, aramanın
  kendisinden zor olabilir.
- Yanlış seçim **anında görünür**: rapor yanlış kişinin yayınlarını
  listeler, kullanıcı hemen anlar ve grubu düzeltir. Feed'in aksine sessizce
  birikmez.
- Grup üyesi `UserAuthor.active = False` ile eklenir, yani **gecelik feed'e
  hiç girmez**. Faz 5.4'ün korumak istediği yüzey zaten dokunulmamış olur.

### Neden otomatik eşleştirme değil

En yüksek skorlu adayı otomatik almak hızlı olurdu ve reddedildi. Akademik
bir değerlendirme bağlamında yanlış kişinin yayınlarını doğruymuş gibi
sunmak, "biraz hatalı sonuç" değil, **kullanılamaz sonuç**tur — ve hatanın
kullanıcı tarafından fark edilmesi, sistemin sessizce yanlış cevabı
sunmasından sonra gelir. Belirsizliği kullanıcıya göstermek, onu gizleyip
ortalama vakada haklı çıkmaya tercih edildi.

## Kalan risk — açıkça

**Aday listesi de yanlış seçilebilir.** Meta veri gösteriyoruz ama kullanıcı
yine de yanlış profili işaretleyebilir; özellikle aynı kurumda aynı adı
taşıyan iki araştırmacı varsa. Bunu sistem çözemez.

Riski sınırlayan üç şey: seçim geri alınabilir (gruptan çıkar, yeniden ekle),
seçim **feed'i etkilemez** (üye pasif eklenir), ve rapor hangi OpenAlex
profilinden üretildiğini gösterir — yani hata teşhis edilebilir.

**İsim araması OpenAlex'in kendi eşleştirmesine güvenir.** OpenAlex'in yazar
ayrıştırması (author disambiguation) mükemmel değildir; bir profil iki kişiyi
birleştirmiş ya da bir kişiyi ikiye bölmüş olabilir. Bu bizim kontrolümüzde
değil ve raporun altında **kaynağın OpenAlex olduğu** görünür olmalıdır.

## Kararın yeniden açılma koşulu

1. **İsim araması feed'i besleyen bir yola bağlanmak istenirse** — yani grup
   üyesi varsayılan olarak `active=True` açılacaksa. O noktada Faz 5.4'ün
   orijinal gerekçesi tekrar tam güçle geçerli olur ve bu ADR yeniden
   tartışılmalıdır.
2. **Otomatik eşleştirme talebi gelirse** (ör. toplu içe aktarma) — belirsizliği
   kullanıcıya gösterme ilkesinden vazgeçmek demektir; ayrı bir karar gerektirir.
3. **OpenAlex dışında bir yazar kimlik kaynağı eklenirse** — aday listesinin
   hangi kaynaktan geldiği ve kaynaklar çelişirse ne olacağı yeniden
   tanımlanmalıdır.

## İlgili

- [PHASE5.md §5.4](../PHASE5.md) — yazar takibinin ilk hâli ve kimlik zorunluluğu
- [SCRAPING.md](../SCRAPING.md) — yazar takibi bölümü, `last_work_at` su seviyesi işareti
- [ADR-0001](0001-headless-browser-yok.md) — bu formatın kaynağı
