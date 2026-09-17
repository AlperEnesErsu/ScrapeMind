@echo off
title ScrapeMind - Worker + Beat
echo ScrapeMind arka plan gorevleri baslatiliyor (Celery worker + beat)...

:: --- Sanal ortam ---
:: SCRAPEMIND_VENV tanimliysa o kullanilir (ornegin repo disinda kurulmus bir
:: Python 3.11 ortami), degilse proje icindeki venv. Aktif etmeden ONCE varligi
:: kontrol edilir: yoksa "call" sessizce hicbir sey yapmaz ve asagidaki python,
:: pip ve flask PATH'teki sistem Python'una gider -- paketler oraya kurulur.
if not defined SCRAPEMIND_VENV set "SCRAPEMIND_VENV=%~dp0venv"
if not exist "%SCRAPEMIND_VENV%\Scripts\activate.bat" goto venv_missing
call "%SCRAPEMIND_VENV%\Scripts\activate.bat"
goto venv_active

:venv_missing
echo [HATA] Sanal ortam bulunamadi: %SCRAPEMIND_VENV%
echo        Once setup.bat calistir.
pause & exit /b 1

:venv_active
:: CI ve Docker imaji Python 3.11. Farkli bir surumle mypy farkli sayar ve
:: scripts\mypy_ratchet.py yerelde sebepsiz yere duser. Engellemez, uyarir.
python -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)" >nul 2>&1
if %errorlevel% neq 0 echo [UYARI] Sanal ortamdaki Python 3.11 degil. CI ve Docker 3.11 kullaniyor; setup.bat ile yeniden kur.

:: Proje koku Python path'e ekle
set PYTHONPATH=%~dp0

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
