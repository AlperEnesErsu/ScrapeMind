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

## Açık soru — `fetcher.py` ile kural 7 çelişkisi

HANDOVER §5.4, `rss_source._get_with_redirects`'in ortak bir
`app/modules/scrape/fetcher.py`'ye taşınmasını istiyor. Bu, **`CLAUDE.md` kural 7 ile
kısmen çelişiyor**: kural, adaptörlerin `requests`'i ortak bir wrapper arkasına
saklamamasını söylüyor (testler modülün kendi `requests`'ini monkeypatch'liyor) — ama
`_get_with_redirects` tam da bir `requests` çağrı yeridir. Çıkarım yapılırsa testler
`rss_source.requests` yerine `fetcher.requests`'i patch'lemeye geçer.

Bu çelişki §5.4 yapılırken çözülmeli, önceden değil. Önerilen çözüm: kuralı
*"yönlendirme takibi ve SSRF politikası tek bir `fetcher`'a aittir; tek atışlık GET
yapan adaptörler kendi modül seviyesi `requests`'ini kullanmaya devam eder"* diye
netleştirmek.

Bu turda çıkarım **yapılmadı**: getirisi ancak `web_source.py` var olduğunda doğuyor,
şimdi taşımak davranış değiştirmeyen ama test yüzeyi kıran saf risk olurdu.

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
