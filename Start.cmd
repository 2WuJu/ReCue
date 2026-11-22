@echo off
setlocal
pushd "%~dp0" || exit /b 1
node recue.mjs launch %*
set "result=%errorlevel%"
popd
if not "%result%"=="0" pause
exit /b %result%
