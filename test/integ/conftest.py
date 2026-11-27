# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Shared fixtures for the Playwright integration tests, which run the panel in a real ComfyUI.

Set COMFYUI_PATH to a ComfyUI checkout that has its requirements installed, and the fixture
launches it with this extension loaded, the way `install_submitter.py --link` would. Without
COMFYUI_PATH every test skips, so `hatch run test` and the shared CI stay hermetic. COMFYUI_PYTHON
overrides the interpreter that launches ComfyUI; by default one is looked for inside the checkout.
The panel is compiled first when needed (`npm ci` and `npm run build` in frontend/), and the integ
hatch environment downloads Playwright's Chromium.

The ComfyUI server gets fake Deadline Cloud tools, like the unit tests', so the extension's
/deadline routes run for real: the fake bridge signs the panel in and gives it a farm and queue
to submit to, and submitting answers with a job ID, all without the real CLI or AWS credentials.
The queue's details come from the real bridge's queue command, with the library's answers faked.
The server deletes a job bundle once it's submitted, so the fake bridge copies each one first,
with the request it got, for the tests to check.
"""

from __future__ import annotations

import json
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

import pytest
from playwright.sync_api import Locator, Page, expect

REPO_ROOT = Path(__file__).resolve().parents[2]
EXTENSION_DIR_NAME = "deadline-cloud-for-comfyui"
WEB_DIRECTORY = REPO_ROOT / "src" / "deadline" / "comfyui_submitter" / "web"
# Where frontend/tsconfig.json compiles WEB_DIRECTORY from: each source's JavaScript lands at
# the same place relative to the web folder as the source relative to this one.
PANEL_SOURCES = REPO_ROOT / "frontend" / "src" / "extensions" / "deadline-cloud"
# How long ComfyUI can take to start, importing torch and its other requirements.
STARTUP_TIMEOUT_SECONDS = 300
# The fake Deadline Cloud account the tests submit to, and the environment variables of
# src/deadline/comfyui_submitter/submit.py that point the server at the fakes. The server gets
# none of the others from the test run, so it uses the repository's job template.
FARM_ID = "farm-0123456789abcdef0123456789abcdef"
QUEUE_ID = "queue-0123456789abcdef0123456789abcdef"
# The fake queue's environment parameters, as the library lists a Conda queue environment's.
QUEUE_PARAMETERS = [
    {
        "name": "CondaChannels",
        "type": "STRING",
        "default": "deadline-cloud",
        "userInterface": {
            "control": "LINE_EDIT",
            "label": "Conda Channels",
            "groupLabel": "Queue Environment: Conda",
        },
    }
]
ENV_VAR_PREFIX = "COMFYUI_DEADLINE_"
CLI_ENV_VAR = "COMFYUI_DEADLINE_CLI"
PYTHON_ENV_VAR = "COMFYUI_DEADLINE_PYTHON"
CONFIG_ENV_VAR = "COMFYUI_DEADLINE_CONFIG"
# Where the installers unpack the Deadline Cloud library. The server runs the bridge with it
# instead of the fake CLI's Python when it's there, so the tests refuse to run with it.
INSTALLED_DEPENDENCIES = REPO_ROOT / "dependencies"
WORKFLOWS = Path(__file__).resolve().parent / "workflows"


@dataclass
class ComfyUI:
    """A ComfyUI server that has this extension loaded, and a fake Deadline Cloud CLI."""

    url: str
    # ComfyUI's --base-directory, with its input, output and user directories.
    base_directory: Path
    # Where the fake bridge copies each submitted bundle, as <job ID>/bundle, with the request it
    # got as <job ID>/request.json.
    submitted: Path
    # Whether a page has closed the templates dialog, which ComfyUI then doesn't open again.
    closed_first_run_dialog: bool = False


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
        pytest.skip("Node.js and npm are needed to build the panel for the integration tests.")
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


# Stands in for bridge.py, with the same protocol: the request on the first line of stdin, and a
# "result" or "error" event on stdout. fake_bridge.json next to it has the answers and where to
# copy submitted bundles.
FAKE_BRIDGE = """\
import json
import shutil
import sys
import types
import uuid
from pathlib import Path

config = json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))
command = sys.argv[-1]
request = json.loads(sys.stdin.readline() or "{}")
captured = Path(config["submitted"])


def result(data):
    print(json.dumps({"event": "result", "data": data}), flush=True)


if command == "submit":
    # A job ID as Deadline Cloud makes them. The server deletes the bundle after this answer.
    job_id = "job-" + uuid.uuid4().hex
    shutil.copytree(request["bundleDir"], captured / job_id / "bundle")
    (captured / job_id / "request.json").write_text(json.dumps(request), encoding="utf-8")
    result({"jobId": job_id, "farmId": request.get("farmId"), "queueId": request.get("queueId")})
elif command == "queue":
    # The real bridge's answer, from the fake queue's parameters, so the job gets the values the
    # bridge adds, such as Conda Channels with deadline-cloud-v2.
    library = types.SimpleNamespace(
        get_queue_parameter_definitions=lambda **kwargs: config["queueParameters"],
        list_storage_profiles_for_queue=lambda **kwargs: {"storageProfiles": []},
    )
    settings = types.SimpleNamespace(read_config=dict)
    sys.modules["deadline.client"] = types.SimpleNamespace(api=library)
    sys.modules["deadline.client.api"] = library
    sys.modules["deadline.client.config"] = types.SimpleNamespace(config_file=settings)
    sys.modules["deadline.client.config.config_file"] = settings
    from deadline.comfyui_submitter import bridge

    result(bridge.queue(request))
elif command in config["responses"]:
    result(config["responses"][command])
else:
    print(json.dumps({"event": "error", "message": f"The fake bridge has no {command}."}))
    sys.exit(1)
"""


def _fake_tools(directory: Path, submitted: Path) -> dict[str, str]:
    """
    Fakes of the Deadline Cloud CLI and its Python, like the unit tests', as files for the
    ComfyUI server process. The fake CLI answers --version, and the fake Python stands in for
    bridge.py: every command the routes run gets a logged-in account, a farm and queue to submit
    to, and submitting copies the bundle into submitted and answers with a job ID. Returns the
    environment variables that point the server at them. The fakes are POSIX shell scripts, so
    the tests skip on Windows.
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
        "set-defaults": {},
    }
    bridge = directory / "fake_bridge.py"
    bridge.write_text(FAKE_BRIDGE, encoding="utf-8")
    fakes = {
        "responses": responses,
        "queueParameters": QUEUE_PARAMETERS,
        "submitted": str(submitted),
    }
    bridge.with_suffix(".json").write_text(json.dumps(fakes), encoding="utf-8")
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
    """A ComfyUI server with this extension loaded, shared by the whole integration run."""
    if os.name == "nt":
        pytest.skip("The fake Deadline Cloud CLI and bridge are POSIX shell scripts.")
    checkout = _checkout()
    if checkout is None:
        pytest.skip("Set COMFYUI_PATH to a ComfyUI checkout to run the integration tests.")
    python = _python(checkout)
    if python is None:
        pytest.skip(
            "ComfyUI's requirements aren't installed where the integration tests can find them. Install "
            "them into a venv in the ComfyUI checkout, or set COMFYUI_PYTHON."
        )
    if INSTALLED_DEPENDENCIES.is_dir():
        pytest.fail(
            f"{INSTALLED_DEPENDENCIES} would make the server submit with the real Deadline Cloud "
            "library rather than the fake bridge. Move it away to run the integration tests."
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
    submitted = work / "submitted"
    submitted.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith(ENV_VAR_PREFIX)}
    env.update(_fake_tools(work, submitted))
    # A submitter config the checkout may have, from install_submitter.py, isn't read.
    env[CONFIG_ENV_VAR] = str(work / "no_submitter_config.json")
    with log.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            command,
            cwd=checkout,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env={**env, "PYTHONUNBUFFERED": "1"},
        )
        url = f"http://127.0.0.1:{port}"
        try:
            _wait_for_extension(url, process, log)
            yield ComfyUI(url=url, base_directory=work, submitted=submitted)
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
    open_comfyui(page, comfy)


def open_comfyui(page: Page, comfy: ComfyUI) -> None:
    """Opens ComfyUI in a page, and waits until its app has loaded the graph."""
    page.goto(comfy.url)
    page.wait_for_function("() => !!window.app?.graph")
    # ComfyUI saves that the dialog was closed in the server's user settings.
    if not comfy.closed_first_run_dialog:
        _close_first_run_dialog(page)
        comfy.closed_first_run_dialog = True


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


def load_workflow(page: Page, workflow: Path) -> None:
    """Opens a workflow file through the topbar's file input, as Workflow > Open does."""
    page.set_input_files("#comfy-file-input", str(workflow))


def open_panel(page: Page) -> Locator:
    """Opens the Deadline Cloud sidebar tab, and returns the panel once its root renders.

    The panel root (.dl-panel) is the div that renderPanel in deadline_ui.ts builds.
    """
    page.get_by_test_id(SIDEBAR_TAB_TESTID).click()
    panel = page.locator(".dl-panel")
    expect(panel).to_be_visible()
    return panel


def open_popover(page: Page) -> Locator:
    """Opens the quick submit popover from the action bar button, and returns it.

    The button has the dl-action-button class that deadline_submit.ts registers, and the popover
    (.dl-popover) is the dialog that openPopover in deadline_ui.ts appends to the page.
    """
    page.locator("button.dl-action-button").click()
    popover = page.locator(".dl-popover")
    expect(popover).to_be_visible()
    return popover
