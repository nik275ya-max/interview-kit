"""План монтажа и сборка итогового видео."""
from __future__ import annotations

import os
import random
from dataclasses import asdict, dataclass, field
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
    categories: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "EditItem":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


class Editor:
    def __init__(self, ffmpeg: FFmpeg):
        self.ffmpeg = ffmpeg

    # --- каталог перебивок ---

    @staticmethod
    def list_categories() -> List[str]:
        d = interruptions_dir()
        if not d.exists():
            return []
        return sorted(f.name for f in d.iterdir() if f.is_dir())

    @staticmethod
    def list_interruptions(category: str) -> List[str]:
        d = interruptions_dir() / category
        if not d.exists():
            return []
        return sorted(
            str(f) for f in d.iterdir() if f.suffix.lower() in VIDEO_EXTS
        )

    # --- построение плана ---

    def build_edit_plan(
        self,
        video_path: str,
        transcript: List[TranscriptSegment],
        matcher,
    ) -> List[EditItem]:
        plan: List[EditItem] = []
        for seg in transcript:
            cats = matcher.find_categories(seg.text)
            plan.append(
                EditItem(
                    kind="segment",
                    source_path=video_path,
                    start=seg.start,
                    end=seg.end,
                    text=seg.text,
                    categories=cats,
                )
            )
            if cats:
                inter = self._pick_interruption(cats)
                if inter:
                    plan.append(
                        EditItem(
                            kind="interruption",
                            source_path=inter,
                            categories=cats,
                        )
                    )
        return plan

    def _pick_interruption(self, categories: List[str]) -> Optional[str]:
        for cat in categories:
            files = self.list_interruptions(cat)
            if files:
                return random.choice(files)
        return None

    def replace_interruption(self, item: EditItem) -> None:
        """Заменяет перебивку на другую случайную из той же категории."""
        if item.kind != "interruption":
            return
        new = self._pick_interruption(item.categories)
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
        for i, item in enumerate(plan):
            norm = str(tmpdir / f"norm_{i:04d}.mp4")
            if item.kind == "segment":
                self.ffmpeg.cut_and_normalize(
                    item.source_path,
                    item.start,
                    item.end,
                    norm,
                    width,
                    height,
                    fps,
                    sample_rate,
                    channels,
                )
            else:
                self.ffmpeg.normalize_clip(
                    item.source_path,
                    norm,
                    width,
                    height,
                    fps,
                    sample_rate,
                    channels,
                )
            norm_files.append(norm)
            if progress_cb:
                progress_cb((i + 1) / total)

        # concat-лист
        list_file = tmpdir / "concat.txt"
        with open(list_file, "w", encoding="utf-8") as f:
            for nf in norm_files:
                # экранируем одинарные кавычки для concat-формата
                safe = nf.replace("'", r"'\''")
                f.write(f"file '{safe}'\n")

        try:
            self.ffmpeg.concat(str(list_file), out_path, copy=True)
        except Exception:
            # если параметры разошлись — перекодируем
            self.ffmpeg.concat(str(list_file), out_path, copy=False)

        # уборка
        for nf in norm_files:
            try:
                os.remove(nf)
            except OSError:
                pass
        try:
            os.remove(list_file)
        except OSError:
            pass
        try:
            tmpdir.rmdir()
        except OSError:
            pass
        return out_path
