"""Конфигурация программы."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from .paths import config_path


@dataclass
class Config:
    polza_api_key: str = ""               # ключ polza.ai для умного монтажа (LLM)
    polza_model: str = "deepseek/deepseek-v4.1-flash-1017"  # дешёвая быстрая LLM
    polza_base_url: str = "https://polza.ai/api/v1"
    stt_backend: str = "whisper_local"
    whisper_model: str = "small"          # tiny/base/small/medium/large-v3
    whisper_language: str = "ru"
    whisper_device: str = "cpu"           # cpu или cuda
    whisper_compute_type: str = "int8"    # int8 (cpu), float16 (cuda)
    silence_db: float = -30.0             # порог тишины в dB
    silence_min_duration: float = 0.6     # мин. длительность паузы, сек
    fallback_segment_sec: float = 10.0    # длина отрезка при отсутствии пауз
    output_resolution: str = "1920x1080"
    output_fps: int = 30
    audio_sample_rate: int = 48000
    audio_channels: int = 2
    watch_incoming: bool = False          # автообновление списка incoming

    @classmethod
    def load(cls) -> "Config":
        p = config_path()
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # берём только известные поля
                known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
                return cls(**known)
            except Exception:
                return cls()
        return cls()

    def save(self) -> None:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)

    def resolution_wh(self) -> tuple[int, int]:
        try:
            w, h = self.output_resolution.lower().split("x", 1)
            width, height = int(w), int(h)
            if width <= 0 or height <= 0:
                raise ValueError
            return width, height
        except (TypeError, ValueError):
            raise ValueError(
                f"Некорректное разрешение {self.output_resolution!r}; ожидается формат 1920x1080"
            ) from None
