@echo off
cd /d "%~dp0"
set GOEXE=%ProgramFiles%\Go\bin\go.exe
if not exist "%GOEXE%" set GOEXE=go
"%GOEXE%" run -buildvcs=false .
