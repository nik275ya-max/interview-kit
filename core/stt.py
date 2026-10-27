"""Распознавание речи через локальный faster-whisper."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .config import Config
from .paths import models_dir


@dataclass
class TranscriptSegment:
    index: int
    start: float   # абсолютное время в исходном аудио
    end: float
    text: str


class STT:
    def __init__(self, config: Config):
        self.config = config
        self._model = None

    def _resolve_model(self) -> str:
        """Возвращает путь к модели: локальная папка, либо имя для авто-скачивания."""
        name = self.config.whisper_model
        local = models_dir() / name
        # CTranslate2-модель содержит model.bin
        if (local / "model.bin").exists():
            return str(local)
        # Пробуем скачать в локальную папку (для офлайн-сборки)
        try:
            from faster_whisper.utils import download_model  # type: ignore

            local.mkdir(parents=True, exist_ok=True)
            download_model(name, str(local))
            if (local / "model.bin").exists():
                return str(local)
        except Exception:
            pass
        # Fallback: имя модели -> faster-whisper сам скачает в кэш HF
        return name

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # ленивый импорт

            source = self._resolve_model()
            self._model = WhisperModel(
                source,
                device=self.config.whisper_device,
                compute_type=self.config.whisper_compute_type,
            )
        return self._model

    def transcribe(self, audio_path: str, offset: float = 0.0) -> List[TranscriptSegment]:
        """Распознаёт аудиофайл. offset прибавляется к тайм-кодам сегментов."""
        model = self._load()
        segments, _info = model.transcribe(
            audio_path,
            language=self.config.whisper_language,
            word_timestamps=False,
            vad_filter=True,
        )
        result: List[TranscriptSegment] = []
        idx = 0
        for seg in segments:
            text = (seg.text or "").strip()
            if not text:
                continue
            result.append(
                TranscriptSegment(
                    index=idx,
                    start=offset + float(seg.start),
                    end=offset + float(seg.end),
                    text=text,
                )
            )
            idx += 1
        return result
