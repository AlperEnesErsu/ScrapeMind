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

:: --- Paket kontrolu ---
echo.
echo Paketler kontrol ediliyor...
python -c "import flask, sqlalchemy, flask_login, flask_migrate, flask_babel, flask_wtf, flask_limiter, authlib, passlib, structlog, dotenv" 2>nul
if %errorlevel% neq 0 (
    echo Eksik paketler var, yukleniyor...
    pip install -r requirements.txt
) else (
    echo Tum paketler yuklu.
)

:: --- htmx ---
if not exist app\core\static\js\htmx.min.js (
    echo.
    echo htmx indiriliyor...
    curl -sL -o app\core\static\js\htmx.min.js https://unpkg.com/htmx.org/dist/htmx.min.js
    echo htmx indirildi.
)

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

echo.
echo Sunucu baslatildi!
echo Ana Sayfa   : http://localhost:5000
echo Giris       : http://localhost:5000/auth/login  -- admin / admin1234
echo.
echo Durdurmak icin Ctrl+C basin
echo.

flask run --debug
