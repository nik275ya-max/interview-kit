"""Умный монтаж интервью через LLM (polza.ai, OpenAI-совместимый API).

Нейросеть читает расшифровку интервью и сама решает, какие фрагменты
оставить, а какие вырезать (заполнители, хезитации «эээ», повторы,
отступления от темы), и какую видео-перебивку (b-roll) вставить после
ответа. Все перебивки лежат в одной папке `interruptions/`, и модель
выбирает конкретный файл по его имени.

Используется только стандартный urllib — новых зависимостей не добавляем.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, List, Optional

from .stt import TranscriptSegment

DEFAULT_BASE_URL = "https://polza.ai/api/v1"

SYSTEM_PROMPT = (
    "Ты — опытный видеомонтажёр интервью. Тебе дают расшифровку как список "
    "сегментов с индексами, тайм-кодами и текстом, а также список доступных "
    "коротких видео-перебивок (b-roll) по именам файлов. Отбери лучшие "
    "законченные реплики; вырежи заполнители, хезитации («эээ», «ну»), "
    "повторы, отступления от темы и «мёртвый» воздух. Для оставленных реплик "
    "при желании подбери уместную перебивку по её имени. Отвечай ТОЛЬКО "
    "валидным JSON без пояснений и без markdown-ограждений."
)


class LLMError(Exception):
    """Ошибка обращения к LLM или разбора её ответа."""


@dataclass
class SegmentDecision:
    """Решение нейросети по одному сегменту расшифровки."""

    index: int
    keep: bool = True
    interruption: str = ""   # имя файла перебивки (пусто — не вставлять)
    reason: str = ""


class PolzaClient:
    """Минимальный клиент OpenAI-совместимого API polza.ai на urllib."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 120.0,
    ):
        self.api_key = (api_key or "").strip()
        self.model = model
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout

    def chat(
        self,
        messages: List[dict],
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> str:
        if not self.api_key:
            raise LLMError("Не задан API-ключ polza.ai")
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as ex:
            detail = ex.read().decode("utf-8", "replace")[:500]
            raise LLMError(f"HTTP {ex.code}: {detail}") from ex
        except (urllib.error.URLError, TimeoutError, OSError) as ex:
            raise LLMError(f"Сетевая ошибка: {ex}") from ex
        try:
            data = json.loads(raw)
            return data["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as ex:
            raise LLMError(f"Неожиданный формат ответа LLM: {raw[:300]}") from ex


def extract_json_value(text: str):
    """Возвращает первый сбалансированный JSON (массив или объект) из текста.

    Устойчив к markdown-ограждению ```json и лишнему тексту вокруг JSON:
    сканирует первый открывающий символ, считает только его пару и игнорирует
    скобки внутри строк, чтобы не сбиться на тексте ответа.
    """
    start = None
    for i, ch in enumerate(text):
        if ch in "[{":
            start = i
            break
    if start is None:
        raise LLMError("В ответе LLM нет JSON")
    open_ch = text[start]
    close_ch = "]" if open_ch == "[" else "}"
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError as ex:
                    raise LLMError(f"Некорректный JSON в ответе LLM: {ex}") from ex
    raise LLMError("Несбалансированный JSON в ответе LLM")


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "да", "yes", "1", "keep", "оставить")
    return bool(value)


def parse_decisions(text: str, valid_interruptions: Optional[List[str]]) -> List[SegmentDecision]:
    """Разбирает JSON с решениями из ответа LLM.

    Неизвестные имена перебивок отбрасываются, отсутствующие поля получают
    безопасные значения (keep по умолчанию True — контент не теряем).
    """
    data = extract_json_value(text)
    if isinstance(data, dict):
        for key in ("decisions", "segments", "edits", "results", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        raise LLMError("Ожидался JSON-массив решений")

    valid = {os.path.basename(p) for p in (valid_interruptions or ())}
    decisions: List[SegmentDecision] = []
    for element in data:
        if not isinstance(element, dict):
            continue
        index = element.get("index")
        if not isinstance(index, int):
            try:
                index = int(index)
            except (TypeError, ValueError):
                continue
        keep = element.get("keep")
        keep = True if keep is None else _as_bool(keep)
        interruption = element.get("interruption") or ""
        if not isinstance(interruption, str):
            interruption = str(interruption)
        interruption = interruption.strip()
        # Модель может вернуть путь; сверяем по имени файла.
        if interruption and os.path.basename(interruption) in valid:
            interruption = os.path.basename(interruption)
        elif interruption:
            interruption = ""
        reason = element.get("reason") or ""
        if not isinstance(reason, str):
            reason = str(reason)
        decisions.append(
            SegmentDecision(index=index, keep=keep, interruption=interruption, reason=reason.strip())
        )
    return decisions


def decide_segments(
    client: PolzaClient,
    transcript: List[TranscriptSegment],
    interruptions: List[str],
    log: Callable[[str], None] = lambda s: None,
) -> List[SegmentDecision]:
    """Просит LLM решить, какие фрагменты оставить и какую перебивку вставить.

    interruptions — список имён файлов перебивок (basename), из которых модель
    выбирает конкретный файл для вставки после реплики.
    """
    if not transcript:
        return []
    lines = [
        f"[{seg.index}] {seg.start:.1f}-{seg.end:.1f}с: {(seg.text or '').strip()}"
        for seg in transcript
    ]
    names = [os.path.basename(p) for p in (interruptions or [])]
    clip_line = (
        "Доступные перебивки (имена файлов, можно вставить после реплики): "
        + ", ".join(names)
        if names
        else "Перебивок нет — только оставь/вырежь."
    )
    user = (
        f"{clip_line}\n\n"
        "Расшифровка интервью (сегменты):\n" + "\n".join(lines) + "\n\n"
        "Для каждого сегмента реши: оставить (keep=true) или вырезать (keep=false). "
        "Для оставленных при желании укажи уместную перебивку точно по имени файла "
        '(или пустую строку). Верни ТОЛЬКО JSON-массив вида '
        '[{"index":0,"keep":true,"interruption":"","reason":""}] для КАЖДОГО сегмента.'
    )
    log("LLM: анализ расшифровки…")
    max_tokens = min(8000, max(1024, 120 * len(transcript)))
    reply = client.chat(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tokens,
    )
    decisions = parse_decisions(reply, interruptions)
    kept = sum(1 for d in decisions if d.keep)
    log(f"LLM: получено решений {len(decisions)}, оставлено сегментов {kept}")
    return decisions
