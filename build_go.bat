@echo off
setlocal
cd /d "%~dp0"
if not exist bin mkdir bin
set GOEXE=%ProgramFiles%\Go\bin\go.exe
if not exist "%GOEXE%" set GOEXE=go
"%GOEXE%" build -p 1 -buildvcs=false -trimpath -ldflags="-s -w" -o bin\interviewkit.exe .
if errorlevel 1 exit /b %errorlevel%
echo Built bin\interviewkit.exe
