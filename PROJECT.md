# PROJECT.md — ScrapeMind & Reusable Flask Base

> **Proje Adı:** ScrapeMind
> **Base İskelet Adı:** `flask-core-base` (bu proje aynı zamanda gelecek projelerin başlangıç noktası olacak)
> **Ekip:** 3 kişi
> **Doküman Sürümü:** 1.0 (onay sürümü)
> **Son Güncelleme:** Mayıs 2026

---

## 0. Strateji: "Base + Proje" Yaklaşımı

Bu mimari **iki şapkalı** geliştirilecek:

| Şapka | İçerik | Yer |
|---|---|---|
| 🧱 **`flask-core-base`** | Auth, RBAC, Menu, Settings, Audit, i18n, UI shell | `app/core/` |
| 🎯 **ScrapeMind** | Web scraping iş modülleri (proje-özgü) | `app/modules/` |

**Kural:** `app/core/` içindeki hiçbir şey ScrapeMind'a özel olmayacak. Gelecek projede sadece `app/modules/` boşaltılıp yeni modüller eklenerek aynı temel kullanılacak.

**Sonuç:** İlk projeyi bitirdiğinizde elinizde:
- ScrapeMind çalışan uygulaması
- Reusable bir Flask iskeleti (template repo olarak fork edip yeni proje açabilirsiniz)

> **GitHub stratejisi önerisi:** Bu repo'yu **"template repository"** olarak işaretle (Settings → Template repository). Yeni projede "Use this template" deyip bağımsız repo oluşturursun, git history'si temiz başlar.

---

## 1. Onaylanmış Kararlar

| Karar | Seçim | Etki |
|---|---|---|
| Auth | Local + OAuth (Google/Microsoft), strategy pattern | LDAP/JWT sonradan kolayca eklenebilir |
| Yetkilendirme | RBAC + dinamik menü + modül izinleri | Tam dinamik, DB'den yönetilir |
| Modüler yapı | Flask Blueprint + plugin discovery | Yeni modül = yeni klasör |
| **i18n** | **Faz 1'de** | Flask-Babel; TR ve EN baştan |
| Multi-tenant | Hayır | Single tenant — şema basit kalır |
| **Soft delete** | **Standart** | `BaseModel` mixin'de `deleted_at` |
| Frontend | Bootstrap 5 + HTMX | SPA değil, server-rendered |
| **Menü konumu** | **Sol sidebar** (collapsible) | Sabit layout |
| Deploy | Henüz karar yok | Docker hazır, hedef sonra netleşir |

---

## 2. Teknoloji Yığını

| Katman | Seçim | Neden |
|---|---|---|
| Web framework | **Flask 3.x** | Blueprint + plugin discovery |
| ORM | **SQLAlchemy 2.x** (Flask-SQLAlchemy 3.x) | Type-hint, modern API |
| Migration | **Alembic** (Flask-Migrate) | Multi-head merge |
| DB | **PostgreSQL 15+** | JSONB, partial index |
| Auth (local) | **Flask-Login** + **passlib (argon2)** | |
| Auth (OAuth) | **Authlib** | |
| Form | **Flask-WTF** + **WTForms** | |
| **i18n** | **Flask-Babel** | TR/EN — kullanıcı profilden seçebilir |
| Frontend | **Jinja2** + **Bootstrap 5.3** + **HTMX** + **Bootstrap Icons** | Sol sidebar layout |
| Test | **pytest** + **pytest-flask** + **factory-boy** | |
| Lint | **ruff** + **black** + **mypy** | |
| Config | **python-dotenv** | |
| Logging | **structlog** | JSON log |
| Container | **Docker + docker-compose** | |
| Task queue (Faz 2) | **Celery + Redis** | Scraping işleri için kritik |

> **Not (ScrapeMind için ileride):** Scraping kütüphaneleri (Playwright, Scrapy, BeautifulSoup) `app/modules/`'da modüllerin kendi `requirements`'larına yazılacak — core'a sızmayacak.

---

## 3. Klasör Yapısı

```
scrapemind/
├── app/
│   ├── __init__.py              # create_app() factory
│   ├── extensions.py            # db, login_manager, migrate, oauth, babel
│   ├── config.py                # DevConfig, ProdConfig, TestConfig
│   │
│   ├── core/                    # 🧱 BASE — sonraki projelerde değişmeyecek
│   │   ├── __init__.py
│   │   ├── base_model.py        # BaseModel (id, created_at, updated_at, deleted_at)
│   │   ├── soft_delete.py       # SoftDeleteQuery mixin
│   │   ├── models/
│   │   │   ├── user.py
│   │   │   ├── role.py
│   │   │   ├── permission.py
│   │   │   ├── menu.py
│   │   │   ├── module.py
│   │   │   ├── settings.py
│   │   │   ├── audit.py
│   │   │   └── oauth_account.py
│   │   ├── auth/
│   │   │   ├── __init__.py      # auth_bp
│   │   │   ├── strategies/
│   │   │   │   ├── base.py
│   │   │   │   ├── local.py
│   │   │   │   ├── oauth_google.py
│   │   │   │   ├── oauth_microsoft.py
│   │   │   │   ├── ldap.py      # iskelet (Faz 3)
│   │   │   │   └── jwt_api.py   # iskelet (Faz 3)
│   │   │   ├── routes.py
│   │   │   ├── forms.py
│   │   │   └── decorators.py
│   │   ├── rbac/
│   │   │   ├── service.py
│   │   │   └── routes.py
│   │   ├── menu/
│   │   │   ├── builder.py
│   │   │   └── routes.py
│   │   ├── settings/
│   │   │   ├── service.py
│   │   │   └── routes.py
│   │   ├── audit/
│   │   │   ├── middleware.py
│   │   │   └── routes.py
│   │   ├── i18n/                # 🌐 i18n yardımcıları
│   │   │   ├── __init__.py
│   │   │   └── utils.py         # locale_selector, format helpers
│   │   ├── templates/
│   │   │   ├── base.html        # Sol sidebar + topbar layout
│   │   │   ├── _sidebar.html    # Dinamik menü
│   │   │   ├── _topbar.html     # Kullanıcı dropdown, dil seçici, bildirimler
│   │   │   ├── _flash.html
│   │   │   ├── _pagination.html
│   │   │   ├── auth/
│   │   │   ├── rbac/
│   │   │   ├── settings/
│   │   │   └── errors/
│   │   └── static/
│   │       ├── css/theme.css    # CSS variables — light/dark
│   │       ├── js/htmx.min.js
│   │       ├── js/app.js
│   │       └── img/
│   │
│   ├── modules/                 # 🎯 PROJE MODÜLLERİ (ScrapeMind buraya yazılır)
│   │   ├── __init__.py          # plugin discovery
│   │   ├── _template/           # yeni modül için şablon
│   │   ├── dashboard/           # örnek modül (her projede olur)
│   │   └── (ScrapeMind modülleri Faz 2'de eklenecek)
│   │
│   ├── api/v1/                  # JSON API (Faz 2)
│   └── utils/
│
├── translations/                # 🌐 Babel — core çevirileri
│   ├── tr/LC_MESSAGES/messages.po
│   └── en/LC_MESSAGES/messages.po
│
├── migrations/versions/
├── tests/{core,modules}/
├── scripts/{seed.py,create_module.py}
├── docker/{Dockerfile,docker-compose.yml}
├── docs/{PROJECT,MODULE_GUIDE,AUTH_FLOW,DATABASE,I18N_GUIDE,DEPLOYMENT}.md
├── babel.cfg
├── .env.example
├── pyproject.toml
├── requirements.txt
├── README.md
└── wsgi.py
```

---

## 4. Veritabanı Şeması

### 4.1. BaseModel (TÜM modeller miras alır)

```python
# app/core/base_model.py
class BaseModel(db.Model):
    __abstract__ = True

    id = db.Column(db.BigInteger, primary_key=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=db.func.now())
    deleted_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def soft_delete(self):
        self.deleted_at = datetime.utcnow()

    def restore(self):
        self.deleted_at = None
```

**Soft delete query:** Custom query class default'ta `deleted_at IS NULL` filtresi ekler. `User.query.all()` silinmemişleri, `User.all_with_deleted()` hepsini döner.

### 4.2. ER Diyagramı (özet)

```
┌────────────┐     ┌──────────────┐     ┌────────────┐
│   users    │─────│  user_roles  │─────│   roles    │
└─────┬──────┘     └──────────────┘     └─────┬──────┘
      │                                        │
      ├──→ oauth_accounts             ┌────────┴────────┐
      ├──→ user_settings (JSONB)      │ role_permissions│
      ├──→ audit_logs                 └────────┬────────┘
      │                                        │
      │                                  ┌─────┴──────┐
      │                                  │permissions │
      │                                  └────────────┘

┌────────────┐
│  modules   │ ← plugin discovery ile manifest'ten sync
└──────┬─────┘
       │
       ▼
┌─────────────┐     ┌──────────────┐
│ menu_items  │─────│  role_menus  │
└─────────────┘     └──────────────┘
       │
       ▼
  required_permission → permissions
```

### 4.3. Kritik Tablolar (özet)

#### `users`
`id, username UNIQUE, email UNIQUE, password_hash NULL, full_name, avatar_url, is_active, is_locked, is_superuser, failed_login_count, last_login_at, locale (tr/en), timezone, created_at, updated_at, deleted_at`

#### `oauth_accounts`
`id, user_id FK, provider, provider_user_id, email, raw_data JSONB`
UNIQUE(provider, provider_user_id)

#### `roles`, `permissions`, `user_roles`, `role_permissions`
Standart RBAC. İzin formatı: `<modül>.<eylem>` (örn. `scrape.create`, `users.delete`).

#### `modules`
`code PK, name, version, is_enabled, installed_at, settings_schema JSONB`

#### `menu_items`
`id, parent_id, code UNIQUE, label_key (i18n key), icon, url, endpoint, module_code FK, required_permission, order_index, is_visible`

> `label_key` çeviri anahtarıdır (örn. `menu.scrape.list`). Render'da Babel ile çevrilir. Sabit Türkçe yazmıyoruz.

#### `user_settings`
`user_id PK, settings JSONB, updated_at`

Örnek `settings` içeriği:
```json
{
  "theme": "dark",
  "locale": "tr",
  "timezone": "Europe/Istanbul",
  "date_format": "DD.MM.YYYY",
  "sidebar_collapsed": false,
  "notifications": {"email": true, "in_app": true},
  "modules": {
    "scrape": {"default_concurrency": 5, "auto_retry": true}
  }
}
```

#### `system_settings`
`key VARCHAR PK, value JSONB, updated_by FK, updated_at`

#### `audit_logs`
`id, user_id FK, action, entity_type, entity_id, changes JSONB, ip_address INET, user_agent, locale, created_at`

---

## 5. i18n Mimarisi (Faz 1)

### 5.1. Diller
- **Default:** `tr` (Türkçe)
- **Destekli:** `tr`, `en`
- Yeni dil eklemek: `flask translate init <lang>` + çeviri dosyası doldur

### 5.2. Locale Seçim Sırası

```python
def select_locale():
    # 1. URL parametresi: ?lang=en (geçici override)
    if 'lang' in request.args:
        return request.args['lang']
    # 2. Giriş yapmış kullanıcının ayarı (user_settings.locale)
    if current_user.is_authenticated:
        return current_user.locale or 'tr'
    # 3. Browser Accept-Language header
    return request.accept_languages.best_match(['tr', 'en']) or 'tr'
```

### 5.3. Çeviri Dosyaları

```
translations/                          # core çevirileri
├── tr/LC_MESSAGES/messages.po
└── en/LC_MESSAGES/messages.po

app/modules/scrape/translations/       # modüle özel çeviriler
├── tr/LC_MESSAGES/messages.po
└── en/LC_MESSAGES/messages.po
```

Her modül kendi çevirilerini taşır.

### 5.4. Kullanımı

```python
from flask_babel import _
flash(_('Successfully saved'))
```

Template:
```jinja
<h1>{{ _('Welcome %(name)s', name=current_user.full_name) }}</h1>
{{ format_datetime(item.created_at) }}    {# locale'e göre #}
```

---

## 6. UI Layout — Sol Sidebar (Bootstrap 5 + HTMX)

### 6.1. Layout Yapısı

```
┌─────────────────────────────────────────────────────────────┐
│  🧠 ScrapeMind          [arama]     🔔 5    👤 Mustafa ▼   │ ← topbar (sticky)
├──────────────┬──────────────────────────────────────────────┤
│              │                                              │
│ 📊 Dashboard │                                              │
│ ▼ 🕷️ Scrape  │           İÇERİK ALANI                     │
│   Hedefler   │                                              │
│   Görevler   │           (HTMX ile bölümsel update)         │
│   Raporlar   │                                              │
│ 👥 Kullanıcı │                                              │
│ 🔐 Roller    │                                              │
│ ⚙️  Ayarlar  │                                              │
│              │                                              │
│ [<<]         │                                              │ ← collapse button
└──────────────┴──────────────────────────────────────────────┘
```

### 6.2. Sidebar Özellikleri

- **Sticky** sol tarafta sabit
- **Collapsible** — daraltıldığında sadece ikonlar görünür (tercih `user_settings.sidebar_collapsed`'ta)
- **2 seviye menü** (parent + alt menüler, accordion)
- **İzin filtreli** — kullanıcının izni olan öğeler render edilir
- **Aktif rota işaretli**
- **Mobil**: 768px altında off-canvas drawer

### 6.3. Topbar

Logo + uygulama adı, global arama (HTMX live search), bildirim dropdown, dil seçici (TR/EN), kullanıcı dropdown (Profil, Ayarlar, Tema toggle, Çıkış)

### 6.4. Tema

CSS variables ile light/dark:
```css
:root { --bg: #f8f9fa; --text: #212529; --sidebar-bg: #ffffff; }
[data-theme="dark"] { --bg: #1a1a1a; --text: #e0e0e0; --sidebar-bg: #242424; }
```

### 6.5. HTMX Kullanım Senaryoları

Form submit'ler, tablo sayfalama, inline edit, bildirim poll, modal'lar, confirm dialog'lar — hepsi HTMX ile tam sayfa yenilemeden.

---

## 7. Auth — Strategy Pattern

```python
# app/core/auth/strategies/base.py
class AuthStrategy(ABC):
    name: str

    @abstractmethod
    def authenticate(self, credentials: dict) -> User | None: ...

    @abstractmethod
    def get_login_url(self) -> str | None: ...
```

**OAuth → User eşleme:**
1. `oauth_accounts`'ta `(provider, provider_user_id)` ara → varsa giriş
2. Yoksa email eşleşmesi ara → varsa OAuth hesabı bağla
3. Hiç yoksa → `system_settings.oauth_auto_register=true` ise oluştur, değilse hata

LDAP ve JWT için `strategies/ldap.py`, `strategies/jwt_api.py` iskelet dosyaları Faz 1'de oluşturulacak ama implement edilmeyecek — strateji eklemenin ne kadar kolay olduğunu kanıtlamak için.

---

## 8. RBAC + Dinamik Menü

İzin kontrolü:
```python
@bp.route('/scrape/delete/<int:id>')
@permission_required('scrape.delete')
def delete_target(id): ...
```

Menü dinamik:
```python
def build_menu_for_user(user) -> list[MenuNode]:
    user_perms = get_user_permissions(user)  # cached
    items = MenuItem.query.filter_by(is_visible=True).order_by('order_index').all()
    filtered = [m for m in items
                if not m.required_permission or m.required_permission in user_perms]
    return build_tree(filtered)
```

**Süper admin** (`is_superuser=True`) tüm izinleri bypass eder.

**Soft delete + RBAC:**
- Silinmiş user'lar login olamaz
- Silinmiş rol'ler atanamaz
- Admin panelinde "Silinmişleri göster" toggle'ı

---

## 9. Modül Manifest (ScrapeMind örneği)

```python
# app/modules/scrape/manifest.py
MODULE = {
    "code": "scrape",
    "name_key": "module.scrape.name",
    "version": "1.0.0",
    "permissions": [
        {"code": "scrape.view",   "label_key": "perm.scrape.view"},
        {"code": "scrape.create", "label_key": "perm.scrape.create"},
        {"code": "scrape.run",    "label_key": "perm.scrape.run"},
        {"code": "scrape.delete", "label_key": "perm.scrape.delete"},
    ],
    "menu": [
        {
            "code": "scrape_root",
            "label_key": "menu.scrape.root",
            "icon": "bi-bug",
            "order": 20,
        },
        {
            "code": "scrape_targets",
            "parent": "scrape_root",
            "label_key": "menu.scrape.targets",
            "endpoint": "scrape.list_targets",
            "required_permission": "scrape.view",
            "order": 1,
        },
    ],
    "settings_schema": {
        "scrape.default_concurrency": {
            "type": "int", "default": 5, "min": 1, "max": 50,
            "label_key": "settings.scrape.concurrency"
        },
        "scrape.auto_retry": {
            "type": "bool", "default": True,
            "label_key": "settings.scrape.auto_retry"
        },
    },
}
```

Startup'ta `app/modules/__init__.py` tüm klasörleri tarar, manifest'leri DB'ye sync eder (idempotent).

---

## 10. Yol Haritası

### 🟦 Faz 0 — Hazırlık (1-2 gün, hep birlikte)
- [ ] Repo kur, GitHub **template repository** olarak işaretle
- [ ] `pyproject.toml`, `.env.example`, `.pre-commit-config.yaml`
- [ ] Docker-compose: Postgres + Redis (Redis'i Faz 2'de Celery için)
- [ ] CI pipeline (lint + test + migration check)
- [ ] `PROJECT.md`, `MODULE_GUIDE.md`, `I18N_GUIDE.md`, `README.md`

### 🟩 Faz 1 — Çekirdek (~2 hafta, 3 kişi paralel)

**Geliştirici A — Auth & User**
- [ ] `BaseModel` + soft delete mixin
- [ ] `users`, `oauth_accounts` modelleri
- [ ] `AuthStrategy` base + Local
- [ ] Google + Microsoft OAuth
- [ ] Login, logout, register, password-reset, OAuth callback
- [ ] LDAP/JWT iskelet dosyalar (boş)
- [ ] Testler

**Geliştirici B — RBAC, Menü, Plugin Loader**
- [ ] `roles`, `permissions`, `menu_items`, `modules`
- [ ] `@permission_required`
- [ ] Menu builder service
- [ ] Plugin discovery + manifest sync
- [ ] Admin paneli: rol/yetki/menü CRUD (HTMX)
- [ ] `create_module.py` script
- [ ] Testler

**Geliştirici C — Profil, Ayarlar, Audit, UI Shell, i18n**
- [ ] `user_settings`, `system_settings`, `audit_logs`
- [ ] `/profile` sayfası (6 tab, HTMX)
- [ ] Audit middleware
- [ ] **Flask-Babel kurulumu, locale selector, TR+EN core çeviriler**
- [ ] Dil seçici (topbar)
- [ ] `base.html` — sol sidebar, topbar, tema toggle
- [ ] Hata sayfaları (403/404/500)
- [ ] Seed script (ilk admin, default rol, default menü)
- [ ] Testler

### 🟨 Faz 2 — ScrapeMind Modülleri + Altyapı Ekleri
- [ ] Celery + Redis (scraping job queue)
- [ ] 2FA (TOTP)
- [ ] SMTP / email
- [ ] API v1 (JWT)
- [ ] **ScrapeMind iş modülleri** (ayrı `SCRAPEMIND.md`'de planlanacak)

### 🟧 Faz 3 — İleri Auth & Ölçek
- [ ] `LdapAuthStrategy` (gerçek implementation)
- [ ] `JwtApiStrategy` (gerçek implementation)
- [ ] Audit log partition
- [ ] Redis cache (permission, menu)
- [ ] Sentry, Prometheus
- [ ] Deploy hedefini netleştir + production guide

---

## 11. Geliştirme Workflow'u

### 11.1. Git Branch Stratejisi

```
main          ← prod, tagged release
  └── dev     ← entegrasyon
       ├── feature/core-auth          (Geliştirici A)
       ├── feature/core-rbac-menu     (Geliştirici B)
       └── feature/core-profile-i18n  (Geliştirici C)
```

### 11.2. Çakışma Yönetimi

| Alan | Sahibi |
|---|---|
| `app/core/` | 3 kişinin onayı |
| `app/core/<benim_bölümüm>/` | İlgili geliştirici (Faz 1'de) |
| `app/modules/<modül>/` | Modül sahibi |
| `translations/` (core) | 3 kişi düzenleyebilir, **stringler eklenir, silinmez** |
| `migrations/` | Multi-head merge |
| `base.html`, `_sidebar.html` | Sadece UI sahibi (C) |

### 11.3. Migration Çakışması

```bash
flask db merge -m "merge: auth + rbac heads" heads
flask db upgrade
```

### 11.4. CI (her PR'da)

1. `ruff check`
2. `black --check`
3. `mypy app/`
4. `pytest --cov=app --cov-fail-under=70`
5. Migration test (boş DB → upgrade)
6. **Çeviri tutarlılığı** — `tr` ve `en` aynı key set'ine sahip mi?

---

## 12. Güvenlik Checklist

- ✅ Argon2 password hashing
- ✅ CSRF (Flask-WTF)
- ✅ Session: HttpOnly, Secure, SameSite=Lax
- ✅ XSS: Jinja2 autoescape
- ✅ Rate limit (Flask-Limiter)
- ✅ Brute-force: 5 başarısız → 15 dk kilit
- ✅ Audit log her kritik aksiyonda
- ✅ Secrets `.env`, asla commit yok
- ✅ Soft delete → veri silmek yerine işaretle
- ⏳ 2FA (Faz 2)
- ⏳ Password policy (Faz 1 sonu)

---

## 13. Karar Verilecekler (sonraya bırakılan)

1. **Deploy hedefi** — Docker hazır olacağı için her ortama taşınabilir, kararı Faz 1 sonunda
2. **Avatar yükleme** — local storage mı, S3-compatible mı? (Faz 1 sonu)
3. **Email servisi** — SMTP relay mi, SendGrid/Resend gibi servis mi? (Faz 2)
4. **2FA türü** — TOTP / SMS / Email (Faz 2)
5. **ScrapeMind** — scraping kütüphanesi, proxy, scheduling, export formatları (ayrı `SCRAPEMIND.md`)

---

## 14. Onay

Bu doküman aşağıdaki kararlarla onay bekliyor:

- ✅ ScrapeMind = ilk proje, ama `flask-core-base` yeniden kullanılabilir iskelet olarak yapılandırılacak
- ✅ Faz 1'de: Auth (Local+OAuth) + RBAC + Dinamik Menü + Soft Delete + i18n (TR/EN) + Sol Sidebar UI
- ✅ Faz 2'de: Celery, ScrapeMind modülleri, 2FA, Email, API
- ✅ Faz 3'te: LDAP, JWT, Cache, Monitoring

| Kişi | Onay | Tarih |
|---|---|---|
| Mustafa | ☐ | |
| Geliştirici 2 | ☐ | |
| Geliştirici 3 | ☐ | |

---

**Onayladıktan sonra ilk yapılacak:** Faz 0 — repo iskeletini birlikte kurmak. `requirements.txt`, `docker-compose.yml`, `create_app()` factory'sinden başlayacağız.
