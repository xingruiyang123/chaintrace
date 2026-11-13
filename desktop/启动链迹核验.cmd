@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 本机若存在 ELECTRON_RUN_AS_NODE=1（部分宿主/工具会注入），Electron 会以纯 Node 模式启动并报
rem "Cannot read properties of undefined (reading 'handle')"。启动前必须清掉。
set ELECTRON_RUN_AS_NODE=
set TEMP=%~dp0.tmp
set TMP=%~dp0.tmp
if not exist "%~dp0.tmp" mkdir "%~dp0.tmp"
if not exist "%~dp0node_modules\electron\dist\electron.exe" (
  echo [错误] 找不到 Electron 运行时：node_modules\electron\dist\electron.exe
  echo         请先在 desktop 目录执行 npm install。
  pause
  exit /b 1
)
if not exist "%~dp0dist\index.html" (
  echo [提示] dist 不存在，正在构建...
  call npm run build || (echo [错误] 构建失败 & pause & exit /b 1)
)
start "" "%~dp0node_modules\electron\dist\electron.exe" --no-sandbox --disable-gpu .
exit /b 0
