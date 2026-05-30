@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "BROWSER_EXE="
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" set "BROWSER_EXE=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not defined BROWSER_EXE if exist "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" set "BROWSER_EXE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if not defined BROWSER_EXE if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "BROWSER_EXE=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined BROWSER_EXE if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "BROWSER_EXE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"

if not defined BROWSER_EXE (
    echo [错误] 未找到 Chrome/Edge。
    pause
    exit /b 1
)

set "TARGET_URL=https://alidocs.dingtalk.com/spreadsheetv2/63AQlVMBbu23N51B/edit?docKey=jP2lRYj8X9o53O8g^&dentryKey=63AQlVMBbu23N51B^&type=s^&onlineEdit=true^&ext=xlsx"
set "PROFILE_DIR=%~dp0output\cdp_browser_profile"
if not exist "%PROFILE_DIR%" mkdir "%PROFILE_DIR%"

del /f /q "%PROFILE_DIR%\SingletonLock" >nul 2>nul
del /f /q "%PROFILE_DIR%\SingletonCookie" >nul 2>nul
del /f /q "%PROFILE_DIR%\SingletonSocket" >nul 2>nul

echo [信息] 正在打开截图浏览器...
echo [信息] 浏览器："%BROWSER_EXE%"
echo [信息] 配置目录："%PROFILE_DIR%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$exe = $env:BROWSER_EXE;" ^
  "$profile = $env:PROFILE_DIR;" ^
  "$url = $env:TARGET_URL;" ^
  "$args = @('--new-window','--remote-debugging-port=18810','--remote-debugging-address=127.0.0.1',('--user-data-dir=' + $profile),'--no-first-run','--no-default-browser-check','--start-maximized',$url);" ^
  "Start-Process -FilePath $exe -ArgumentList $args -WorkingDirectory $pwd.Path | Out-Null"

if errorlevel 1 (
    echo [错误] 启动浏览器失败。
    pause
    exit /b 1
)

echo [信息] 已请求在当前 Windows 桌面会话中打开浏览器。
exit /b 0
