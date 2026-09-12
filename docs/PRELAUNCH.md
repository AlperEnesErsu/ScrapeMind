# Canlı Öncesi Tarama ve Eksik Listesi

> 11 Eylül 2026'da yapıldı. **Her madde koşturularak doğrulandı**, okuyarak
> tahmin edilmedi — komutu da yanında yazıyor ki kapandığında aynı şekilde
> kontrol edilebilsin.
>
> Bu liste `docs/DEPLOYMENT.md §8`'deki kontrol listesinin **yerine geçmez**,
> onu tamamlar. Oradaki maddeler (TLS, yedek, SECRET_KEY, seed admin şifresi)
> hâlâ geçerli; burada olanlar o listenin kapsamadıklarıdır.

## Özet

| Seviye | Adet | Ne demek |
|---|---|---|
| 🔴 Engel | 6 — **hepsi kapandı** ✅ | Canlıya çıkışı engelleyen madde kalmadı |
| 🟠 Yüksek | 5 (**Y1, Y2, Y3, Y5 kapandı** — **Y4 kaldı**) | İlk hafta içinde kapanmalı |
| 🟡 Orta | 13 (**O3, O9, O13 kapandı**) | Planlanmalı, çıkışı engellemez |
| ✅ Doğrulandı | 8 | Bakıldı, iyi durumda — tekrar bakmaya gerek yok |

Toplam **18 açık madde**. Sıralama etkiye göre, çabaya göre değil.

---

## 🔴 Engeller

### ~~E1 — Uygulama `X-Forwarded-*` başlıklarını okumuyor~~ ✅ KAPANDI (PR #80)

`docs/DEPLOYMENT.md §3`'teki nginx yapılandırması `X-Real-IP`,
`X-Forwarded-For` ve `X-Forwarded-Proto` gönderiyor. Uygulamada **ProxyFix
yok**, yani Flask bunların hiçbirini görmüyor.

İki sonucu var ve ikisi de sessiz:

1. **IP başına hız sınırı çalışmıyor.** `request.remote_addr` her istekte
   nginx'in kendisi (`127.0.0.1`) olur. `/auth/login` üzerindeki
   `10 per minute` sınırı kullanıcı başına değil **tüm site için tek havuz**
   hâline gelir. Brute-force koruması yok olur; dahası tek bir saldırgan
   herkesi dışarıda bırakabilir.
2. **`_external=True` URL'ler `http://` üretir.** Şifre sıfırlama ve e-posta
   doğrulama bağlantıları TLS'siz şemayla gider.

**Düzeltme**

```python
# app/__init__.py, create_app() içinde, FLASK_ENV=production iken
from werkzeug.middleware.proxy_fix import ProxyFix

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
```

`x_for=1` — **tam olarak bir** proxy olduğu için. Sayıyı yüksek vermek
istemciye kendi IP'sini uydurma imkânı verir; bu ayarın yanlış tarafa
kaçması sınırları düzeltmez, tamamen kırar.

**Doğrulama**

```bash
curl -H 'X-Forwarded-Proto: https' -H 'X-Forwarded-For: 203.0.113.9' \
  http://127.0.0.1:8000/api/v1/health -i | head -3
```
ve `/auth/login`'e farklı `X-Forwarded-For` değerleriyle 11 kez vurup
yalnızca aynı IP'nin 429 aldığını görmek.

**Yapıldı.** `PROXY_FIX_HOPS` eklendi; **varsayılanı 0**, yani kapalı.
`ProductionConfig` bunu 1'e çekiyor (`DEPLOYMENT.md §3`'teki tek nginx).

Bu sayının yalnızca **tek yönde** tehlikeli olduğunu yazmak gerekiyor: çok
**düşük** olursa hız sınırları tek kovaya çöker — düzeltilmek istenen hata.
Çok **yüksek** olursa uygulama var olmayan bir sıçramaya güvenir ve istemci
`X-Forwarded-For`'un başına istediğini yazıp **kendi kovasını seçebilir**.
İkincisi birincisinden kötü, o yüzden varsayılan güvenli tarafta.

Ölçüldü (prod config + Redis deposu): aynı IP'den 13 istek → **4×429**; farklı
IP'den 3 istek → **hepsi 200**. Yani sınır artık istemci başına sayıyor.

> Yan gözlem: CSRF'i geçemeyen istekler limiter'a **hiç ulaşmıyor** (Flask-WTF
> view'dan önce 400 döndürüyor), yani sayaca girmiyorlar. Gerçek bir saldırı
> için bypass değil — geçerli token almak zaten kolay — ama sayacın neyi
> saymadığı bilinmeli.

---

### ~~E2 — Hiçbir güvenlik başlığı gönderilmiyor~~ ✅ KAPANDI (PR #81, CSP hariç)

Çalışan uygulamada ölçüldü: `Content-Security-Policy`,
`Strict-Transport-Security`, `X-Frame-Options`, `X-Content-Type-Options`,
`Referrer-Policy`, `Permissions-Policy` — **hiçbiri yok**.

Bu projede en çok canı yakacak olanı `Referrer-Policy`: uygulama makale
kartlarından **dış yayıncı sitelerine** link veriyor. Varsayılan davranışta
kullanıcının hangi ScrapeMind sayfasından geldiği (arama sorgusu dahil,
`/library/search?q=...`) referrer olarak o yayıncıya gider.

CSP dikkat ister: uygulama `cdn.jsdelivr.net`'ten yükleniyor (Bootstrap,
Bootstrap Icons) ve **sekiz şablon satır içi `<script>` kullanıyor** — sayıldı,
tahmin edilmedi. Sıkı bir CSP bunların hepsini kırar; `'unsafe-inline'` ile
başlamak ise CSP'yi anlamsızlaştırır. Yani bu, "bir başlık ekle" işi değil;
önce sekiz şablonu temizlemek gerekiyor (bkz. Y4).

**Düzeltme** — nginx'te mi uygulamada mı olacağı bir karar. Öneri:
**uygulamada**, çünkü nginx yapılandırması repoda versiyonlanmıyor ve bir
sonraki sunucuda unutulur.

```python
@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    if app.config.get("SESSION_COOKIE_SECURE"):
        resp.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return resp
```

HSTS'i `SESSION_COOKIE_SECURE`'a bağlamak bilinçli: dev'de HTTP üzerinde
HSTS göndermek tarayıcıyı o host için aylarca HTTPS'e kilitler ve
`localhost` geliştirmeyi bozar.

CSP ayrı bir iş kalemi (bkz. Y4).

**Doğrulama**

```bash
curl -sI https://<host>/auth/login | grep -iE 'strict-transport|x-frame|x-content|referrer|permissions'
```

**Yapıldı** — CSP dışındakiler. Çalışan uygulamada doğrulandı: `nosniff`,
`DENY`, `strict-origin-when-cross-origin`, `Permissions-Policy` geliyor; HSTS
dev'de (HTTP) **gelmiyor**, `SESSION_COOKIE_SECURE` açıkken geliyor.

nginx'te değil **uygulamada**, çünkü nginx yapılandırması versiyonlanmıyor:
her yeni sunucuda doğru yeniden yazılmasına bağlı kalır ve yanlış yazılanı
sessizdir. `setdefault` kullanıldı, yani nginx'i de sertleştiren bir dağıtım
iki çelişen politikayla kalmaz.

**CSP hâlâ açık (Y4)** ve bilerek burada değil: sekiz şablonda satır içi
`<script>` duruyor, dolayısıyla bugün uygulamayı kırmayacak tek CSP
`'unsafe-inline'`'lı olan — o da politika değil.

---

### ~~E3 — Hız sınırı deposu `memory://`~~ ✅ KAPANDI (PR #79)

> Uygulamaya başlayınca bu maddenin bu belgede yazıldığından **daha kötü**
> olduğu ortaya çıktı. Buradaki ilk tarif — "`.env.prod`'a
> `RATELIMIT_STORAGE_URL=redis://...` yaz" — **işe yaramazdı**.

Flask-Limiter 4.1.1 `RATELIMIT_STORAGE_URI` okuyor. `app/config.py` ise
`RATELIMIT_STORAGE_URL` tanımlıyordu — yani ayar **ölüydü**: kütüphane onu hiç
görmüyordu. Çalışan uygulamada ölçüldü; `redis://` verilmişken kullanılan depo
`MemoryStorage` idi.

Yani sorun "prod'da yanlış değere ayarlanmış" değil, "**ayar hiç bağlı
değil**"di. `.env.prod`'a Redis yazan bir ekip, bir şey düzelttiğine inanıp
hiçbir şey düzeltmemiş olacaktı.

Yapılanlar:
- Config anahtarı kütüphanenin okuduğu ada çevrildi; eski **env değişkeni**
  adı yedek olarak okunmaya devam ediyor, böylece mevcut bir `.env` sessizce
  ayarını kaybetmiyor.
- Anahtarın adı, kütüphanenin kendi sabitine karşı **teste bağlandı** —
  ileride bir yeniden adlandırma sessiz bir düşüş değil, düşen bir test olur.
- Prod, `memory://` ile **açılmayı reddediyor**. Uyarı değil ret, çünkü
  koruduğu uç giriş formu ve bir açılış uyarısı olaydan önce değil sonra
  okunur.

> E1 kapanmadan bu tek başına yeterli değildi: doğru depo, yanlış IP'yi doğru
> saymaktan başka işe yaramaz. E1 sıradaki.

---

### ~~E4 — Bağımlılıklarda 115 bilinen açık~~ ✅ KAPANDI (PR #85, #87) — 115 → **0**

`pip-audit -r requirements.txt` ile ölçüldü. 8 pakette uyarı var; ağırlık
merkezi:

| Paket | Sürüm | Uyarı | Neden bu projede önemli |
|---|---|---|---|
| `authlib` | 1.3.2 | **10+** | **OAuth giriş yolu** — Google/Microsoft ile giriş |
| `cryptography` | 48.0.0 | 7 | Kullanıcıların **LLM ve Zotero API anahtarlarını** Fernet ile şifreleyen kütüphane |
| `lxml` | 5.3.0 | 2 | RSS/HTML ayrıştırma — **dışarıdan gelen içeriği** işliyor |
| `pypdf` | 5.1.0 | 14 | OA tam metin — **dışarıdan gelen PDF'leri** işliyor |
| `flask`, `requests`, `python-dotenv`, `pytest` | — | kalan | |

`authlib`, `lxml` ve `pypdf` üçü de **saldırganın içerik sağlayabildiği**
yollarda: sırasıyla kimlik doğrulama, uzak besleme, uzak PDF.

**Düzeltme** — tek seferde değil, sırayla ve her adımda test koşarak:

```bash
venv/Scripts/python.exe -m pip install -U authlib cryptography lxml pypdf
venv/Scripts/python.exe -m pytest -q          # OAuth testleri %0 — bkz. Y1
venv/Scripts/python.exe -m pip freeze | grep -E '^(Authlib|cryptography|lxml|pypdf)=' 
# çıkanları requirements.txt'e pinle
```

> ⚠️ `authlib` 1.3 → 1.6 **majör olmayan ama davranış değiştiren** bir atlama
> ve OAuth yolunun hiç testi yok (Y1). Bu ikisi birlikte planlanmalı:
> önce testi yaz, sonra yükselt.

**Doğrulama**

```bash
venv/Scripts/python.exe -m pip_audit -r requirements.txt --progress-spinner off
```

**Yapıldı.** Yükseltilenler: `authlib` 1.3.2→1.8.0, `cryptography` 48→50,
`lxml` 5.3.0→6.1.3, `pypdf` 5.1.0→6.16.1, `flask` 3.1.0→3.1.3,
`python-dotenv` 1.0.1→1.2.2, `pytest` 8.3.3→9.0.3, `requests` 2.32.3→2.32.4.

**115 uyarı → 1.**

> **Güncelleme (PR #87):** "bilerek açık" bırakılan son uyarı da kapandı.
> `arxiv` 2.1.3 → 4.0.1 yükseltildi, `requests` pini serbest kaldı ve 2.33.0
> alındı. `pip-audit` artık **"No known vulnerabilities found"** diyor.
> Aşağıdaki gerekçe, o kararın neden o an doğru olduğunu kayda geçiriyor.

Kalan tek uyarı (`requests` PYSEC-2026-2275) bilerek açıktı:

- Yalnızca `requests.utils.extract_zipped_paths()`'i **doğrudan çağıran**
  uygulamaları etkiliyor; danışmanlığın kendi ifadesiyle *"standart kullanım
  etkilenmiyor"*. Kod tabanında bu fonksiyonun **sıfır** kullanımı var
  (`grep` ile doğrulandı).
- Kapatmak `requests` 2.33.0 gerektiriyor, onu da `arxiv` SDK'sı engelliyor:
  `requests~=2.32.0` pinliyor. Aşmak için `arxiv` 2.1.3 → **4.0.1**, iki major
  atlama — canlı arXiv doğrulaması gerektiren, kendi PR'ını hak eden ayrı bir iş.

Alınan `requests` 2.32.4 ise **uygulanabilir olanı** kapatıyor:
PYSEC-2026-1872, `.netrc` kimlik bilgilerinin kötü niyetli URL'lere sızması.

Doğrulandı: OAuth yönlendirmesi Google'a gidiyor, Fernet ile şifrelenmiş
sırlar çözülüyor, API token'ı üretilip korumalı uçta kabul ediliyor. 1303 test,
UI denetimi temiz.

---

### ~~E5 — `REMEMBER_COOKIE_SECURE` tanımsız~~ ✅ KAPANDI (PR #79)

`app/config.py` oturum çerezini sıkılaştırıyor (`HTTPONLY`, `SAMESITE`,
prod'da `SECURE`) ama **"beni hatırla" çerezine hiç dokunmuyor**.
Flask-Login'in varsayılanı `REMEMBER_COOKIE_SECURE = False`.

O çerez kalıcı bir kimlik doğrulama belirtecidir — oturum çerezinden **daha
uzun** yaşar. Düz HTTP üzerinde gidebilmesi, sıkılaştırılmış oturum
çerezinin anlamını ortadan kaldırır.

**Düzeltme** — `BaseConfig`'e:

```python
REMEMBER_COOKIE_HTTPONLY = True
REMEMBER_COOKIE_SAMESITE = "Lax"
```

ve `ProductionConfig`'e `SESSION_COOKIE_SECURE`'un yanına:

```python
REMEMBER_COOKIE_SECURE = True
```

**Doğrulama** — giriş yaparken "Beni hatırla" işaretleyip `Set-Cookie:
scrapemind_remember=...` satırında `Secure; HttpOnly; SameSite=Lax` görmek.

`HTTPONLY` ve `SAMESITE` `BaseConfig`'e, `SECURE` ise `ProductionConfig`'e
`SESSION_COOKIE_SECURE`'un **yanına** kondu — ikisi bir arada dursun ki
birbirinden ayrı düşmesinler.

---

### E6 — OAuth girişi hiç çalışmıyormuş ✅ KAPANDI (PR #83)

> **Bu madde ilk taramada yoktu.** Y1'in testleri yazılırken çıktı, ve neden
> kaçırıldığını yazmak listeyi kullanacak olan için önemli: tarama
> *yapılandırmaya* ve *bağımlılıklara* baktı, **özelliğin çalışıp
> çalışmadığına** bakmadı.

`GoogleOAuthStrategy.register()` ve Microsoft ikizi, `oauth.register(...)`'ın
tek çağıranlarıydı — ve onları **hiçbir yer çağırmıyordu**. Sınıflar ölü koddu.

Sonuç: authlib'in kayıtlı istemcisi yok, `getattr(oauth, "google", None)`
`None` dönüyor, rota "Bilinmeyen OAuth sağlayıcısı"na düşüyor. Giriş sayfası
ise **iki düğmeyi de** gösteriyordu.

Çalışan uygulamada doğrulandı: Google düğmesine basmak o hatayla giriş
sayfasına geri atıyordu.

**Kimseyi içeri alamayan bir giriş yolu sunmak, hiç sunmamaktan kötü.**

Yapılanlar: sağlayıcılar **kimlik bilgileri varsa** kaydediliyor, ve düğme
**yalnızca sağlayıcı kayıtlıysa** gösteriliyor. Bu ikisi aynı koşul olmak
zorunda.

Bu aynı zamanda "OAuth'u istiyor muyuz" kararını kimseye sordurmadan çözüyor:
env değişkenlerini ver, sağlayıcı görünür; boş bırak, görünmez.

Doğrulandı: kimlik bilgileri verilince düğme beliriyor ve tıklama Google'ın
gerçek yetkilendirme adresine 302 veriyor.

---

## 🟠 Yüksek

### ~~Y1 — Güvenlik açısından en kritik yüzey, en az test edilen yer~~ ✅ KAPANDI (PR #82)

`pytest --cov` ile ölçüldü:

| Modül | Kapsam |
|---|---|
| `app/core/auth/strategies/oauth_google.py` | **%0** |
| `app/core/auth/strategies/oauth_microsoft.py` | **%0** |
| `app/core/auth/strategies/jwt_api.py` | **%0** |
| `app/core/auth/routes.py` | **%38** |
| `app/core/users/routes.py` | %29 |
| `app/core/audit/routes.py` | %33 |

Genel kapsam **%81** ve CI eşiği %80 — yani ortalama iyi görünürken giriş
yolları neredeyse tamamen test dışı. E4'teki authlib yükseltmesi bu testler
olmadan kör bir atlama olur.

En az şunlar: OAuth callback'inde state doğrulaması, hesap bağlama
(`_oauth_link_user_id`), var olan e-postayla eşleşme davranışı.

### ~~Y2 — Test süiti istek-bağlamı hatalarını göremiyor~~ ✅ KAPANDI (PR #88)

`pytest-flask`, `app` fixture'ını kullanan **her** testin etrafına bir istek
bağlamı itiyor. Süit, "burada istek bağlamı yok" hatasını **yapısal olarak**
yeniden üretemiyor.

Bu teorik değil: Faz 7.1 (kayıtlı arama uyarıları) tam bu yüzden **çalışmayan
hâlde** indi ve bir ay sonra, uygulama elle koşturulurken bulundu
(`docs/HANDOVER.md §5.8`, PR #71).

`-p no:flask` ile **36 test düşüyor**. İş, o 36'sının hangisinin gerçek bir
kusuru örttüğünü, hangisinin yalnızca test kolaylığı olduğunu ayırmak.

> Bu, `CLAUDE.md`'deki "Sıradaki iş" listesinin 2. maddesi. Canlıya çıkışı
> engellemez ama **canlıdayken bulunacak hataların sınıfını** belirler.

**Yapıldı — ve iş sanıldığı gibi çıkmadı.** Buradaki tarif "36 testin
hangisinin gerçek hata olduğunu ayırmak" diyordu. Ayrılacak 36 test yoktu:
**36'sı da iki yerin aynı varsayımıydı.**

| kök | ne yapıyordu |
|---|---|
| `app/core/i18n/utils.py::select_locale` | `request.args`'ı koşulsuz okuyordu → istek dışı her `_()` `RuntimeError` |
| `app/__init__.py::inject_menu` | `current_user.is_authenticated`'a dokunuyordu; istek dışında `current_user` `None` → `AttributeError` |

İkisi düzeltildi, **hiçbir test düzenlenmedi**, 36 → 0.

Selectör artık istek yokken `BABEL_DEFAULT_LOCALE`'e düşüyor. Bu takas
bilerek ve bedava değil: `force_locale`'ı unutan bir arka plan işi, İngilizce
okuyan birine sessizce Türkçe yazacak. Ama **yanlış dildeki bir bildirim
görünür ve düzeltilebilir; hiç oluşturulmamış bir bildirim ikisi de değil** —
ki Faz 7.1'de olan tam olarak buydu.

`inject_menu` de istek dışında boş dönüyor. Bugün şablon render eden e-postalar
yalnızca rotalardan çağrılıyor, ama şablonlu bir e-postayı görevden gönderen
ilk kod aynı taşa çarpacaktı.

**Geri dönmesin diye kapı:** `tests/core/test_no_ambient_request.py`. Eklenti
geri kurulunca **üç testi düşüyor** — denendi.

Ölçüldü: eklentiyi kaldırmanın süit süresine etkisi yok (aynı altküme 42.3s'ye
karşı 43.7s).

### ~~Y3 — HTMX + CSRF süresi dolması sessizce başarısız~~ ✅ KAPANDI (PR #89)

`WTF_CSRF_TIME_LIMIT = 3600`. Bir sekme bir saat açık kaldıktan sonra HTMX
formu gönderildiğinde sunucu ham bir Flask `400 Bad Request` döndürüyor;
kullanıcıya **hiçbir geri bildirim gitmiyor** — kutu sessizce hiçbir şey
yapmıyor.

Tarama sırasında panelde doğrudan görüldü (`POST /interests/add → 400`,
gövde: "The CSRF token has expired.").

Araştırmacıların sekmeyi gün boyu açık bırakması bu uygulamanın normal
kullanımı, yani bu **kesin** yaşanacak.

Seçenekler bir mimari karardı:
- global bir `htmx:responseError` yakalayıcı + görünür uyarı, ya da
- `hx-headers` yerine her yanıtta tazelenen token, ya da
- CSRF süresini kaldırıp yalnızca oturum ömrüne bağlamak.

**Yapılan: birinci ve üçüncü, çünkü ayrı şeyleri çözüyorlar.** Üçüncüsü sebebi
kaldırıyor, birincisi geriye kalan sessizliği.

**`WTF_CSRF_TIME_LIMIT = None`.** 3600 bu projenin verdiği bir karar değil,
Flask-WTF'nin varsayılanıydı. `None` "koruma yok" demek değil: token hâlâ
`SECRET_KEY` ile imzalı ve oturumun kendi CSRF değerine bağlı, yani kullanmak
için **kurbanın oturumu** gerekiyor. Süre sınırının koruduğu şey *sızmış* bir
token'ın sonradan tekrar oynatılması — ve bu uygulama token'ı hiçbir zaman
URL'ye koymuyor, yalnızca form gövdesine ve `X-CSRFToken` başlığına.

**Sessizlik ise daha kötü yarısıydı ve sebebi düzeltilse de kalırdı.**
`app.js`'teki tek HTMX dinleyicisi sadece `evt.detail.successful` dalına
bakıyordu: 400, 403, 500 ve kopmuş bağlantı, "kutu tepki vermedi"den ayırt
edilemiyordu. Dört dal da artık görünür bir toast veriyor, mesajlar
`<body data-msg-*>` üzerinden `_()`'den geçiyor — satır içi `<script>` değil,
çünkü onları kaldırmak Y4'ün işi.

Yolda çıkan bir kusur: `showToast` uyarı tipinde **onay işareti** gösteriyordu,
yani "bir şeyler ters gitti" metni "başarılı" ikonuyla çıkıyordu. Tek uyarı
toast'ı "Makale gizlendi" iken kimse fark etmemiş.

Doğrulama tarayıcıda: gerçek bir başarısız HTMX isteği 400 dalını tetikledi;
403, 500 ve ağ hatası dalları da doğru mesajı verdi.

### Y4 — CSP yok, ve satır içi script'ler onu engelliyor  ◐ **kısmen yapıldı (PR #91)**

E2'nin ayrılan parçası, ve asıl iş burada: **sekiz şablonda** satır içi
`<script>` var (`base.html`, `core/_password_rules.html`, `core/_splash.html`,
`settings/profile.html`, `library/index.html`, `scrape/feed.html`,
`scrape/_citation_graph.html`, `scrape/_notes_list.html`,
`scrape/_paper_chat.html`). `'unsafe-inline'`'lı bir CSP yazmak, CSP
yazmamakla aynı kapıya çıkar.

Sıra: satır içi script'leri `static/js/`'e taşı → nonce ya da hash'li CSP →
`report-only` ile bir hafta izle → zorunlu kıl. İlk adım tek başına birkaç
günlük iş; CSP'yi ondan önce planlamak yanlış sırayla ilerlemek olur.

---

#### ⚠️ Kapsam düzeltmesi — bu madde eksik sayılmıştı

İşe başlayınca ölçüldü: sorun sekiz `<script>` bloğundan ibaret değil.

| ne | adet | nerede |
|---|---|---|
| satır içi `<script>` bloğu | 8 | 8 şablon (~459 satır, 323'ü tek dosyada) |
| satır içi olay işleyicisi (`onclick=`, `onsubmit=`, `onchange=`, `onkeydown=`) | **36** | **18 şablon** |
| satır içi `style=` özniteliği | **127** | **39 şablon** |

**CSP satır içi olay işleyicilerini script blokları kadar engeller.** İlk
tahmin onları saymamıştı; gerçek iş iki üç katı.

#### Bu yüzden sıra değişti: bedelsiz olan kısım önce gitti (PR #91)

`script-src` ve `style-src` beklemek zorunda. Ama CSP'nin geri kalanı bu
temizliğe hiç bağlı değil ve gerçek delikler kapatıyor:

```
base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'
```

- `base-uri` — enjekte edilmiş bir `<base>` sayfadaki bütün göreli URL'leri
  yeniden yönlendiremez.
- `object-src 'none'` — uygulamada hiç `<object>`/`<embed>` yok, bedava.
- `frame-ancestors 'none'` — X-Frame-Options'ın modern hâli; ikisi de
  gönderiliyor ve aynı şeyi söylüyorlar.
- `form-action 'self'` — her form kendi origin'ine post ediyor (doğrulandı),
  yani enjekte edilmiş bir form gönderimi dışarı sızdıramaz.

`script-src` **bilerek yok**. ``'unsafe-inline'`` taşıyan bir direktif, adı olan
ama işlevi olmayan bir politikadır; başlığı okuyan biri script'lerin
kısıtlandığını sanmasın. Bir test bunu sabitliyor: politikada `script-src` de
`unsafe-inline` de geçmemeli.

Doğrulandı: dört sayfa gezildi, **tek bir CSP ihlali yok**; form gönderimi
çalışıyor.

#### Kalan iş (sırayla)
1. ✅ 36 satır içi olay işleyicisini delegasyona çevir (18 şablon)
   - ✅ **core — 7 → 0 (PR #92).** Altısı `onsubmit="return confirm(…)"`,
     biri bildirim rozetini silen `onclick`. Karşılıkları `app.js`'te
     delegasyonlu: `data-confirm` ve `data-remove-on-click`. HTMX ile sonradan
     gelen işaretlemeye de yeniden bağlama gerekmeden uygulanıyor.
     `tests/core/test_csp_readiness.py` mandallı bir kapı: temizlenen dizin
     sıfırda kalmak zorunda, toplam azalabilir ama artamaz.
   - ✅ **scrape-A — genel kalıplar, 13 → 0 (PR #93).** Değişince-gönder (6),
     onay (2), Enter / Ctrl+Enter ile gönder (3), panoya kopyala (1), formun
     dışındaki silme düğmesi (1). Yeni kancalar: `data-autosubmit`,
     `data-submit-on-enter`, `data-submit-on-mod-enter`, `data-copy-text`.
     Silme düğmesi JS'ye hiç ihtiyaç duymadan HTML'in `form=` özniteliğiyle
     çözüldü.
   - ✅ **scrape-B — sayfa fonksiyonları, 16 → 0.** İşleyiciler, onları
     tanımlayan üç satır içi `<script>` bloğuyla (`feed.html`,
     `_notes_list.html`, `_paper_chat.html`) birlikte taşındı. Yeni kancalar:
     `data-bulk-select` / `data-bulk-clear`, `data-toggle-abstract`,
     `data-notes-filter`, `data-chat-question`, `data-heatmap-date-input` /
     `data-heatmap-clear` (ısı haritası günü zaten `data-date` taşıyordu).
     Yolda üç hata kapandı: `/library/search`'te toplu seçim kutusu
     `ReferenceError: toggleBulkPanel is not defined` atıyordu (fonksiyon
     yalnızca `feed.html`'de tanımlıydı); özet düğmesi çevrilmiş etiketi Türkçe
     sabit metinle eziyordu; sohbetteki dört hazır soru `_()` dışındaydı, EN
     arayüzde Türkçe görünüyordu. **Toplam işleyici sıfır** — mandal artık
     `app/modules`'ü de temiz dizin sayıyor, tavan 0.
2. ✅ **Satır içi `<script>` blokları → 0.** Başta sekiz sayılmıştı; üçü
   scrape-B'de işleyicileriyle birlikte gitti, kalan beşi burada:
   - `_citation_graph` (~330 satır) → `app/modules/scrape/static/js/citation_graph.js`.
     `scrape` blueprint'i bunun için `static_folder` aldı; çekirdek `app.js`
     modül koduna dokunmuyor. URL'ler ve 12 çevrilmiş metin artık
     `data-*` özniteliklerinden okunuyor — metin JS dizesine gömülmediği için
     tırnaklı bir çeviri script'i kıramıyor. Parça HTMX ile geldiğinde
     `<script src>` onunla birlikte geliyor ve dosya `data-cg-ready`
     bayrağıyla iki kez çalışmaya dayanıklı. Yolda: düğüm ipucundaki sabit
     `Atıf:` (EN arayüzde de Türkçe), `'Untitled'` ve `alert()` ile verilen
     İngilizce hata metinleri çeviriye / toast'a taşındı.
   - `_splash` temizliği, profil sekmesi vurgusu ve Bootstrap tooltip
     başlatma `app.js`'e (tooltip artık HTMX ile gelen içerikte de çalışıyor).
   - `_password_rules.html` **silindi**: hiçbir yerden include edilmiyordu.
   `test_no_inline_script_blocks` sıfırı kilitliyor (Jinja yorumları hariç).
   > `vis-network` hâlâ jsDelivr'den **dinamik** yükleniyor ve standalone
   > paketi kendi `<style>`'ını enjekte ediyor — adım 3'te `script-src`
   > jsDelivr'i kapsamalı, adım 4'te bu stil hesaba katılmalı.
3. ✅ **`script-src` report-only olarak yayında.**
   `Content-Security-Policy-Report-Only: script-src 'self'
   https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/
   https://cdn.jsdelivr.net/npm/vis-network@9.1.9/; report-uri /csp-report`.
   - jsDelivr **paket@sürüm yoluna** sabitlendi — çıplak `cdn.jsdelivr.net`
     npm'deki her paketi izinli yapardı, bu bir baypas olurdu. Bir test
     koddaki her CDN script'inin listede olduğunu tarıyor.
   - **Yolda bulunan engel:** üç formda `hx-on::after-request` vardı; htmx
     bunları `new Function` ile çalıştırıyor, yani `'unsafe-eval'` olmadan
     kırılacaklardı ve işleyici mandalı `hx-on`'u saymıyordu. `data-reset-on-success`
     / `data-clear-on-success` kancasına çevrildi, mandal artık `hx-on`'u da
     sayıyor, `base.html` htmx'in `allowEval`'ini kapattı. Sohbet formu eskiden
     başarısız istekte de sıfırlanıp soruyu kaybediyordu.
   - `/csp-report` (`app/core/csp_report.py`): CSRF muaf, dakikada 60, gövde
     16 KB ile sınırlı, yalnızca bilinen alanlar loglanıyor ve URL'lerden
     sorgu dizesi atılıyor (arama terimleri loga düşmesin). Hem eski
     `csp-report` hem Reporting API biçimini okuyor. Log olayı: `csp_violation`.
   - Doğrulama: oturum açıkken ana sayfa, Keşfet, makale (grafik + sohbet),
     kütüphane, arama, profil, kullanıcılar, görevler gezildi — **sıfır ihlal**.
     Pozitif kontrol: sayfaya bilerek satır içi script eklenince rapor sunucu
     loguna `csp_violation blocked=inline` olarak düştü.
   - ⬜ **Zorlamaya geçiş:** bir sürüm boyunca prod logunda `csp_violation`
     çıkmazsa `CSP_ENFORCE_SCRIPT_SRC=true`; aynı direktif gerçek başlığa geçer.
4. 127 satır içi `style=` → `style-src`

### ~~Y5 — Konteyner root olarak koşuyor~~ ✅ KAPANDI (PR #86)

`docker/Dockerfile`'da `USER` yönergesi yok. Uygulama, worker ve beat
konteynerlerinin üçü de root.

```dockerfile
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app
```

`uploads` volume'ünün sahipliği de buna göre ayarlanmalı, yoksa avatar
yükleme bozulur.

**Yapıldı**, ve uid **sabit** (10001) seçildi: Docker yeni bir adlandırılmış
volume'ü imajdaki dizinden *ve onun sahipliğinden* tohumluyor. Build'den
build'e değişen bir uid, mevcut bir volume'ü artık var olmayan bir kullanıcıya
ait bırakırdı.

Yanında iki şey daha çıktı ve aynı PR'a girdi:

- **`gcc` ve `libpq-dev` gereksizmiş.** Onları oraya koyduran `psycopg2` idi;
  proje `psycopg2-binary` kullanıyor ve kalan her bağımlılık manylinux wheel'i
  ile geliyor. İmaj **derleyicisiz build edildi** — doğrulandı, tahmin
  edilmedi. Çalışma imajında derleyici bırakmak boyut değil **erişim** sorunu:
  konteynerde kod çalıştırabilen her şey yanında bir araç zinciri buluyor.
- **O3 (aşağıda) aynı dosyada olduğu için birlikte kapatıldı.**

Doğrulama (build + çalıştırma): `id` → `uid=10001(app)`, uploads dizini
yazılabilir, imajda `gcc` yok, varsayılan yol boş bir veritabanında
**44 tabloyu migrate edip** gunicorn'u açtı ve `/api/v1/health` **200** döndü.

---

## 🟡 Orta

| # | Bulgu | Not |
|---|---|---|
| O1 | `MAX_CONTENT_LENGTH` tanımsız | nginx `client_max_body_size 3m` ile koruyor; uygulama seviyesinde derinlemesine savunma yok |
| O2 | `audit_logs.user_id` indekssiz | Admin denetim sayfası kullanıcıya göre filtreliyor; tablo büyüdükçe yavaşlar |
| ~~O3~~ ✅ | ~~`entrypoint.sh` `"$@"`'ı yok sayıyor~~ | **PR #86 ile kapandı.** Geçirilen komut artık kazanıyor ve migration koşmuyor (migration'lar web servisine ait). Geçici çözüm çağıranın tarafındaydı — `entrypoint: []` — yani tuzak bir sonraki servisi bekliyordu |
| O4 | 6 env değişkeni `.env.example`'da yok | `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `OPENROUTER_BASE_URL`, `SCRAPE_RATE_{EPO_OPS,PATENTSVIEW}_PER_MIN`, `SCRAPE_RATE_SCOPUS_PER_SEC` — hepsinin varsayılanı var |
| O5 | 1/37 migration geri alınamıyor | `c4e91b0a77d2` (çift `scrape.feed` menü kaydını gizleyen veri migration'ı). Boş `downgrade()` burada muhtemelen **doğru** — geri almak bilerek düzeltilmiş bir hatayı geri getirir. Yapılacak iş, bunu `downgrade()` içine bir satır yorum olarak yazmak; sessiz boşluk ile bilinçli karar aynı görünmemeli |
| O6 | `journals` tablosu elle seed gerektiriyor | Scimago CSV yüklenmezse **hiçbir kartta quartile rozeti çıkmaz**. Bozukluk değil (`CLAUDE.md`), ama lansmanda "özellik eksik" gibi görünür — çıkış öncesi yüklenmeli |
| O7 | Python sürüm farkı — **tarama bunu yanlış yazmış** | Prod imajı `python:3.11-slim` ve **CI de 3.11** (`ci.yml`); yani prod ile CI zaten eşleşiyor. Sapma **geliştiricinin venv'inde**: 3.14. Sonucu kozmetik değil — yerel mypy ile CI'ınkinin ayrışmasının sebebi bu (O11), ve o ayrışma bir kırmızı PR'ın merge edilmesine yol açtı. Yapılacak iş venv'i 3.11'e çekmek, Dockerfile'a dokunmak değil |
| O8 | Zotero hiç gerçek hesaba karşı koşulmadı | Faz 7.2 yalnızca `requests` sınırında taklit edilerek doğrulandı. Çıkıştan önce bir gerçek anahtarla bir kez denenmeli |

| ~~O9~~ ✅ | ~~`arxiv` SDK'sı 2.1.3, güncel 4.0.1~~ | **PR #87 ile kapandı.** İki major atlamaya rağmen kullanılan API yüzeyi birebir aynı çıktı: `Client(page_size, delay_seconds, num_retries)`, `Search(query, id_list, max_results, sort_by, sort_order)`, `Client.results` ve `Result`'ın sekiz alanı — hiçbiri değişmemiş, adaptör tek satır değişmeden çalıştı. **Canlı arXiv sorgusuyla doğrulandı**, üç gerçek sonuç tam alanlarla döndü. Pin kalkınca `requests` 2.33.0 alındı ve son uyarı da kapandı |
| O10 | `authlib.jose` kullanımdan kaldırıldı | API v1 JWT'leri onu kullanıyor. authlib **2.0'da kaldırılacak**, yerine `joserfc`. Şimdi çalışıyor, ama bir sonraki major yükseltmede kırılacak — planlanmalı |
| O11 | Yerel mypy ile CI mypy aynı sonucu vermiyor | Yerelde 98, CI'da 95 çıkabiliyor (Python sürüm farkı, bkz. O7). Geliştirici yerel ratchet'e **güvenemiyor**; bu, kırmızı bir PR'ın merge edilmesine yol açtı |

| O12 | `g` testler arasında sızıyor | `tests/conftest.py`'deki `app` fixture'ı `scope="session"` ve **tek bir app context'i** bütün koşu boyunca açık tutuyor, yani `g` 1312 testin ortak malı. Kanıtlandı: bir testte `g`'ye yazıp diğerinde okunabiliyor. Y3'ün testleri buna çarptı (`generate_csrf` token'ı `g`'de önbelleğe alıyor). **Belirgin çözüm ucuz değil:** test başına iç içe app context açmak, Flask-SQLAlchemy oturumu app context'e bağladığı için testlere fixture'larından farklı bir DB oturumu verir |
| ~~O13~~ ✅ | ~~Paylaşımlı geliştirme veritabanı dalları birbirine kilitliyor~~ | **PR #90 ile kapandı.** Menü satırları **veri**, ve veri ona anlam veren koddan uzun yaşıyor: modül kaldırılır, kapatılır ya da o dalda hiç yoktur. `build_menu_for_user` artık uygulamanın sahip olmadığı endpoint'leri eliyor — `_prune_empty_groups`'un tıklanamayan öğeler için zaten verdiği kararın aynısı. Admin menü sayfası satırı DB'den okuyup çözmeden bastığı için elenen satır orada **hâlâ görünür ve düzeltilebilir** |

Ayrıca duran teknik borç: `mypy-baseline.txt` 95'te, en yoğun yer
`app/modules/scrape`.

---

## ✅ Bakıldı, iyi durumda

Bunlar tarandı ve sorun çıkmadı — tekrar bakmaya gerek yok.

- **Prod'da traceback sızmıyor.** `FLASK_ENV=production` ile yapay bir 500
  tetiklendi: kullanıcıya düzgün hata sayfası, traceback yalnızca sunucu
  log'unda.
- **Repoda gizli bilgi yok.** `.env` hiç commit edilmemiş; anahtar
  desenleri (`sk-`, `AKIA`, `ghp_`) takip edilen dosyalarda yok.
- **`_validate_production_config`** boş `SECRET_KEY` ya da `DATABASE_URL` ile
  açılışı gürültülü şekilde reddediyor.
- **İndeksleme iyi.** `papers` 9 indeks (pgvector HNSW dahil), `user_papers`
  5, `scan_runs`'ta bileşik `(user_id, started_at)`.
- **Yedekleme ve geri yükleme dokümante** (`DEPLOYMENT.md §6`), günlük cron
  + 14 gün saklama.
- **Sağlık ucu ve healthcheck'ler** yerinde; `/api/v1/health` kimlik
  istemiyor ve dış servise dokunmuyor.
- **Sentry ve Prometheus** opsiyonel ama hazır; DSN verilince devreye giriyor.
- **SSRF guard, denetim günlüğü, 2FA, oturum rotasyonu, refresh-token iptali**
  mevcut ve testli.

---

## Önerilen sıra

1. **E5 + E3** — ikisi de tek satırlık config, aynı PR'da.
2. **E1** — ProxyFix. E3'ün bir anlam ifade etmesi buna bağlı.
3. **E2** — güvenlik başlıkları (CSP hariç).
4. **Y1** — OAuth testleri.
5. **E4** — bağımlılık yükseltmesi. Y1'den **sonra**, çünkü authlib atlaması
   testsiz kör olur.
6. **Y5, O1, O3, O7** — konteyner ve dağıtım sertleştirmesi, tek PR.
7. **Y3** — HTMX/CSRF; mimari karar gerektiriyor.
8. **Y4** — CSP; satır içi script taşıma işiyle birlikte.
9. **Y2** — `pytest-flask`; en büyük iş, en az acil, ama bundan sonra
   bulunacak hataların sınıfını bu belirliyor.
10. **O6** — Scimago CSV, çıkıştan hemen önce.

**E1–E5 kapanmadan canlıya çıkılmamalı.** Kalanlar çıkışı engellemez.
