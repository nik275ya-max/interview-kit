#!/usr/bin/env python3
"""CLI: нарезка одного или нескольких видео по сменам сцен."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core.ffmpeg_wrapper import FFmpeg, FFmpegError
from core.paths import ffmpeg_path
from core.scene_splitter import (
    SceneSplitter, collect_videos, content_range, make_boundaries, scene_ranges,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Находит визуальные смены сцен и сохраняет каждую сцену в отдельный MP4."
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="Видеофайлы или папки")
    parser.add_argument("-o", "--output", type=Path, help="Корневая папка результата")
    parser.add_argument(
        "-t", "--threshold", type=float, default=0.35,
        help="Чувствительность: меньше = больше сцен (по умолчанию: 0.35)",
    )
    parser.add_argument(
        "--min-duration", type=float, default=1.0,
        help="Минимальная длительность сцены в секундах (по умолчанию: 1)",
    )
    parser.add_argument("-r", "--recursive", action="store_true", help="Искать видео во вложенных папках")
    parser.add_argument(
        "--copy", action="store_true",
        help="Не перекодировать (быстрее, но границы привязаны к ключевым кадрам)",
    )
    parser.add_argument("--analyze-only", action="store_true", help="Только показать найденные границы")
    parser.add_argument(
        "--keep-edge-black", action="store_true",
        help="Не удалять чёрный экран в начале и конце видео",
    )
    parser.add_argument("--overwrite", action="store_true", help="Перезаписать прежний результат")
    return parser


def output_dir_for(video: Path, root: Path | None, multiple: bool) -> Path:
    if root is not None:
        return root / video.stem if multiple else root
    return video.parent / f"{video.stem}_scenes"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0 < args.threshold < 1:
        print("Ошибка: --threshold должен быть между 0 и 1", file=sys.stderr)
        return 2
    if args.min_duration < 0:
        print("Ошибка: --min-duration не может быть отрицательной", file=sys.stderr)
        return 2

    missing = [str(path) for path in args.inputs if not path.exists()]
    if missing:
        print(f"Ошибка: путь не найден: {missing[0]}", file=sys.stderr)
        return 2
    videos = collect_videos(args.inputs, args.recursive)
    if not videos:
        print("Видео не найдены", file=sys.stderr)
        return 2

    splitter = SceneSplitter(FFmpeg(ffmpeg_path()))
    failures = 0
    for number, video in enumerate(videos, 1):
        print(f"\n=== [{number}/{len(videos)}] {video} ===")
        try:
            if args.analyze_only:
                duration, timestamps, black_intervals = splitter.detect(video, args.threshold)
                trim_start, trim_end = (
                    (0.0, duration) if args.keep_edge_black
                    else content_range(black_intervals, duration)
                )
                boundaries = make_boundaries(
                    timestamps, duration, args.min_duration, trim_start, trim_end,
                    black_intervals,
                )
                if trim_start > 0 or trim_end < duration:
                    print(
                        f"Удалён чёрный экран: начало {trim_start:.3f} сек, "
                        f"конец {duration - trim_end:.3f} сек"
                    )
                for index, (start, end) in enumerate(
                    scene_ranges(boundaries, black_intervals), 1
                ):
                    print(f"{index:04d}: {start:.3f} - {end:.3f} сек ({end - start:.3f} сек)")
            else:
                out = output_dir_for(video, args.output, len(videos) > 1)
                scenes = splitter.split(
                    video, out, args.threshold, args.min_duration,
                    args.copy, args.overwrite, not args.keep_edge_black,
                )
                print(f"Готово: {len(scenes)} сцен в {out}")
        except (FFmpegError, OSError, ValueError) as error:
            failures += 1
            print(f"Ошибка: {error}", file=sys.stderr)
            if isinstance(error, FFmpegError) and error.returncode is not None:
                if error.returncode & 0xFFFFFFFF == 0xC0000005:
                    print(
                        "FFmpeg завершился с ошибкой Windows 0xC0000005 даже после "
                        "безопасного повтора. Попробуйте --copy или другой файл FFmpeg.",
                        file=sys.stderr,
                    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
