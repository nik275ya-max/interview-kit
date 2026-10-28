"""Обёртка над ffmpeg.exe: извлечение аудио, детекция пауз, нарезка,
нормализация и склейка клипов."""
from __future__ import annotations

import json
import re
import subprocess
from typing import List, Tuple


class FFmpegError(Exception):
    def __init__(self, message: str, returncode: int | None = None):
        super().__init__(message)
        self.returncode = returncode


class FFmpeg:
    def __init__(self, exe: str = "ffmpeg"):
        self.exe = exe

    # --- низкоуровневый запуск ---

    def _run(self, args: List[str], check: bool = True) -> str:
        cmd = [self.exe, *args]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if _is_windows() else 0,
        )
        if check and proc.returncode != 0:
            tail = (proc.stderr or proc.stdout)[-2000:]
            raise FFmpegError(
                f"ffmpeg failed (code {proc.returncode}):\n{tail}",
                returncode=proc.returncode,
            )
        return (proc.stderr or "") + (proc.stdout or "")

    # --- информация о файле ---

    def probe_duration(self, media_path: str) -> float:
        # `ffmpeg -i file` prints media information and then returns code 1
        # because no output was specified.  That is expected for a probe.
        log = self._run(["-hide_banner", "-i", media_path], check=False)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", log)
        if not m:
            raise FFmpegError("Не удалось определить длительность")
        h, mn, s = m.groups()
        return int(h) * 3600 + int(mn) * 60 + float(s)

    def probe_fps(self, video_path: str) -> float:
        log = self._run(["-hide_banner", "-i", video_path], check=False)
        m = re.search(r"(\d+(?:\.\d+)?)\s*fps\b", log)
        return float(m.group(1)) if m else 30.0

    def has_audio(self, media_path: str) -> bool:
        """Проверяет наличие аудиопотока без зависимости от ffprobe."""
        log = self._run(["-hide_banner", "-i", media_path], check=False)
        # Между индексом потока и `Audio:` могут быть язык и метаданные,
        # например `Stream #0:1(und): Audio: aac`.
        return bool(re.search(r"Stream\s+#.*\bAudio:", log))

    # --- аудио ---

    def extract_audio(self, video_path: str, out_wav: str) -> None:
        """Извлекает моно-аудио 16 кГц (формат для STT)."""
        self._run(
            [
                "-y", "-i", video_path,
                "-vn",
                "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le",
                out_wav,
            ]
        )

    def detect_speech_segments(
        self, wav_path: str, noise_db: float, min_duration: float
    ) -> List[Tuple[float, float]]:
        """Возвращает список (start, end) сегментов РЕЧИ (инверт к паузам)."""
        log = self._run(
            [
                "-i", wav_path,
                "-af", f"silencedetect=noise={noise_db}dB:d={min_duration}",
                "-f", "null", "-",
            ]
        )
        # Собираем события в хронологическом порядке
        events: List[Tuple[str, float]] = []
        for line in log.splitlines():
            m = re.search(r"silence_start:\s*([\d.]+)", line)
            if m:
                events.append(("start", float(m.group(1))))
            m = re.search(r"silence_end:\s*([\d.]+)", line)
            if m:
                events.append(("end", float(m.group(1))))

        duration = self.probe_duration(wav_path)

        # Формируем интервалы тишины
        silences: List[Tuple[float, float]] = []
        cur_start = None
        for ev_type, t in events:
            if ev_type == "start":
                cur_start = t
            else:  # end
                if cur_start is not None:
                    silences.append((cur_start, t))
                    cur_start = None
                else:
                    # аудио начинается с тишины
                    silences.append((0.0, t))
        if cur_start is not None:
            # аудио заканчивается тишиной
            silences.append((cur_start, duration))

        # Сегменты речи = промежутки между тишиной
        speech: List[Tuple[float, float]] = []
        prev_end = 0.0
        for s_start, s_end in silences:
            if s_start > prev_end + 0.05:
                speech.append((prev_end, s_start))
            prev_end = max(prev_end, s_end)
        if prev_end < duration - 0.05:
            speech.append((prev_end, duration))

        if not speech:
            # всё — тишина или не определилось; отдаём весь файл как один сегмент
            speech = [(0.0, duration)]
        return speech

    @staticmethod
    def split_into_equal_segments(
        duration: float, chunk_sec: float
    ) -> List[Tuple[float, float]]:
        segs: List[Tuple[float, float]] = []
        t = 0.0
        while t < duration:
            end = min(t + chunk_sec, duration)
            if end - t > 0.2:
                segs.append((t, end))
            t = end
        return segs

    def cut_audio_segment(self, wav_path: str, start: float, end: float, out_path: str) -> None:
        self._run(
            [
                "-y", "-ss", f"{start:.3f}", "-i", wav_path,
                "-t", f"{end - start:.3f}",
                "-c:a", "pcm_s16le",
                out_path,
            ]
        )

    # --- видео ---

    def cut_video_segment(self, video_path: str, start: float, end: float, out_path: str) -> None:
        """Вырезает фрагмент видео с перекодированием (точная граница)."""
        self._run(
            [
                "-y", "-ss", f"{start:.3f}", "-i", video_path,
                "-t", f"{end - start:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "192k",
                "-avoid_negative_ts", "make_zero",
                out_path,
            ]
        )

    def normalize_clip(
        self,
        input_path: str,
        out_path: str,
        width: int,
        height: int,
        fps: int,
        sample_rate: int,
        channels: int,
    ) -> None:
        """Приводит клип к единым параметрам (масштаб, паддинг, fps, формат, звук)."""
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"fps={fps},format=yuv420p"
        )
        args = ["-y", "-i", input_path]
        if self.has_audio(input_path):
            args += ["-map", "0:v:0", "-map", "0:a:0"]
        else:
            # У всех нормализованных файлов должен быть одинаковый набор
            # потоков, иначе concat теряет звук после немой перебивки.
            args += [
                "-f", "lavfi", "-i",
                f"anullsrc=channel_layout={'mono' if channels == 1 else 'stereo'}:sample_rate={sample_rate}",
                "-map", "0:v:0", "-map", "1:a:0",
            ]
        args += [
                "-vf", vf,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "192k",
                "-ar", str(sample_rate), "-ac", str(channels),
                "-shortest",
                out_path,
        ]
        self._run(args)

    def cut_and_normalize(
        self,
        video_path: str,
        start: float,
        end: float,
        out_path: str,
        width: int,
        height: int,
        fps: int,
        sample_rate: int,
        channels: int,
    ) -> None:
        """Вырезает сегмент видео и сразу нормализует (один проход)."""
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"fps={fps},format=yuv420p"
        )
        args = ["-y", "-ss", f"{start:.3f}", "-i", video_path]
        if self.has_audio(video_path):
            args += ["-map", "0:v:0", "-map", "0:a:0"]
        else:
            args += [
                "-f", "lavfi", "-i",
                f"anullsrc=channel_layout={'mono' if channels == 1 else 'stereo'}:sample_rate={sample_rate}",
                "-map", "0:v:0", "-map", "1:a:0",
            ]
        args += [
            "-t", f"{end - start:.3f}",
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
            "-ar", str(sample_rate), "-ac", str(channels),
            "-shortest", out_path,
        ]
        self._run(args)

    def concat(self, list_file: str, out_path: str, copy: bool = True) -> None:
        args = ["-y", "-f", "concat", "-safe", "0", "-i", list_file]
        if copy:
            args += ["-c", "copy"]
        else:
            args += [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "192k",
            ]
        args.append(out_path)
        self._run(args)


def _is_windows() -> bool:
    import sys
    return sys.platform.startswith("win")
