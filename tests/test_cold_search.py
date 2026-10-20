"""
Первий пошук не має імпортувати numpy (через rank_bm25) у робочому потоці.

Раніше `warmup.ensure_loaded()` на головному потоці не чіпав rank_bm25, тож
`BM25Index.build()` тягнув numpy вже в потоці інструмента — саме той шлях,
на якому на Windows імпорт розширення numpy зависає. Тест запускає окремий
інтерпретатор, підставляє заглушку rank_bm25, що пише, з якого потоку її
імпортували, і перевіряє, що це головний потік ще до першого пошуку.
Заглушка робить тест незалежним від того, встановлено rank_bm25 чи ні.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

STUB = (
    "import sys, threading\n"
    "sys.stdout.write('LOADED main=%s\\n' % (threading.current_thread() is threading.main_thread()))\n"
)

PROBE = (
    "import threading, warmup\n"
    "warmup.ensure_loaded()\n"
    "def worker():\n"
    "    import rank_bm25  # то, що робить BM25Index.build у потоці інструмента\n"
    "t = threading.Thread(target=worker)\n"
    "t.start(); t.join(30)\n"
    "print('DONE')\n"
)


# Холодний прогрів важких імпортів сам триває 30–40 с, тож типова стеля 30 с червонить тест хибно.
@pytest.mark.timeout(300)
def test_rank_bm25_is_imported_on_main_thread_by_warmup(tmp_path: Path) -> None:
    (tmp_path / "rank_bm25.py").write_text(STUB, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), str(REPO)])
    env["PROJECT_ROOT"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    out = proc.stdout
    assert "DONE" in out, f"probe did not finish:\n{proc.stdout}\n{proc.stderr[-2000:]}"
    loads = [line for line in out.splitlines() if line.startswith("LOADED")]
    assert loads == ["LOADED main=True"], (
        "rank_bm25 (numpy) must be imported by warmup.ensure_loaded() on the main "
        f"thread, got {loads!r}"
    )
