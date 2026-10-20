"""
Лічильники часу серверного процесу: де саме він витрачає секунди й де застряг.

Навіщо. У редакторі перший `search_codebase` інколи мовчить ~3 хв, а в
вимірах той самий пошук триває 3–4 с. Причину не встановлено (гіпотеза —
потік стоїть в `import numpy` через `rank_bm25`, але це гіпотеза). Тут немає
жодної здогадки про причину: кожна підозрювана ділянка (імпорти, побудова BM25,
завантаження моделі, сам пошук) міряється й пишеться в журнал `.ai/projectmind.log`.

Два рядки журналу:

    TIMING <мітка> 3.21s thread=<потік> [поля]      — INFO, ділянка закінчилась;
    SLOW <мітка> ще триває 21s thread=<потік> ...   — WARNING + стек потоку, поки
                                                      ділянка ще НЕ закінчилась.

Другий рядок — головний. Завислу ділянку по закінченню не побачиш (вона не
закінчується), тож за порогом `PROJECTMIND_SLOW_SECTION_SECONDS` (типово 20 с)
сторожовий потік пише стек потоку, що в ній стоїть, — саме той знімок, якого не
вистачило, коли сервер мовчав. Знімок повторюється раз на 60 с, поки ділянка жива.

Лічильники (кількість, сума, максимум) віддає `snapshot()`; їх показує `health`.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from logger import get_logger

logger = get_logger()

SLOW_ENV = "PROJECTMIND_SLOW_SECTION_SECONDS"
DEFAULT_SLOW_SECONDS = 20.0
REPEAT_STACK_SECONDS = 60.0
WATCHDOG_TICK_SECONDS = 1.0


def slow_threshold_seconds() -> float:
    raw = os.environ.get(SLOW_ENV)
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_SLOW_SECONDS


class _Section:
    __slots__ = ("label", "thread_id", "thread_name", "started", "warn_after", "last_report")

    def __init__(self, label: str, warn_after: float) -> None:
        current = threading.current_thread()
        self.label = label
        self.thread_id = threading.get_ident()
        self.thread_name = current.name
        self.started = time.monotonic()
        self.warn_after = warn_after
        self.last_report: float | None = None


_lock = threading.Lock()
_active: dict[int, _Section] = {}
_stats: dict[str, dict[str, float]] = {}
_next_key = 0
_watchdog: threading.Thread | None = None


def _format_fields(fields: dict[str, Any]) -> str:
    if not fields:
        return ""
    return " " + " ".join(f"{k}={v}" for k, v in fields.items())


def _stack_of(thread_id: int) -> str:
    frame = sys._current_frames().get(thread_id)
    if frame is None:
        return "  (потік уже завершився)"
    return "".join(traceback.format_stack(frame)).rstrip()


def check_slow_sections(now: float | None = None) -> int:
    """Пише SLOW + стек для кожної ділянки, що живе довше за свій поріг. Повертає, скільки."""
    now = time.monotonic() if now is None else now
    with _lock:
        due = [
            s
            for s in _active.values()
            if now - s.started >= s.warn_after
            and (s.last_report is None or now - s.last_report >= REPEAT_STACK_SECONDS)
        ]
        for s in due:
            s.last_report = now
    for s in due:
        logger.warning(
            f"SLOW {s.label} ще триває {now - s.started:.0f}s "
            f"thread={s.thread_name}; стек потоку зараз:\n{_stack_of(s.thread_id)}"
        )
    return len(due)


def _watchdog_loop() -> None:
    while True:
        time.sleep(WATCHDOG_TICK_SECONDS)
        try:
            check_slow_sections()
        except Exception:  # сторож не має права впасти й змовкнути
            pass


def _ensure_watchdog() -> None:
    global _watchdog
    if _watchdog is not None and _watchdog.is_alive():
        return
    with _lock:
        if _watchdog is not None and _watchdog.is_alive():
            return
        _watchdog = threading.Thread(target=_watchdog_loop, name="pm-timing-watchdog", daemon=True)
        _watchdog.start()


@contextmanager
def timed(label: str, warn_after: float | None = None, **fields: Any) -> Iterator[None]:
    """Міряє ділянку: INFO при закінченні, SLOW зі стеком, якщо вона затяглась.

    `warn_after` — поріг SLOW у секундах (типово `PROJECTMIND_SLOW_SECTION_SECONDS`).
    Виняток у ділянці проходить крізь, час усе одно записується.
    """
    global _next_key
    _ensure_watchdog()
    section = _Section(label, slow_threshold_seconds() if warn_after is None else warn_after)
    with _lock:
        _next_key += 1
        key = _next_key
        _active[key] = section
    try:
        yield
    finally:
        elapsed = time.monotonic() - section.started
        with _lock:
            _active.pop(key, None)
            entry = _stats.setdefault(label, {"count": 0, "total": 0.0, "max": 0.0, "last": 0.0})
            entry["count"] += 1
            entry["total"] += elapsed
            entry["max"] = max(entry["max"], elapsed)
            entry["last"] = elapsed
            reported_slow = section.last_report is not None
        suffix = " (раніше було SLOW)" if reported_slow else ""
        logger.info(
            f"TIMING {label} {elapsed:.2f}s thread={section.thread_name}"
            f"{_format_fields(fields)}{suffix}"
        )


def snapshot() -> dict[str, Any]:
    """Лічильники й ділянки, що ще тривають: {'sections': {...}, 'active': [...]}"""
    now = time.monotonic()
    with _lock:
        sections = {label: dict(entry) for label, entry in _stats.items()}
        active = [
            {
                "label": s.label,
                "seconds": now - s.started,
                "thread": s.thread_name,
            }
            for s in _active.values()
        ]
    return {"sections": sections, "active": active}


def format_report(limit: int = 12) -> str:
    """Markdown для `health`: найдовші ділянки й те, що зараз триває."""
    snap = snapshot()
    lines: list[str] = []
    for item in sorted(snap["active"], key=lambda a: -a["seconds"]):
        lines.append(f"  - ⏳ **{item['label']}** триває {item['seconds']:.0f}s ({item['thread']})")
    ranked = sorted(snap["sections"].items(), key=lambda kv: -kv[1]["max"])[:limit]
    for label, entry in ranked:
        lines.append(
            f"  - {label}: {entry['count']:.0f}× · разом {entry['total']:.2f}s · "
            f"найдовше {entry['max']:.2f}s · останнє {entry['last']:.2f}s"
        )
    if not lines:
        return "  - (жодної ділянки ще не міряно)"
    return "\n".join(lines)


def reset() -> None:
    """Скидає лічильники (для тестів)."""
    with _lock:
        _stats.clear()
        _active.clear()
