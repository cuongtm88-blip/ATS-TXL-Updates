@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "UPDATE_REPOSITORY=cuongtm88-blip/ATS-TXL-Updates"
set "GH_EXE="

if not exist ".venv\Scripts\python.exe" (
    echo [LOI] Chua co moi truong build. Hay chay build_windows.bat truoc.
    goto :failed
)

for /f "delims=" %%V in ('.venv\Scripts\python.exe -c "from version import APP_VERSION; print(APP_VERSION)"') do set "APP_VERSION=%%V"
if not defined APP_VERSION (
    echo [LOI] Khong doc duoc APP_VERSION trong version.py
    goto :failed
)

if not exist "dist\ATS-TXL.exe" (
    echo [LOI] Khong tim thay dist\ATS-TXL.exe. Hay chay build_windows.bat truoc.
    goto :failed
)
if not exist "dist\ATS-TXL.exe.sha256" (
    echo [LOI] Khong tim thay dist\ATS-TXL.exe.sha256. Hay chay build_windows.bat lai.
    goto :failed
)
if not exist "RELEASE_NOTES.md" (
    echo [LOI] Khong tim thay RELEASE_NOTES.md.
    goto :failed
)

where gh >nul 2>nul
if not errorlevel 1 set "GH_EXE=gh"
if not defined GH_EXE if exist "%ProgramFiles%\GitHub CLI\gh.exe" set "GH_EXE=%ProgramFiles%\GitHub CLI\gh.exe"
if not defined GH_EXE if exist "%LocalAppData%\Programs\GitHub CLI\gh.exe" set "GH_EXE=%LocalAppData%\Programs\GitHub CLI\gh.exe"

if not defined GH_EXE (
    echo Chua co GitHub CLI. Dang tu dong cai bang winget...
    where winget >nul 2>nul
    if errorlevel 1 (
        echo [LOI] May khong co winget. Hay cai GitHub CLI tu https://cli.github.com/
        goto :failed
    )
    winget install --id GitHub.cli --exact --accept-package-agreements --accept-source-agreements
    if errorlevel 1 goto :failed
    if exist "%ProgramFiles%\GitHub CLI\gh.exe" set "GH_EXE=%ProgramFiles%\GitHub CLI\gh.exe"
    if not defined GH_EXE if exist "%LocalAppData%\Programs\GitHub CLI\gh.exe" set "GH_EXE=%LocalAppData%\Programs\GitHub CLI\gh.exe"
)

if not defined GH_EXE (
    echo [LOI] GitHub CLI da duoc cai nhung chua tim thay. Hay dong cua so va chay lai file nay.
    goto :failed
)

"%GH_EXE%" auth status --hostname github.com >nul 2>nul
if errorlevel 1 (
    echo Lan dau phat hanh can dang nhap GitHub. Trinh duyet se duoc mo de xac nhan.
    "%GH_EXE%" auth login --hostname github.com --web --git-protocol https
    if errorlevel 1 goto :failed
)

"%GH_EXE%" release view "v%APP_VERSION%" --repo "%UPDATE_REPOSITORY%" >nul 2>nul
if not errorlevel 1 (
    echo [LOI] Release v%APP_VERSION% da ton tai.
    echo Hay tang APP_VERSION trong version.py, build lai roi moi phat hanh.
    goto :failed
)

echo.
echo Sap phat hanh ATS TXL v%APP_VERSION% len repository cong khai:
echo https://github.com/%UPDATE_REPOSITORY%
echo Chi hai file EXE va SHA-256 duoc tai len. Token va settings.json khong duoc tai len.
echo.
set /p "CONFIRM=Nhap PHAT HANH de tiep tuc: "
if /I not "%CONFIRM%"=="PHAT HANH" (
    echo Da huy.
    exit /b 0
)

"%GH_EXE%" release create "v%APP_VERSION%" "dist\ATS-TXL.exe#ATS-TXL.exe" "dist\ATS-TXL.exe.sha256#ATS-TXL.exe.sha256" --repo "%UPDATE_REPOSITORY%" --target main --title "ATS TXL v%APP_VERSION%" --notes-file "RELEASE_NOTES.md" --latest
if errorlevel 1 goto :failed

echo.
echo [THANH CONG] Da phat hanh ATS TXL v%APP_VERSION%.
echo Cac may dang chay EXE cu se tu kiem tra cap nhat khi mo ung dung va moi 6 gio.
echo https://github.com/%UPDATE_REPOSITORY%/releases/latest
echo.
pause
exit /b 0

:failed
echo.
echo [THAT BAI] Chua phat hanh ban cap nhat.
pause
exit /b 1
