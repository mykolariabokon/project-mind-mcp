"""
Каждий модуль верхнього рівня, який імпортує пакет, мусить бути в
`[tool.setuptools] py-modules` у pyproject.toml.

Інакше wheel/sdist збирається без модуля, і встановлений сервер падає з
ImportError на першому ж шляху, що його імпортує (так уже було з `warmup`:
його імпортують сім місць у mcp_server/context/query_router, а в списку його
не було). Тест читає сам pyproject.toml і самі файли — список не переписується
вручну.
"""

from __future__ import annotations

import ast
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: tomllib з'явився в 3.11
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent

# Скрипти поза продуктом: їх немає в wheel і їх не імпортує пакет.
NON_PRODUCT = {"_smoke", "run_index", "test_import"}


def _declared_py_modules() -> set[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return set(data["tool"]["setuptools"]["py-modules"])


def _top_level_product_modules() -> set[str]:
    return {p.stem for p in ROOT.glob("*.py")} - NON_PRODUCT


def _imported_top_level_names() -> set[str]:
    """Імена верхнього рівня, які імпортує будь-який модуль пакета (включно з ледачими імпортами)."""
    local = _top_level_product_modules()
    found: set[str] = set()
    for path in ROOT.glob("*.py"):
        if path.stem in NON_PRODUCT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                head = name.split(".")[0]
                if head in local:
                    found.add(head)
    return found


def test_every_imported_product_module_is_packaged() -> None:
    imported = _imported_top_level_names()
    declared = _declared_py_modules()
    missing = sorted(imported - declared)
    assert (
        not missing
    ), f"Модулі імпортуються пакетом, але відсутні в py-modules (pyproject.toml): {missing}"


def test_every_declared_py_module_exists() -> None:
    # Зворотний бік: запис у py-modules без файлу ламає збірку так само.
    declared = _declared_py_modules()
    absent = sorted(m for m in declared if not (ROOT / f"{m}.py").exists())
    assert not absent, f"py-modules називає неіснуючі файли: {absent}"
