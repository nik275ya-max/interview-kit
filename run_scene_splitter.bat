@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" scene_splitter_app.py
) else (
    python scene_splitter_app.py
)
endlocal
