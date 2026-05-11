# 🧠 ScrapeMind

> Akademik makalelerden ve güncel gelişmelerden anlamlı çıkarımlar üreten, kişiselleştirilmiş bir bilgi keşif platformu.

**🔗 Repo:** [github.com/AlperEnesErsu/ScrapeMind](https://github.com/AlperEnesErsu/ScrapeMind)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Status](https://img.shields.io/badge/status-WIP-orange.svg)](https://github.com/AlperEnesErsu/ScrapeMind)

---

## 🎯 Proje Amacı

İnternet her gün milyonlarca yeni içerikle büyüyor; ama gerçekten **anlamlı** olan bilgiye ulaşmak giderek zorlaşıyor. ScrapeMind, kullanıcının ilgi alanlarına göre:

- 📰 **Güncel haberleri** ve sektörel gelişmeleri,
- 📚 **Akademik makaleleri** (arXiv, Semantic Scholar, PubMed vb.),
- 🔍 Bu kaynaklar arasındaki **örüntü, trend ve büyük çıkarımları**

otomatik olarak toplayıp özetler, sınıflandırır ve kullanıcıya günlük bir "bilgi özeti" olarak sunar.

## ✨ Temel Özellikler

- 🕸️ **Modüler scraping motoru** — yeni kaynak eklemek tek bir Python sınıfı yazmak kadar kolay
- 🤖 **LLM tabanlı özetleme** — uzun makaleleri 3-5 cümlede özetler; teknik terimleri açıklar
- 🧭 **Trend tespiti** — embedding tabanlı kümeleme ile günün öne çıkan konularını çıkarır
- 🔔 **Kişiselleştirilmiş feed** — kullanıcı ilgi alanlarına göre filtrelenmiş içerik
- 📅 **Zamanlanmış görevler** — kaynaklar otomatik aralıklarla taranır
- ⚖️ **Etik scraping** — `robots.txt` uyumu, rate limiting, resmi API tercihi

## 🏗️ Mimari

```
┌─────────────┐    ┌──────────────┐    ┌─────────────┐
│  Scrapers   │───▶│  Normalizer  │───▶│  Postgres   │
│ (Scrapy +   │    │  (temizlik,  │    │  (+pgvector)│
│  API'ler)   │    │   dedup)     │    └──────┬──────┘
└─────────────┘    └──────────────┘           │
                                              ▼
                   ┌──────────────┐    ┌─────────────┐
                   │  Frontend    │◀───│  FastAPI    │
                   │  (Next.js)   │    │  + LLM      │
                   └──────────────┘    │  pipeline   │
                                       └─────────────┘
```

## 🧰 Teknoloji Stack'i

| Katman | Araç |
|---|---|
| Scraping | Scrapy, Playwright, httpx |
| Veri kaynakları | arXiv, Semantic Scholar, CrossRef, RSS feeds |
| Özetleme & Çıkarım | Claude API (Anthropic SDK) |
| Embedding | sentence-transformers, FAISS / pgvector |
| Veritabanı | PostgreSQL + pgvector |
| Backend | FastAPI |
| Frontend | Next.js (MVP'de Streamlit) |
| Scheduler | APScheduler / Celery + Redis |
| Container | Docker, docker-compose |

> **Neden Python?** Scraping ekosistemi (Scrapy, Playwright) olgun; üstüne LLM/NLP entegrasyonu (Anthropic SDK, sentence-transformers) tek dilde sorunsuz çalışıyor. JS-heavy siteler için Playwright zaten Python'dan kullanılabildiğinden ayrı bir Node.js servisine ihtiyaç yok.

## 🚀 Kurulum (Geliştirme)

```bash
git clone https://github.com/AlperEnesErsu/ScrapeMind.git
cd ScrapeMind
cp .env.example .env          # API anahtarlarını doldurun
docker-compose up -d          # postgres + redis
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
alembic upgrade head          # DB migration
python -m scrapemind.scheduler     # arka plan görevleri
uvicorn scrapemind.api:app --reload
```

## 📂 Klasör Yapısı (planlanan)

```
ScrapeMind/
├── scrapemind/
│   ├── scrapers/        # Her kaynak için bir spider
│   ├── pipeline/        # Temizlik, dedup, embedding
│   ├── summarizer/      # LLM özetleme katmanı
│   ├── api/             # FastAPI endpoint'leri
│   ├── scheduler/       # Periyodik görevler
│   └── models/          # SQLAlchemy modelleri
├── frontend/            # Next.js uygulaması
├── tests/
├── docker-compose.yml
└── README.md
```

## 🗺️ Yol Haritası

- [ ] **MVP**: arXiv + 2-3 haber kaynağı, basit özetleme, Streamlit arayüzü
- [ ] Kullanıcı hesapları & kişiselleştirme
- [ ] Embedding tabanlı trend tespiti
- [ ] E-mail / Telegram günlük özet bildirimleri
- [ ] Çoklu dil desteği (TR/EN)
- [ ] Mobil uygulama (uzun vadeli)

## ⚖️ Etik & Yasal

ScrapeMind, scraping yaparken:
- Hedef sitelerin `robots.txt` dosyasına uyar
- Sunuculara aşırı yük bindirmemek için rate limit uygular
- Mümkün olan her durumda **resmi API'leri** tercih eder
- Telif hakkıyla korunan içeriği **yeniden yayınlamaz** — yalnızca özetler ve kaynağa yönlendirir

## 👥 Ekip

| İsim | Rol |
|---|---|
| Alper Enes Ersü | Proje sahibi / Backend |
| _eklenecek_ | _eklenecek_ |

## 🤝 Katkı

Bu proje şu anda erken aşamada. Fikir, issue ve PR'ler için açığız — `CONTRIBUTING.md` yakında.

## 📄 Lisans

[MIT](LICENSE)
