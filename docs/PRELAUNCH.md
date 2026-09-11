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
| 🔴 Engel | 5 (**4 kapandı**, kalan: E4) | Bunlar kapanmadan canlıya çıkılmamalı |
| 🟠 Yüksek | 5 | İlk hafta içinde kapanmalı |
| 🟡 Orta | 8 | Planlanmalı, çıkışı engellemez |
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

### E4 — Bağımlılıklarda 115 bilinen açık

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

## 🟠 Yüksek

### Y1 — Güvenlik açısından en kritik yüzey, en az test edilen yer

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

### Y2 — Test süiti istek-bağlamı hatalarını göremiyor

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

### Y3 — HTMX + CSRF süresi dolması sessizce başarısız

`WTF_CSRF_TIME_LIMIT = 3600`. Bir sekme bir saat açık kaldıktan sonra HTMX
formu gönderildiğinde sunucu ham bir Flask `400 Bad Request` döndürüyor;
kullanıcıya **hiçbir geri bildirim gitmiyor** — kutu sessizce hiçbir şey
yapmıyor.

Tarama sırasında panelde doğrudan görüldü (`POST /interests/add → 400`,
gövde: "The CSRF token has expired.").

Araştırmacıların sekmeyi gün boyu açık bırakması bu uygulamanın normal
kullanımı, yani bu **kesin** yaşanacak.

Seçenekler bir mimari karar:
- global bir `htmx:responseError` yakalayıcı + görünür uyarı, ya da
- `hx-headers` yerine her yanıtta tazelenen token, ya da
- CSRF süresini kaldırıp yalnızca oturum ömrüne bağlamak.

### Y4 — CSP yok, ve satır içi script'ler onu engelliyor

E2'nin ayrılan parçası, ve asıl iş burada: **sekiz şablonda** satır içi
`<script>` var (`base.html`, `core/_password_rules.html`, `core/_splash.html`,
`settings/profile.html`, `library/index.html`, `scrape/feed.html`,
`scrape/_citation_graph.html`, `scrape/_notes_list.html`,
`scrape/_paper_chat.html`). `'unsafe-inline'`'lı bir CSP yazmak, CSP
yazmamakla aynı kapıya çıkar.

Sıra: satır içi script'leri `static/js/`'e taşı → nonce ya da hash'li CSP →
`report-only` ile bir hafta izle → zorunlu kıl. İlk adım tek başına birkaç
günlük iş; CSP'yi ondan önce planlamak yanlış sırayla ilerlemek olur.

### Y5 — Konteyner root olarak koşuyor

`docker/Dockerfile`'da `USER` yönergesi yok. Uygulama, worker ve beat
konteynerlerinin üçü de root.

```dockerfile
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app
```

`uploads` volume'ünün sahipliği de buna göre ayarlanmalı, yoksa avatar
yükleme bozulur.

---

## 🟡 Orta

| # | Bulgu | Not |
|---|---|---|
| O1 | `MAX_CONTENT_LENGTH` tanımsız | nginx `client_max_body_size 3m` ile koruyor; uygulama seviyesinde derinlemesine savunma yok |
| O2 | `audit_logs.user_id` indekssiz | Admin denetim sayfası kullanıcıya göre filtreliyor; tablo büyüdükçe yavaşlar |
| O3 | `entrypoint.sh` `"$@"`'ı yok sayıyor | Yeni bir servis `command:` verip `entrypoint: []` yazmayı unutursa **sessizce gunicorn** koşar. Mevcut worker/beat doğru kurulmuş, ama tuzak duruyor |
| O4 | 6 env değişkeni `.env.example`'da yok | `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `OPENROUTER_BASE_URL`, `SCRAPE_RATE_{EPO_OPS,PATENTSVIEW}_PER_MIN`, `SCRAPE_RATE_SCOPUS_PER_SEC` — hepsinin varsayılanı var |
| O5 | 1/37 migration geri alınamıyor | `c4e91b0a77d2` (çift `scrape.feed` menü kaydını gizleyen veri migration'ı). Boş `downgrade()` burada muhtemelen **doğru** — geri almak bilerek düzeltilmiş bir hatayı geri getirir. Yapılacak iş, bunu `downgrade()` içine bir satır yorum olarak yazmak; sessiz boşluk ile bilinçli karar aynı görünmemeli |
| O6 | `journals` tablosu elle seed gerektiriyor | Scimago CSV yüklenmezse **hiçbir kartta quartile rozeti çıkmaz**. Bozukluk değil (`CLAUDE.md`), ama lansmanda "özellik eksik" gibi görünür — çıkış öncesi yüklenmeli |
| O7 | Python sürüm farkı | Prod imajı `python:3.11-slim`, yerel geliştirme 3.14. CI hangisinde koşuyorsa prod onunla eşleşmeli |
| O8 | Zotero hiç gerçek hesaba karşı koşulmadı | Faz 7.2 yalnızca `requests` sınırında taklit edilerek doğrulandı. Çıkıştan önce bir gerçek anahtarla bir kez denenmeli |

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
