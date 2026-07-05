# ScrapeMind — Arayüz Denetimi & Geliştirme Önerileri

> **Tarih:** Temmuz 2026
> **Kapsam:** Admin paneli + Kullanıcı (araştırmacı) paneli — tasarımsal iyileştirmeler ve eklenebilecek özellikler
> **Amaç:** Faz 2 frontend redesign'ı sonrası biriken fikirleri tek yerde toplamak. Bu bir "yapılacaklar" listesi değil, "değerlendirilecekler" havuzu.

Öncelik notasyonu: 🔴 yüksek (görünür etki/kolay) · 🟡 orta · 🟢 düşük (nice-to-have)

---

## 1. Kullanıcı Paneli (Araştırmacı Tarafı)

### 1.1 Tasarımsal İyileştirmeler

| # | Alan | Sorun / Gözlem | Öneri | Öncelik |
|---|------|----------------|-------|---------|
| U1 | `dashboard/for_you.html:78-110` | User stats kartları inline `linear-gradient` + hardcoded HSL renkleri kullanıyor — `theme.css` değişkenlerini baypas ediyor, dark/light tutarsızlığı riski. | Gradient'leri `theme.css`'e taşı (`.stat-card--interests` gibi), CSS değişkenlerine bağla. | 🟡 |
| U2 | `for_you.html` geneli | Ana sayfa (`/`) `bg-body`, `shadow-sm`, `card` yoğun — Discover feed'deki temiz `paper-card` diline göre daha "ağır". İki ekran görsel olarak kopuk. | Ana sayfayı da `paper-card` + flat border diline yaklaştır, gölge azalt. | 🟡 |
| U3 | Onboarding stepper `for_you.html:125-158` | 3 adımlı stepper statik — kullanıcı adımları tamamladıkça işaretlenmiyor (hep "1,2,3" gri). | Adım durumunu dinamik yap: keyword var → adım 1 ✓, paper var → adım 2 ✓. | 🔴 |
| U4 | Paper detail — mode toggle | `Orijinal / Türkçe / AI Analiz` geçişi tam sayfa yenileme yapıyor (URL `?mode=`). HTMX ile partial swap daha akıcı olur. | `hx-get` + `hx-target="#paper-body"` ile mod geçişini partial'a çevir. | 🟡 |
| U5 | Feed kartları | Uzun abstract'lar `-webkit-line-clamp` ile 2 satıra kısılıyor ama "devamı" göstergesi yok — kullanıcı kesildiğini anlamıyor. | Kısılan karta ince bir alt-fade gradient veya "…" ekle. | 🟢 |
| U6 | Not paneli | Notlar tag rengine göre sol-border alıyor ama tag filtresi yok. 10+ not olunca "sadece 'soru' notlarını göster" gerekir. | Not panelinin üstüne küçük tag filtre chip'leri ekle (client-side). | 🟡 |
| U7 | Dark mode | AI analiz kartlarındaki `text-purple`, `text-warning` gibi Bootstrap util renkleri dark'ta yeterince kontrastlı değil bazı yerlerde. | Dark mode kontrast geçişi — özellikle `ai-card` başlık ikonları. | 🟢 |
| U8 | Boş durumlar | Feed/Library boş state'leri iyi ama "Filtrele" sonucu 0 makale gelince ayrı bir boş-durum yok (sadece hiç kart yok). | Filtre 0 sonuç → "X için sonuç yok, filtreyi temizle" mesajı. | 🟡 |

### 1.2 Eklenebilecek Özellikler

| # | Özellik | Açıklama | Değer | Öncelik |
|---|---------|----------|-------|---------|
| UF1 | **"Makaleye sor" (RAG chat)** | Paper detay'da Claude ile sohbet: "bu modelin X'ten farkı ne?". Abstract + (varsa) tam metin context. | Çok yüksek — projenin "wow" özelliği. | 🔴 |
| UF2 | **Haftalık digest / Insights** | "Bu hafta 12 yeni makale, en aktif ilgi alanın: diffusion. 3 makalede 'sparse attention' notu aldın — keyword ekleyelim mi?" | Yüksek — engagement + geri dönüş sebebi. | 🔴 |
| UF3 | **Bulk actions** | Feed'de çoklu seçim → toplu favori/gizle/etiketle. 50+ makale olunca tek tek zahmetli. | Orta | 🟡 |
| UF4 | **Okuma listesi / "sonra oku"** | Favori ≠ sonra oku. Ayrı bir "kuyruk" durumu (bookmark). | Orta | 🟡 |
| UF5 | **Not export** | Bir makalenin notlarını (veya tüm notları) Markdown/PDF olarak dışa aktar. Araştırma günlüğü için. | Orta | 🟡 |
| UF6 | **Yazar takibi** | Bir yazarı takip et → o yazarın yeni makaleleri feed'e düşsün (keyword gibi). | Orta-yüksek | 🟡 |
| UF7 | **Benzer makaleler** | Paper detay'da "buna benzer 5 makale" (keyword/kategori örtüşmesi ile). | Orta | 🟢 |
| UF8 | **Okuma istatistikleri / heatmap** | Library'de takvim heatmap: hangi gün kaç makale okudun (GitHub katkı grafiği gibi). | Düşük-orta (motivasyon) | 🟢 |
| UF9 | **Klavye kısayolları** | `j/k` ile kart gezinme, `f` favori, `n` not — power-user hızı. | Düşük | 🟢 |
| UF10 | **Kayıtlı aramalar** | `?q=transformer` filtresini "kayıtlı arama" olarak sakla, tek tıkla çağır. | Düşük | 🟢 |

---

## 2. Admin Paneli (Sistem Yönetimi)

### 2.1 Tasarımsal İyileştirmeler

| # | Alan | Sorun / Gözlem | Öneri | Öncelik |
|---|------|----------------|-------|---------|
| A1 | `admin_overview.html:9-50` | Metrik kartları `text-bg-primary/success/warning` gibi dolu-renk Bootstrap util'leri — user paneldeki flat/gradient dille tutarsız. İki panel iki farklı tasarım dili konuşuyor. | Metrik kartlarını tek bir tasarım diline birleştir (flat + ikon + sayı). | 🟡 |
| A2 | `for_you.html` admin dalı | Admin, ana sayfada hem araştırmacı feed'i hem sistem metrikleri görüyor — rol karışımı. Admin'in araştırma feed'i işine yaramıyor olabilir. | Admin için ana sayfayı doğrudan `admin_overview`'a yönlendirmeyi değerlendir (ya da net ayır). | 🟡 |
| A3 | `users/list.html`, `audit/list.html` | Tablolar `table-light` thead + `table-hover` — işlevsel ama yoğun. Kullanıcı panelindeki kart estetiğinden kopuk. | Tabloları biraz havalandır (satır yüksekliği, badge tutarlılığı). Zaten iyi durumda, düşük öncelik. | 🟢 |
| A4 | `audit/list.html:54-56` | Audit tablosunda `action` ham key olarak gösteriliyor (`user.totp_failed`) — TR kullanıcı teknik string görüyor. | Action key'leri için okunabilir label sözlüğü (`ACTION_LABELS`). | 🟡 |
| A5 | Admin genel | Tüm admin sayfaları ayrı tam-sayfa; HTMX partial güncelleme neredeyse yok (tablolar sayfalama hariç). | Kritik değil ama kullanıcı tarafındaki akıcılık admin'de yok — tutarlılık için değerlendir. | 🟢 |
| A6 | `admin_overview.html` | Metrikler statik sayı — trend yok ("kullanıcı bu hafta +3"). | Metrik kartlarına küçük trend göstergesi (↑/↓ son 7 gün). | 🟢 |

### 2.2 Eklenebilecek Özellikler

| # | Özellik | Açıklama | Değer | Öncelik |
|---|---------|----------|-------|---------|
| AF1 | **Audit log retention + filtre/sayfalama iyileştirme** | Zaten Task #6'da planlı. Audit tablosu büyüyecek — retention politikası + gelişmiş filtre. | Yüksek (ölçeklenme) | 🔴 |
| AF2 | **Scrape/sistem sağlık paneli** | Celery worker durumu, son scrape zamanları, kuyruk uzunluğu, başarısız task sayısı tek ekranda. | Yüksek — operasyonel görünürlük. | 🟡 |
| AF3 | **Kullanıcı impersonation** | Admin "bu kullanıcı gibi gör" ile debug — destek için değerli (audit'e loglanmalı). | Orta | 🟡 |
| AF4 | **Toplu kullanıcı işlemleri** | Users tablosunda çoklu seçim → toplu rol atama / kilitleme / aktifleştirme. | Orta | 🟡 |
| AF5 | **Sistem ayarları UI genişletme** | `system.html` var ama scrape ayarları (kaynak aç/kapa, rate limit, varsayılan concurrency) admin'den yönetilemiyor. | Orta | 🟡 |
| AF6 | **Metrik grafiği** | Kullanıcı büyümesi, günlük scrape sayısı, aktif kullanıcı trendi — Chart.js ile mini grafikler. | Düşük-orta | 🟢 |
| AF7 | **Kullanıcı detay sayfası** | Users listesinden bir kullanıcıya tıklayınca: profili, ilgi alanları, son aktivitesi, oturumları tek ekranda. | Orta | 🟢 |
| AF8 | **Duyuru / bildirim sistemi** | Admin tüm kullanıcılara banner duyuru gönderebilsin ("bakım 22:00'de"). | Düşük | 🟢 |

---

## 3. Çapraz-Kesen (Her İki Panel)

| # | Konu | Açıklama | Öncelik |
|---|------|----------|---------|
| X1 | **Tasarım dili birleştirme** | Kullanıcı paneli flat/modern (`paper-card`), admin paneli klasik Bootstrap (dolu renkli kartlar, tablolar). Ortak bir tasarım-token seti (`theme.css`) ile iki panel aynı dili konuşmalı. | 🔴 |
| X2 | **Bildirim sistemi (in-app)** | Topbar'da 🔔 — "scrape bitti", "yeni makaleler geldi", "admin duyurusu". PROJECT.md'de vardı, henüz yok. | 🟡 |
| X3 | **Global arama kapsamı** | Topbar arama sadece kullanıcı arıyor gibi (search blueprint). Makale/keyword/sayfa da kapsamalı. | 🟡 |
| X4 | **Mobil deneyim** | Hamburger eklendi ama admin tabloları (`users/list`, `audit/list`) mobilde yatay scroll. Kart-görünüm alternatifi. | 🟡 |
| X5 | **Boş-durum tutarlılığı** | Bazı boş durumlar `empty-state` class'ı, bazıları inline card — tek `_empty_state.html` partial'ı. | 🟢 |
| X6 | **Erişilebilirlik (a11y)** | Tab navigasyonu, ARIA rolleri (özellikle profil tab'ları, mode toggle), focus göstergeleri. | 🟡 |
| X7 | **Loading/skeleton durumları** | HTMX istekleri sırasında "Claude'a soruluyor…" dışında görsel feedback az. Skeleton loader'lar. | 🟢 |

---

## 4. Önerilen Yol Haritası (bu havuzdan)

Aşağıdaki sıralama etki/maliyet dengesine göre öneridir — nihai karar senin:

**Öncelikli paket (yüksek etki):**
1. **UF1 — "Makaleye sor" RAG chat** (kullanıcı paneli wow özelliği)
2. **UF2 — Haftalık digest / Insights** (engagement)
3. **X1 — Tasarım dili birleştirme** (admin ↔ user tutarlılık)
4. **U3 — Dinamik onboarding stepper** (kolay, görünür)

**İkincil paket (operasyonel/olgunluk):**
5. **AF2 — Sistem sağlık paneli** (Celery/scrape görünürlük)
6. **A4 + X2 — Audit label sözlüğü + bildirim sistemi**
7. **UF3/UF4 — Bulk actions + okuma listesi**

**Nice-to-have (fırsat buldukça):**
8. UF5–UF10, AF3–AF8, X4–X7

---

## 5. Notlar

- Bu doküman **değerlendirme havuzu**dur; hepsi yapılacak diye bir zorunluluk yok.
- Admin paneli işlevsel olarak sağlam; ana eksik **tasarım dili tutarlılığı** ve **operasyonel görünürlük** (sistem sağlığı).
- Kullanıcı paneli görsel olarak olgun; ana fırsat **AI etkileşimi derinleştirme** (RAG chat + insights).
- Güvenlik/mimari borçları (Task #9, #10, #11) bu dokümanın kapsamı dışında ama frontend işlerinden önce ele alınmalı.
