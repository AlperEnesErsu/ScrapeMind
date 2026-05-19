@echo off
title ScrapeMind - Ilk Kurulum
echo ================================================
echo  ScrapeMind - Ilk Kurulum
echo ================================================
echo.

:: --- Python ---
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [HATA] Python bulunamadi. https://python.org adresinden Python 3.11+ yukle.
    pause & exit /b 1
)

:: --- Docker ---
docker info >nul 2>&1
if %errorlevel% neq 0 (
    echo [HATA] Docker calismıyor. Docker Desktop'i ac ve tekrar dene.
    pause & exit /b 1
)

:: --- Sanal ortam ---
echo [1/7] Sanal ortam olusturuluyor...
if not exist venv (
    python -m venv venv
    echo       venv olusturuldu.
) else (
    echo       venv zaten mevcut.
)
call venv\Scripts\activate.bat
set PYTHONPATH=%~dp0

:: --- Paketler ---
echo.
echo [2/7] Paketler yukleniyor...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [HATA] Paket yuklemesi basarisiz.
    pause & exit /b 1
)

:: --- .env ---
echo.
echo [3/7] .env dosyasi hazirlaniyor...
if exist .env goto env_exists
copy .env.example .env >nul
echo       .env olusturuldu.
echo.
echo [!] .env icindeki SECRET_KEY degerini degistir, kaydet ve bu pencereye don.
notepad .env
echo.
pause

:env_exists
echo       .env hazir.

:: --- Docker servisleri ---
echo.
echo [4/7] Postgres ve Redis baslatiliyor...
docker-compose -f docker/docker-compose.yml up -d
if %errorlevel% neq 0 (
    echo [HATA] Docker servisleri baslatilamaidi.
    pause & exit /b 1
)

echo       Postgres hazir olana kadar bekleniyor...
:wait_db
docker exec scrapemind-db-1 pg_isready -U scrapemind >nul 2>&1
if %errorlevel% neq 0 (
    timeout /t 2 /nobreak >nul
    goto wait_db
)
echo       Postgres hazir.

:: --- Migration ---
echo.
echo [5/7] Migration baslatiliyor...
if not exist migrations\versions (
    flask db init
    flask db migrate -m "initial"
)
flask db upgrade
if %errorlevel% neq 0 (
    echo [HATA] Migration basarisiz.
    pause & exit /b 1
)

:: --- Seed ---
echo.
echo [6/7] Seed verileri yukleniyor...
python scripts\seed.py

:: --- htmx (boyut kontrolu ile) ---
echo.
echo [7/7] htmx kontrol ediliyor...
for %%F in (app\core\static\js\htmx.min.js) do set HTMX_SIZE=%%~zF
if not defined HTMX_SIZE set HTMX_SIZE=0
if %HTMX_SIZE% LSS 10000 (
    powershell -Command "Invoke-WebRequest -Uri 'https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js' -OutFile 'app\core\static\js\htmx.min.js'"
    echo       htmx indirildi.
) else (
    echo       htmx mevcut.
)

echo.
echo ================================================
echo  Kurulum tamamlandi!
echo  Bundan sonra her gun: development.bat
echo.
echo  Adres : http://localhost:5000/auth/login
echo  Giris : admin / admin1234
echo ================================================
pause
