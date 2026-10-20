"""Перевірка випуску: колесо збирається, ставиться в ЧИСТИЙ venv і сервер стартує.

Саме цього бракувало 0.11.0: тести в робочому дереві були зелені, а встановлений
пакет не стартував (`No module named 'warmup'`), бо модуля не було в колесі.

Що робить (нічого не публікує):
  1. копіює дерево в тимчасову теку (без build/ і egg-info) і збирає колесо;
  2. створює порожній venv і ставить туди колесо разом із залежностями з PyPI;
  3. запускає `projectmind-mcp --help` з цього venv;
  4. запускає сервер по stdio з теки, де нема коду проєкту, і чекає на відповідь
     `initialize` (холодний старт — десятки секунд).

Запуск:  python scripts/verify_release.py [--keep]
Код виходу 0 — усе гаразд; інакше назва кроку, що впав, і хвіст його виводу.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOT_BUDGET_SECONDS = 180.0


def run(step: str, argv: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    print(f"[{step}] {' '.join(argv)}", flush=True)
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr)[-3000:]
        raise SystemExit(f"FAIL [{step}] код {proc.returncode}:\n{tail}")
    return proc


def build_wheel(work: Path) -> Path:
    src = work / "src"
    src.mkdir()
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(ROOT / name, src / name)
    for module in ROOT.glob("*.py"):
        shutil.copy2(module, src / module.name)
    out = work / "dist"
    run(
        "збірка колеса",
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "-w",
            str(out),
            ".",
        ],
        src,
        600,
    )
    wheels = sorted(out.glob("projectmind_mcp-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"FAIL [збірка колеса] очікував одне колесо, є: {wheels}")
    return wheels[0]


def venv_script(venv: Path, name: str) -> Path:
    scripts = venv / ("Scripts" if sys.platform == "win32" else "bin")
    return scripts / (name + (".exe" if sys.platform == "win32" else ""))


def stdio_initialize(exe: Path, cwd: Path) -> dict:
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "verify-release", "version": "1.0.0"},
        },
    }
    print(f"[старт] {exe} (stdio, бюджет {BOOT_BUDGET_SECONDS:.0f} с)", flush=True)
    proc = subprocess.Popen(
        [str(exe), "--project-root", str(cwd)],
        cwd=str(cwd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    started = time.monotonic()
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        elapsed = time.monotonic() - started
        if not line:
            err = proc.stderr.read() if proc.stderr else ""
            raise SystemExit(f"FAIL [старт] сервер завершився без відповіді:\n{err[-3000:]}")
        if elapsed > BOOT_BUDGET_SECONDS:
            raise SystemExit(
                f"FAIL [старт] initialize за {elapsed:.0f} с (бюджет {BOOT_BUDGET_SECONDS:.0f})"
            )
        reply = json.loads(line)
        print(f"[старт] initialize за {elapsed:.1f} с", flush=True)
        return reply
    finally:
        proc.kill()
        proc.wait(timeout=30)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--keep", action="store_true", help="не видаляти тимчасову теку")
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="pm-release-"))
    try:
        wheel = build_wheel(work)
        print(f"колесо: {wheel.name}", flush=True)

        venv = work / "venv"
        run("чистий venv", [sys.executable, "-m", "venv", str(venv)], work, 300)
        python = venv_script(venv, "python")
        run("встановлення колеса", [str(python), "-m", "pip", "install", str(wheel)], work, 1800)

        exe = venv_script(venv, "projectmind-mcp")
        help_out = run("--help", [str(exe), "--help"], work, 300).stdout
        if "--project-root" not in help_out:
            raise SystemExit(f"FAIL [--help] у довідці немає --project-root:\n{help_out}")

        project = work / "empty-project"
        project.mkdir()
        reply = stdio_initialize(exe, project)
        info = reply.get("result", {}).get("serverInfo", {})
        if info.get("name") != "ProjectMind":
            raise SystemExit(f"FAIL [старт] неочікувана відповідь initialize: {reply}")
        print(f"OK: {wheel.name} стартує у чистому venv, serverInfo={info}")
        return 0
    finally:
        if args.keep:
            print(f"тека лишена: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
