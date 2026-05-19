# ScrapeMind — Bağlam Dosyası (Claude için)

## Projeye Genel Bakış
ScrapeMind aynı zamanda `flask-core-base` adlı yeniden kullanılabilir bir Flask iskeletidir.
**İki şapka:** `app/core/` = her projede kullanılabilecek çekirdek, `app/modules/` = ScrapeMind'a özel.

## Konum
`D:\app\ScrapeMind`

## Teknoloji
Flask 3.1 · SQLAlchemy 2 · PostgreSQL 17 · Bootstrap 5 + HTMX · Flask-Babel (TR/EN)

## Veritabanı (yerel geliştirme)
- Postgres: `myo_postgres17` Docker container'ı (port 5432, user: postgres, pw: myopassword123, db: scrapemind)
- Redis: `shared_redis` Docker container'ı (port 6379)
- `.env.local` → `.env`'e kopyalanarak aktif edilir

## Kritik Mimari Kurallar
1. `app/core/` asla `app/modules/`'dan import ETMEZ
2. `is_superuser` bypass YALNIZCA `app/core/auth/decorators.py`'deki `permission_required`'da
3. Plugin discovery (`app/modules/__init__.py`) tablo yokken sessizce geçer
4. Migration sırası: `flask db upgrade` → uygulama başlatma (bkz. `docker/entrypoint.sh`)
5. Profil tab'ları genişletilebilir: `app/core/settings/tab_registry.py`

## Klasör Yapısı
```
app/core/          → Auth, RBAC, Menü, Settings, Audit, i18n, UI — dokunma
app/modules/       → ScrapeMind modülleri (scrape, academic, dashboard)
app/tasks/         → Celery task'ları
translations/      → TR + EN .po/.mo dosyaları
scripts/           → seed.py, create_module.py, export_core_template.py
```

## Çeviri İş Akışı
```bash
# Yeni string eklenince:
pybabel extract -F babel.cfg -o translations/messages.pot .
pybabel update -i translations/messages.pot -d translations
# .po dosyalarını düzenle
pybabel compile -d translations
```

## Tamamlanan Faz Durumu (Mayıs 2026)
- **Faz 0** ✅ tam
- **Faz 1** ✅ ~%95 (eksik: email gönderimi, şifre politikası, oturum yönetimi)
- **Faz 2** 🔶 başlanmadı

## Bilinen Kısıtlar / Phase 2 Bekleyenler
- Şifre resetleme tokeni üretiliyor ama email gönderilmiyor (dev'de flash'ta gösteriyor)
- Oturum listesi / revoke yok
- Şifre karmaşıklık kuralı yok (min 8 karakter var)

## Template Olarak Yeni Projede Kullanım
```bash
# 1. GitHub'da "Use this template" → yeni repo
# 2. Kopyaladıktan sonra:
python scripts/export_core_template.py --target . --name yeni_proje_adi
# 3. app/modules/_template/ kullanarak ilk modülü oluştur
python scripts/create_module.py ilk_modul_adi
```
