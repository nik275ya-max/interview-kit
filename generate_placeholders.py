"""Генератор тестовых видео-перебивок (текстовые карточки на чёрном фоне).

Создаёт по одному placeholder-видео в общей папке interruptions/ (все
перебивки лежат в одной папке, без категорий-подпапок). Полезно для проверки
пайплайна, пока нет настоящих мемов.

Перебивки: 1920x1080, 3 сек, 30 fps, со звуком (тишина) — полностью
совместимы с пайплайном монтажа. Кириллица рендерится через arial.ttf.

Запуск:
    .venv\\Scripts\\python.exe generate_placeholders.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from core.paths import ffmpeg_path, interruptions_dir

# Имя файла (без расширения) -> русская фраза на карточке.
# Имя файла — это то, по чему нейросеть выбирает перебивку, поэтому описательное.
# Без эмодзи (drawtext + arial.ttf их не отрисует цветом).
PHRASES = {
    "lyubov_zla": "Любовь зла...",
    "romantika": "Романтика!",
    "gorko": "ГОРЬКО!",
    "zdorovye": "Za здоровье!",
    "tantsuyut_vse": "Танцуют все!",
    "deneg_reshayut": "Деньги решают!",
    "nu Vy_dayote": "Ну вы даёте!",
    "semya_svyatoe": "Семья — святое",
    "pokhali": "Поехали!",
}

# Шрифт с поддержкой кириллицы.
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\calibri.ttf",
]


def find_font() -> str:
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            return f
    raise FileNotFoundError("Не найден TTF-шрифт с кириллицей в C:\\Windows\\Fonts")


def escape_filter_path(path: str) -> str:
    """Экранирование пути для filtergraph ffmpeg (':', '\\')."""
    return path.replace("\\", "/").replace(":", "\\:")


def make_placeholder(ffmpeg_exe: str, font: str, phrase: str, out_path: Path) -> None:
    # Фраза во временный UTF-8 файл (надёжнее, чем text= в фильтре)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(phrase)
        text_file = tf.name
    try:
        vf = (
            f"drawtext="
            f"fontfile='{escape_filter_path(font)}':"
            f"textfile='{escape_filter_path(text_file)}':"
            f"fontcolor=white:fontsize=120:"
            f"x=(w-text_w)/2:y=(h-text_h)/2:line_spacing=20"
        )
        cmd = [
            ffmpeg_exe, "-y",
            "-f", "lavfi", "-i", "color=c=black:s=1920x1080:d=3:r=30",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-shortest",
            str(out_path),
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[-1000:])
    finally:
        try:
            os.unlink(text_file)
        except OSError:
            pass


def main() -> int:
    ffmpeg_exe = ffmpeg_path()
    font = find_font()
    print(f"ffmpeg: {ffmpeg_exe}")
    print(f"шрифт:  {font}")
    print()

    out_root = interruptions_dir()
    out_root.mkdir(parents=True, exist_ok=True)
    ok, fail = 0, 0
    for name, phrase in PHRASES.items():
        out = out_root / f"{name}.mp4"
        try:
            make_placeholder(ffmpeg_exe, font, phrase, out)
            size_kb = out.stat().st_size // 1024
            print(f"[OK]   {name:16s} -> {out}  ({size_kb} KB)  «{phrase}»")
            ok += 1
        except Exception as ex:
            print(f"[FAIL] {name:16s} -> {ex}")
            fail += 1
    print(f"\nГотово: {ok} успешно, {fail} с ошибкой.")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
