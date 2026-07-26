# ScrapeMind — UI/UX İyileştirme ve Özellik Yol Haritası

> **Tarih:** Temmuz 2026 · **Kapsam:** Admin paneli + Kullanıcı (araştırmacı) paneli
> **Amaç:** Genel tarama sonucu tespit edilen tasarımsal iyileştirmeler ve eklenmesi değerli özelliklerin önceliklendirilmiş listesi.
> Görev dağılımı yapılırken bu dosyadaki madde numaraları referans alınabilir (örn. "U-3'ü ben alıyorum").

---

## Mevcut Durum Özeti (Temmuz 2026)

### Kullanıcı paneli — büyük oranda tamam ✅
| Alan | Durum |
|------|-------|
| Ana sayfa (`/`) | ✅ Premium redesign: stats kartları, stepper onboarding, interest manager, trending topics |
| Discover feed (`/papers/`) | ✅ Kart tasarımı, kaynak badge, keyword pill, quick actions (favori/not/PDF/gizle), arama (`?q=`) |
| Paper detail | ✅ 3-mod (Orijinal/Türkçe/AI Analiz), notes paneli, BibTeX "Atıf" butonu |
| Kütüphane (`/library/`) | ✅ Timeline + Favoriler + Notlar + Gizlenenler tab'ları |
| Notlar | ✅ Ekle/düzenle/sil, 4 etiket tipi, Cmd+Enter |
| AI servisi | ✅ Claude analiz + TR çeviri, cache'li (`ANTHROPIC_API_KEY` gate'li) |
| Scrape | ✅ Manuel tetikleme + HTMX task polling (`/papers/status/<task_id>`) |
| 2FA | ✅ TOTP + recovery codes |
| Mobile | ✅ Hamburger toggle + off-canvas sidebar |

### Admin paneli — işlevsel ama tasarımsal olarak eski nesil 🟡
| Alan | Durum |
|------|-------|
| Admin overview | ✅ Split multi-pane, metrik kartları, top keywords |
| Kullanıcı yönetimi | ✅ Liste + arama + create paneli (güçlü şifre validasyonu) |
| RBAC | *(kayıt burada kesilmiş — aşağıya bak)* |

---

> ⚠️ **Bu dosya eksik.** Yukarıdaki tablo "RBAC" satırının ortasında kesilmiş ve
> maddelendirilmiş punch list (U-1, U-2, … / admin maddeleri) hiç yazılmamış — dosyanın
> başındaki "madde numaraları referans alınabilir" cümlesinin işaret ettiği liste yok.
>
> Devralan geliştirici için: bu dosyayı **yeniden oluşturmak, kurtarmaya çalışmaktan
> daha hızlı** olacaktır. Güncel UI durumu için [docs/UI_REVIEW.md](docs/UI_REVIEW.md),
> öncelikli teknik iş listesi için [docs/HANDOVER.md §5](docs/HANDOVER.md) zaten güncel —
> bu dosya yalnızca UI/UX punch list'i için gerekiyor. 