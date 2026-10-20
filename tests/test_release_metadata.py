"""Випуск: версія збігається з CHANGELOG, а зібране колесо справді містить продукт.

Пакет 0.11.0 на PyPI не стартував (`No module named 'warmup'`): модуль імпортувався,
але не потрапив у `py-modules`. test_packaging_modules.py стереже сам список; тут
колесо збирається по-справжньому з чистої копії дерева (без застарілої теки build/),
і перевіряється те, що отримає користувач: файли в архіві та точки входу.
"""

from __future__ import annotations

import os
import re
import shutil
import zipfile
from pathlib import Path

import pytest

from tests.test_packaging_modules import (
    NON_PRODUCT,
    _imported_top_level_names,
)

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: tomllib з'явився в 3.11
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent


def _project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def _changelog_versions() -> list[str]:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return re.findall(r"(?m)^## \[(\d+\.\d+\.\d+)\]", text)


def test_pyproject_version_is_the_top_changelog_entry() -> None:
    versions = _changelog_versions()
    assert versions, "у CHANGELOG.md немає жодного запису виду '## [x.y.z]'"
    assert (
        _project_version() == versions[0]
    ), f"pyproject.toml каже {_project_version()}, а верхній запис CHANGELOG — {versions[0]}"


def test_changelog_has_no_duplicate_versions() -> None:
    versions = _changelog_versions()
    assert len(versions) == len(set(versions)), versions


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Колесо з чистої копії дерева: стара тека build/ не підмішується в архів."""
    from setuptools import build_meta

    src = tmp_path_factory.mktemp("wheel-src")
    out = tmp_path_factory.mktemp("wheel-out")
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(ROOT / name, src / name)
    for module in ROOT.glob("*.py"):
        shutil.copy2(module, src / module.name)
    previous = Path.cwd()
    os.chdir(src)
    try:
        filename = build_meta.build_wheel(str(out))
    finally:
        os.chdir(previous)
    return out / filename


@pytest.mark.timeout(300)
def test_wheel_contains_every_imported_product_module(built_wheel: Path) -> None:
    with zipfile.ZipFile(built_wheel) as wheel:
        shipped = {Path(n).stem for n in wheel.namelist() if n.endswith(".py") and "/" not in n}
    needed = _imported_top_level_names() | {"mcp_server"}
    missing = sorted(needed - shipped)
    assert not missing, f"у колесі бракує модулів, які імпортує пакет: {missing}"
    assert not (
        shipped & NON_PRODUCT
    ), f"у колесо потрапили не продуктові скрипти: {shipped & NON_PRODUCT}"


@pytest.mark.timeout(300)
def test_wheel_declares_the_console_scripts_and_the_project_version(built_wheel: Path) -> None:
    assert f"-{_project_version()}-" in built_wheel.name, built_wheel.name
    with zipfile.ZipFile(built_wheel) as wheel:
        entry = next(n for n in wheel.namelist() if n.endswith("entry_points.txt"))
        text = wheel.read(entry).decode("utf-8")
    assert "projectmind-mcp = mcp_server:main" in text, text
    assert "projectmind = mcp_server:main" in text, text
