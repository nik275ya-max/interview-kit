"""Разрешение путей к ресурсам программы (ffmpeg, модели, папки).

Корректно работает как при запуске из исходников, так и в собранном
PyInstaller --onedir exe.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def app_root() -> Path:
    """Корневая папка приложения — рядом с interview_app.exe или interview_app.py."""
    if getattr(sys, "frozen", False):
        # PyInstaller --onedir: sys.executable = .../dist/interview_app/interview_app.exe
        return Path(sys.executable).resolve().parent
    # Из исходников: core/paths.py -> поднимаемся на уровень выше
    return Path(__file__).resolve().parent.parent


def bundle_root() -> Path:
    """Папка с зашитыми данными (ffmpeg.exe и т.д.). При --onedir совпадает с app_root."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return app_root()


def ffmpeg_path() -> str:
    """Путь к ffmpeg.exe. Сначала ищем зашитый бинарь, потом системный."""
    candidates = [
        app_root() / "ffmpeg.exe",
        app_root() / "bin" / "ffmpeg.exe",
        bundle_root() / "ffmpeg.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return "ffmpeg"  # системный PATH


def models_dir() -> Path:
    return app_root() / "models"


def interruptions_dir() -> Path:
    return app_root() / "interruptions"


def incoming_dir() -> Path:
    return app_root() / "incoming"


def interviews_dir() -> Path:
    return app_root() / "interviews"


def config_path() -> Path:
    return app_root() / "config.json"


def keywords_path() -> Path:
    return app_root() / "keywords.json"


def ensure_dirs() -> None:
    """Создаёт рабочие папки, если их ещё нет."""
    for d in (interruptions_dir(), incoming_dir(), interviews_dir(), models_dir()):
        d.mkdir(parents=True, exist_ok=True)
