"""План монтажа и сборка итогового видео."""
from __future__ import annotations

import os
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, List, Optional

from .ffmpeg_wrapper import FFmpeg
from .paths import interruptions_dir
from .stt import TranscriptSegment

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v")


@dataclass
class EditItem:
    """Элемент таймлайна: сегмент исходного видео или перебивка."""
    kind: str                          # "segment" | "interruption"
    source_path: str
    start: float = 0.0
    end: float = 0.0
    text: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "EditItem":
        if not isinstance(d, dict):
            raise TypeError("Элемент плана должен быть объектом")
        # Старые планы могли содержать categories — игнорируем их.
        return cls(
            kind=d.get("kind", "segment"),
            source_path=d.get("source_path", ""),
            start=d.get("start", 0.0),
            end=d.get("end", 0.0),
            text=d.get("text", ""),
        )


class Editor:
    def __init__(self, ffmpeg: FFmpeg):
        self.ffmpeg = ffmpeg

    # --- каталог перебивок ---

    @staticmethod
    def list_interruptions() -> List[str]:
        """Все видео-перебивки в одной папке interruptions/ (рекурсивно).

        Нейросеть выбирает конкретный файл по имени, поэтому категории и
        подпапки больше не используются.
        """
        d = interruptions_dir()
        if not d.exists():
            return []
        return sorted(
            str(f) for f in d.rglob("*")
            if f.is_file() and f.suffix.lower() in VIDEO_EXTS
        )

    @staticmethod
    def interruption_by_name(name: str) -> Optional[str]:
        """Находит полный путь перебивки по имени файла (basename)."""
        base = os.path.basename(name)
        for path in Editor.list_interruptions():
            if os.path.basename(path) == base:
                return path
        return None

    # --- построение плана ---

    def build_full_plan(
        self, video_path: str, transcript: List[TranscriptSegment]
    ) -> List[EditItem]:
        """Оставляет все реплики без перебивок (безопасный откат без LLM)."""
        return [
            EditItem(
                kind="segment",
                source_path=video_path,
                start=seg.start,
                end=seg.end,
                text=seg.text,
            )
            for seg in transcript
        ]

    def build_edit_plan_llm(
        self,
        decisions,
        source_path: str,
        transcript: List[TranscriptSegment],
    ) -> List[EditItem]:
        """Строит план монтажа по решениям LLM (core.llm.decide_segments).

        decisions — список core.llm.SegmentDecision. Сегменты без явного
        решения считаются оставленными (keep по умолчанию), чтобы не терять
        контент, если LLM вернула не все индексы. Перебивка выбирается по
        имени файла из папки interruptions/.
        """
        by_index = {d.index: d for d in decisions}
        plan: List[EditItem] = []
        for seg in transcript:
            decision = by_index.get(seg.index)
            if decision is not None and not decision.keep:
                continue
            plan.append(
                EditItem(
                    kind="segment",
                    source_path=source_path,
                    start=seg.start,
                    end=seg.end,
                    text=seg.text,
                )
            )
            if decision is not None and decision.interruption:
                clip = self.interruption_by_name(decision.interruption)
                if clip:
                    plan.append(EditItem(kind="interruption", source_path=clip))
        return plan

    def replace_interruption(self, item: EditItem) -> None:
        """Заменяет перебивку на другую случайную из общей папки."""
        if item.kind != "interruption":
            return
        files = self.list_interruptions()
        alternatives = [p for p in files if p != item.source_path]
        new = random.choice(alternatives or files)
        if new:
            item.source_path = new

    # --- сборка ---

    def assemble(
        self,
        plan: List[EditItem],
        out_path: str,
        width: int,
        height: int,
        fps: int,
        sample_rate: int,
        channels: int,
        progress_cb: Optional[Callable[[float], None]] = None,
    ) -> str:
        """Нормализует все элементы и склеивает в итоговое видео."""
        out = Path(out_path)
        tmpdir = out.parent / "_tmp_norm"
        tmpdir.mkdir(exist_ok=True)

        norm_files: List[str] = []
        total = max(1, len(plan))
        try:
            for i, item in enumerate(plan):
                norm = str(tmpdir / f"norm_{i:04d}.mp4")
                if item.kind == "segment":
                    if item.end <= item.start:
                        raise ValueError(f"Некорректные границы сегмента {i + 1}")
                    self.ffmpeg.cut_and_normalize(
                        item.source_path, item.start, item.end, norm,
                        width, height, fps, sample_rate, channels,
                    )
                elif item.kind == "interruption":
                    self.ffmpeg.normalize_clip(
                        item.source_path, norm, width, height, fps,
                        sample_rate, channels,
                    )
                else:
                    raise ValueError(f"Неизвестный тип элемента плана: {item.kind}")
                norm_files.append(norm)
                if progress_cb:
                    progress_cb((i + 1) / total)

            list_file = tmpdir / "concat.txt"
            with open(list_file, "w", encoding="utf-8") as f:
                for nf in norm_files:
                    safe = nf.replace("'", r"'\''")
                    f.write(f"file '{safe}'\n")

            try:
                self.ffmpeg.concat(str(list_file), out_path, copy=True)
            except Exception:
                self.ffmpeg.concat(str(list_file), out_path, copy=False)
        finally:
            # Временные файлы удаляются и после ошибки кодирования.
            for nf in norm_files:
                try:
                    os.remove(nf)
                except OSError:
                    pass
            try:
                os.remove(tmpdir / "concat.txt")
            except OSError:
                pass
            try:
                tmpdir.rmdir()
            except OSError:
                pass
        return out_path
