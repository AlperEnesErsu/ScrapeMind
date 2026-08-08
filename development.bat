@echo off
title ScrapeMind - Development
echo ScrapeMind Gelistirme Sunucusu Baslatiliyor...

:: Sanal ortami aktif et
call venv\Scripts\activate.bat

:: Proje koku Python path'e ekle
set PYTHONPATH=%~dp0

:: --- Ilk kurulum kontrolleri ---
if not exist venv (
    echo [HATA] venv bulunamadi. Once setup.bat calistir.
    pause & exit /b 1
)
if not exist migrations\versions (
    echo [HATA] migrations klasoru eksik. Once setup.bat calistir.
    pause & exit /b 1
)

:: --- .env secimi ---
:: CMD icinde if blogu icinde parantez kullanamayiz, goto kullaniyoruz
if exist .env.local goto use_local_env
if exist .env goto env_ready
copy .env.example .env >nul
echo [UYARI] .env bulunamadi, .env.example kopyalandi. SECRET_KEY doldurun!
echo         Paylasimli Postgres icin: copy .env.local.example .env.local
goto env_ready

:use_local_env
copy .env.local .env >nul
echo [INFO] .env.local aktif edildi.

:env_ready

:: --- Docker kontrol (.env.local yoksa kendi Docker'ini kullan) ---
if exist .env.local goto skip_docker
docker info >nul 2>&1
if %errorlevel% neq 0 (
    echo [HATA] Docker calismıyor. Docker Desktop'i ac ya da .env.local olustur.
    pause & exit /b 1
)
docker-compose -f docker/docker-compose.yml up -d >nul 2>&1

:skip_docker

:: --- Paket kontrolu (requirements.txt hash ile karsilastir) ---
echo.
echo Paketler kontrol ediliyor...
pip install -r requirements.txt -q --disable-pip-version-check
echo Paketler hazir.

:: --- htmx (10KB alti ise placeholder demektir, gercek kutuphaney indir) ---
for %%F in (app\core\static\js\htmx.min.js) do set HTMX_SIZE=%%~zF
if not defined HTMX_SIZE set HTMX_SIZE=0
if %HTMX_SIZE% LSS 10000 (
    echo.
    echo htmx indiriliyor...
    powershell -Command "Invoke-WebRequest -Uri 'https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js' -OutFile 'app\core\static\js\htmx.min.js'"
    echo htmx indirildi.
)

:: --- Ceviriler ---
flask translate compile >nul 2>&1 || pybabel compile -d translations >nul 2>&1

:: --- Migration ---
echo.
echo Migration kontrol ediliyor...
flask db upgrade
if %errorlevel% neq 0 (
    echo [UYARI] Migration basarisiz.
    echo Cozum: flask db init ^& flask db migrate -m "initial" ^& flask db upgrade
) else (
    echo Migration tamam.
)

:: --- Seed ---
echo.
echo Seed verileri kontrol ediliyor...
python scripts\seed.py

:: --- Worker kontrolu ---
:: Worker olmadan gece taramasi, besleme/kanal yutma, digest ve video ozeti
:: hicbir zaman calismaz -- sessizce. Burada tespit edip sormak, "calistirdim
:: ama hicbir sey olmuyor" sikayetini onlemek icin. Redis/worker kapaliysa
:: probe hata verir, bu da "worker yok" ile ayni yola duser -- uygulamayi
:: asla bekletmemeli (bkz. asagidaki /t 10 /d H).
echo.
echo Worker durumu kontrol ediliyor...
python -c "from app.tasks import celery_app; import sys; sys.exit(0 if celery_app.control.ping(timeout=1) else 1)" >nul 2>&1
if %errorlevel% equ 0 goto worker_running

echo.
echo [UYARI] Calisir durumda bir Celery worker bulunamadi.
echo          Worker olmadan calismayacak ozellikler:
echo            - Gece taramasi
echo            - Besleme/kanal yutma
echo            - Digest e-postasi
echo            - Video transkript ozeti
echo.
:: CMD "if" bloklari parantez icermez, bu yuzden goto stiliyle devam ediyoruz.
choice /c EH /n /t 10 /d H /m "Worker'lari simdi baslatayim mi? [E/h] (10sn sonra hayir)"
if errorlevel 2 goto worker_skip
start worker.bat
goto worker_prompt_done

:worker_skip
echo Sonra baslatmak icin: worker.bat
goto worker_prompt_done

:worker_running
echo Arka plan isleri calisiyor (worker bulundu).

:worker_prompt_done

echo.
echo Sunucu baslatildi!
echo Ana Sayfa   : http://localhost:5000
echo Giris       : http://localhost:5000/auth/login  -- admin / admin1234
echo Arka plan islerini baslatmak icin: worker.bat
echo.
echo Durdurmak icin Ctrl+C basin
echo.

flask run --debug
