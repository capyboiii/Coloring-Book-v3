@echo off
REM ============================================================
REM  Cai MonkeyX vao TUNG profile ma GeminiPool su dung
REM  (.chrome-account1, .chrome-account2) va bat "Allow user scripts".
REM
REM  VI SAO PHAI LAM TAY - hai thu deu khong tu dong hoa duoc:
REM
REM  1) --load-extension DA BI CHROME VO HIEU HOA tu ban 137.
REM     Do tren Chrome 151: extension khong vao extensions.settings,
REM     ke ca khi them --enable-unsafe-extension-debugging hay
REM     --disable-features=DisableLoadExtensionCommandLineSwitch.
REM     => Bat buoc dung "Load unpacked" trong chrome://extensions.
REM
REM  2) Cong tac "Allow user scripts" nam trong Secure Preferences co ky
REM     MAC. Sua bang code se bi Chrome coi la gia mao va reset.
REM
REM  Bam 1 lan cho moi profile, xong la vinh vien. Chrome KHONG copy file
REM  vao profile, no chi ghi duong dan tuyet doi -> DUNG DOI CHO thu muc
REM  extension ve sau, doi la ID doi va phai lam lai tu dau.
REM ============================================================

setlocal
set "EXT=C:\Users\Admin\Downloads\monkeyx_1"
set "ROOT=%~dp0"

if not exist "%EXT%\manifest.json" (
    echo  [!] Khong thay "%EXT%\manifest.json"
    echo      Sua bien EXT trong file nay cho khop voi browser.extension_dir trong config.yaml
    pause
    exit /b 1
)

set CHROME=
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"

if "%CHROME%"=="" (
    echo  [!] Khong tim thay chrome.exe. Sua duong dan trong file nay.
    pause
    exit /b 1
)

tasklist /FI "IMAGENAME eq chrome.exe" 2>NUL | find /I "chrome.exe" >NUL
if not errorlevel 1 (
    echo.
    echo  [!] Chrome dang chay. DONG HET cua so Chrome roi chay lai file nay.
    echo      Profile bi khoa thi pool se nhan ban sang .chrome-account1-p1
    echo      va ban se cai extension nham cho.
    echo.
    pause
    exit /b 1
)

echo.
echo  Hai duong dan se can dan vao o "Folder"/"File name":
echo    Load unpacked  ^>  %EXT%
echo    Nhap userscript ^>  %ROOT%static\gemini-sender.user.js
echo.
pause

call :setup ".chrome-account1"
call :setup ".chrome-account2"

echo.
echo  ============================================================
echo   Xong. Kiem chung: chay lai file nay, mo popup MonkeyX
echo   phai thay CHAM XANH. Cham do = chua bat "Allow user scripts",
echo   extension chay nhung KHONG script nao duoc dang ky.
echo  ============================================================
pause
exit /b 0


:setup
set "PROFILE=%ROOT%%~1"
if not exist "%PROFILE%" mkdir "%PROFILE%"

echo.
echo  ------------------------------------------------------------
echo   Profile: %~1
echo  ------------------------------------------------------------
echo   TAB 1 (chrome://extensions):
echo     1. Bat "Developer mode" (cong tac goc tren ben phai)
echo     2. Bam "Load unpacked" -^> chon thu muc:
echo          %EXT%
echo     3. Tren the "MonkeyX - Userscript Manager" bam "Details"
echo     4. Keo xuong, BAT cong tac "Allow user scripts"
echo     5. TAT het cac extension khac (Tampermonkey, Grammarly, McAfee...)
echo        - chi chua MonkeyX. Grammarly bam vao dung o nhap cua Gemini.
echo.
echo   TAB 2 (MonkeyX options) - cai userscript:
echo     6. Bam nut "Nhap" (cot trai, duoi cung)
echo     7. Chon file:
echo          %ROOT%static\gemini-sender.user.js
echo     8. Phai thay dong xanh "Da nhap. Tong cong 1 script."
echo        va "Gemini Sender (Coloring Book)" hien o danh sach ben trai
echo.
echo     9. DONG cua so Chrome de chuyen sang profile tiep theo
echo.
echo   (KHONG dung duong http://127.0.0.1:8000/....user.js nua: Chrome chan
echo    thang moi dieu huong toi URL ket thuc bang .user.js, trang ve trang
echo    va banner cai dat cua MonkeyX khong bao gio hien.)
echo.

start "" /wait "%CHROME%" ^
    --user-data-dir="%PROFILE%" ^
    --no-first-run ^
    --no-default-browser-check ^
    "chrome://extensions" ^
    "chrome-extension://kecpbakjaphdcpdghedhcjbnlckhaafh/options.html"

echo   Da dong profile %~1.
exit /b 0
