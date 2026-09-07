# ADR-0001 — Headless tarayıcı (Selenium/Playwright) kapsam dışı

**Durum:** Kabul edildi · **Tarih:** 6 Ağustos 2026
**Bağlam:** RSS'siz sitelerden veri toplama (HANDOVER §5.4) planlanırken verildi.

---

## Neden bu dosya var

Bu karar bugüne kadar dört ayrı dosyada — `CLAUDE.md`, `PROJECT.md`, `README.md`,
`HANDOVER.md` — birbirinden bağımsız olarak yeniden gerekçelendirilmiş. Her seferinde
aynı sonuca varılmış ama gerekçenin tamamı hiçbirinde yok. Tarihli tek bir kayıt,
beşinci kez tartışılmasını durdurur ve kararın hangi koşullar altında yeniden
açılacağını da yazılı hale getirir.

## Karar

**ScrapeMind headless tarayıcı kullanmaz.** JavaScript render'ı gerektiren siteler
bilinçli olarak kapsam dışıdır. RSS'siz site scrape'i `requests` + `trafilatura` ile
yapılacaktır.

## Gerekçe

### Dağıtım maliyeti
- Tek `docker/Dockerfile` **dört servisi birden** besliyor: `web`, `worker`,
  `worker-io`, `beat`. Chromium hepsine iner. Taban imaj `python:3.11-slim` ve şu an
  yalnızca iki apt paketi var (`libpq-dev`, `gcc`).
- Prod tek VM: `docker-compose.prod.yml` **tek** `worker --concurrency=2` çalıştırıyor.
  Dev'in `worker-io`'su ise **tek proseste 16 thread** — bu model `requests` ve
  `psycopg2`'nin beklerken GIL'i bırakmasına dayanıyor. Bir tarayıcı prosesi bu
  modelin dışında.
- Hiçbir compose dosyasında `shm_size` veya bellek limiti yok. Chromium'un
  `/dev/shm` ihtiyacı ayrı bir operasyon yüzeyi açar.

### CI
15 dakikalık iş cap'i, test başına 60 sn timeout, browser install adımı yok ve pip
cache tarayıcı indirmesini kapsamıyor. `--cov-fail-under=55` de var.

### Güvenlik — en ağır gerekçe
`net_guard`'ın SSRF koruması **hop başına yeniden doğrulama** üzerine kurulu ve bunu
`allow_redirects=False` ile elde ediyor
([`rss_source._get_with_redirects`](../../app/modules/scrape/sources/rss_source.py)):
her yönlendirmede yeni adres tekrar çözülüp kontrol ediliyor, çünkü public görünen bir
URL `http://169.254.169.254/`'e 302 atabilir. **Bir tarayıcı yönlendirmeleri kendi
içinde takip eder** — bu kontrolün tamamı devre dışı kalır ve yerine konacak şey
yazılmamıştır.

Ayrıca `SCRAPING.md` §11, genel sayfa scrape'i başladığı an `robots.txt` uyumunu
zorunlu kılıyor; `app/modules/scrape/robots.py` henüz yok.

### Karşı taraf — dürüstçe
Sorun gerçek: [`rss_source.py`](../../app/modules/scrape/sources/rss_source.py)
başındaki yorum, çalışan bir RSS endpoint'i bulunamadığı için elenen **altı** kaynağı
kaydediyor (Anthropic News, `openai.com/blog/rss.xml`, Meta AI, Mistral, Microsoft AI,
Stability AI). `SCRAPING.md` §10 da "lab/enstitü haber sayfaları erişilemez" diyor.

Ama bu boşluğun büyük kısmı tarayıcı gerektirmiyor: HANDOVER §5.4'ün tespiti,
**"RSS'i yok" sanılan sitelerin çoğunda gizli bir `<link rel="alternate">` olduğu**
ve en ucuz kazancın orada olduğu yönünde.

## Bunun yerine

HANDOVER §5.4'teki keşif sırası:

1. RSS autodiscovery (`<link rel="alternate">`) — en ucuz kazanç
2. JSON-LD
3. Tekrar eden blok sezgisi
4. `trafilatura` ile tek makale çıkarımı

Yeni bağımlılıklar: `beautifulsoup4`, `lxml`, `trafilatura`. Tarayıcı yok.

**Emsal var:** ağır harici araçları subprocess ile çağırmak bu repoda zaten yapılıyor
(`yt-dlp` transkript için, `gh` CLI için `FileNotFoundError` korumasıyla). Yani "opsiyonel
harici binary" yolu kapalı değil — kapalı olan, o binary'nin *imaja gömülmesi*.

## ~~Açık soru~~ → Çözüldü (16 Ağustos 2026, `feat/web-source`)

**Karar:** kural 7 önerildiği gibi netleştirildi —

> Yönlendirme takibi ve SSRF politikası tek bir `fetcher`'a aittir; tek atışlık GET
> yapan adaptörler kendi modül seviyesi `requests`'ini kullanmaya devam eder.

Ayrım üslup değil. Kural 7, konserve yanıt testinin test ettiği adaptörü
monkeypatch'leyebilmesi için var ve sabit, güvenilen bir host'a giden adaptörlerde
bunun maliyeti yok: OpenAlex kimseyi `169.254.169.254`'e 302'lemeyecek.

Kullanıcı URL'si alan yollar tam tersi durum. Güvenlik özellikleri **hop başına yeniden
doğrulama** ve bunun her çağırana **aynı** şekilde uygulanması gerekiyor — kopyala-yapıştır
bir döngünün veremeyeceği garanti tam olarak bu. Bir hop kontrolünü sessizce unutan
üçüncü bir çağıran, üslup ihlali değil gerçek bir açık olurdu.

Çıkarım sırasında bunun **zaten olmakta olduğu** görüldü: `youtube_channel_source`
`rss_source._read_capped`, `rss_source._cfg` ve `rss_source._USER_AGENT`'a — yani başka
bir adaptörün *private* isimlerine — uzanıyordu. Üç çağıranı ortak katmana bağlamak
bunu da temizledi.

**Test yüzeyi:** `fetcher.requests` ile `rss_source.requests` **aynı modül nesnesi**
(ikisi de düz `import requests` yapıyor), yani `monkeypatch.setattr(<mod>.requests,
"get", ...)` her iki isimden de çalışır — çıkarım burada bir şey kırmadı. Kıran şey,
SSRF guard'ının **isimle** import edilmesiydi: `is_public_http_url` artık `fetcher`'ın
namespace'inde, dolayısıyla testler onu orada patch'liyor (`_allow` yardımcısı).

## Kararın yeniden açılma koşulu

Aşağıdakilerin **hepsi** doğruysa yeniden değerlendirilir:

1. JS-render olmadan erişilemeyen, yüksek değerli **≥5 kaynak** birikmiş olması
   (①-④ keşif sırası denenip başarısız olduktan sonra — "RSS'i yok sandım" sayılmaz)
2. Ayrı bir imaj + ayrı bir `render` kuyruğu ile ana imajın temiz kalabilmesi
3. Tarayıcı için SSRF eşdeğerinin yazılmış olması (proxy üzerinden zorlanmış adres
   kontrolü veya ağ seviyesi izolasyon) — bu madde pazarlık dışı

Reddedilen ara yol: **harici render servisi** (Jina Reader gibi). `web_reach` bugün
`s.jina.ai` kullanıyor, yani emsal var; ama hedef URL'leri üçüncü tarafa göndermek
kullanıcının takip ettiği kaynakları dışarı sızdırır ve README'nin veri taahhüdüyle
çelişir. Tek bir adaptörde tolere ediliyor, genel scrape yolu haline getirilemez.
