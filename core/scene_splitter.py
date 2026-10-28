"""Поиск смен сцен и нарезка видео через FFmpeg."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, List

from .ffmpeg_wrapper import FFmpeg, FFmpegError


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
PTS_TIME_RE = re.compile(r"\bpts_time:([+-]?\d+(?:\.\d+)?)")
BLACK_RE = re.compile(
    r"black_start:(?P<start>[\d.]+)\s+black_end:(?P<end>[\d.]+)"
)
WINDOWS_ACCESS_VIOLATION = 0xC0000005


@dataclass(frozen=True)
class Scene:
    index: int
    start: float
    end: float
    path: str

    @property
    def duration(self) -> float:
        return self.end - self.start


def parse_scene_timestamps(log: str) -> List[float]:
    """Извлекает таймкоды кадров смены сцены из вывода showinfo."""
    return sorted({float(match.group(1)) for match in PTS_TIME_RE.finditer(log)})


def parse_black_intervals(log: str) -> List[tuple[float, float]]:
    """Извлекает чёрные интервалы из вывода blackdetect."""
    return [
        (float(match.group("start")), float(match.group("end")))
        for match in BLACK_RE.finditer(log)
    ]


def content_range(
    intervals: Iterable[tuple[float, float]], duration: float, tolerance: float = 0.15
) -> tuple[float, float]:
    """Возвращает диапазон без чёрных интервалов в начале и конце."""
    start, end = 0.0, duration
    ordered = merge_black_intervals(intervals)
    for black_start, black_end in ordered:
        if black_start <= start + tolerance:
            start = max(start, min(black_end, duration))
        else:
            break
    for black_start, black_end in reversed(ordered):
        if black_end >= end - tolerance:
            end = min(end, max(black_start, 0.0))
        else:
            break
    return (start, end) if end > start + 0.05 else (0.0, duration)


def merge_black_intervals(
    intervals: Iterable[tuple[float, float]], gap: float = 0.2
) -> List[tuple[float, float]]:
    """Сливает цепочки чёрных интервалов, разделённых маленькими разрывами.

    Фильтр blackdetect иногда режет один длинный чёрный переход на несколько
    кусков из-за пары некоричневых кадров посередине. Без слияния граница сцены
    может встать внутрь такого перехода, и сцена начнётся или закончится чёрным.
    """
    merged: List[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if merged and start - merged[-1][1] <= gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _shift_past_black(
    timestamp: float, blacks: List[tuple[float, float]], tolerance: float = 0.1
) -> float:
    """Сдвигает точку за конец чёрного перехода, если она в нём или рядом."""
    for black_start, black_end in blacks:
        if black_start - tolerance <= timestamp <= black_end:
            return max(timestamp, black_end)
    return timestamp


def make_boundaries(
    timestamps: Iterable[float], duration: float, min_scene_duration: float,
    content_start: float = 0.0, content_end: float | None = None,
    black_intervals: Iterable[tuple[float, float]] | None = None,
) -> List[float]:
    """Строит границы и отбрасывает слишком близкие смены сцен.

    Если переданы чёрные интервалы, каждая внутренняя граница сдвигается за
    их конец, чтобы новая сцена не начиналась с чёрного кадра. Чёрный переход
    остаётся в хвосте предыдущей сцены.
    """
    if duration <= 0:
        raise ValueError("Длительность видео должна быть больше нуля")
    if min_scene_duration < 0:
        raise ValueError("Минимальная длительность сцены не может быть отрицательной")

    content_end = duration if content_end is None else content_end
    if not 0 <= content_start < content_end <= duration:
        raise ValueError("Некорректный диапазон содержимого")

    blacks = merge_black_intervals(black_intervals or ())
    boundaries = [content_start]
    for timestamp in sorted(set(timestamps)):
        if timestamp <= content_start or timestamp >= content_end:
            continue
        timestamp = _shift_past_black(timestamp, blacks)
        if timestamp >= content_end:
            continue
        if timestamp - boundaries[-1] >= min_scene_duration:
            boundaries.append(timestamp)

    # Не создаём крошечный последний файл: присоединяем хвост к предыдущей сцене.
    if len(boundaries) > 1 and content_end - boundaries[-1] < min_scene_duration:
        boundaries.pop()
    boundaries.append(content_end)
    return boundaries


def scene_ranges(
    boundaries: List[float],
    black_intervals: Iterable[tuple[float, float]] | None = None,
    tolerance: float = 0.1,
) -> List[tuple[float, float]]:
    """Диапазоны сцен, очищенные от чёрных переходов на обоих краях.

    Границы уже сдвинуты так, что сцена не начинается с чёрного. Здесь чёрный
    переход дополнительно убирается из хвоста: если интервал заканчивается
    ровно на границе, хвост обрезается до его начала. Чёрный переход между
    сценами полностью исключается из результата (между сценами остаётся разрыв
    ровно на его длительность).
    """
    blacks = merge_black_intervals(black_intervals or ())
    ranges: List[tuple[float, float]] = []
    for start, end in zip(boundaries, boundaries[1:]):
        trimmed_end = end
        for black_start, black_end in blacks:
            if abs(black_end - end) <= tolerance:
                trimmed_end = black_start
                break
        ranges.append((start, trimmed_end if trimmed_end > start else end))
    return ranges


class SceneSplitter:
    # Сколько раз повторять аварийно упавший запуск FFmpeg в однопоточном
    # режиме, прежде чем сдаться. Сборки с pthreads-w32 падают недетерминированно.
    MAX_CRASH_RETRIES = 3
    # Признаки аварийного падения FFmpeg в stderr (помимо кода 0xC0000005).
    CRASH_MARKERS = (
        "segmentation fault",
        "access violation",
        "has crashed",
        "sigsegv",
        "sigabrt",
        "exception access violation",
    )

    def __init__(self, ffmpeg: FFmpeg):
        self.ffmpeg = ffmpeg

    def detect(
        self, input_path: Path, threshold: float = 0.35,
        log: Callable[[str], None] = print,
    ) -> tuple[float, List[float], List[tuple[float, float]]]:
        if not 0.0 < threshold < 1.0:
            raise ValueError("Порог сцены должен быть между 0 и 1")
        duration = self.ffmpeg.probe_duration(str(input_path))
        # Уменьшение кадра заметно ускоряет анализ и почти не влияет на поиск
        # монтажных склеек. showinfo пишет pts_time выбранных кадров в stderr.
        # d=0.03: ловим и короткие чёрные переходы (2 кадра при 30 к/с),
        # иначе граница сцены встаёт на чёрный кадр и сцена начинается с чёрного.
        # pic_th=0.90: почти чёрные переходы с несколькими светлыми кадрами
        # посередине тоже считаются чёрными, иначе граница встаёт внутрь них.
        vf = (
            "blackdetect=d=0.03:pix_th=0.10:pic_th=0.90,"
            f"scale=640:-2,select=gt(scene\\,{threshold}),showinfo"
        )
        args = [
            "-hide_banner", "-i", str(input_path),
            "-an", "-vf", vf, "-fps_mode", "vfr", "-f", "null", "-",
        ]
        safe_args = [
            "-hide_banner", "-threads", "1", "-i", str(input_path),
            "-an", "-filter_threads", "1", "-vf", vf,
            "-fps_mode", "vfr", "-f", "null", "-",
        ]
        output_log = self._run_with_crash_retry(
            args, safe_args, log, "анализ"
        )
        return duration, parse_scene_timestamps(output_log), parse_black_intervals(output_log)

    @staticmethod
    def _is_access_violation(error: FFmpegError) -> bool:
        """Определяет, упал ли FFmpeg аварийно (а не вернул обычную ошибку).

        Код 0xC0000005 — признак ACCESS_VIOLATION в Windows. Но некоторые
        сборки ловят сигнал и завершаются с другим кодом, лишь напечатав
        текст вида «Segmentation fault» или «has crashed» в stderr, поэтому
        дополнительно проверяем и сообщение об ошибке.
        """
        if error.returncode is not None:
            if error.returncode & 0xFFFFFFFF == WINDOWS_ACCESS_VIOLATION:
                return True
        message = (str(error) or "").lower()
        return any(marker in message for marker in SceneSplitter.CRASH_MARKERS)

    def _run_with_crash_retry(
        self,
        args: List[str],
        safe_args: List[str],
        log: Callable[[str], None],
        what: str = "",
        on_crash: Callable[[], None] | None = None,
    ) -> str:
        """Запускает FFmpeg и повторяет в однопоточном режиме при аварии.

        Некоторые Windows-сборки FFmpeg изредка падают в многопоточном
        декодере/фильтре с 0xC0000005. Однопоточный повтор медленнее, зато
        устойчив на тех же входных файлах. Повторяем несколько раз; если и
        безопасный режим падает — бросаем понятную ошибку с советами.
        """
        try:
            return self.ffmpeg._run(args)
        except FFmpegError as error:
            if not self._is_access_violation(error):
                raise
        for attempt in range(1, self.MAX_CRASH_RETRIES + 1):
            if on_crash is not None:
                on_crash()
            log(
                f"  FFmpeg аварийно завершился ({what}); "
                f"повтор {attempt}/{self.MAX_CRASH_RETRIES} в однопоточном режиме…"
            )
            try:
                return self.ffmpeg._run(safe_args)
            except FFmpegError as error:
                if not self._is_access_violation(error):
                    raise
                last = error
        raise FFmpegError(
            "FFmpeg многократно аварийно завершился ("
            f"{last.returncode}). Возможно, нестабильная сборка (pthreads-w32) "
            "или повреждённый файл. Попробуйте режим --copy (без перекодирования) "
            "или замените bin/ffmpeg.exe на другую сборку.",
            last.returncode,
        )

    def split(
        self,
        input_path: Path,
        output_dir: Path,
        threshold: float = 0.35,
        min_scene_duration: float = 1.0,
        copy_streams: bool = False,
        overwrite: bool = False,
        trim_black: bool = True,
        log: Callable[[str], None] = print,
    ) -> List[Scene]:
        input_path = input_path.resolve()
        if not input_path.is_file():
            raise FileNotFoundError(input_path)

        output_dir.mkdir(parents=True, exist_ok=True)
        existing = list(output_dir.glob("scene_*.mp4"))
        manifest = output_dir / "scenes.json"
        if (existing or manifest.exists()) and not overwrite:
            raise FileExistsError(
                f"В {output_dir} уже есть результат; используйте --overwrite"
            )
        if overwrite:
            for old in existing:
                old.unlink()
            manifest.unlink(missing_ok=True)

        log("Анализ смен сцен…")
        duration, timestamps, black_intervals = self.detect(
            input_path, threshold, log
        )
        trim_start, trim_end = (
            content_range(black_intervals, duration) if trim_black else (0.0, duration)
        )
        if trim_start > 0.0:
            log(f"Удалён чёрный экран в начале: {trim_start:.2f} сек")
        if trim_end < duration:
            log(f"Удалён чёрный экран в конце: {duration - trim_end:.2f} сек")
        boundaries = make_boundaries(
            timestamps, duration, min_scene_duration, trim_start, trim_end,
            black_intervals,
        )
        ranges = scene_ranges(boundaries, black_intervals)
        log(f"Найдено сцен: {len(ranges)}")

        scenes: List[Scene] = []
        for index, (start, end) in enumerate(ranges, 1):
            output = output_dir / f"scene_{index:04d}.mp4"
            args = [
                "-y", "-ss", f"{start:.6f}", "-i", str(input_path),
                "-t", f"{end - start:.6f}",
                "-map", "0:v:0", "-map", "0:a?",
            ]
            if copy_streams:
                args += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
            else:
                args += [
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                ]
            args.append(str(output))
            log(
                f"[{index}/{len(ranges)}] "
                f"{start:.2f} - {end:.2f} сек -> {output.name}"
            )
            safe_args = ["-threads", "1", *args]
            if not copy_streams:
                safe_args[-1:-1] = ["-filter_threads", "1"]
            self._run_with_crash_retry(
                args, safe_args, log,
                what=f"сцена {output.name}",
                on_crash=lambda: output.unlink(missing_ok=True),
            )
            scenes.append(Scene(index, start, end, str(output)))

        with manifest.open("w", encoding="utf-8") as file:
            json.dump(
                {
                    "source": str(input_path),
                    "threshold": threshold,
                    "min_scene_duration": min_scene_duration,
                    "scenes": [asdict(scene) | {"duration": scene.duration} for scene in scenes],
                },
                file,
                ensure_ascii=False,
                indent=2,
            )
        return scenes


def collect_videos(paths: Iterable[Path], recursive: bool = False) -> List[Path]:
    """Разворачивает файлы и каталоги в уникальный список видео."""
    videos: List[Path] = []
    seen = set()
    for path in paths:
        candidates = [path]
        if path.is_dir():
            candidates = list(path.rglob("*")) if recursive else list(path.iterdir())
        for candidate in candidates:
            if candidate.is_file() and candidate.suffix.lower() in VIDEO_EXTS:
                resolved = candidate.resolve()
                if resolved not in seen:
                    videos.append(resolved)
                    seen.add(resolved)
    return sorted(videos)
