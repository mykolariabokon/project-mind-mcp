"""Профіль інструментів PROJECTMIND_TOOL_PROFILE: full (типово) і lean.

Перевірка йде підпроцесом: профіль знімає інструменти з реєстру процесу, і
в тому самому процесі тестів це зламало б інші тести.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

LEAN_EXPECTED = {
    "search_codebase",
    "find_symbol",
    "get_symbol_relations",
    "get_file_summary",
    "get_file_relations",
    "explore_directory",
    "get_project_overview",
    "search_memory",
    "read_memory",
    "get_index_stats",
    "get_index_progress",
    "health",
    "search_for_errors",
    "search_for_feature",
    "index_changed_files",
    "index_codebase",
}

# Деструктивні й адміністративні: у lean їх бути не може.
DANGEROUS = {
    "clear_memory",
    "delete_memory_section",
    "restore_memory_version",
    "maintenance_run",
    "maintenance_status",
    "prune_index",
    "update_memory",
}

PROBE = """
import asyncio, json, sys
sys.path.insert(0, sys.argv[1])
import mcp_server as m
m.apply_tool_profile()
tools = asyncio.run(m.mcp.list_tools())
print(json.dumps({
    "count": len(tools),
    "names": sorted(t.name for t in tools),
    "desc_chars": sum(len(t.description or "") for t in tools),
    "schema_chars": sum(len(json.dumps(t.inputSchema, ensure_ascii=False)) for t in tools),
}))
"""


def _probe(profile: str | None) -> dict:
    env = dict(os.environ)
    env.pop("PROJECTMIND_TOOL_PROFILE", None)
    if profile is not None:
        env["PROJECTMIND_TOOL_PROFILE"] = profile
    out = subprocess.run(
        [sys.executable, "-c", PROBE, str(ROOT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        encoding="utf-8",
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def full_probe() -> dict:
    return _probe(None)


def test_full_is_default_and_registers_all_56(full_probe):
    assert full_probe["count"] == 56


def test_explicit_full_equals_default(full_probe):
    assert _probe("full")["count"] == 56


def test_lean_registers_at_most_20_tools():
    lean = _probe("lean")
    assert lean["count"] <= 20, lean["names"]


def test_lean_is_exactly_the_core_reading_set():
    lean = _probe("lean")
    assert set(lean["names"]) == LEAN_EXPECTED


def test_lean_excludes_dangerous_and_admin_tools():
    lean = _probe("lean")
    assert not (set(lean["names"]) & DANGEROUS)
    assert "clear_memory" not in lean["names"]


def test_lean_descriptions_are_shorter_than_full(full_probe):
    lean = _probe("lean")
    assert lean["desc_chars"] < full_probe["desc_chars"] / 2
    assert lean["schema_chars"] < full_probe["schema_chars"]


def test_unknown_profile_falls_back_to_full():
    assert _probe("bogus")["count"] == 56


def test_lean_has_no_set_project_root():
    """Теплий сервер спільний для всіх сесій: set_project_root одного клієнта змінював би корінь для решти (#5, R-007)."""
    lean = _probe("lean")
    assert "set_project_root" not in lean["names"]
    assert len(lean["names"]) == 16
