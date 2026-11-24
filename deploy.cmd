@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

set "IMAGE_NAME=zzabek/varden:latest"

echo ==^> Sprawdzanie stanu Docker Engine...

:: Sprawdzenie Dockera poza jakimikolwiek blokami IF
docker info >nul 2>&1
if %errorlevel% equ 0 (
    echo ==^> Docker Engine już działa.
    goto :BUILD
)

echo ==^> Docker Engine jest wyłączony. Uruchamianie Docker Desktop...

set "DOCKER_PATH=%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
if exist "%DOCKER_PATH%" (
    start "" "%DOCKER_PATH%"
) else (
    start "" "docker-desktop://"
)

echo ==^> Oczekiwanie na gotowość Docker Engine...
set RETRIES=0
set MAX_RETRIES=30

:WAIT_LOOP
timeout /t 3 /nobreak >nul
set /a RETRIES+=1
echo Oczekiwanie na uruchomienie Dockera... [%RETRIES%/%MAX_RETRIES%]

docker info >nul 2>&1
if %errorlevel% equ 0 goto :DOCKER_READY

if %RETRIES% lss %MAX_RETRIES% goto :WAIT_LOOP

echo.
echo [BŁĄD] Przekroczono limit czasu oczekiwania na uruchomienie Dockera.
goto :END

:DOCKER_READY
echo ==^> Docker Engine jest gotowy!

:: Docker używa cache; zmiana źródeł lub zależności automatycznie unieważnia
:: odpowiedni etap i uruchamia ponowną kompilację.

:BUILD
set "PICOLIMBO_TAG=latest"
set "PICOLIMBO_URL="
for /f "usebackq delims=" %%I in (`curl.exe -fsSI -o NUL -w "%%{redirect_url}" "https://github.com/Quozul/PicoLimbo/releases/latest" 2^>nul`) do (
    set "PICOLIMBO_URL=%%I"
)
if defined PICOLIMBO_URL (
    set "CANDIDATE=!PICOLIMBO_URL:*/releases/tag/=!"
    if not "!CANDIDATE!"=="!PICOLIMBO_URL!" if not "!CANDIDATE!"=="" (
        set "PICOLIMBO_TAG=!CANDIDATE!"
    )
)
set "IMAGE_VERSION=1.0.0"
for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "if ((Get-Content -Raw 'backend\app\config.py') -match '(?m)^VERSION\s*=\s*\x22([^\x22]+)\x22') { $Matches[1] }" 2^>nul`) do (
    if not "%%I"=="" set "IMAGE_VERSION=%%I"
)
set "IMAGE_REVISION="
for /f "usebackq delims=" %%I in (`git rev-parse HEAD 2^>nul`) do (
    set "IMAGE_REVISION=%%I"
)
set "IMAGE_CREATED="
for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "[DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')" 2^>nul`) do (
    set "IMAGE_CREATED=%%I"
)
echo ==^> Budowanie obrazu: %IMAGE_NAME% (v!IMAGE_VERSION!, PicoLimbo: !PICOLIMBO_TAG!)...
docker build ^
  --build-arg "PICOLIMBO_TAG=!PICOLIMBO_TAG!" ^
  --build-arg "IMAGE_VERSION=!IMAGE_VERSION!" ^
  --build-arg "IMAGE_REVISION=!IMAGE_REVISION!" ^
  --build-arg "IMAGE_CREATED=!IMAGE_CREATED!" ^
  -t "%IMAGE_NAME%" .

if %errorlevel% neq 0 (
    echo [BŁĄD] Wystąpił błąd podczas budowania obrazu.
    goto :END
)

echo ==^> Wysyłanie obrazu na Docker Hub...
docker push "%IMAGE_NAME%"

if %errorlevel% neq 0 (
    echo [BŁĄD] Błąd podczas wysyłania obrazu na Docker Hub. Upewnij się, że jesteś zalogowany przez docker login.
    goto :END
)

echo ==^> Sukces! Obraz %IMAGE_NAME% został pomyślnie wysłany.

:END
echo.
echo Nacisnij dowolny klawisz, aby zamknąć to okno...
pause >nul
