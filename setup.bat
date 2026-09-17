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
:: Python 3.11 ile kurulur, varsayilan "python" ile degil: CI ve Docker imaji
:: 3.11. Bu ayrim 2026 Eylul'e kadar yoktu ve venv 3.14 ile kuruluyordu. Yerel
:: mypy sayisinin CI'dan farkli cikmasi ise surumden degil requirements.txt'ten
:: kayan paketlerden (PRELAUNCH O7); scripts\mypy_ratchet.py onlari uyarir.
:: SCRAPEMIND_VENV tanimliysa venv oraya kurulur.
echo [1/7] Sanal ortam olusturuluyor...
if not defined SCRAPEMIND_VENV set "SCRAPEMIND_VENV=%~dp0venv"
if exist "%SCRAPEMIND_VENV%\Scripts\activate.bat" goto venv_exists

where uv >nul 2>&1
if %errorlevel% equ 0 goto venv_with_uv
py -3.11 --version >nul 2>&1
if %errorlevel% equ 0 goto venv_with_py
echo [HATA] Python 3.11 bulunamadi. "uv python install 3.11" ya da python.org uzerinden 3.11 kur.
pause & exit /b 1

:venv_with_uv
:: --seed: uv venv'e varsayilan olarak pip koymaz. pip olmazsa asagidaki
:: "pip install" PATH'teki sistem pip'ine gider ve paketleri oraya kurar.
uv venv --seed --python 3.11 "%SCRAPEMIND_VENV%"
goto venv_created

:venv_with_py
py -3.11 -m venv "%SCRAPEMIND_VENV%"

:venv_created
if not exist "%SCRAPEMIND_VENV%\Scripts\activate.bat" goto venv_failed
echo       venv olusturuldu (Python 3.11): %SCRAPEMIND_VENV%
goto venv_ready

:venv_failed
echo [HATA] Sanal ortam olusturulamadi: %SCRAPEMIND_VENV%
pause & exit /b 1

:venv_exists
echo       venv zaten mevcut: %SCRAPEMIND_VENV%

:venv_ready
call "%SCRAPEMIND_VENV%\Scripts\activate.bat"
python -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)" >nul 2>&1
if %errorlevel% neq 0 echo [UYARI] Mevcut venv Python 3.11 degil. Silip setup.bat'i yeniden calistir.
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
