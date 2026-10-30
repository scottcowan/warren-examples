"""AJ2 harness: a real warren-server (scripted in-memory runtime, reaper on) in its own
process and environment, on 127.0.0.1, so the frameworks share nothing with warren
but HTTP. Tokens: a reservation token for the framework, and two other workspaces
(one the same owner's, one another user's) it must not be able to touch."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

os.environ["HOME"] = tempfile.mkdtemp(prefix="warren-frameworks-home-")
for _var in ("AWS_PROFILE", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(_var, None)
os.environ.update(CREWAI_DISABLE_TELEMETRY="true", CREWAI_TRACING_ENABLED="false", OTEL_SDK_DISABLED="true")

_THIS_REPO = Path(__file__).resolve().parents[1]
# Moved out of the warren monorepo into this standalone repo (2026-10-30, to keep
# warren's own Dependabot alerts free of this project's example-framework
# dependencies -- notably chromadb, an unpatched transitive dep of crewai with no
# fixed release upstream as of writing). The acceptance tests still need a real
# checkout of github.com/scottcowan/warren next to this one (its own uv workspace,
# `tests.control_plane_server` included, isn't a published artifact) -- set
# WARREN_REPO_ROOT explicitly, or clone warren as a sibling directory
# (../warren relative to this repo's own root) for the default to find it.
_REPO = Path(os.environ.get("WARREN_REPO_ROOT", str(_THIS_REPO.parent / "warren")))
_SERVER_PKG = _REPO / "packages" / "warren-server"
if not _SERVER_PKG.is_dir():
    raise RuntimeError(
        f"warren-server checkout not found at {_SERVER_PKG} -- clone "
        "github.com/scottcowan/warren as a sibling directory (../warren), or set "
        "WARREN_REPO_ROOT to point at an existing checkout."
    )
ADMIN_TOKEN = "test-bootstrap-token000000000000"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


@dataclass
class Warren:
    base_url: str
    mcp_url: str
    reservation: dict
    sibling_workspace: str
    other_users_workspace: str
    other_user_token: str

    def admin(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}, timeout=30)

    def status(self, workspace_id: str, token: str = ADMIN_TOKEN) -> str:
        resp = httpx.get(
            f"{self.base_url}/workspaces/{workspace_id}/status",
            headers={"Authorization": f"Bearer {token}"}, timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["status"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _server_command() -> list[str]:
    python = os.environ.get("WARREN_SERVER_PYTHON")
    if python:
        return [python]
    return ["uv", "run", "--frozen", "--package", "warren-server", "--extra", "dev", "python"]


@pytest.fixture
def warren(tmp_path) -> Warren:
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT")}
    env["HOME"] = str(tmp_path / "home")
    log = open(tmp_path / "server.log", "w")
    proc = subprocess.Popen(
        [*_server_command(), "-m", "tests.control_plane_server", str(port), str(tmp_path / "state")],
        cwd=_SERVER_PKG, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 90
        while True:
            if proc.poll() is not None:
                raise RuntimeError(f"warren-server exited: {(tmp_path / 'server.log').read_text()}")
            try:
                if httpx.get(f"{base}/healthz", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("warren-server did not start")
            time.sleep(0.3)

        with httpx.Client(base_url=base, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}, timeout=30) as admin:
            sibling = admin.post("/workspaces", json={"project": "demo", "template": "observe"}).json()["workspace_id"]
            bob = admin.post("/admin/users", json={"username": "bob"}).json()["token"]["token"]
            other = httpx.post(
                f"{base}/workspaces", json={"project": "demo", "template": "observe"},
                headers={"Authorization": f"Bearer {bob}"}, timeout=30,
            ).json()["workspace_id"]
            reservation = admin.post("/workspace-tokens", json={"ttl_seconds": 900}).json()
        yield Warren(base, f"{base}/mcp", reservation, sibling, other, bob)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        if sys.exc_info()[0] is not None:
            print((tmp_path / "server.log").read_text())
