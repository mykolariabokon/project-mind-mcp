"""У режимі streamable-http корінь проєкту зафіксований аргументом --project-root.

Теплий http-сервер спільний для всіх сесій, а `config.PROJECT_ROOT` — один на
процес. Якщо одна сесія може його змінити, наступні сесії (окремий
`initialize`) побачать чужий корінь. Корінь міняють два інструменти:
`set_project_root` і `session_init(project_path=...)`.

Основні перевірки йдуть через справжній процес сервера, як у
test_transport_http.py: сесія A намагається змінити корінь, сесія B через
окремий `initialize` мусить бачити свій, незмінений. Решта — у цьому ж процесі
тестів, з явною перевіркою побічних ефектів відмови.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

import config
from tests.test_transport_http import _free_port, _parse_mcp_body, _wait_for_port

ROOT = Path(__file__).resolve().parent.parent
BOOT_BUDGET_SECONDS = 150.0
HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


class McpHttpSession:
    """Одна MCP-сесія поверх streamable-http: власний `initialize`, власний id сесії."""

    def __init__(self, port: int) -> None:
        self.url = f"http://127.0.0.1:{port}/mcp"
        self.client = httpx.Client(timeout=120)
        self.session_id: str | None = None
        self._next_id = 1

    def _post(self, body: dict) -> httpx.Response:
        headers = dict(HEADERS)
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        return self.client.post(self.url, json=body, headers=headers)

    def initialize(self) -> None:
        resp = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._take_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1.0.0"},
                },
            }
        )
        assert resp.status_code == 200, resp.text[:500]
        self.session_id = resp.headers.get("mcp-session-id")
        assert (
            self.session_id
        ), "сервер не видав mcp-session-id: сесій немає, тест нічого не доводить"
        note = self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert note.status_code in (200, 202), note.text[:500]

    def _take_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def call(self, tool: str, **arguments: object) -> str:
        resp = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._take_id(),
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        )
        assert resp.status_code == 200, resp.text[:500]
        payload = _parse_mcp_body(resp)
        assert "error" not in payload, payload
        content = payload["result"]["content"]
        return "\n".join(part.get("text", "") for part in content)

    def close(self) -> None:
        self.client.close()


def _server_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in ("PROJECTMIND_TOOL_PROFILE", "WORKSPACE_FOLDER", "PROJECT_ROOT", "PROJECT_PATH"):
        env.pop(name, None)
    return env


@pytest.fixture(scope="module")
def http_server() -> Iterator[tuple[int, Path, Path]]:
    """Один процесс streamable-http (профіль full) з коренем A; B — «чужий» проєкт."""
    work = Path(tempfile.mkdtemp(prefix="pm-rootlock-")).resolve()
    root_a = work / "project_a"
    root_b = work / "project_b"
    for root in (root_a, root_b):
        root.mkdir()
        (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    port = _free_port()
    out_f = open(work / "stdout.txt", "wb")
    err_f = open(work / "stderr.txt", "wb")
    proc = subprocess.Popen(
        [
            sys.executable,
            "mcp_server.py",
            "--transport",
            "streamable-http",
            "--port",
            str(port),
            "--project-root",
            str(root_a),
        ],
        cwd=str(ROOT),
        env=_server_env(),
        stdin=subprocess.PIPE,
        stdout=out_f,
        stderr=err_f,
    )
    try:
        _wait_for_port(port, proc, BOOT_BUDGET_SECONDS)
        yield port, root_a, root_b
    finally:
        proc.kill()
        proc.wait(timeout=30)
        out_f.close()
        err_f.close()


def _open_session(port: int) -> McpHttpSession:
    session = McpHttpSession(port)
    session.initialize()
    return session


@pytest.mark.timeout(400)
def test_set_project_root_in_one_session_does_not_change_root_for_another(http_server) -> None:
    port, root_a, root_b = http_server
    session_a = _open_session(port)
    try:
        reply = session_a.call("set_project_root", path=str(root_b))
        assert str(root_a) in reply, f"відмова мусить назвати зафіксований корінь: {reply}"

        session_b = _open_session(port)
        try:
            health = session_b.call("health")
        finally:
            session_b.close()
        assert f"`{root_a}`" in health, health
        assert str(root_b) not in health, health
        assert not (root_b / ".ai").exists(), "відмова не мала нічого створювати в чужому проєкті"
    finally:
        session_a.close()


@pytest.mark.timeout(400)
def test_session_init_with_other_project_path_is_refused(http_server) -> None:
    port, root_a, root_b = http_server
    session_a = _open_session(port)
    try:
        reply = session_a.call("session_init", project_path=str(root_b))
        assert str(root_a) in reply, f"відмова мусить назвати зафіксований корінь: {reply}"
        assert "Session Init" not in reply, "session_init не мав відпрацювати для чужого кореня"

        session_b = _open_session(port)
        try:
            health = session_b.call("health")
        finally:
            session_b.close()
        assert f"`{root_a}`" in health, health
        assert str(root_b) not in health, health
        assert not (root_b / ".ai").exists()
    finally:
        session_a.close()


@pytest.mark.timeout(400)
def test_session_init_with_the_fixed_root_still_works(http_server) -> None:
    # Контроль: відмова не може бути просто «сервер мертвий» або «session_init зламаний».
    port, root_a, _root_b = http_server
    session = _open_session(port)
    try:
        reply = session.call("session_init", project_path=str(root_a))
        assert "Session Init" in reply, reply
        assert str(root_a) in reply, reply
    finally:
        session.close()


@pytest.mark.timeout(120)
def test_http_server_refuses_to_start_when_project_root_does_not_exist(tmp_path: Path) -> None:
    # Раніше неіснуючий --project-root мовчки замінювався коренем з cwd. Для зафіксованого
    # кореня це означало б тихо обслуговувати не той проєкт.
    missing = tmp_path / "no_such_project"
    proc = subprocess.run(
        [
            sys.executable,
            "mcp_server.py",
            "--transport",
            "streamable-http",
            "--port",
            str(_free_port()),
            "--project-root",
            str(missing),
        ],
        cwd=str(ROOT),
        env=_server_env(),
        capture_output=True,
        text=True,
        timeout=100,
    )
    assert proc.returncode != 0, "сервер мусив відмовитись стартувати"
    assert str(missing) in (proc.stderr + proc.stdout), proc.stderr[-2000:]


# --- у цьому ж процесі: відмова без побічних ефектів, і stdio як було ---------------


@pytest.fixture
def root_pair(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    original = config.PROJECT_ROOT
    was_locked = getattr(config, "_ROOT_LOCKED", False)
    root_a = (tmp_path / "a").resolve()
    root_b = (tmp_path / "b").resolve()
    root_a.mkdir()
    root_b.mkdir()
    config._ROOT_LOCKED = False
    config.reconfigure(root_a)
    try:
        yield root_a, root_b
    finally:
        config._ROOT_LOCKED = False
        config.reconfigure(original)
        config._ROOT_LOCKED = was_locked


def _forbid_side_effects(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import mcp_server

    calls: list[str] = []

    def spy(name: str):
        def _spy(*_args, **_kwargs):
            calls.append(name)

        return _spy

    monkeypatch.setattr(mcp_server, "reset_context", spy("reset_context"))
    monkeypatch.setattr(mcp_server, "_stop_background_indexing", spy("_stop_background_indexing"))
    monkeypatch.setattr(mcp_server, "_reset_indexing_state_for_new_root", spy("_reset_state"))
    return calls


def test_locked_set_project_root_refuses_before_any_side_effect(
    root_pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mcp_server

    root_a, root_b = root_pair
    config.lock_project_root()
    calls = _forbid_side_effects(monkeypatch)

    reply = mcp_server.set_project_root(str(root_b))

    assert config.PROJECT_ROOT == root_a
    assert str(root_a) in reply and "--project-root" in reply, reply
    assert calls == [], f"відмова зачепила спільний стан: {calls}"
    assert not (root_b / ".ai").exists()


def test_locked_set_project_root_to_the_same_root_does_not_reset_shared_state(
    root_pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mcp_server

    root_a, _root_b = root_pair
    config.lock_project_root()
    calls = _forbid_side_effects(monkeypatch)

    reply = mcp_server.set_project_root(str(root_a))

    assert config.PROJECT_ROOT == root_a
    assert str(root_a) in reply, reply
    assert calls == [], f"повторний виклик скинув контекст усіх сесій: {calls}"


def test_locked_reconfigure_raises_and_keeps_root(root_pair) -> None:
    # Нижній шар: навіть шлях повз інструменти не змінює зафіксований корінь.
    root_a, root_b = root_pair
    config.lock_project_root()

    with pytest.raises(config.ProjectRootLockedError):
        config.reconfigure(root_b)

    assert config.PROJECT_ROOT == root_a
    assert config.AI_DIR == root_a / ".ai"
    config.reconfigure(root_a)  # той самий корінь — не зміна


def test_unlocked_set_project_root_still_switches_root_as_before(
    root_pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    # stdio-режим не змінюється: без блокування корінь міняється, як і раніше.
    import mcp_server

    _root_a, root_b = root_pair
    monkeypatch.setattr(mcp_server, "ensure_startup", lambda: None)
    monkeypatch.setattr(mcp_server, "_ensure_default_indexignore", lambda: None)

    reply = mcp_server.set_project_root(str(root_b))

    assert config.PROJECT_ROOT == root_b
    assert str(root_b) in reply


def test_cli_accepts_project_root_in_both_spellings() -> None:
    import mcp_server

    assert mcp_server.parse_cli_args(["--project-root", "x"]).project_root == "x"
    assert mcp_server.parse_cli_args(["--project-root=x"]).project_root == "x"
    assert mcp_server.parse_cli_args([]).project_root is None


def test_fix_root_for_http_locks_the_root_and_applies_the_equals_form(
    root_pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    # config.find_project_root розуміє лише `--project-root X`; форма `--project-root=X`
    # мовчки давала б корінь з cwd. main() мусить зафіксувати саме той, що просили.
    import mcp_server

    _root_a, root_b = root_pair
    args = mcp_server.parse_cli_args(["--transport", "streamable-http", f"--project-root={root_b}"])

    mcp_server.fix_project_root_for_http(args)

    assert config.PROJECT_ROOT == root_b
    assert config.is_project_root_locked()
    with pytest.raises(config.ProjectRootLockedError):
        config.reconfigure(root_b.parent)


def test_fix_root_for_http_exits_when_root_is_missing(root_pair) -> None:
    import mcp_server

    root_a, _root_b = root_pair
    args = mcp_server.parse_cli_args(
        ["--transport", "streamable-http", "--project-root", str(root_a / "missing")]
    )

    with pytest.raises(SystemExit) as exc:
        mcp_server.fix_project_root_for_http(args)

    assert exc.value.code not in (0, None)
    assert not config.is_project_root_locked()


def test_structure_cache_does_not_leak_between_roots(root_pair) -> None:
    # R-009: кеш analyze_project_structure жив 5 хвилин і не знав про корінь.
    import mcp_server

    root_a, root_b = root_pair
    (root_a / "alpha_pkg").mkdir()
    (root_a / "alpha_pkg" / "one.py").write_text("x = 1\n", encoding="utf-8")
    (root_b / "beta_pkg").mkdir()
    (root_b / "beta_pkg" / "two.py").write_text("y = 2\n", encoding="utf-8")
    mcp_server._structure_cache = None

    first = mcp_server.analyze_project_structure()
    config.reconfigure(root_b)
    second = mcp_server.analyze_project_structure()

    assert "alpha_pkg" in first
    assert "beta_pkg" in second, f"кеш віддав структуру попереднього проєкту:\n{second}"
    assert "alpha_pkg" not in second
