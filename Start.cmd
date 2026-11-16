@echo off
setlocal
pushd "%~dp0" || exit /b 1
node scripts\recue.mjs launch %*
set "result=%errorlevel%"
popd
if not "%result%"=="0" pause
exit /b %result%
