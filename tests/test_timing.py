"""Лічильники часу сервера (timing.py): що пишуть і чи бачать завислу ділянку.

Усі перевірки викликають код і дивляться на результат — записи журналу й
лічильники. Тест на «зависання» ловить саме те, чого бракувало, коли сервер
мовчав: стек потоку, що стоїть у ділянці, ДО того, як вона закінчилась.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import timing

REPO = Path(__file__).resolve().parent.parent


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self, level: int | None = None) -> list[str]:
        return [r.getMessage() for r in self.records if level is None or r.levelno == level]


@pytest.fixture
def log() -> _Capture:
    handler = _Capture()
    timing.logger.addHandler(handler)
    timing.reset()
    try:
        yield handler
    finally:
        timing.logger.removeHandler(handler)
        timing.reset()


def test_timed_logs_label_duration_and_thread_at_info(log: _Capture) -> None:
    with timing.timed("unit.sleep", docs=7):
        time.sleep(0.05)

    lines = [m for m in log.messages(logging.INFO) if m.startswith("TIMING unit.sleep")]
    assert len(lines) == 1, log.messages()
    assert "docs=7" in lines[0]
    assert f"thread={threading.current_thread().name}" in lines[0]
    seconds = float(lines[0].split()[2].rstrip("s"))
    assert 0.04 <= seconds < 2.0, lines[0]


def test_counters_accumulate_per_label(log: _Capture) -> None:
    for _ in range(3):
        with timing.timed("unit.repeat"):
            time.sleep(0.05)
    with timing.timed("unit.other"):
        pass

    sections = timing.snapshot()["sections"]
    assert sections["unit.repeat"]["count"] == 3
    # Таймер Windows грубий (~15 мс), тож межі беремо з запасом.
    assert sections["unit.repeat"]["total"] >= 0.1
    assert sections["unit.repeat"]["max"] >= sections["unit.repeat"]["last"] >= 0.04
    assert sections["unit.other"]["count"] == 1


def test_a_failing_section_is_still_recorded_and_the_error_propagates(log: _Capture) -> None:
    with pytest.raises(ValueError):
        with timing.timed("unit.boom"):
            raise ValueError("x")

    assert timing.snapshot()["sections"]["unit.boom"]["count"] == 1
    assert timing.snapshot()["active"] == []
    assert any(m.startswith("TIMING unit.boom") for m in log.messages(logging.INFO))


def _stuck_in_here(started: threading.Event, release: threading.Event) -> None:
    with timing.timed("unit.stuck", warn_after=0.2):
        started.set()
        release.wait(30)


def test_a_stuck_section_logs_slow_with_the_stack_of_the_stuck_thread(log: _Capture) -> None:
    started, release = threading.Event(), threading.Event()
    worker = threading.Thread(target=_stuck_in_here, args=(started, release), name="pm-test-stuck")
    worker.start()
    try:
        assert started.wait(10)
        time.sleep(0.3)

        reported = timing.check_slow_sections()

        assert reported == 1
        slow = [m for m in log.messages(logging.WARNING) if m.startswith("SLOW unit.stuck")]
        assert len(slow) == 1, log.messages()
        assert "thread=pm-test-stuck" in slow[0]
        # Стек — саме того потоку, що стоїть у ділянці, а не сторожового чи поточного.
        assert "_stuck_in_here" in slow[0], slow[0]
        assert "release.wait" in slow[0], slow[0]
        assert timing.snapshot()["active"][0]["label"] == "unit.stuck"
    finally:
        release.set()
        worker.join(10)

    assert timing.snapshot()["active"] == []


def test_the_watchdog_reports_a_stuck_section_without_anyone_asking(
    log: _Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Під час справжнього зависання нікому викликати check_slow_sections: це робить сторож.
    monkeypatch.setattr(timing, "WATCHDOG_TICK_SECONDS", 0.1)
    started, release = threading.Event(), threading.Event()
    worker = threading.Thread(target=_stuck_in_here, args=(started, release), name="pm-test-stuck2")
    worker.start()
    try:
        assert started.wait(10)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if any(m.startswith("SLOW unit.stuck") for m in log.messages(logging.WARNING)):
                break
            time.sleep(0.1)
        else:
            pytest.fail("сторож не написав SLOW за 15 с")
    finally:
        release.set()
        worker.join(10)


def test_a_section_under_its_threshold_is_not_reported(log: _Capture) -> None:
    with timing.timed("unit.quick", warn_after=60):
        assert timing.check_slow_sections() == 0
    assert not [m for m in log.messages(logging.WARNING) if m.startswith("SLOW")]


def test_slow_threshold_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(timing.SLOW_ENV, "7.5")
    assert timing.slow_threshold_seconds() == 7.5
    monkeypatch.setenv(timing.SLOW_ENV, "nonsense")
    assert timing.slow_threshold_seconds() == timing.DEFAULT_SLOW_SECONDS


def test_report_lists_finished_and_running_sections(log: _Capture) -> None:
    with timing.timed("unit.done"):
        pass
    started, release = threading.Event(), threading.Event()
    worker = threading.Thread(target=_stuck_in_here, args=(started, release))
    worker.start()
    try:
        assert started.wait(10)
        report = timing.format_report()
    finally:
        release.set()
        worker.join(10)

    assert "unit.done" in report and "1×" in report
    assert "unit.stuck" in report and "триває" in report


# --- вузли, де міряємо: сервер справді пише ці рядки ------------------------------


def test_bm25_build_and_load_are_timed(log: _Capture, tmp_path: Path) -> None:
    from bm25_index import BM25Index

    index = BM25Index(tmp_path / "bm25.json")
    index.build(["a", "b"], ["alpha beta", "gamma delta"], [{"source": "x"}, {"source": "y"}])
    index.save()
    assert BM25Index(tmp_path / "bm25.json").load()

    messages = log.messages(logging.INFO)
    assert any(m.startswith("TIMING bm25.import rank_bm25") for m in messages), messages
    build = [m for m in messages if m.startswith("TIMING bm25.build")]
    assert build and "docs=2" in build[0], messages
    assert any(m.startswith("TIMING bm25.load") for m in messages), messages


def test_every_tool_call_is_timed_with_its_queue_wait(log: _Capture) -> None:
    import mcp_server

    asyncio.run(mcp_server.mcp.call_tool("get_cache_stats", {}))

    lines = [m for m in log.messages(logging.INFO) if m.startswith("TIMING tool.get_cache_stats")]
    assert len(lines) == 1, log.messages()
    assert "queued=" in lines[0]
    assert "pm-timing-watchdog" not in lines[0]


def test_health_shows_the_timings(log: _Capture) -> None:
    import mcp_server

    with timing.timed("unit.visible_in_health"):
        pass

    report = mcp_server.health()

    assert "Timings" in report
    assert "unit.visible_in_health" in report


@pytest.mark.timeout(300)
def test_warmup_times_each_heavy_import_in_the_project_log(tmp_path: Path) -> None:
    # Подвійний сенс: рядки є в журналі проєкту, і імпорти міряються окремо — тоді
    # зависання видно по імені імпорту, а не «щось у прогріві».
    env = dict(os.environ)
    env["PROJECT_ROOT"] = str(tmp_path)
    env["PYTHONPATH"] = str(REPO)
    code = "import warmup; warmup.ensure_loaded()"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=280,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    text = (tmp_path / ".ai" / "projectmind.log").read_text(encoding="utf-8")
    assert "TIMING import rank_bm25" in text, text[-3000:]
    assert "TIMING import symbol_graph" in text, text[-3000:]
