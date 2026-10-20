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


# --- знахідки змагального проходу #8: крайові шляхи запуску й відмови ---------------


@pytest.mark.timeout(400)
def test_session_init_gives_the_same_refusal_for_every_foreign_path(http_server) -> None:
    # Відмова замка не мусить відрізнятись для існуючої теки, неіснуючої й файлу:
    # інакше сесія питає сервер «чи є там таке» про довільні шляхи поза коренем.
    port, _root_a, root_b = http_server
    (root_b / "secret.txt").write_text("x", encoding="utf-8")
    foreign = [root_b, root_b / "no-such-dir", root_b / "secret.txt"]
    session = _open_session(port)
    try:
        replies = [session.call("session_init", project_path=str(p)) for p in foreign]
    finally:
        session.close()
    for path, reply in zip(foreign, replies, strict=True):
        assert "fixed for this server" in reply, f"{path}: {reply}"
        assert "does not exist" not in reply and "not a directory" not in reply, f"{path}: {reply}"
    assert not (root_b / ".ai").exists()


def _start_http(args: list[str], cwd: Path, extra_env: dict[str, str] | None = None):
    env = _server_env()
    env.update(extra_env or {})
    try:
        return subprocess.run(
            [sys.executable, str(ROOT / "mcp_server.py"), "--transport", "streamable-http"]
            + ["--port", str(_free_port())]
            + args,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"сервер стартував (слухає порт) з непридатним --project-root {args}")


@pytest.mark.timeout(400)
@pytest.mark.parametrize(
    "spelling",
    ["missing", "missing_equals", "empty", "empty_equals", "no_value", "is_a_file"],
)
def test_refused_http_start_leaves_no_trace_in_the_working_directory(
    tmp_path: Path, spelling: str
) -> None:
    cwd = tmp_path / "somewhere_else"
    cwd.mkdir()
    # Куди впаде автовизначення кореня, коли --project-root непридатний: саме там,
    # а не в cwd, раніше з'являлась тека .ai.
    fallback = tmp_path / "fallback_root"
    fallback.mkdir()
    a_file = tmp_path / "a_file.txt"
    a_file.write_text("x", encoding="utf-8")
    missing = str(tmp_path / "no_such_project")
    args = {
        "missing": ["--project-root", missing],
        "missing_equals": [f"--project-root={missing}"],
        "empty": ["--project-root", ""],
        "empty_equals": ["--project-root="],
        "no_value": ["--project-root"],
        "is_a_file": ["--project-root", str(a_file)],
    }[spelling]

    proc = _start_http(args, cwd, {"WORKSPACE_FOLDER": str(fallback)})

    assert proc.returncode != 0, (proc.stdout + proc.stderr)[-1500:]
    for stray in (cwd / ".ai", fallback / ".ai"):
        assert not stray.exists(), f"відмовлений старт створив {stray}"


@pytest.mark.timeout(200)
def test_equals_spelling_picks_the_requested_root_before_anything_is_written(
    tmp_path: Path,
) -> None:
    # config.find_project_root розумів лише `--project-root X`; запис `=X` падав у
    # WORKSPACE_FOLDER, і логер встигав створити .ai/ у теці, якої ніхто не просив.
    wanted = tmp_path / "wanted"
    other = tmp_path / "other"
    wanted.mkdir()
    other.mkdir()
    code = "import config, sys; print('ROOT=' + str(config.PROJECT_ROOT)); import mcp_server"
    env = _server_env()
    env["WORKSPACE_FOLDER"] = str(other)
    env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.run(
        [sys.executable, "-c", code, f"--project-root={wanted}"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=150,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert f"ROOT={wanted.resolve()}" in proc.stdout, proc.stdout
    assert not (other / ".ai").exists(), "побічний ефект у WORKSPACE_FOLDER, якого не просили"


def test_locked_session_init_refuses_a_missing_path_with_the_lock_message(
    root_pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mcp_server

    root_a, root_b = root_pair
    config.lock_project_root()
    calls = _forbid_side_effects(monkeypatch)

    reply = mcp_server.session_init(project_path=str(root_b / "no-such-dir"))

    assert "fixed for this server" in reply and str(root_a) in reply, reply
    assert "does not exist" not in reply
    assert calls == []


@pytest.mark.parametrize("transport", ["streamable-http", "sse"])
def test_any_shared_transport_locks_the_root_even_without_main(
    root_pair, monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    # Вбудовувач/`mcp run` викликає mcp.run(...) мимо main(): замок мусить стояти там,
    # де сервер стає спільним, а не лише в одній точці входу.
    import mcp_server

    started: list[str] = []
    monkeypatch.setattr(mcp_server, "_fastmcp_run", lambda t="stdio", *a, **k: started.append(t))
    assert not config.is_project_root_locked()

    mcp_server.mcp.run(transport=transport)

    assert started == [transport]
    assert config.is_project_root_locked()


def test_stdio_run_does_not_lock_the_root(root_pair, monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_server

    started: list[str] = []
    monkeypatch.setattr(mcp_server, "_fastmcp_run", lambda t="stdio", *a, **k: started.append(t))

    mcp_server.mcp.run()

    assert started == ["stdio"]
    assert not config.is_project_root_locked()


@pytest.mark.parametrize("factory", ["streamable_http_app", "sse_app"])
def test_asgi_apps_lock_the_root_when_they_are_built(root_pair, factory: str) -> None:
    # uvicorn mcp_server:mcp.streamable_http_app — ще один вхід у спільний режим.
    import mcp_server

    assert not config.is_project_root_locked()

    getattr(mcp_server.mcp, factory)()

    assert config.is_project_root_locked()


# --- другий прохід #8: розбір аргумента, відлуння відмови, мовчання stdio ----------------


def test_whitespace_only_project_root_counts_as_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Path("   ").resolve() на Windows дає поточну теку: без обрізання такий рядок
    # приймався б за cwd, і логер створював би там .ai/ до будь-якої відмови.
    monkeypatch.setattr(sys, "argv", ["projectmind", "--project-root", "   "])
    monkeypatch.setattr(config, "ROOT_ARG_REJECTED", None)

    config.find_project_root()

    assert config.ROOT_ARG_REJECTED is not None


def test_cli_does_not_accept_abbreviated_project_root() -> None:
    # config.find_project_root розуміє лише повні форми; argparse не має права приймати
    # скорочення, які config не бачить (два розбирачі одного прапорця розходились).
    import mcp_server

    assert mcp_server.parse_cli_args(["--project-r", "x"]).project_root is None
    assert mcp_server.parse_cli_args(["--project-ro=x"]).project_root is None
    assert mcp_server.parse_cli_args(["--project-root=x"]).project_root == "x"


def test_refusal_does_not_echo_the_requested_path(root_pair) -> None:
    # resolve() нормалізує регістр і йде за junction лише для існуючих складових,
    # тож відлуння шляху в відмові відрізняло «є» від «немає» й розкривало ціль посилання.
    import mcp_server

    _root_a, root_b = root_pair
    config.lock_project_root()
    missing = root_b / "no-such-dir"

    replies = {
        "set existing": mcp_server.set_project_root(str(root_b)),
        "set missing": mcp_server.set_project_root(str(missing)),
        "init existing": mcp_server.session_init(project_path=str(root_b)),
        "init missing": mcp_server.session_init(project_path=str(missing)),
    }

    assert len(set(replies.values())) == 1, replies
    for reply in replies.values():
        assert str(root_b) not in reply and "no-such-dir" not in reply, reply


def test_stdio_reports_a_rejected_project_root(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import mcp_server

    monkeypatch.setattr(config, "ROOT_ARG_REJECTED", "'/definitely/not/here'")

    mcp_server.report_rejected_root_argument()

    err = capsys.readouterr().err
    assert "/definitely/not/here" in err and "ignored" in err, err
    assert str(config.PROJECT_ROOT) in err, err


def test_health_names_a_rejected_project_root(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_server

    monkeypatch.setattr(config, "ROOT_ARG_REJECTED", "'/definitely/not/here'")

    assert "/definitely/not/here" in mcp_server.health()


def test_nothing_is_reported_when_the_project_root_was_fine(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import mcp_server

    monkeypatch.setattr(config, "ROOT_ARG_REJECTED", None)

    mcp_server.report_rejected_root_argument()

    assert capsys.readouterr().err == ""


def test_tilde_in_project_root_is_expanded_the_same_way_in_config_and_in_http_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # fix_project_root_for_http розкривав `~`, а config — ні: сервер працював з правильним
    # коренем, а health казав, що аргумент «проігноровано».
    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(sys, "argv", ["projectmind", "--project-root", "~/proj"])
    monkeypatch.setattr(config, "ROOT_ARG_REJECTED", None)

    found = config.find_project_root()

    assert found == (home / "proj").resolve()
    assert config.ROOT_ARG_REJECTED is None


def test_a_file_given_as_project_root_is_rejected_not_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # config приймав будь-який існуючий шлях, а set_project_root і http-фіксація — лише теку:
    # у stdio файл мовчки ставав коренем без жодного попередження.
    a_file = tmp_path / "not_a_dir.txt"
    a_file.write_text("x", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["projectmind", "--project-root", str(a_file)])
    monkeypatch.setattr(config, "ROOT_ARG_REJECTED", None)

    found = config.find_project_root()

    assert found != a_file.resolve()
    assert config.ROOT_ARG_REJECTED is not None


def test_config_stops_reading_arguments_at_the_double_dash() -> None:
    # argparse лишає все після `--` позиційним; config читав далі й бачив прапор, якого
    # argparse не бачив, — два розбирачі одного прапорця давали різні корені.
    assert config._root_argument(["x", "--", "--project-root", "y"]) == (False, None)
    assert config._root_argument(["x", "--project-root", "y", "--", "--project-root", "z"]) == (
        True,
        "y",
    )


def test_http_start_follows_the_verdict_of_config_not_only_of_argparse(root_pair) -> None:
    # Якщо config побачив --project-root і відхилив його, а argparse нічого не бачить,
    # http-старт не може мовчки зафіксувати автовизначений корінь.
    import mcp_server

    args = mcp_server.parse_cli_args(["--transport", "streamable-http"])
    assert args.project_root is None
    config.ROOT_ARG_REJECTED = "'/not/a/dir'"
    try:
        with pytest.raises(SystemExit) as exc:
            mcp_server.fix_project_root_for_http(args)
    finally:
        config.ROOT_ARG_REJECTED = None

    assert exc.value.code not in (0, None)
    assert not config.is_project_root_locked()


def test_environment_root_that_is_a_file_is_not_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Той самий розрив exists()/is_dir(), що й для прапорця, але для змінних середовища.
    a_file = tmp_path / "not_a_dir.txt"
    a_file.write_text("x", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["projectmind"])
    for name in ("WORKSPACE_FOLDER", "PROJECT_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PROJECT_ROOT", str(a_file))

    assert config.find_project_root() != a_file.resolve()


def test_http_start_refuses_to_lock_a_root_that_is_not_a_directory(root_pair) -> None:
    import mcp_server

    root_a, _root_b = root_pair
    a_file = root_a / "not_a_dir.txt"
    a_file.write_text("x", encoding="utf-8")
    config.PROJECT_ROOT = a_file  # те, що дав би будь-який шлях автовизначення
    args = mcp_server.parse_cli_args(["--transport", "streamable-http"])

    with pytest.raises(SystemExit) as exc:
        mcp_server.fix_project_root_for_http(args)

    assert exc.value.code not in (0, None)
    assert not config.is_project_root_locked()
