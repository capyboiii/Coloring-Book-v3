@echo off
REM ============================================================
REM  Mo Chrome THAT cua ban voi cong debug de Playwright gan vao.
REM  Dung profile that -> Gemini khong chan, khong bi trang trang.
REM
REM  CACH DUNG:
REM    1. DONG HET cua so Chrome dang mo (bat buoc)
REM    2. Double-click file nay
REM    3. Dang nhap Google, doi thay giao dien chat Gemini
REM    4. GIU CUA SO DO MO, mo terminal khac va chay:
REM         python test_gemini.py --cdp
REM ============================================================

set PORT=9333
set PROFILE=%~dp0.chrome-cdp-profile

set CHROME=
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"

if "%CHROME%"=="" (
    echo  [!] Khong tim thay chrome.exe. Sua duong dan trong file nay.
    pause
    exit /b 1
)

echo Dang mo Chrome tren cong Debug %PORT% ...
start "" "%CHROME%" --remote-debugging-port=%PORT% --user-data-dir="%PROFILE%" "https://gemini.google.com/app"

echo.
echo  Chrome da mo. Dang nhap Google, doi den khi thay giao dien chat Gemini.
echo  Sau do GIU CUA SO NAY MO va chay o terminal khac:
echo.
echo      python test_gemini.py --cdp
echo.
pause
