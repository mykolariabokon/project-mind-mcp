"""
Граф символів мусить бачити файли, створені ПІСЛЯ останньої побудови.

Раніше `is_symbol_graph_stale` дивився лише на файли з `index_metadata.json`
(і лише там, де метадані є): файл, якого індексатор ще не записав, або файл
глибше другого рівня, був сліпий — `find_symbol` казав «не знайдено», поки
хтось не перезапустив індексацію. Тепер сканування йде через `file_scanner`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import config
import symbol_graph


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    """Тимчасовий проєкт: config і кеш графа перемикаються на нього і повертаються назад."""
    original_root = config.PROJECT_ROOT
    original_cache = (symbol_graph._cached_graph, symbol_graph._cache_path)
    config.reconfigure(tmp_path)
    symbol_graph._cached_graph = None
    symbol_graph._cache_path = None
    try:
        (tmp_path / "old.py").write_text("def old_fn():\n    return 1\n", encoding="utf-8")
        yield tmp_path
    finally:
        config.reconfigure(original_root)
        symbol_graph._cached_graph, symbol_graph._cache_path = original_cache


def _write_metadata_with_old_entry(root: Path) -> None:
    # Метадані індексатора знають лише старий файл: так виглядає проєкт після індексації.
    config.AI_DIR.mkdir(parents=True, exist_ok=True)
    config.INDEX_METADATA_FILE.write_text(
        json.dumps({"old.py": {"mtime": 1.0, "size": 10}}), encoding="utf-8"
    )


def _new_file_is_found_after_rebuild(root: Path, rel: str) -> bool:
    graph = symbol_graph.get_or_build_symbol_graph(force=True)
    assert graph.defs_of("old_fn"), "перша побудова мусить бачити старий файл"

    # Файл створено після побудови: mtime строго пізніший за built_at.
    time.sleep(0.05)
    new_path = root / rel
    new_path.parent.mkdir(parents=True, exist_ok=True)
    new_path.write_text("def new_fn():\n    return 2\n", encoding="utf-8")

    symbol_graph._cached_graph = None  # імітуємо новий запит у наступному процесі
    graph = symbol_graph.get_or_build_symbol_graph()
    return bool(graph.defs_of("new_fn"))


def test_new_file_is_seen_when_index_metadata_exists(project: Path) -> None:
    # Метадані є, і глибокий файл у них відсутній — раніше метадані відповідали «не застаріло».
    _write_metadata_with_old_entry(project)
    assert _new_file_is_found_after_rebuild(project, "src/pkg/nested/new_mod.py")


def test_new_file_is_seen_without_index_metadata_at_depth(project: Path) -> None:
    # Без метаданих лишається лише ручне сканування з двох рівнів — глибший файл не бачило.
    assert _new_file_is_found_after_rebuild(project, "src/pkg/nested/new_mod.py")


def test_new_top_level_file_is_seen_when_index_metadata_exists(project: Path) -> None:
    _write_metadata_with_old_entry(project)
    assert _new_file_is_found_after_rebuild(project, "new_top.py")
