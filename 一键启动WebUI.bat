@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "WEBUI_URL=http://127.0.0.1:8787"
set "HOST=127.0.0.1"
set "PORT=8787"
set "PY_CMD="

where python >nul 2>nul
if not errorlevel 1 set "PY_CMD=python"
if not defined PY_CMD (
    where py >nul 2>nul
    if not errorlevel 1 set "PY_CMD=py -3"
)

if not defined PY_CMD (
    echo [错误] 未找到 Python，请先安装 Python 并确保已加入 PATH。
    pause
    exit /b 1
)

echo [信息] 检查 WebUI 是否已在运行...
powershell -NoProfile -Command "try { $c = New-Object Net.Sockets.TcpClient; $iar = $c.BeginConnect('%HOST%', %PORT%, $null, $null); $ok = $iar.AsyncWaitHandle.WaitOne(600); if($ok -and $c.Connected){ $c.EndConnect($iar); exit 0 } else { try { $c.Close() } catch {}; exit 1 } } catch { exit 1 }" >nul 2>nul
if not errorlevel 1 (
    echo [信息] WebUI 已在运行，直接打开页面：%WEBUI_URL%
    start "" "%WEBUI_URL%"
    exit /b 0
)

echo [信息] 正在启动 WebUI 服务...
start "DingtalkChatbot WebUI" cmd /c "cd /d "%~dp0" && %PY_CMD% app\webui_server.py"

echo [信息] 等待 WebUI 就绪...
set /a WAIT_COUNT=0
:wait_loop
powershell -NoProfile -Command "try { $c = New-Object Net.Sockets.TcpClient; $iar = $c.BeginConnect('%HOST%', %PORT%, $null, $null); $ok = $iar.AsyncWaitHandle.WaitOne(800); if($ok -and $c.Connected){ $c.EndConnect($iar); exit 0 } else { try { $c.Close() } catch {}; exit 1 } } catch { exit 1 }" >nul 2>nul
if not errorlevel 1 goto open_ui
set /a WAIT_COUNT+=1
if !WAIT_COUNT! GEQ 20 goto timeout_open
timeout /t 1 /nobreak >nul
goto wait_loop

:open_ui
echo [信息] WebUI 已启动，正在打开：%WEBUI_URL%
start "" "%WEBUI_URL%"
exit /b 0

:timeout_open
echo [提示] WebUI 启动较慢，已尝试启动服务。你可以稍后手动打开：%WEBUI_URL%
start "" "%WEBUI_URL%"
exit /b 0
