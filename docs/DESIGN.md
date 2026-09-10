# Tasarım sistemi

Bu sayfa `app/core/static/css/theme.css`'in **neden** öyle olduğunu anlatır. Ne
olduğunu dosyanın kendisi zaten söylüyor; buradaki şey, oraya bakınca
anlaşılmayan kararlar ve onları koruyan denetimler.

## İşaret

Elle yazılan tek kopya `app/core/static/img/logo.svg`. Geri kalan üçü ondan
türetilir:

```bash
venv/Scripts/python.exe scripts/render_favicon.py
```

Bu komut `core/_logo.html` (topbar'a inline giren Jinja partial'ı),
`favicon.svg` (marka rengi gömülü) ve iki PNG üretir. **İşareti değiştirdiysen
bu komutu çalıştır** — dördü elle tutulsaydı ilk oynatmada birbirlerinden
kopardı.

Partial'ın inline olması zorunlu: `<img>` ile yüklenen SVG kendi belgesidir ve
sayfanın `currentColor`'ını göremez, dolayısıyla işaret siyah çıkar.

## Palet

Nar kabuğundan türer. `--brand-*` marka, `--neutral-*` ona doğru eğik gri,
dördüncü aile semantik roller.

Metin olarak kullanılan her adım `--neutral-50` zemininde WCAG AA'yı geçer ve
ölçülen oran token'ın yanındaki yorumda yazar. **O yorumlar süs değil** —
`test_documented_contrast_ratios_are_true` onları yeniden hesaplayıp
karşılaştırır, yani yanlış bir yorum testi düşürür.

### Kırmızı marka / kırmızı tehlike

Marka kırmızı, tehlike de kırmızı; aralarında 17° var. Bu, "Kaydet" ile "Sil"i
bir bakışta ayırmaya yetmiyor. AA'yı geçip 38°'deki `--warning` amberine de
çarpmayan daha kırmızı bir tehlike rengi arandı — o dilimin tamamı tarandı,
sadece kahverengiler döndü. Yani **renk bu sorunu çözemiyor.**

Çözüm forma bağlı:

- Birincil eylem **dolu** garnet buton.
- Yıkıcı eylem **outline** kırmızı, asla dolu değil.
- İkincil eylem **nötr** outline — garnet outline, kırmızı outline'la yarışıp
  yıkıcı olanı gizlediği için bilerek markasız.
- Dolu bir primary ile dolu bir danger yan yana durmaz.

`test_brand_and_danger_stay_separable_by_form_not_hue` bu varyantların
kalktığını fark eder.

## Tipografi

IBM Plex Sans (arayüz) + IBM Plex Mono (DOI, crontab, görev adı). Plex teknik
dokümantasyon için çizildi ve Türkçenin ihtiyaç duyduğu Latin Extended setini
tam taşıyor.

Dokuz adımlık ölçek: `--text-2xs` (11px) … `--text-3xl` (32px). **11px taban** —
altındaki her değer karar değil, birikmiş bir kazaydı.

İkon boyutları ayrı (`--icon-dot`, `--icon-xl`): boş durum çizimi ve durum
noktası metin değil, onları okuma ölçeğine sokmak 7px ve 36px literallerini
üreten hatanın kendisiydi.

`font-size: <sayı>` yazma — ne CSS'te ne şablonda. İki test bunu tutuyor. Tek
istisna bildirimi rozetindeki `0.5em`, ki o bilerek ebeveynine göreli.

## Kategorik renkler

Altı ton artı bir nötr: `--cat-{blue,teal,green,orange,indigo,magenta,slate}`,
her biri `-tint` (zemin) ve `-ink` (metin) çiftiyle.

Tonlar göz kararı değil: normal görüş **ve** simüle edilmiş döteranopi ile
protanopi altında en küçük çift mesafesi maksimize edilerek seçildi. En kötü
çift 41 birim. Altıda durmasının sebebi, dokuzda hiçbir dizilimin ayrılabilir
kalmaması. Her ton markadan en az 30° uzak, ki bir kaynak rozeti marka kromu
gibi okunmasın.

**14 kaynak, 7 renk.** Renk on dört ayrımı taşıyamaz ve rozet zaten kaynak adını
yazıyor. Aileye göre gruplanmış — turuncu "patent" demek. Yeni bir kaynak
eklerken `theme.css`'te ona bir aile ver; kural yoksa rozet stilsiz çıkar ve bu
sessizce olur (altı kaynak uzun süre böyle kaldı).

### Quartile sıralıdır, kategorik değil

Q1–Q4 bir sıralama, o yüzden tek tonda **sıralı rampa**: doygunluk düşer,
parlaklık artar. Tint parlaklığının monoton artması, sıranın gri tonda ve renk
körlüğünde hayatta kalmasını sağlayan şey.
`test_quartile_ramp_stays_ordinal` bunu ölçer.

## Denetimler

### Otomatik (her koşuda)

`tests/core/test_design_system.py` — 8 test, tarayıcı gerektirmez, CI'da
kendiliğinden çalışır. Her biri bu repoda **en az bir kez gerçekleşmiş** bir
bozulmayı yakalar. Hepsi mutasyonla doğrulandı: kuralı bozunca gerçekten
düşüyorlar.

### Elle (tarayıcı gerektirir)

Şunlar Playwright ve ayakta bir uygulama istiyor, script'leri de bu repoda
değil komşu `UI-UX/` klasöründe — o yüzden CI'a bağlı değiller:

```bash
# Sayfaları statik dosyaya bas (giriş yapmış hâlleriyle), sonra:
cd ../UI-UX
node scripts/verify_responsive.mjs <klasör>   # 280/320/414px yatay taşma
node scripts/verify_states.mjs <dosya>        # default/hover/focus kontrastı
node scripts/axe_audit.mjs <dosya>            # WCAG 2.2 A/AA
```

Büyük bir arayüz değişikliğinden sonra bunları koştur.

### Bilinen muafiyet

Kütüphane sayfasındaki aktivite ısı haritasının hücreleri 10px, WCAG 2.2'nin
istediği 24px dokunma hedefinin altında — ve bir yıl görünümü 24px'te
çizilemez. Standart bunun yerine **eşdeğer kontrol** koymaya izin veriyor; ısı
haritasının yanındaki tarih girdisi aynı filtreyi sürüyor ve klavyeyle
erişilebilir.

axe bunu görmez ve `target-size` ihlali raporlamaya devam eder. **Bu bilinen ve
kabul edilmiş tek ihlal.** Başka bir `target-size` bulgusu çıkarsa o gerçektir.
