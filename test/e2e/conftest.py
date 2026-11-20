# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Shared fixtures for the Playwright e2e tests, which run the panel in a real ComfyUI.

Set COMFYUI_PATH to a ComfyUI checkout that has its requirements installed, and the fixture
launches it with this extension loaded, the way `install_submitter.py --link` would. Without
COMFYUI_PATH every test skips, so `hatch run test` and the shared CI stay hermetic. COMFYUI_PYTHON
overrides the interpreter that launches ComfyUI; by default one is looked for inside the checkout.
The panel is compiled first when needed (`npm ci` and `npm run build` in frontend/), and the e2e
hatch environment downloads Playwright's Chromium.

The ComfyUI server gets fake Deadline Cloud tools, like the unit tests', so the extension's
/deadline routes run for real: the fake bridge signs the panel in and gives it a farm and queue
to submit to, and submitting answers with a job ID, all without the real CLI or AWS credentials.
"""

from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent

import pytest
from playwright.sync_api import Page, expect

REPO_ROOT = Path(__file__).resolve().parents[2]
EXTENSION_DIR_NAME = "deadline-cloud-for-comfyui"
WEB_DIRECTORY = REPO_ROOT / "src" / "deadline" / "comfyui_submitter" / "web"
# Where frontend/tsconfig.json compiles WEB_DIRECTORY from: each source's JavaScript lands at
# the same place relative to the web folder as the source relative to this one.
PANEL_SOURCES = REPO_ROOT / "frontend" / "src" / "extensions" / "deadline-cloud"
# How long ComfyUI can take to start, importing torch and its other requirements.
STARTUP_TIMEOUT_SECONDS = 300
# The fake Deadline Cloud account the tests submit to, and the environment variables of
# src/deadline/comfyui_submitter/submit.py that point the server at the fakes.
FARM_ID = "farm-0123456789abcdef0123456789abcdef"
QUEUE_ID = "queue-0123456789abcdef0123456789abcdef"
CLI_ENV_VAR = "COMFYUI_DEADLINE_CLI"
PYTHON_ENV_VAR = "COMFYUI_DEADLINE_PYTHON"


@dataclass
class ComfyUI:
    """A ComfyUI server that has this extension loaded, and a fake Deadline Cloud CLI."""

    url: str
    log: Path
    # Where the /deadline routes create job bundles: the user directory's deadline_submissions.
    submissions: Path

    def log_text(self) -> str:
        """The server's log so far, to report when a test fails."""
        return self.log.read_text(encoding="utf-8", errors="replace")


def _checkout() -> Path | None:
    """The ComfyUI checkout to test in, or None when COMFYUI_PATH isn't set."""
    configured = os.environ.get("COMFYUI_PATH", "").strip()
    if not configured:
        return None
    checkout = Path(configured).expanduser().resolve()
    if not (checkout / "main.py").is_file():
        pytest.fail(f"COMFYUI_PATH {checkout} doesn't look like a ComfyUI checkout (no main.py).")
    return checkout


def _python(checkout: Path) -> str | None:
    """An interpreter that has ComfyUI's requirements installed, or None when there's none."""
    configured = os.environ.get("COMFYUI_PYTHON", "").strip()
    if configured:
        return configured
    for path in (
        checkout / ".venv" / "bin" / "python",
        checkout / ".venv" / "Scripts" / "python.exe",
        checkout / "venv" / "bin" / "python",
        checkout / "venv" / "Scripts" / "python.exe",
    ):
        if path.is_file():
            return str(path)
    return None


def _build_panel() -> None:
    """Compiles the panel's TypeScript into the extension's web folder, when it's missing or
    older than its sources.

    ComfyUI serves everything in the web folder, so the panel counts as compiled only when it
    holds exactly one JavaScript file per TypeScript source, none older than its source: a
    leftover from before a source was renamed would run as if it belonged, and a stale file
    would put the tests a change behind. Anything else clears the folder for a fresh build,
    since tsc never removes what's already there.
    """
    sources = [path for path in PANEL_SOURCES.rglob("*.ts") if not path.name.endswith(".d.ts")]
    outputs = [
        WEB_DIRECTORY / path.relative_to(PANEL_SOURCES).with_suffix(".js") for path in sources
    ]
    up_to_date = set(outputs) == set(WEB_DIRECTORY.rglob("*.js")) and all(
        output.is_file() and output.stat().st_mtime >= source.stat().st_mtime
        for source, output in zip(sources, outputs)
    )
    if up_to_date:
        return
    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("Node.js and npm are needed to build the panel for the e2e tests.")
    frontend = REPO_ROOT / "frontend"
    if not (frontend / "node_modules").is_dir():
        subprocess.run([npm, "ci"], cwd=frontend, check=True)
    shutil.rmtree(WEB_DIRECTORY, ignore_errors=True)
    subprocess.run([npm, "run", "build"], cwd=frontend, check=True)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_extension(url: str, process: subprocess.Popen[bytes], log: Path) -> None:
    """Waits until ComfyUI serves this extension's JavaScript, or fails with the server log."""
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while process.poll() is None:
        try:
            with urllib.request.urlopen(f"{url}/extensions", timeout=10) as response:
                if EXTENSION_DIR_NAME in response.read().decode("utf-8", errors="replace"):
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            # Continue waiting; if the timeout expires, Pytest will fail.
            pass
        if time.monotonic() > deadline:
            break
        time.sleep(1)
    pytest.fail(
        f"ComfyUI didn't serve the {EXTENSION_DIR_NAME} extension. Server log:\n"
        + log.read_text(encoding="utf-8", errors="replace")[-4000:]
    )


def _fake_tools(directory: Path) -> dict[str, str]:
    """
    Fakes of the Deadline Cloud CLI and its Python, like the unit tests', as files for the
    ComfyUI server process. The fake CLI answers --version, and the fake Python stands in for
    bridge.py: every command the routes run gets a logged-in account, a farm and queue to submit
    to, and submitting answers with a job ID. Returns the environment variables that point the
    server at them. The fakes are POSIX shell scripts, so Windows runs without them.
    """
    responses = {
        "status": {
            "authenticationStatus": "AUTHENTICATED",
            "profile": "default",
            "farmId": FARM_ID,
            "queueId": QUEUE_ID,
        },
        "farms": {"farms": [{"farmId": FARM_ID, "displayName": "Test Farm"}]},
        "queues": {"queues": [{"queueId": QUEUE_ID, "displayName": "Test Queue"}]},
        "queue": {"parameters": [], "storageProfiles": []},
        "set-defaults": {},
        "submit": {"jobId": "job-1"},
    }
    bridge = directory / "fake_bridge.py"
    bridge.write_text(
        dedent(f"""\
            import json
            import sys
            json.loads(sys.stdin.readline())
            print(json.dumps({{"event": "result", "data": {responses!r}[sys.argv[-1]]}}))
            """),
        encoding="utf-8",
    )
    # A shell wrapper rather than a shebang, which breaks when the interpreter path has spaces.
    fake_python = directory / "fake_cli_python"
    fake_python.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(bridge))} "$@"\n'
    )
    fake_python.chmod(0o755)
    fake_cli = directory / "fake_deadline"
    fake_cli.write_text('#!/bin/sh\necho "deadline, version 0.60.10"\n')
    fake_cli.chmod(0o755)
    return {CLI_ENV_VAR: str(fake_cli), PYTHON_ENV_VAR: str(fake_python)}


@pytest.fixture(scope="session")
def comfy(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ComfyUI]:
    """A ComfyUI server with this extension loaded, shared by the whole e2e run."""
    checkout = _checkout()
    if checkout is None:
        pytest.skip("Set COMFYUI_PATH to a ComfyUI checkout to run the e2e tests.")
    python = _python(checkout)
    if python is None:
        pytest.skip(
            "ComfyUI's requirements aren't installed where the e2e tests can find them. Install "
            "them into a venv in the ComfyUI checkout, or set COMFYUI_PYTHON."
        )
    _build_panel()

    port = _free_port()
    work = tmp_path_factory.mktemp("comfyui")
    nodes = work / "custom_nodes"
    nodes.mkdir()
    try:
        os.symlink(REPO_ROOT, nodes / EXTENSION_DIR_NAME, target_is_directory=True)
    except (OSError, NotImplementedError):  # For example Windows without developer mode.
        pytest.skip(f"Couldn't link {REPO_ROOT} into {nodes}.")

    command = [
        python,
        "main.py",
        "--cpu",
        "--listen",
        "127.0.0.1",
        "--port",
        str(port),
        "--base-directory",
        str(work),
        "--disable-auto-launch",
    ]
    log = work / "comfyui.log"
    fake_tools = {} if sys.platform == "win32" else _fake_tools(work)
    with log.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            command,
            cwd=checkout,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1", **fake_tools},
        )
        url = f"http://127.0.0.1:{port}"
        try:
            _wait_for_extension(url, process, log)
            yield ComfyUI(url=url, log=log, submissions=work / "user" / "deadline_submissions")
        finally:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


@pytest.fixture(autouse=True)
def comfy_page(comfy: ComfyUI, page: Page) -> None:
    """Opens ComfyUI before each test. Resolves comfy first, so the skip happens before
    Playwright launches a browser."""
    page.goto(comfy.url)
    _close_first_run_dialog(page)


def _close_first_run_dialog(page: Page) -> None:
    """Dismisses the templates dialog ComfyUI opens over a fresh user directory.

    Its overlay covers the whole UI, so any click before it's gone times out. Escape closes it;
    a ComfyUI that stops opening one makes the wait below give up quietly.
    """
    try:
        expect(page.get_by_role("dialog")).to_be_visible(timeout=15_000)
    except AssertionError:
        return
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_be_hidden()


# ComfyUI names the extension's sidebar tab button "<tab id>-tab-button", from the
# SIDEBAR_TAB_ID that frontend/src/extensions/deadline-cloud/deadline_submit.ts registers.
SIDEBAR_TAB_TESTID = "deadline-cloud-tab-button"


def open_panel(page: Page) -> None:
    """Opens the Deadline Cloud sidebar tab, and waits for the panel's root to render.

    The panel root (.dl-panel) is the div that renderPanel in deadline_ui.ts builds.
    """
    page.get_by_test_id(SIDEBAR_TAB_TESTID).click()
    expect(page.locator(".dl-panel")).to_be_visible()
