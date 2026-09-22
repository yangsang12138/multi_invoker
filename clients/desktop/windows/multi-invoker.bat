@echo off
REM 桌面客户端启动器（Windows，模板）
REM
REM 放在 dist\ 目录下，和 multi-invoker.pyz 同级即可双击运行。
REM 需要先装 Python 3.9+（在「添加 PATH」勾选的情况下 python 命令才可用）。
REM
REM 行为：起核心 → 等就绪 → 用默认浏览器打开 → 关掉黑窗口就结束核心。

setlocal
set "SCRIPT_DIR=%~dp0"
set "CORE=%SCRIPT_DIR%multi-invoker.pyz"
set "PORT=8765"
if not "%MULTI_INVOKER_DESKTOP_PORT%"=="" set "PORT=%MULTI_INVOKER_DESKTOP_PORT%"
set "HOME_URL=http://127.0.0.1:%PORT%/"
set "LOG=%SCRIPT_DIR%multi-invoker.log"

if not exist "%CORE%" (
  echo 找不到核心程序：%CORE%
  echo 请先运行：python tools\build_single_file.py
  pause
  exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
  echo 未找到 python，请先安装 Python 3.9 及以上版本并加入 PATH。
  pause
  exit /b 1
)

echo 正在启动 Multi-Service Invoker（端口 %PORT%）…
start "multi-invoker-core" /min cmd /c "python "%CORE%" serve --port %PORT% --no-open >> "%LOG%" 2>&1"

REM 等核心就绪；最多等 20 秒
for /l %%i in (1,1,40) do (
  powershell -NoProfile -Command "try{ (Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 '%HOME_URL%api/health').Content -match 'ok' | Out-Null; exit 0 } catch { exit 1 }" >nul 2>nul
  if not errorlevel 1 goto ready
  timeout /t 1 /nobreak >nul
)

echo 核心启动失败，日志：%LOG%
pause
exit /b 1

:ready
start "" "%HOME_URL%"
echo 核心已启动。关闭这个窗口即停止服务。
pause >nul
taskkill /f /im python.exe /fi "WINDOWTITLE eq multi-invoker-core*" >nul 2>nul
endlocal
