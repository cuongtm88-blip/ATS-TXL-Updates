@echo off
setlocal EnableExtensions
cd /d "%~dp0"
echo ========================================
echo ATS TXL - Windows build
echo Thu muc build: %CD%
echo ========================================

set "PY_EXE="
set "PY_ARGS="
where py >nul 2>nul
if not errorlevel 1 (
    set "PY_EXE=py"
    set "PY_ARGS=-3"
)
if not defined PY_EXE (
    where python >nul 2>nul
    if not errorlevel 1 set "PY_EXE=python"
)
if not defined PY_EXE (
    echo Khong tim thay Python. Dang tu dong cai Python 3.12...
    where winget >nul 2>nul
    if errorlevel 1 (
        echo Winget khong co san. Dang tai bo cai chinh thuc tu python.org...
        powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile (Join-Path $env:TEMP 'python-3.12.10-amd64.exe')"
        if errorlevel 1 goto :failed
        "%TEMP%\python-3.12.10-amd64.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_pip=1
        if errorlevel 1 goto :failed
    ) else (
        winget install --id Python.Python.3.12 --exact --scope user --accept-package-agreements --accept-source-agreements
        if errorlevel 1 goto :failed
    )

    for %%P in (
        "%LocalAppData%\Programs\Python\Python312\python.exe"
        "%ProgramFiles%\Python312\python.exe"
    ) do (
        if not defined PY_EXE if exist "%%~P" set "PY_EXE=%%~P"
    )
    if not defined PY_EXE (
        where py >nul 2>nul
        if not errorlevel 1 (
            set "PY_EXE=py"
            set "PY_ARGS=-3.12"
        )
    )
)
if not defined PY_EXE goto :python_restart_required
echo Python: %PY_EXE% %PY_ARGS%

if not exist ".venv\Scripts\python.exe" (
    echo Tao moi moi truong Python...
    "%PY_EXE%" %PY_ARGS% -m venv .venv
    if errorlevel 1 goto :failed
)

call ".venv\Scripts\activate.bat"
for /f "delims=" %%V in ('python -c "from version import APP_VERSION; print(APP_VERSION)"') do set "APP_VERSION=%%V"
if not defined APP_VERSION (
    echo [LOI] Khong doc duoc APP_VERSION trong version.py
    goto :failed
)
echo Phien ban: %APP_VERSION%
echo Dang cai cac thu vien cua ATS TXL...
python -m pip install --upgrade pip
if errorlevel 1 goto :failed
python -m pip install -r requirements-build.txt
if errorlevel 1 goto :failed

rem Install Chromium inside the Python package so PyInstaller bundles it into
rem ATS-TXL.exe. The destination computer will not need Chrome or Playwright.
set "PLAYWRIGHT_BROWSERS_PATH=0"
echo Dang cai Chromium tuong thich voi Playwright...
python -m playwright install chromium
if errorlevel 1 goto :failed
echo Dang dong goi ATS-TXL.exe (file co the lon vi kem ca Chromium)...
python -m PyInstaller --noconfirm --clean --onefile --windowed --collect-all playwright --name ATS-TXL app.py
if errorlevel 1 goto :failed

if not exist "dist\ATS-TXL.exe" (
    echo [LOI] PyInstaller khong tao ra dist\ATS-TXL.exe
    goto :failed
)

echo Dang tao ma kiem tra SHA-256...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$hash=(Get-FileHash -Algorithm SHA256 -LiteralPath 'dist\ATS-TXL.exe').Hash.ToLower(); Set-Content -LiteralPath 'dist\ATS-TXL.exe.sha256' -Value ($hash + '  ATS-TXL.exe') -Encoding ascii"
if errorlevel 1 goto :failed
if not exist "dist\ATS-TXL.exe.sha256" (
    echo [LOI] Khong tao duoc dist\ATS-TXL.exe.sha256
    goto :failed
)

echo.
echo [THANH CONG] Cac file da tao cho phien ban %APP_VERSION%:
echo %CD%\dist\ATS-TXL.exe
echo %CD%\dist\ATS-TXL.exe.sha256
echo Chi can chep file ATS-TXL.exe sang may Windows dich lan dau.
echo De phat hanh cap nhat, chay publish_windows_release.bat.
echo Khong can MonitorTXL-1.exe, Python, Chrome hay extension rieng.
echo.
pause
exit /b 0

:python_restart_required
echo.
echo [LOI] Python da duoc cai nhung cua so nay chua tim thay duong dan Python.
echo Hay dong cua so nay va chay lai build_windows.bat mot lan nua.
pause
exit /b 1

:failed
echo.
echo [THAT BAI] Build khong thanh cong. Hay doc loi phia tren.
pause
exit /b 1
