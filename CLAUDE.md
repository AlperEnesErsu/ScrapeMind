# ScrapeMind — Bağlam Dosyası (Claude için)

## Projeye Genel Bakış
ScrapeMind aynı zamanda `flask-core-base` adlı yeniden kullanılabilir bir Flask iskeletidir.
**İki şapka:** `app/core/` = her projede kullanılabilecek çekirdek, `app/modules/` = ScrapeMind'a özel.

## Konum
`d:\app\ScrapeMind` (Windows geliştirme)

## Teknoloji
Flask 3.1 · SQLAlchemy 2 · PostgreSQL 17 · Bootstrap 5 + HTMX · Flask-Babel (TR/EN) · Celery 5.4 + Redis
LLM çok sağlayıcılı: OpenRouter (varsayılan, ücretsiz model) · Ollama (yerel) · Anthropic
Scraping: `arxiv` SDK · `feedparser` · `requests` · `yt-dlp` (yalnızca transkript)
— **tarayıcı otomasyonu yok**. Scrapy/Playwright/Selenium kurulu değil ve
**kurulmayacak**; gerekçe + kararın yeniden açılma koşulu:
`docs/adr/0001-headless-browser-yok.md`. (README/PROJECT.md'nin eski hali bunları
vaat ediyordu, doğru değil.)

## Önce Bunları Oku
- `docs/SCRAPING.md` — veri toplama mimarisi, **yeni kaynak eklemeden önce zorunlu**
- `docs/HANDOVER.md` — durum, commit'lenmemiş iş, tuzaklar, sıradaki görevler
- `docs/PHASE5.md` — Faz 5 planı (patentler, dergi kalitesi, yazar takibi, opsiyonel Scopus)
- `docs/DESIGN.md` — tasarım sistemi, **arayüze dokunmadan önce zorunlu**
- `docs/PHASE7.md` — Faz 7 planı (kayıtlı arama + uyarı, Zotero, #58)
- `docs/PHASE8.md` — Faz 8, Patent Takibi (USPTO tam metni, 3 haftalık pencere)
- `docs/PRELAUNCH.md` — canlı öncesi tarama; **açık maddeler burada**

## Veritabanı (yerel geliştirme)
- **Postgres artık ScrapeMind'in kendisinin, Redis hâlâ paylaşımlı.** Postgres: `scrapemind-db-1`, **5433**, `pgvector/pgvector:pg17`, kullanıcı/şifre `scrapemind`, volume `scrapemind_pg_data`. Ayağa kaldır:
  `SCRAPEMIND_DB_PORT=5433 docker compose -f docker/docker-compose.yml -p scrapemind up -d db`
  Redis: myoChtBt'nin `shared_redis`'i, **6379**.
  > Bu bir geri dönüş: uzun süre paylaşımlı `myo_postgres17` (5432) kullanıldı ve bu dosya "5433 artık doğru değil" diyordu. Faz 5.4 `Paper.embedding`'i `VECTOR(1536)` yapınca o kurulum çalışamaz oldu — `myo_postgres17` `postgres:17-alpine` ve pgvector içermiyor; alpine'ın hazır paketi de `postgresql18`'e bağlı. `myo_postgres17` başka bir projenin (myoChtBt) container'ı olduğu için imajı değiştirilmedi; `scrapemind` veritabanı oradan `pg_dump` ile kopyalandı ve orijinali olduğu gibi duruyor. Tam gerekçe + eski bir DB'nin nasıl onarılacağı: `docs/HANDOVER.md §4.9`.
- **`TEST_DATABASE_URL` kökteki `.env`'de bulunmak zorunda** — yoksa `create_app()` conftest override'ından önce TestingConfig default'uyla bağlanmaya kalkar ve testlerin tamamı `psycopg2.OperationalError` verir. Bu, "kod bozuk" gibi okunur; ilk bakılacak yer burasıdır.
- Docker kapalıysa `docker ps` boş döner ve yine aynı tabloya çıkarsın — önce Docker Desktop'ı aç.
- **venv Python 3.11 olmalı** — CI ve Docker imajı 3.11. `setup.bat` venv'i 3.11 ile kurar; başka bir sürümde (ör. 3.14) mypy CI'dan 3 fazla sayar ve `scripts/mypy_ratchet.py` sebepsiz düşer. Repo dışında bir venv kullanmak için `SCRAPEMIND_VENV` (`setup/development/worker.bat` okur).
- **`venv`'i `requirements.txt` ile senkron tut.** `sentry-sdk` ve `prometheus-flask-exporter` eksikken `tests/core/test_observability.py` 4 test patlatır; kodla ilgisi yoktur.
- `.env.local` → `.env`'e kopyalanarak aktif edilir

## Kritik Mimari Kurallar
1. `app/core/` asla `app/modules/`'dan import ETMEZ
2. `is_superuser` bypass YALNIZCA `app/core/auth/decorators.py`'deki `permission_required`'da
3. Plugin discovery (`app/modules/__init__.py`) tablo yokken sessizce geçer
4. Migration sırası: `flask db upgrade` → uygulama başlatma (bkz. `docker/entrypoint.sh`)
5. Profil tab'ları genişletilebilir: `app/core/settings/tab_registry.py`
6. Kaynak adaptörleri **duck-typed modül**, ABC yok — `SOURCE_NAME` + `search()` +
   `search_for_keywords()`. Yeni kaynak = modül + `sources/__init__.py`'de 3 satır
7. **Tek atışlık GET yapan adaptörler** modül seviyesinde `requests` kullanır — testler
   modülün kendi `requests`'ini monkeypatch'liyor, ortak wrapper'ın arkasına saklama.
   **Ama yönlendirme takibi ve SSRF politikası `app/modules/scrape/fetcher.py`'ye
   aittir**: kullanıcı URL'si alan her yol `get_with_redirects` + `read_capped`
   kullanır, kendi döngüsünü yazmaz (hop başına yeniden doğrulama garantisi ancak tek
   yerde tutulursa geçerli). Gerekçe: `docs/adr/0001-headless-browser-yok.md`

## Klasör Yapısı
```
app/core/          → Auth, RBAC, Menü, Settings, Audit, i18n, Email, Sessions, API v1, UI — dokunma
app/modules/
  ├── dashboard/   → ana sayfa, ilgi alanları, kaynak seçici kartı
  ├── academic/    → kimlikler (ORCID/Scopus/WoS), Keyword sözlüğü (+ TR→EN çeviri kolonları)
  ├── patent/      → Patent Takibi (Faz 8): uspto.py · parser.py · ingest.py · search.py
                     embedding.py · explain.py — kendi tabloları, `papers`'a yazmaz
  └── scrape/      → sources/ (adaptörler) · service.py (orkestrasyon) · ai_service.py (LLM)
                     net_guard.py (SSRF) · ratelimit.py (Redis bütçe) · doi.py · forms.py
app/tasks/         → core_tasks, scrape_tasks, feed_tasks, digest_tasks, channel_tasks,
                     patent_bulk_tasks (haftalık USPTO yükleme, purge, istem-1 embedding),
                     schedule (BEAT_SCHEDULE), schedule_info (crontab→zaman), fanout
translations/      → TR + EN .po (`.mo` derleme çıktısı, commit'lenmez — `scripts/compile_translations.py`)
scripts/           → seed.py, create_module.py, export_core_template.py,
                     render_favicon.py (işareti logo.svg'den türetir)
docs/              → SCRAPING.md, HANDOVER.md, API_V1.md, UI_REVIEW.md, DESIGN.md,
                     PHASE5.md, PHASE7.md, PHASE8.md, PRELAUNCH.md
docs/adr/          → mimari karar kayıtları — neden **yapmadığımız** şeyler
```

## Çeviri İş Akışı
> ⚠️ **`pybabel extract` + `pybabel update` KULLANMA** — bu akış bir kez mevcut
> TR çevirilerin ~300'ünü sildi (fuzzy-match kazası). Yeni string'leri Babel
> API ile **tek tek ekle**:
```python
# venv/Scripts/python.exe ile çalıştır:
from babel.messages.pofile import read_po, write_po
with open("translations/tr/LC_MESSAGES/messages.po", "rb") as f:
    cat = read_po(f)
cat.add("New string", string="Yeni metin")   # EN kataloğunda string=msgid
with open("translations/tr/LC_MESSAGES/messages.po", "wb") as f:
    write_po(f, cat, sort_output=False, sort_by_file=False)
```
```bash
# Sonra derle (bu güvenli). `.mo` commit'lenmez; Docker build, CI, test paketi
# ve production dışındaki `create_app` bayat katalogları kendiliğinden derler:
python scripts/compile_translations.py
```
- TR ve EN kataloglarının **msgid key set'leri eşit olmalı** — CI bunu kontrol ediyor
- EN kataloğunda çeviri = msgid'nin kendisi (identity translation)

## Tamamlanan Faz Durumu (Eylül 2026)
- **Faz 0-6** ✅ hepsi main'de. Açık dal yok.
- Faz detayları aşağıda; en yenisi **Faz 6** (raporlar) ve onun ardından gelen
  **tasarım sistemi** (`docs/DESIGN.md`).

### Faz 2 — merge'lenenler
| PR    | Konu |
|-------|------|
| #4-#6 | Multi-email, identity model düzeltmesi, ORCID/Scopus/WoS seed + admin panel |
| #7    | Celery + Redis worker + Beat scheduler + `/admin/tasks` paneli |
| #8    | arXiv scraping — `Paper` modeli, source adapter, `/papers` feed |
| #12-#16 | Yeni nesil frontend — Discover feed, `/library` timeline, notlar, AI analiz + TR çeviri (Claude API), BibTeX, mobil nav |
| #17   | Makale sohbeti, bildirimler, read-later, bulk actions + HTTP test paketi |
| #18-#19 | Güvenlik sertleştirme (OAuth takeover, session fixation, open redirect, 2FA TTL, recovery race) + mimari düzeltmeler |
| #20-#22 | AI service testleri, TR çeviri kurtarma + audit label'ları, UX quick wins |
| —     | **2FA (TOTP)** — profil kurulum sihirbazı + login challenge + recovery kodları |
| #23   | **Avatar dosya yükleme** — Pillow yeniden kodlama, 256×256 WEBP |
| #24   | **API v1 (JWT)** — `/api/v1` bearer-token JSON API (bkz. `docs/API_V1.md`) |
| #25   | **Audit retention** — `AUDIT_RETENTION_DAYS` + gecelik purge task + admin rozeti |

| #27   | **Semantic Scholar + PubMed** — çok kaynaklı tarama (Faz 2 kapandı) |
| #28-#30 | Refresh-token revocation · API yazma endpoint'leri · RBAC izin cache'i |

### Faz 3 ✅ — `feat/homepage-source-selection` main'e merge edildi
RSS beslemeler · SSRF guard · Redis rate limit · TR→EN anahtar kelime çevirisi ·
konu sınıflandırma + ilgi-farkında kaynak seçici · ScanRun + durum paneli · digest ·
çok sağlayıcılı LLM · worker ayrımı + deterministik fan-out · health paneli.

### Faz 4 ✅ — `feat/openalex-crossref-youtube-channels` main'e merge edildi
Her commit tek başına test-yeşil, `git bisect` güvenilir. Commit listesi
`docs/HANDOVER.md §3`'te. Kapsam:
- **DOI normalizasyonu + `upsert_paper` zenginleştirmesi** — boş alanı doldur, doluyu
  asla ezme (`app/modules/scrape/doi.py`)
- **OpenAlex + Crossref** adaptörleri (akademik kaynak sayısı 3 → 5)
- **YouTube kanal aboneliği** — kanal RSS'i ile yeni video tespiti, `yt-dlp` ile
  transkript, LLM ile TR özet; kanal limiti admin panelinden (`max_user_channels`)
- **`fix(deploy)`** — prod worker `-Q` olmadan çalışıyordu, routed task'ların hiçbiri
  tüketilmiyordu

### Faz 5 ✅ tam — plan `docs/PHASE5.md`, uygulama PR #42-#45 ile main'de
5.1 kimlik/admin kapılı kaynaklar + kalıcı haftalık kota (fail-closed) · 5.2 patent
kaynakları (EPO OPS + PatentsView) + `/papers/patents` prior-art araması · 5.3 dergi
kalite katmanı (Scimago quartile + atıf sayısı) · 5.4 yazar takibi (ORCID → OpenAlex) +
admin panelinden açılan, varsayılan kapalı, **discovery-only** Scopus.
> ⚠️ `journals` tablosu **elle seed edilir** — Scimago CSV'siz hiçbir kartta quartile
> rozeti çıkmaz, bu bozukluk değil. `scopus_source`'daki `abstract=None` bir **lisans
> kısıtı**, optimizasyon değil (`docs/adr/0002-elsevier-discovery-only.md`).
> **WoS Lite / Elsevier tekrar sorulursa:** engel kota değil. WoS Starter ücretsiz
> katmanı **50 istek/gün** ve atıf döndürmüyor; Elsevier sözleşmesi içeriğin **kalıcı
> saklanmasını** ve rekabet eden türev servisi yasaklıyor; Scopus anahtarı kurum IP'sine
> bağlı. Tam tablo ve karar `docs/PHASE5.md §2`.

### Faz 6 ✅ tam — PR #53 ile main'de
Rapor hattı: tek `report` tablosu, iki tür (konu raporu + yazar grubu raporu).
**Önce sayılar, sonra anlatı** — metin hesaplanmış veriden üretilir, tersi değil.
Uzun külliyat için map/reduce özetleme (reduce adımı yapılandırılmış bir dosya
görür, parça metinlerinin birleşimini değil) · yazar grupları ve gruplanınca
hayatta kalan duraklatma · OpenAlex'e aralık hasadı, agregatlar, yazar arama ·
rapor üretimi istek dışında, llm kuyruğunda.

### Tasarım sistemi ✅ — PR #54 ile main'de
Nar işareti + ondan türetilen garnet palet · IBM Plex Sans/Mono ve dokuz adımlı
tip ölçeği · CVD-güvenli kategorik palet · dark mode kaldırıldı.
**Değiştirmeden önce `docs/DESIGN.md` oku** — özellikle kırmızı marka ile kırmızı
tehlike renginin neden formla ayrıldığını, çünkü bu kural renkle kendini
koruyamıyor.
`tests/core/test_design_system.py` 8 denetimle bu kararları tutuyor: ölçek dışı
`font-size`, üründe emoji, AA altına düşen renk çifti, kategorikleşen quartile
rampası, gerçeği söylemeyen kontrast yorumu, kalkan outline varyantları.
Tarayıcı gerektiren denetimler (axe, durum bazlı kontrast, 280px reflow) CI'da
değil — script'leri komşu `UI-UX/` klasöründe, elle koşulur.

### Faz 8 ✅ — Patent Takibi, PR #98 → #109 ile main'de
USPTO haftalık verilmiş patent tam metninden **son ~3 hafta** (`PATENT_WINDOW_WEEKS`),
`app/modules/patent/`, kendi tabloları. İstem ağacı, istem düzeyinde FTS, istem-1
vektörleri + RRF, kütüphaneye ekleme, düz dille istem açıklaması, yükleme paneli.
Ayrıntı: `docs/PHASE8.md`. Değiştirmeden önce bilinmesi gerekenler:
- **`USPTO_ODP_API_KEY` zorunlu, anahtarsız yol yok.** `bulkdata.uspto.gov` emekli;
  Open Data Portal 18 Haziran 2026'dan beri MFA'lı USPTO.gov hesabı istiyor. Anahtar
  yoksa haftalık yükleme gerekçesiyle **atlanır**, purge çalışır. **Hiçbir istek henüz
  gerçek USPTO'ya gitmedi** — parser ve ODP yanıt şeması ilk gerçek koşuda sınanacak.
- **AI filtresi CPC** (`G06N`); AIPD kullanılmıyor (yıllık, pencereye yetişmez).
- **Tam metin `papers`'a girmez.** Kütüphaneye ekleme `papers` satırı üretir; purge
  onu silemez (`paper_id` SET NULL). Patentlerin DOI'si yok — aynı patentin
  `US11123456` / `US11123456B2` biçimleri `service.library_paper`'da eşleniyor.
- **Vektörler modelini taşır** (`patent_chunks.embedding_model`, `papers.embedding_model`).
  Model adının tek kaynağı `embedding_service.current_embedding_model()`.

### Sıradaki iş (öncelik sırasıyla)
1. **[#58](https://github.com/AlperEnesErsu/ScrapeMind/issues/58) — hesap ayarları ile
   ürün yapılandırmasını ayır.** `Profilim` altında 12 sekme var ve ikisi farklı şey:
   sekiz tanesi hesap (kim olduğun, nasıl giriş yaptığın), dördü ürün yapılandırması
   (ORCID kimlikleri, ilgi alanları, LLM anahtarları, takip edilen yazarlar). Bir
   kullanıcının LLM sağlayıcısı profil ayarı değil. Sol menü **zaten sakin** — sorun
   sidebar'da değil, profil sayfasının içinde; çözüm sidebar'ı şişirmemeli.
2. **Faz 8'i gerçek USPTO verisiyle koş** — ODP anahtarı (MFA'lı USPTO.gov hesabı)
   gerekiyor; `/patents/admin` → "Haftalık yüklemeyi çalıştır".
3. Canlı öncesi kalanlar: `docs/PRELAUNCH.md` — engeller ve yüksek seviye kapandı;
   **Y4**'te yalnızca prod'da CSP zorlamasına geçiş kaldı. Orta: O6 (Scimago CSV),
   O8 (Zotero'yu gerçek hesapla dene).
4. Küçük borç: `mypy-baseline.txt` 75'te (14 Eylül'de 95'ten indi).

> ✅ **Faz 7.1 (kayıtlı arama + uyarı)** PR #64, **Faz 7.2 (Zotero aktarımı)**
> PR #65 ile main'de. 7.1 indiği hâlde **çalışmıyordu** — uyarılar beat'te koşuyor,
> `notification_text` içindeki `_()` istek bağlamı istiyordu; hata arama başına
> `except`'e takılıp "yeni eşleşme yok" gibi görünüyordu. PR #71 düzeltti ve
> `announce()` sırasını da tersine çevirdi: **önce teslim et, sonra işaretle** —
> eski sıra, aradaki her hatada makaleleri kalıcı olarak "bildirildi" yapıp uyarıyı
> sessizce kaybediyordu.
> ✅ OA tam metin (Faz 7.0) PR #62 ile indi — lisans kapılı saklama, bkz.
> `docs/SCRAPING.md §11`. Tam metin araması külliyatın tamamını **kapsamıyor**,
> yalnızca lisansın saklamaya izin verdiği alt kümeyi; bu bir eksik değil.

Gerekçeler: `docs/HANDOVER.md §5` · Faz 5 detayı: `docs/PHASE5.md`
> Bu liste 10 Eylül 2026'da gerçeğe karşı denetlendi. İki madde bitmiş olduğu
> hâlde duruyordu: RSS'siz site scrape'i + alan seçici (PR #47/#48 ile inmişti) ve
> sol menüdeki çift aktiflik hatası (PR #56). İkincisinin buradaki teşhisi de
> yanlıştı — `is_active` ön ek değil tam endpoint eşliyordu, hata veriydi: iki menü
> kaydı aynı endpoint'i gösteriyordu.

## Bilinen Kısıtlar / Tuzaklar
- **Arka planda `_()` çağıran her yol `force_locale` ile sarılmalı.** Dil **alıcının**
  dili olmalı, worker'ı çalıştıranın değil; kalıp `digest_tasks`, `report_tasks` ve
  `alerts`'te. Unutmak artık `RuntimeError` değil **yanlış dil** üretiyor: PR #88
  `select_locale`'i istek yokken `BABEL_DEFAULT_LOCALE`'e düşürdü, çünkü atılan hata
  zaten yutuluyordu ve Faz 7.1'i ölü gösteriyordu.
- **`pytest-flask` süitten çıkarıldı (PR #88).** Eklenti her testin etrafına bir istek
  bağlamı itiyordu, yani süit "burada istek bağlamı yok" hatasını **göremiyordu**.
  Geri kurulursa `tests/core/test_no_ambient_request.py` düşer — bilerek.
  Bir teste istek gerekiyorsa **kendi içinde** `app.test_request_context()` ile ister.
- **i18n denetimi: `venv/Scripts/python.exe scripts/i18n_audit.py`** (CI'da da koşuyor).
  Üç kontrol: kaynakta `_()` ile sarılıp katalogda olmayan string; tek kelimelik
  etiketin cümleye çevrilmesi; **bir Türkçe metnin iki msgid'de görünmesi** — sonuncusu
  `pybabel update` fuzzy hasarının imzası ve 21 tane buldu (`Toggle favorite` →
  "Tema Değiştir", `Save` → "Aktif"). Yeni string'i **elle Babel API'siyle** ekle.
- Email gönderimi `MAIL_SUPPRESS_SEND=true` ise dev modu — link `flash` ile gösteriliyor
- API v1: auth (token/refresh/logout) + okuma + yazma (favorite, read-later, dismiss, notlar) — bkz. `docs/API_V1.md`
- **Tekilleştirme sırası: normalize DOI → `(source, external_id)`.** Eşleşen satır
  zenginleştirilir (boş alan dolar, dolu alan **asla** ezilmez). `doi` üzerinde UNIQUE
  yok, sadece index — yarış penceresi var. İki yol farklı satırlara işaret ederse DOI
  satırı kazanır, diğeri olduğu gibi kalır; satır birleştirme yok
- **Conditional GET her iki besleme yolunda da canlı.** Kullanıcı beslemeleri validator'ı
  `UserFeed` satırında, küratörlü beslemeler `system_settings["feed_validators"]`'te
  tutuyor. Üç kural: validator'lar **upsert'lerden sonra** yazılır, `ok` olmayan durum
  saklı validator'a **dokunmaz**, besleme aktifleşince validator'lar **temizlenir**
  (`add_user_feed`'in doğrulama fetch'i de etag saklamaz — yoksa ilk gecelik koşu 304
  alıp beslemeyi kalıcı boş gösterir). Detay: `docs/SCRAPING.md §7.1`
- `rss_source.fetch_feed` **kaldırıldı** — `fetch_feed_conditional` kullan. Kaldırıldı
  çünkü payload-only sarmalayıcı olarak durduğu sürece conditional GET'i sessizce
  atlamanın kolay yolu oydu; bu hata zaten bir kez böyle oluştu
- **BEAT_SCHEDULE saatleri UTC değil**, `BABEL_DEFAULT_TIMEZONE` (Europe/Istanbul).
  Kullanıcıya gösterilen zamanı elle hesaplama — `app/tasks/schedule_info.py` var
- **`beat` tek replika olmalı** — zamanlama yerel dosyada, ikincisi her şeyi çift tetikler
- **`SCRAPE_SOURCES`, `SEMANTIC_SCHOLAR_API_KEY`, `NCBI_API_KEY` `BaseConfig`'i atlar**
  (doğrudan `os.getenv`). Testte `monkeypatch.setenv` kullan, `setitem(app.config)` değil
- Test config'i bilinçli "kırık": `FEED_ALLOW_PRIVATE_HOSTS=True` (CI'da dışa DNS yok),
  `REDIS_URL` kapalı porta bakar (kilit/rate limit fail-open olsun), LLM anahtarları
  boşaltılır (yanlışlıkla faturalı çağrı olmasın)
- `ask_paper` **gerçek RAG** (PR #50): soru vektörleştirilir, kütüphaneden en yakın
  makaleler bağlama girer — yalnızca geçerli modelin vektörleri karşılaştırılır
- **Uygulanmış bir migration'ın `down_revision`'ı değiştirilmez.** İki dal aynı
  ebeveynden migration eklerse ("multiple heads") **merge revision** yaz; ebeveyn
  değiştirmek damgalı DB'de diğer migration'ı sessizce atlatır (HANDOVER §4.9,
  §4.11). Yeni migration eklemeden hemen önce `origin/main`'in head'ine bak
- **Push'tan önce CI'ın tamamını yerelde koş** — liste HANDOVER §4.12. Testler
  `create_all()` kullanır, migration zincirini **görmez**: boş DB'ye
  `flask db upgrade` ayrı adımdır. CI bir adımda düşünce sonrakiler koşmaz
- **Ana checkout paylaşımlı** — başka bir oturum ya da geliştirici orada dal
  değiştiriyor olabilir. Git'e yazan işleri ayrı bir worktree'de yap, komutla aynı
  satırda `git branch --show-current` doğrula (HANDOVER §4.13)
- **Testlerde `g` artık her istek ve her test başında temiz** (PR #110). Tek
  session app context'i yüzünden eskiden aynı test içindeki iki kullanıcının
  istekleri aynı `g._login_user`'ı görüyordu — sahte bir 200 böyle üretildi
- **Video transkripti saklanmaz** — `VideoSummary` yalnızca özeti ve `transcript_chars`
  sayacını tutar (`docs/SCRAPING.md §11` telif sınırı). `paper_id` üzerinde tekil, yani
  dil başına cache yok — feed'de N+1 olmasın diye bilinçli
- **Yeni Celery task modülü `app/tasks/__init__.py`'deki import satırına eklenmezse**
  worker task'ı hiç görmez, hata da vermez. `TASK_ROUTES` girdisi de gerekir

## Açık Kaynak Kuralları
- Bu repo **halka açık** — commit/PR/dokümantasyona gizli bilgi (şifre, API key, gerçek e-posta) yazma
- `.env` asla commit'lenmez; yeni config değişkeni eklerken `.env.example`'ı placeholder ile güncelle
- Örneklerde/testlerde `example.com` / `example.test` adresleri kullan

## Template Olarak Yeni Projede Kullanım
```bash
# 1. GitHub'da "Use this template" → yeni repo
# 2. Kopyaladıktan sonra:
python scripts/export_core_template.py --target . --name yeni_proje_adi
# 3. app/modules/_template/ kullanarak ilk modülü oluştur
python scripts/create_module.py ilk_modul_adi
```
