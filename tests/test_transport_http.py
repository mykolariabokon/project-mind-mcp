"""Транспорт streamable-http: запуск процесу, MCP initialize по HTTP, зупинка.

Типовий транспорт — stdio (покрито test_stdio.py). Тут перевіряємо, що
--transport streamable-http піднімає HTTP-ендпоінт /mcp на 127.0.0.1 і що
initialize через HTTP відповідає успішно.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
BOOT_BUDGET_SECONDS = 150.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, proc: subprocess.Popen, budget: float) -> None:
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"процес завершився з кодом {proc.returncode} до відкриття порту")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise AssertionError(f"порт {port} не відкрився за {budget:.0f} с")


def _parse_mcp_body(response: httpx.Response) -> dict:
    """Відповідь — JSON або SSE-потік з рядком data: ..."""
    if "text/event-stream" in response.headers.get("content-type", ""):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        raise AssertionError(f"SSE без data-рядка: {response.text[:500]}")
    return response.json()


@pytest.mark.timeout(300)
def test_streamable_http_initialize_round_trip():
    port = _free_port()
    env = dict(os.environ)
    env.pop("PROJECTMIND_TOOL_PROFILE", None)
    log_dir = Path(tempfile.mkdtemp(prefix="pm-http-"))
    out_f = open(log_dir / "stdout.txt", "wb")
    err_f = open(log_dir / "stderr.txt", "wb")
    proc = subprocess.Popen(
        [sys.executable, "mcp_server.py", "--transport", "streamable-http", "--port", str(port)],
        cwd=str(ROOT),
        env=env,
        stdin=subprocess.PIPE,
        stdout=out_f,
        stderr=err_f,
    )
    try:
        _wait_for_port(port, proc, BOOT_BUDGET_SECONDS)

        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0.0"},
            },
        }
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=60) as client:
            resp = client.post(f"http://127.0.0.1:{port}/mcp", json=body, headers=headers)

        assert resp.status_code == 200, resp.text[:500]
        payload = _parse_mcp_body(resp)
        assert "error" not in payload, payload
        result = payload["result"]
        assert result["serverInfo"]["name"] == "ProjectMind"
        assert "capabilities" in result
    finally:
        proc.kill()
        proc.wait(timeout=30)
        out_f.close()
        err_f.close()


def test_default_host_is_loopback():
    import argparse

    import mcp_server

    args = mcp_server.parse_cli_args([])
    assert args.transport == "stdio"
    assert args.host == "127.0.0.1"
    assert args.port == 8000
