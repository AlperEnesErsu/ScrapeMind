@echo off
title ScrapeMind - Worker + Beat
echo ScrapeMind arka plan gorevleri baslatiliyor (Celery worker + beat)...

:: Sanal ortami aktif et
call venv\Scripts\activate.bat

:: Proje koku Python path'e ekle
set PYTHONPATH=%~dp0

if not exist venv (
    echo [HATA] venv bulunamadi. Once setup.bat calistir.
    pause & exit /b 1
)

:: --- Worker ---
:: -Q celery,io,scrape,llm : "celery" default kuyruktur (yonlendirilmemis
:: gorevler, ornegin core.heartbeat); io/scrape/llm ise app/tasks/__init__.py
:: TASK_ROUTES'daki kuyruklardir. Buradaki liste TASK_ROUTES ile ayni kalmali
:: -- acikca -Q verilen bir worker, listede olmayan bir kuyruktan sessizce
:: hicbir sey tuketmez (hata da vermez). Bu TAM OLARAK bu branch'te daha once
:: canliya giden bir hataydi (bkz. "fix(deploy)" commit'i) -- kuyruk eklersen
:: bu listeyi de guncelle.
:: -P solo : Celery'nin varsayilan "prefork" havuzu Windows'ta calismaz;
:: tek surecli "solo" havuzu kullan.
echo Celery worker baslatiliyor (kuyruklar: celery, io, scrape, llm)...
start "ScrapeMind Worker" cmd /k celery -A app.tasks worker -P solo -Q celery,io,scrape,llm --loglevel=info

:: --- Beat ---
:: Ayri bir surec: BEAT_SCHEDULE'daki zamanlanmis gorevleri tetikler, kendisi
:: hicbir gorevi calistirmaz. TEK REPLIKA olmali -- zamanlama yerel bir
:: dosyada (celerybeat-schedule) tutulur; ikinci bir beat calisirsa ayni
:: gorevleri (gece taramasi, digest, ...) ikinci kez tetikler.
echo Celery beat baslatiliyor (zamanlayici)...
start "ScrapeMind Beat" cmd /k celery -A app.tasks beat --loglevel=info

echo.
echo Worker ve beat ayri pencerelerde baslatildi.
echo Durdurmak icin o pencereleri kapatin ya da icinde Ctrl+C basin.
