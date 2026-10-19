"""
Теплий сервер не мусить губити граф у пам'яті.

`get_or_build_symbol_graph` раніше повертав `_cached_graph` без жодної перевірки
застарілості (перевірка була лише для графа з диска). Довгоживучий процес, у
якому граф уже побудовано, не бачив файлів, створених пізніше: `find_symbol`
казав «не знайдено». Тут перевіряємо саме кешований шлях, без скидання кешу.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import config
import symbol_graph


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Тимчасовий проєкт: config і кеш графа перемикаються на нього і повертаються назад."""
    original_root = config.PROJECT_ROOT
    original_cache = (symbol_graph._cached_graph, symbol_graph._cache_path)
    config.reconfigure(tmp_path)
    symbol_graph._cached_graph = None
    symbol_graph._cache_path = None
    monkeypatch.setattr(symbol_graph, "_stale_checked_at", 0.0, raising=False)
    try:
        (tmp_path / "a.py").write_text("def old_alpha():\n    return 1\n", encoding="utf-8")
        yield tmp_path
    finally:
        config.reconfigure(original_root)
        symbol_graph._cached_graph, symbol_graph._cache_path = original_cache


def test_warm_cache_sees_file_created_after_build(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Перевірка кешу без паузи: інакше тест перевіряв би лише вікно throttle, а не сам фікс.
    monkeypatch.setattr(symbol_graph, "STALE_RECHECK_SECONDS", 0.0, raising=False)

    graph = symbol_graph.get_or_build_symbol_graph(force=True)
    assert graph.defs_of("old_alpha"), "перша побудова мусить бачити старий файл"

    # Новий файл у підтеці, mtime строго пізніший за побудову.
    time.sleep(1.1)
    new_path = project / "sub" / "b.py"
    new_path.parent.mkdir(parents=True, exist_ok=True)
    new_path.write_text("def brand_new_beta():\n    return 2\n", encoding="utf-8")

    # Без скидання _cached_graph: саме теплий шлях має побачити новий символ.
    graph = symbol_graph.get_or_build_symbol_graph()
    assert graph.defs_of("brand_new_beta"), "теплий граф мусить побачити файл, створений після побудови"


def test_warm_cache_does_not_rebuild_without_new_files(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Страж: перевірка застарілості не має перебудовувати граф, коли нових файлів немає.
    monkeypatch.setattr(symbol_graph, "STALE_RECHECK_SECONDS", 0.0, raising=False)
    calls = {"n": 0}
    real_build = symbol_graph.build_symbol_graph

    def counting_build(*args, **kwargs):
        calls["n"] += 1
        return real_build(*args, **kwargs)

    monkeypatch.setattr(symbol_graph, "build_symbol_graph", counting_build)

    symbol_graph.get_or_build_symbol_graph(force=True)
    assert calls["n"] == 1

    for _ in range(3):
        symbol_graph.get_or_build_symbol_graph()
    assert calls["n"] == 1, "без нових файлів граф не мав перебудовуватись"
