@echo off
REM ============================================================
REM  Сборка InterviewKit в переносимый exe (PyInstaller --onedir)
REM ============================================================
REM  Требования:
REM    - pip install -r requirements.txt
REM    - ffmpeg.exe положить в bin\ffmpeg.exe (или рядом с проектом)
REM  Перед первой сборкой рекомендуется один раз запустить программу
REM  из исходников, чтобы модель Whisper скачалась и кэшировалась.
REM ============================================================

setlocal

REM Путь к ffmpeg.exe (укажите свой при необходимости)
set FFMPEG=bin\ffmpeg.exe
if not exist "%FFMPEG%" (
    if exist "..\bin\ffmpeg.exe" set FFMPEG=..\bin\ffmpeg.exe
)

echo [1/3] Установка зависимостей...
pip install -r requirements.txt

echo [2/3] Проверка ffmpeg...
if not exist "%FFMPEG%" (
    echo ВНИМАНИЕ: ffmpeg.exe не найден в %FFMPEG%
    echo Положите ffmpeg.exe в bin\ и повторите, либо он будет взят из PATH.
    set FFMPEG=
)

echo [3/3] Сборка PyInstaller...
if defined FFMPEG (
    pyinstaller --noconfirm --onedir --windowed ^
        --name interview_app ^
        --add-binary "%FFMPEG%;." ^
        --add-data "interruptions;interruptions" ^
        --add-data "config.example.json;." ^
        --hidden-import faster_whisper ^
        --collect-all faster_whisper ^
        interview_app.py
) else (
    pyinstaller --noconfirm --onedir --windowed ^
        --name interview_app ^
        --add-data "interruptions;interruptions" ^
        --add-data "config.example.json;." ^
        --hidden-import faster_whisper ^
        --collect-all faster_whisper ^
        interview_app.py
)

echo.
echo Готово. Переносимая папка: dist\interview_app\
echo Скопируйте туда же папку models\ с моделью Whisper для офлайн-работы.
echo Для работы: рядом с interview_app.exe должны быть папки interruptions\, incoming\, interviews\, models\
endlocal
pause
