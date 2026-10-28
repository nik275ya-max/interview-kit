"""Оркестрация всего пайплайна: видео -> расшифровка -> план -> сборка.

Двухфазный поток:
  1. prepare()  — извлечение аудио, сегментация, STT, построение плана.
                  Возвращает (папка_интервью, план_монтажа).
  2. assemble() — сборка итогового видео по (возможно отредактированному) плану.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .config import Config
from .editor import EditItem, Editor
from .ffmpeg_wrapper import FFmpeg
from .paths import ensure_dirs, ffmpeg_path, interviews_dir
from .stt import STT, TranscriptSegment

LogFn = Callable[[str], None]
ProgressFn = Callable[[float], None]


class Pipeline:
    def __init__(
        self,
        config: Config,
        log: LogFn = lambda s: None,
        progress: ProgressFn = lambda p: None,
    ):
        self.config = config
        self.log = log
        self.progress = progress
        ensure_dirs()
        self.ffmpeg = FFmpeg(ffmpeg_path())
        self.editor = Editor(self.ffmpeg)
        self._stt: Optional[STT] = None

    @property
    def stt(self) -> STT:
        if self._stt is None:
            self._stt = STT(self.config)
        return self._stt

    # --- Фаза 1: подготовка ---

    def prepare(self, video_path: str, guest_name: str) -> Tuple[Path, List[EditItem]]:
        """Полный анализ видео вплоть до плана монтажа."""
        video_path = str(video_path)
        folder = self._make_interview_folder(guest_name)
        segs_dir = folder / "segments"
        segs_dir.mkdir(exist_ok=True)
        self.log(f"Папка интервью: {folder}")

        # 1. Копируем исходник и извлекаем аудио
        raw = folder / "raw.mp4"
        shutil.copy2(video_path, raw)
        audio = folder / "audio.wav"
        self.log("Извлечение аудио…")
        self.ffmpeg.extract_audio(str(raw), str(audio))
        self.progress(0.10)

        duration = self.ffmpeg.probe_duration(str(audio))
        self.log(f"Длительность: {duration:.1f} сек")

        # 2. Детекция пауз / сегментация
        speech = self.ffmpeg.detect_speech_segments(
            str(audio), self.config.silence_db, self.config.silence_min_duration
        )
        if len(speech) <= 1:
            self.log("Паузы не найдены — нарезка на равные отрезки…")
            speech = self.ffmpeg.split_into_equal_segments(
                duration, self.config.fallback_segment_sec
            )
        self.log(f"Сегментов речи: {len(speech)}")
        self.progress(0.20)

        # 3. Нарезаем аудио-сегменты для STT
        seg_files: List[Tuple[float, float, Path]] = []
        n = max(1, len(speech))
        for i, (s, e) in enumerate(speech):
            sp = segs_dir / f"seg_{i:03d}.wav"
            self.ffmpeg.cut_audio_segment(str(audio), s, e, str(sp))
            seg_files.append((s, e, sp))
            self.progress(0.20 + 0.10 * (i + 1) / n)
        self.progress(0.30)

        # 4. Распознавание речи
        transcript: List[TranscriptSegment] = []
        for i, (s, e, sp) in enumerate(seg_files):
            self.log(f"Распознавание сегмента {i + 1}/{len(seg_files)}…")
            try:
                sub = self.stt.transcribe(str(sp), offset=s)
            except Exception as ex:
                self.log(f"  ошибка STT: {ex}")
                sub = []
            # Единицей монтажа является найденный по паузам ответ, а не
            # внутренние фрагменты Whisper. Так не пропадают смех, короткие
            # реплики и края ответа, которые VAD не вернул как текст.
            transcript.append(
                TranscriptSegment(
                    index=i,
                    start=s,
                    end=e,
                    text=" ".join(part.text for part in sub if part.text).strip(),
                )
            )
            self.progress(0.30 + 0.40 * (i + 1) / n)

        # 5. Сохраняем расшифровку
        trans_path = folder / "transcript.json"
        with open(trans_path, "w", encoding="utf-8") as f:
            json.dump(
                [{"start": t.start, "end": t.end, "text": t.text} for t in transcript],
                f, indent=2, ensure_ascii=False,
            )
        self.progress(0.72)

        # 6. План монтажа
        plan = self._build_plan(str(raw), transcript)
        plan_path = folder / "edit_plan.json"
        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump([item.to_dict() for item in plan], f, indent=2, ensure_ascii=False)

        inter_count = sum(1 for it in plan if it.kind == "interruption")
        self.log(f"Перебивок в плане: {inter_count}")
        self.progress(0.75)

        # Чистим сегменты-аудио (больше не нужны)
        try:
            shutil.rmtree(segs_dir)
        except OSError:
            pass

        return folder, plan

    # --- Фаза 2: сборка ---

    def assemble(self, folder: Path, plan: List[EditItem]) -> Path:
        """Собирает итоговое видео по плану."""
        w, h = self.config.resolution_wh()
        final = folder / "final.mp4"
        if not plan:
            raise ValueError("План монтажа пуст")
        # Сохраняем изменения, сделанные пользователем при проверке плана.
        with open(folder / "edit_plan.json", "w", encoding="utf-8") as f:
            json.dump([item.to_dict() for item in plan], f, indent=2, ensure_ascii=False)
        self.log("Сборка итогового видео…")

        def asm_progress(p: float) -> None:
            self.progress(0.75 + 0.25 * p)

        self.editor.assemble(
            plan,
            str(final),
            w,
            h,
            self.config.output_fps,
            self.config.audio_sample_rate,
            self.config.audio_channels,
            asm_progress,
        )
        self.log(f"Готово: {final}")
        self.progress(1.0)
        return final

    # --- утилиты ---

    def _build_plan(self, source_path: str, transcript) -> List[EditItem]:
        """Строит план монтажа: нейросеть polza.ai решает, что оставить и какие
        перебивки вставить.

        Если ключ polza.ai не задан или LLM недоступна (сеть, лимиты, разбор
        ответа), откатываемся к полному плану без перебивок — оставляем все
        реплики, чтобы пайплайн не падал.
        """
        if not self.config.polza_api_key:
            self.log(
                "Поле polza_api_key пустое — нейросеть недоступна. "
                "План: все реплики без перебивок."
            )
            return self.editor.build_full_plan(source_path, transcript)
        try:
            from .llm import PolzaClient, decide_segments

            client = PolzaClient(
                self.config.polza_api_key,
                self.config.polza_model,
                self.config.polza_base_url,
            )
            interruptions = self.editor.list_interruptions()
            decisions = decide_segments(
                client, transcript, interruptions, self.log
            )
            plan = self.editor.build_edit_plan_llm(
                decisions, source_path, transcript
            )
            inters = sum(1 for it in plan if it.kind == "interruption")
            self.log(
                f"План по LLM ({self.config.polza_model}): "
                f"элементов {len(plan)}, перебивок {inters}"
            )
            return plan
        except Exception as ex:  # noqa: BLE001 — любой сбой LLM -> откат
            self.log(f"LLM-монтаж недоступен ({ex}); оставляем все реплики без перебивок")
            return self.editor.build_full_plan(source_path, transcript)

    def _make_interview_folder(self, guest_name: str) -> Path:
        date_str = time.strftime("%Y-%m-%d")
        safe = "".join(c for c in guest_name if c.isalnum() or c in "-_ ").strip()[:50]
        if not safe:
            safe = "guest"
        folder = interviews_dir() / f"{date_str}_{safe}"
        i = 1
        while folder.exists():
            folder = interviews_dir() / f"{date_str}_{safe}_{i}"
            i += 1
        folder.mkdir(parents=True)
        return folder
