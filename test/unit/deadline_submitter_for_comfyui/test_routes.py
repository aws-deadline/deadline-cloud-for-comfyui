# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Tests for the /deadline routes. ComfyUI's `server` and `folder_paths` modules are replaced with
stubs, and a fake `deadline` CLI and a fake bridge.py stand in for the Deadline Cloud tools.
"""

import asyncio
import importlib
import json
import os
import re
import shlex
import sys
import textwrap
import time
import types
from pathlib import Path

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from deadline.comfyui_submitter import submit

pytestmark = pytest.mark.skipif(os.name == "nt", reason="The fake CLI is a POSIX shell script")

WORKFLOW = {
    "3": {"class_type": "KSampler", "inputs": {"seed": 5, "model": ["4", 0]}},
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "m.safetensors"}},
    "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "comfyui"}},
}


class FakePromptServer:
    instance: "FakePromptServer"

    def __init__(self):
        self.routes = web.RouteTableDef()
        self.events: list[tuple[str, dict]] = []
        # The client each event went to, or None for every client.
        self.sids: list[str | None] = []

    def send_sync(self, event, data, sid=None):
        self.events.append((event, data))
        self.sids.append(sid)


@pytest.fixture
def comfy(monkeypatch, tmp_path):
    prompt_server = FakePromptServer()
    FakePromptServer.instance = prompt_server
    monkeypatch.setitem(sys.modules, "server", types.SimpleNamespace(PromptServer=FakePromptServer))
    monkeypatch.setitem(
        sys.modules,
        "folder_paths",
        types.SimpleNamespace(
            get_user_directory=lambda: str(tmp_path / "user"),
            get_output_directory=lambda: str(tmp_path / "output"),
            get_input_directory=lambda: str(tmp_path / "input"),
            get_temp_directory=lambda: str(tmp_path / "temp"),
            folder_names_and_paths={
                "checkpoints": ([str(tmp_path / "models" / "checkpoints")], {".safetensors"}),
                "vae": ([str(tmp_path / "models" / "vae")], {".safetensors"}),
                "loras": ([str(tmp_path / "models" / "loras")], {".safetensors"}),
            },
        ),
    )
    monkeypatch.delitem(sys.modules, "deadline.comfyui_submitter.routes", raising=False)
    routes = importlib.import_module("deadline.comfyui_submitter.routes")
    # Tests read the bundle after submitting, so it's kept. test_submitted_bundles_are_deleted
    # covers deleting it.
    remove_bundle = routes._remove_bundle
    monkeypatch.setattr(routes, "_remove_bundle", lambda bundle_dir: None)
    for var in (
        submit.TEMPLATE_ENV_VAR,
        submit.CONDA_CHANNELS_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"conda_channels": "s3://bucket/Conda/Default conda-forge"}))
    monkeypatch.setenv(submit.CONFIG_ENV_VAR, str(config))
    return types.SimpleNamespace(
        routes=routes, server=prompt_server, tmp_path=tmp_path, remove_bundle=remove_bundle
    )


def fake_cli(
    monkeypatch,
    tmp_path: Path,
    stdout: str,
    exit_code: int = 0,
    stderr: str = "",
    version: str = "0.60.10",
    stderr_noise: int = 0,
) -> Path:
    args_file = tmp_path / "cli-args.json"
    script = tmp_path / "fake_deadline.py"
    script.write_text(textwrap.dedent(f"""\
            import json, sys
            if sys.argv[1:] == ["--version"]:
                print("deadline, version {version}")
                sys.exit(0)
            json.dump(sys.argv[1:], open({str(args_file)!r}, "w"))
            sys.stderr.write("x" * {stderr_noise})
            sys.stderr.flush()
            print({stdout!r})
            print({stderr!r}, file=sys.stderr)
            sys.exit({exit_code})
            """))
    # A shell wrapper rather than a shebang, which breaks when the interpreter path has spaces.
    cli = tmp_path / "deadline"
    cli.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n'
    )
    cli.chmod(0o755)
    monkeypatch.setenv(submit.CLI_ENV_VAR, str(cli))
    return args_file


async def post(comfy, body, remote="127.0.0.1", content_type="application/json"):
    @web.middleware
    async def set_remote(request, handler):
        return await handler(request.clone(remote=remote))

    app = web.Application(middlewares=[set_remote])
    app.add_routes(comfy.server.routes)
    async with TestClient(TestServer(app)) as client:
        if isinstance(body, dict):
            body = {"farmId": "farm-1", "queueId": "queue-1", **body}
        data = json.dumps(body) if not isinstance(body, str) else body
        response = await client.post(
            "/deadline/jobs", data=data, headers={"Content-Type": content_type}
        )
        result = response.status, await response.json()
        await asyncio.gather(*comfy.routes._watchers)
        return result


def run(coro):
    return asyncio.run(coro)


def fake_tools(monkeypatch, tmp_path: Path) -> Path:
    """A fake CLI and bridge.py, where submitting creates job-1. Returns the bridge's requests."""
    fake_cli(monkeypatch, tmp_path, "")
    return fake_bridge(monkeypatch, tmp_path, {"submit": [result({"jobId": "job-1"})]})


def sent_parameters(requests: Path) -> dict[str, str]:
    return bridge_requests(requests)[0]["request"]["parameters"]


def test_jobs_fills_in_the_workflow_parameters(comfy, monkeypatch):
    requests = fake_tools(monkeypatch, comfy.tmp_path)

    status, body = run(post(comfy, {"workflow": WORKFLOW, "name": "portrait.json"}))

    assert status == 202
    bundle_dir = Path(body["job_bundle"])
    assert bundle_dir.parent == comfy.tmp_path / "user" / "deadline_submissions"
    assert bundle_dir.name.endswith("_portrait")
    assert (bundle_dir / "template.yaml").read_text() == submit.JOB_TEMPLATE.read_text()
    workflow_file = Path(body["workflow_file"])
    assert workflow_file == bundle_dir / "portrait_api.json"
    assert json.loads(workflow_file.read_text()) == WORKFLOW

    request = bridge_requests(requests)[0]["request"]
    assert request["bundleDir"] == str(bundle_dir)
    assert request["settings"]["name"] == "ComfyUI - portrait"
    parameters = request["parameters"]
    assert parameters["WorkflowFile"] == str(workflow_file)
    assert parameters["OutputDir"] == str(comfy.tmp_path / "output")
    assert parameters["InputDir"] == str(comfy.tmp_path / "input")


def test_jobs_reports_an_unreadable_job_template(comfy, monkeypatch):
    template = comfy.tmp_path / "studio.yaml"
    template.write_text("steps: [\n  unclosed\n")
    monkeypatch.setenv(submit.TEMPLATE_ENV_VAR, str(template))
    fake_tools(monkeypatch, comfy.tmp_path)
    status, body = run(post(comfy, {"workflow": WORKFLOW}))
    assert status == 400
    assert "Couldn't read the job template" in body["error"]


def test_jobs_rejects_old_cli(comfy, monkeypatch):
    args_file = fake_cli(monkeypatch, comfy.tmp_path, "", version="0.60.3")
    status, body = run(post(comfy, {"workflow": WORKFLOW}))
    assert status == 400
    assert "version 0.60.3" in body["error"]
    assert "0.60.8" in body["error"]
    assert not args_file.exists()
    # A rejected version isn't cached, so an upgrade takes effect without restarting ComfyUI.
    assert comfy.routes._cli_versions == {}


def test_jobs_caches_cli_version(comfy, monkeypatch):
    fake_tools(monkeypatch, comfy.tmp_path)
    run(post(comfy, {"workflow": WORKFLOW}))
    assert list(comfy.routes._cli_versions.values()) == ["deadline, version 0.60.10\n"]


def test_jobs_rejects_remote_clients(comfy, monkeypatch):
    fake_tools(monkeypatch, comfy.tmp_path)
    status, body = run(post(comfy, {"workflow": WORKFLOW}, remote="10.0.0.5"))
    assert status == 403
    assert "only accepts requests" in body["error"]


@pytest.mark.parametrize(
    ("remote", "headers", "status"),
    [
        ("127.0.0.1", {}, 200),
        ("::1", {}, 200),
        ("10.0.0.5", {}, 403),
        # A reverse proxy on this machine connects from loopback on behalf of other browsers.
        ("127.0.0.1", {"X-Forwarded-For": "10.0.0.5"}, 403),
        ("127.0.0.1", {"Forwarded": "for=10.0.0.5"}, 403),
        ("127.0.0.1", {"X-Real-IP": "10.0.0.5"}, 403),
        # Another website's page, in a browser on this machine, when CORS is turned on.
        ("127.0.0.1", {"Sec-Fetch-Site": "cross-site"}, 403),
        ("127.0.0.1", {"Sec-Fetch-Site": "same-origin"}, 200),
        # A page on another port of this machine, such as a dev server, is "same-site".
        ("127.0.0.1", {"Sec-Fetch-Site": "same-site"}, 403),
        # Typed into the address bar or sent by a tool, not by another page.
        ("127.0.0.1", {"Sec-Fetch-Site": "none"}, 200),
        ("127.0.0.1", {"Host": "localhost:8188"}, 200),
        ("::1", {"Host": "[::1]:8188"}, 200),
        # A website that points its own name at 127.0.0.1 (DNS rebinding) is same-origin.
        (
            "127.0.0.1",
            {"Host": "attacker.example:8188", "Sec-Fetch-Site": "same-origin"},
            403,
        ),
        ("127.0.0.1", {"Host": "attacker.example:8188"}, 403),
        ("127.0.0.1", {"Host": "[invalid]:8188"}, 403),
        ("127.0.0.1", {"Host": "user@localhost:8188"}, 403),
        ("127.0.0.1", {"Host": "localhost:invalid"}, 403),
        ("127.0.0.1", {"Host": "localhost:65536"}, 403),
    ],
)
@pytest.mark.parametrize("path", ["/deadline/plan", "/deadline/jobs/cancel"])
def test_routes_only_answer_allowed_browsers(comfy, path, remote, headers, status):
    body = {"workflow": WORKFLOW, "submissionId": "a" * 32}
    got, response = run(call(comfy, path, body, remote=remote, headers=headers))
    if status == 403:
        assert got == 403
        assert any(text in response["error"] for text in ("websites", "only accepts"))
    else:
        # Cancel answers 404 for a submission that isn't running, past the access check.
        assert got == (200 if path == "/deadline/plan" else 404)


@pytest.mark.parametrize(
    ("origin", "host", "status"),
    [
        ("http://localhost:8188", "localhost:8188", 200),
        ("http://LOCALHOST:8188", "localhost:8188", 200),
        ("http://localhost", "localhost:80", 200),
        ("http://[::1]:8188", "[::1]:8188", 200),
        ("http://attacker.example:8188", "localhost:8188", 403),
        ("http://localhost:8000", "localhost:8188", 403),
        ("https://localhost:8188", "localhost:8188", 403),
        ("http://127.0.0.1:8188", "localhost:8188", 403),
        ("null", "localhost:8188", 403),
        ("", "localhost:8188", 403),
        ("not a URL", "localhost:8188", 403),
        ("http://[invalid]:8188", "localhost:8188", 403),
        ("http://localhost:invalid", "localhost:8188", 403),
        ("http://user@localhost:8188", "localhost:8188", 403),
        ("http://localhost:8188/path", "localhost:8188", 403),
        ("http://localhost:8188?query=value", "localhost:8188", 403),
        ("http://localhost:8188#fragment", "localhost:8188", 403),
        ("http://localhost:8188 http://attacker.example", "localhost:8188", 403),
    ],
)
@pytest.mark.parametrize("fetch_site", [None, "same-origin", "none"])
@pytest.mark.parametrize("path", ["/deadline/plan", "/deadline/jobs/cancel"])
def test_routes_check_origin_even_without_fetch_metadata(
    comfy, path, origin, host, status, fetch_site
):
    headers = {"Host": host, "Origin": origin}
    if fetch_site is not None:
        headers["Sec-Fetch-Site"] = fetch_site
    got, response = run(
        call(comfy, path, {"workflow": WORKFLOW, "submissionId": "a" * 32}, headers=headers)
    )
    if status == 403:
        assert got == 403
        assert "websites" in response["error"]
    else:
        assert got == (200 if path == "/deadline/plan" else 404)


@pytest.mark.parametrize("fetch_site", [None, "same-origin", "none"])
@pytest.mark.parametrize(
    "path",
    [
        "/deadline/plan",
        "/deadline/bundle",
        "/deadline/jobs",
        "/deadline/jobs/confirm",
        "/deadline/jobs/cancel",
        "/deadline/jobs/download",
        "/deadline/cloud/farms",
        "/deadline/choose",
    ],
)
def test_routes_reject_other_origins_with_a_local_host(comfy, path, fetch_site):
    # A local proxy can rewrite Host and omit forwarding headers while preserving Origin.
    headers = {"Host": "127.0.0.1:8188", "Origin": "http://other.example"}
    if fetch_site is not None:
        headers["Sec-Fetch-Site"] = fetch_site
    status, response = run(
        call(
            comfy,
            path,
            {"workflow": WORKFLOW, "submissionId": "a" * 32},
            remote="127.0.0.1",
            headers=headers,
        )
    )
    assert status == 403
    assert "websites" in response["error"]


def test_routes_reject_multiple_origins(comfy):
    status, response = run(
        call(
            comfy,
            "/deadline/plan",
            {"workflow": WORKFLOW},
            headers=[
                ("Host", "localhost:8188"),
                ("Origin", "http://localhost:8188"),
                ("Origin", "http://attacker.example"),
            ],
        )
    )
    assert status == 403
    assert "websites" in response["error"]


@pytest.mark.parametrize(
    "values",
    [
        ("same-origin", "cross-site"),
        ("cross-site", "same-origin"),
        ("none", "same-origin"),
        ("same-origin", "same-origin"),
    ],
)
def test_routes_reject_duplicate_fetch_metadata(comfy, values):
    status, response = run(
        call(
            comfy,
            "/deadline/plan",
            {"workflow": WORKFLOW},
            headers=[("Sec-Fetch-Site", value) for value in values],
        )
    )
    assert status == 403
    assert "websites" in response["error"]


@pytest.mark.parametrize(
    ("host", "target_host", "status"),
    [
        ("localhost:8188", "localhost:8188", 200),
        ("LOCALHOST:8188", "localhost:8188", 200),
        ("other.example:8188", "localhost:8188", 403),
        ("localhost:8188", "other.example:8188", 403),
        ("localhost:8188", "127.0.0.1:8188", 403),
        ("user@localhost:8188", "localhost:8188", 403),
        (None, "localhost:8188", 403),
    ],
)
def test_routes_validate_host_with_an_absolute_request_target(comfy, host, target_host, status):
    """The URL's authority cannot hide a different or malformed Host header."""

    async def request():
        app = web.Application()
        app.add_routes(comfy.server.routes)
        async with TestServer(app) as server:
            reader, writer = await asyncio.open_connection(server.host, server.port)
            body = json.dumps({"workflow": WORKFLOW}).encode()
            version = "1.0" if host is None else "1.1"
            host_header = "" if host is None else f"Host: {host}\r\n"
            headers = (
                f"POST http://{target_host}/deadline/plan HTTP/{version}\r\n"
                f"{host_header}"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            try:
                writer.write(headers + body)
                await writer.drain()
                response = await asyncio.wait_for(reader.read(), 5)
            finally:
                writer.close()
                await writer.wait_closed()
            header_block, _, response_body = response.partition(b"\r\n\r\n")
            return int(header_block.split()[1]), json.loads(response_body)

    got, response = run(request())
    assert got == status
    if status == 403:
        assert "only accepts requests addressed" in response["error"]


def test_jobs_requires_json(comfy, monkeypatch):
    fake_tools(monkeypatch, comfy.tmp_path)
    status, _ = run(post(comfy, {"workflow": WORKFLOW}, content_type="text/plain"))
    assert status == 415


def test_jobs_rejects_invalid_json(comfy, monkeypatch):
    fake_tools(monkeypatch, comfy.tmp_path)
    status, body = run(post(comfy, "{not json"))
    assert status == 400
    assert "valid JSON" in body["error"]


def test_jobs_rejects_ui_format_workflow(comfy, monkeypatch):
    fake_tools(monkeypatch, comfy.tmp_path)
    status, body = run(post(comfy, {"workflow": {"nodes": [], "links": []}}))
    assert status == 400
    assert "UI format" in body["error"]


def test_jobs_reports_missing_cli(comfy, monkeypatch):
    monkeypatch.delenv(submit.CLI_ENV_VAR, raising=False)
    monkeypatch.setenv("PATH", str(comfy.tmp_path))
    status, body = run(post(comfy, {"workflow": WORKFLOW}))
    assert status == 400
    assert "deadline[gui]" in body["error"]


def test_jobs_rejects_a_body_that_isnt_an_object(comfy, monkeypatch):
    status, body = run(post(comfy, "[1]"))
    assert (status, body) == (400, {"error": "The request body isn't a JSON object."})


def test_jobs_attaches_the_folders_the_workflow_loads(comfy, monkeypatch):
    cats = comfy.tmp_path / "input" / "cats"
    cats.mkdir(parents=True)
    (cats / "a.png").write_text("png")
    workflow = {
        **WORKFLOW,
        "2": {"class_type": "LoadImageDataSetFromFolder", "inputs": {"folder": "cats"}},
    }
    fake_tools(monkeypatch, comfy.tmp_path)
    _, body = run(post(comfy, {"workflow": workflow}))
    references = yaml.safe_load((Path(body["job_bundle"]) / "asset_references.yaml").read_text())
    assert references["assetReferences"]["inputs"]["directories"] == [str(cats)]


def fake_bridge(monkeypatch, tmp_path: Path, responses: dict) -> Path:
    """
    Stands in for the CLI's Python running bridge.py. Each command prints its lines from
    responses, and "confirm" waits for an answer on stdin. The requests are saved to a file.
    """
    requests = tmp_path / "bridge-requests.jsonl"
    script = tmp_path / "fake_bridge.py"
    script.write_text(textwrap.dedent(f"""\
            import json, sys, time
            command = sys.argv[-1]
            request = json.loads(sys.stdin.readline())
            with open({str(requests)!r}, "a") as f:
                f.write(json.dumps({{"argv": sys.argv[1:], "request": request}}) + "\\n")
            for line in {responses!r}.get(command, []):
                if line == "confirm":
                    print(json.dumps({{"event": "confirm", "message": "Upload?", "default": True}}))
                    sys.stdout.flush()
                    answer = json.loads(sys.stdin.readline())
                    if not answer["accept"]:
                        print(json.dumps({{"event": "error", "message": "Canceled"}}))
                        sys.exit(1)
                elif line == "hang":
                    time.sleep(60)
                elif line == "noisy":
                    # More than the OS pipe buffer and asyncio's read buffer hold.
                    sys.stderr.write("warning\\n" * 75000)
                    sys.stderr.flush()
                elif line == "crash":
                    print("Traceback: boom", file=sys.stderr)
                    sys.exit(1)
                else:
                    print(json.dumps(line))
                    sys.stdout.flush()
            """))
    python = tmp_path / "python"
    python.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n'
    )
    python.chmod(0o755)
    monkeypatch.setenv(submit.PYTHON_ENV_VAR, str(python))
    return requests


def bridge_requests(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


async def call(comfy, path, body, remote="127.0.0.1", headers=None):
    @web.middleware
    async def set_remote(request, handler):
        return await handler(request.clone(remote=remote))

    app = web.Application(middlewares=[set_remote])
    app.add_routes(comfy.server.routes)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(path, json=body, headers=headers)
        result = response.status, await response.json()
        await asyncio.gather(*comfy.routes._watchers)
        return result


def result(data):
    return {"event": "result", "data": data}


def test_plan_lists_parameters_and_attachments(comfy, monkeypatch):
    input_dir = comfy.tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "fox.png").write_text("png")
    workflow = {
        **WORKFLOW,
        "2": {"class_type": "LoadImage", "inputs": {"image": "fox.png"}},
    }
    status, body = run(call(comfy, "/deadline/plan", {"workflow": workflow, "name": "fox.json"}))
    assert status == 200
    assert body["name"] == "fox"
    assert body["template"] == "comfy_cli_job_template.yaml"
    names = [parameter["name"] for parameter in body["parameters"]]
    assert {"WorkflowFile", "OutputDir", "InputDir", "ExtraModelPathsConfig"} <= set(names)
    assert body["values"]["OutputDir"] == str(comfy.tmp_path / "output")
    assert body["values"]["InputDir"] == str(input_dir)
    assert body["queueValues"] == {"CondaChannels": "s3://bucket/Conda/Default conda-forge"}
    assert "sources" not in body and "tasks" not in body
    assert body["attachments"] == {"filenames": [str(input_dir / "fox.png")], "directories": []}
    # Planning doesn't create a job bundle.
    assert not (comfy.tmp_path / "user" / "deadline_submissions").exists()


def test_plan_reports_workflows_it_cant_submit(comfy):
    workflow = {**WORKFLOW, "10": {"inputs": {}}}
    status, body = run(call(comfy, "/deadline/plan", {"workflow": workflow}))
    assert status == 400
    assert body["error"].startswith("This ComfyUI can't convert node 10")


@pytest.mark.parametrize(
    ("node", "error"),
    [
        ({"class_type": ["KSampler"], "inputs": {}}, "Node 1 has no node type in the workflow."),
        ({"class_type": 5, "inputs": {}}, "Node 1 has no node type in the workflow."),
        ({"class_type": "", "inputs": {}}, "Node 1 has no node type in the workflow."),
        ({"class_type": "SaveImage", "inputs": "x"}, "Node 1 has inputs that aren't an object."),
        ({"class_type": "SaveImage", "inputs": [1]}, "Node 1 has inputs that aren't an object."),
    ],
)
@pytest.mark.parametrize("path", ["/deadline/plan", "/deadline/jobs"])
def test_malformed_workflows_get_a_400(comfy, monkeypatch, path, node, error):
    """Hand-made requests get a message, not a 500 from a TypeError or an AttributeError."""
    fake_tools(monkeypatch, comfy.tmp_path)
    request = {"workflow": {"1": node}, "farmId": "farm-1", "queueId": "queue-1"}
    status, body = run(call(comfy, path, request))
    assert status == 400
    assert body["error"].startswith(error)


def test_cloud_runs_the_bridge_with_the_cli_python(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    farms = {"farms": [{"farmId": "farm-1", "displayName": "Render"}]}
    requests = fake_bridge(monkeypatch, comfy.tmp_path, {"farms": [result(farms)]})
    status, body = run(call(comfy, "/deadline/cloud/farms", {"region": "eu-west-2"}))
    assert (status, body) == (200, farms)
    [request] = bridge_requests(requests)
    assert request["argv"] == ["-I", str(comfy.routes.BRIDGE), "farms"]
    assert request["request"] == {"region": "eu-west-2"}


def test_cloud_reports_library_errors(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    error = {"event": "error", "message": "The security token included in the request is expired"}
    fake_bridge(monkeypatch, comfy.tmp_path, {"farms": [error], "queues": ["crash"]})
    status, body = run(call(comfy, "/deadline/cloud/farms", {}))
    assert (status, body["error"]) == (502, error["message"])
    status, body = run(call(comfy, "/deadline/cloud/queues", {}))
    assert (status, body["error"]) == (502, "The Deadline Cloud library failed. Traceback: boom")


def test_cloud_times_out(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    fake_bridge(monkeypatch, comfy.tmp_path, {"status": ["hang"]})
    monkeypatch.setattr(comfy.routes, "BRIDGE_TIMEOUT", 0.5)
    status, body = run(call(comfy, "/deadline/cloud/status", {}))
    assert status == 502
    assert "longer than 0.5 seconds" in body["error"]


def test_cloud_only_runs_known_commands(comfy, monkeypatch):
    status, _ = run(call(comfy, "/deadline/cloud/submit", {}))
    assert status == 404
    status, _ = run(call(comfy, "/deadline/cloud/farms", {}, remote="10.0.0.5"))
    assert status == 403


def test_cloud_needs_the_cli_python(comfy, monkeypatch):
    monkeypatch.setenv(submit.PYTHON_ENV_VAR, str(comfy.tmp_path / "missing"))
    fake_cli(monkeypatch, comfy.tmp_path, "")
    status, body = run(call(comfy, "/deadline/cloud/farms", {}))
    assert status == 502
    assert "isn't an executable file" in body["error"]


JOB = {
    "workflow": WORKFLOW,
    "name": "fox",
    "farmId": "farm-1",
    "queueId": "queue-1",
    "storageProfileId": "sp-1",
    "parameters": {"ComfyUIArgs": "", "CondaChannels": "s3://bucket/Conda/Default"},
    "settings": {"priority": 70, "targetTaskRunStatus": "SUSPENDED"},
}


async def submit_and_confirm(comfy, body, accept=True):
    """Submits through /deadline/jobs and answers the confirmation when it comes."""
    app = web.Application()
    app.add_routes(comfy.server.routes)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/deadline/jobs", json=body)
        status, result = response.status, await response.json()
        if status == 202:
            for _ in range(200):
                if any(event == "deadline.confirm" for event, _ in comfy.server.events):
                    break
                await asyncio.sleep(0.05)
            answer = await client.post(
                "/deadline/jobs/confirm",
                json={"submissionId": result["submissionId"], "accept": accept},
            )
            assert answer.status == 200
        await asyncio.gather(*comfy.routes._watchers)
        return status, result


def test_jobs_submits_with_the_library(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    progress = {"event": "progress", "stage": "uploading", "progress": 50.0, "message": "1 of 2"}
    submitted = {"jobId": "job-1", "farmId": "farm-1", "queueId": "queue-1"}
    requests = fake_bridge(
        monkeypatch, comfy.tmp_path, {"submit": [progress, "confirm", result(submitted)]}
    )
    status, body = run(submit_and_confirm(comfy, JOB))
    assert status == 202
    bundle_dir = Path(body["job_bundle"])
    assert (bundle_dir / "asset_references.yaml").exists()

    [request] = bridge_requests(requests)
    assert request["argv"][-1] == "submit"
    sent = request["request"]
    assert sent["bundleDir"] == str(bundle_dir)
    assert (sent["farmId"], sent["queueId"], sent["storageProfileId"]) == (
        "farm-1",
        "queue-1",
        "sp-1",
    )
    assert sent["parameters"] == {
        "OutputDir": str(comfy.tmp_path / "output"),
        "InputDir": str(comfy.tmp_path / "input"),
        # The artist cleared Extra ComfyUI Arguments.
        "ComfyUIArgs": "",
        "CondaChannels": "s3://bucket/Conda/Default",
        "WorkflowFile": body["workflow_file"],
    }
    assert sent["settings"] == {
        "priority": 70,
        "targetTaskRunStatus": "SUSPENDED",
        "name": "ComfyUI - fox",
    }

    events = comfy.server.events
    submission = {"submissionId": body["submissionId"], "name": "ComfyUI - fox"}
    assert len(events) == 3
    assert events[0] == (
        "deadline.progress",
        {
            **submission,
            "stage": "uploading",
            "progress": 50.0,
            "message": "1 of 2",
            "kind": "progress",
        },
    )
    assert events[1] == (
        "deadline.confirm",
        {**submission, "message": "Upload?", "default": True},
    )
    # The panel shows each job's output directory, where Download puts its outputs.
    assert events[2] == (
        "deadline.submitted",
        {
            **submission,
            **submitted,
            "status": "SUBMITTED",
            "outputDir": str(comfy.tmp_path / "output"),
        },
    )
    assert comfy.routes._submissions == {}


def test_jobs_reports_a_declined_confirmation(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    fake_bridge(monkeypatch, comfy.tmp_path, {"submit": ["confirm"]})
    run(submit_and_confirm(comfy, JOB, accept=False))
    event, data = comfy.server.events[-1]
    assert (event, data["status"], data["error"]) == ("deadline.submitted", "FAILED", "Canceled")


def test_jobs_sends_only_the_parameters_the_template_defines(comfy, monkeypatch):
    template = comfy.tmp_path / "template.yaml"
    template.write_text(
        "specificationVersion: jobtemplate-2023-09\nname: x\n"
        "parameterDefinitions:\n- name: WorkflowFile\n  type: PATH\n"
        "steps:\n- name: s\n  script:\n    actions:\n      onRun:\n        command: 'true'\n"
    )
    monkeypatch.setenv(submit.TEMPLATE_ENV_VAR, str(template))
    fake_cli(monkeypatch, comfy.tmp_path, "")
    requests = fake_bridge(monkeypatch, comfy.tmp_path, {"submit": [result({"jobId": "job-1"})]})
    body = {**JOB, "parameters": {}}
    status, _ = run(call(comfy, "/deadline/jobs", body))
    assert status == 202
    assert list(bridge_requests(requests)[0]["request"]["parameters"]) == ["WorkflowFile"]


def test_jobs_keeps_the_templates_host_requirements_by_default(comfy, monkeypatch):
    requests = fake_tools(monkeypatch, comfy.tmp_path)
    status, response = run(call(comfy, "/deadline/jobs", {**JOB, "parameters": {}}))
    assert status == 202
    assert bridge_requests(requests)
    bundle = Path(response["job_bundle"])
    assert (bundle / "template.yaml").read_text() == submit.JOB_TEMPLATE.read_text()


def test_jobs_passes_require_paths_exist_to_the_library(comfy, monkeypatch):
    requests = fake_tools(monkeypatch, comfy.tmp_path)
    body = {**JOB, "settings": {"requirePathsExist": True}}
    status, _ = run(call(comfy, "/deadline/jobs", body))
    assert status == 202
    assert bridge_requests(requests)[0]["request"]["settings"]["requirePathsExist"] is True


@pytest.fixture
def umask_022():
    previous = os.umask(0o022)
    yield
    os.umask(previous)


def test_bundles_are_only_readable_by_their_owner(comfy, monkeypatch, umask_022):
    """With the usual umask, files would be 0644 and folders 0755, readable by other users."""
    fake_tools(monkeypatch, comfy.tmp_path)
    submissions = comfy.tmp_path / "user" / "deadline_submissions"
    submissions.mkdir(parents=True, mode=0o755)
    status, response = run(call(comfy, "/deadline/jobs", {**JOB, "name": "fox"}))
    assert status == 202
    bundle = Path(response["job_bundle"])
    modes = {
        path.name: path.stat().st_mode & 0o777
        for path in (submissions, bundle, *bundle.iterdir())
        if not path.is_dir() or path in (submissions, bundle)
    }
    assert modes == {
        "deadline_submissions": 0o700,
        bundle.name: 0o700,
        "template.yaml": 0o600,
        "fox_api.json": 0o600,
        "asset_references.yaml": 0o600,
        ".deadline-submission": 0o600,
    }


def test_jobs_attaches_the_artists_files(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    fake_bridge(monkeypatch, comfy.tmp_path, {"submit": [result({"jobId": "job-1"})]})
    attachments = {"filenames": ["/a/fox.png", " "], "directories": ["/a/cats"]}
    status, body = run(call(comfy, "/deadline/jobs", {**JOB, "attachments": attachments}))
    assert status == 202
    references = yaml.safe_load((Path(body["job_bundle"]) / "asset_references.yaml").read_text())
    assert references["assetReferences"]["inputs"] == {
        "filenames": ["/a/fox.png"],
        "directories": ["/a/cats"],
    }


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"farmId": ""}, "Choose a farm and a queue."),
        ({"parameters": {"a": [1]}}, "parameters must be an object of strings and numbers."),
        ({"settings": "fast"}, "settings must be an object of strings and numbers."),
        ({"attachments": []}, "attachments must be an object."),
        ({"attachments": {"filenames": "/a"}}, "attachments.filenames must be a list of paths."),
    ],
)
def test_jobs_rejects_bad_requests(comfy, monkeypatch, change, error):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    status, body = run(call(comfy, "/deadline/jobs", {**JOB, **change}))
    assert (status, body) == (400, {"error": error})


def test_jobs_reports_a_bridge_crash(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    fake_bridge(monkeypatch, comfy.tmp_path, {"submit": ["crash"]})
    status, _ = run(call(comfy, "/deadline/jobs", JOB))
    assert status == 202
    _, data = comfy.server.events[-1]
    assert data["status"] == "FAILED"
    assert data["error"] == "The Deadline Cloud library failed. Traceback: boom"


@pytest.mark.parametrize(
    "requested, kept",
    [("a" * 32, True), ("not-an-id", False), (None, False), ("A" * 32, False)],
)
def test_jobs_use_the_panels_submission_id(comfy, monkeypatch, requested, kept):
    """The panel's ID lets it match events the server sends before it answers the request."""
    fake_cli(monkeypatch, comfy.tmp_path, "")
    fake_bridge(monkeypatch, comfy.tmp_path, {"submit": [result({"jobId": "job-1"})]})
    job = {**JOB, "submissionId": requested} if requested is not None else JOB
    status, body = run(call(comfy, "/deadline/jobs", job))
    assert status == 202
    assert (body["submissionId"] == requested) is kept
    assert re.fullmatch(r"[0-9a-f]{32}", body["submissionId"])
    assert all(data["submissionId"] == body["submissionId"] for _, data in comfy.server.events)


@pytest.mark.parametrize(
    ("client_id", "sid"),
    [("tab-1", "tab-1"), (None, None), ("../tab", None), ("a" * 129, None)],
)
def test_jobs_send_events_only_to_the_submitting_tab(comfy, monkeypatch, client_id, sid):
    """Another tab or browser on the server doesn't see the job, its prompts or its errors."""
    fake_cli(monkeypatch, comfy.tmp_path, "")
    events = [{"event": "progress", "value": 50}, result({"jobId": "job-1"})]
    fake_bridge(monkeypatch, comfy.tmp_path, {"submit": events})
    job = {**JOB, "clientId": client_id} if client_id is not None else JOB
    status, _ = run(call(comfy, "/deadline/jobs", job))
    assert status == 202
    assert [event for event, _ in comfy.server.events] == [
        "deadline.progress",
        "deadline.submitted",
    ]
    assert comfy.server.sids == [sid, sid]


def test_jobs_reads_stderr_while_waiting_for_the_result(comfy, monkeypatch):
    """A bridge that fills its stderr pipe before printing its result still finishes."""
    fake_cli(monkeypatch, comfy.tmp_path, "")
    fake_bridge(monkeypatch, comfy.tmp_path, {"submit": ["noisy", result({"jobId": "job-1"})]})
    status, _ = run(asyncio.wait_for(call(comfy, "/deadline/jobs", JOB), 30))
    assert status == 202
    _, data = comfy.server.events[-1]
    assert (data["status"], data["jobId"]) == ("SUBMITTED", "job-1")


def test_jobs_can_be_canceled(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    fake_bridge(monkeypatch, comfy.tmp_path, {"submit": ["hang"]})

    async def submit_and_cancel():
        app = web.Application()
        app.add_routes(comfy.server.routes)
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/deadline/jobs", json=JOB)
            submission_id = (await response.json())["submissionId"]
            canceled = await client.post(
                "/deadline/jobs/cancel", json={"submissionId": submission_id}
            )
            assert canceled.status == 200
            await asyncio.gather(*comfy.routes._watchers)
            again = await client.post("/deadline/jobs/cancel", json={"submissionId": submission_id})
            return again.status

    assert run(submit_and_cancel()) == 404
    event, data = comfy.server.events[-1]
    assert (event, data["status"]) == ("deadline.submitted", "CANCELED")


def test_resolve_cli_python_reads_the_shebang(tmp_path, monkeypatch):
    monkeypatch.delenv(submit.PYTHON_ENV_VAR, raising=False)
    python = tmp_path / "env" / "python3.11"
    python.parent.mkdir()
    python.write_text("")
    cli = tmp_path / "deadline"
    cli.write_text(f"#!{python}\nimport sys\n")
    assert submit.resolve_cli_python(str(cli)) == str(python)
    # pip's launcher for interpreter paths with spaces
    spaced = tmp_path / "my env" / "python"
    spaced.parent.mkdir()
    spaced.write_text("")
    cli.write_text(f"#!/bin/sh\n'''exec' \"{spaced}\" \"$0\" \"$@\"\n' '''\nimport sys\n")
    assert submit.resolve_cli_python(str(cli)) == str(spaced)


def test_resolve_cli_python_looks_next_to_the_cli(tmp_path, monkeypatch):
    monkeypatch.delenv(submit.PYTHON_ENV_VAR, raising=False)
    cli = tmp_path / "deadline"
    cli.write_text("#!/bin/sh\nexec something\n")
    with pytest.raises(submit.SubmitError, match="Couldn't find the Python"):
        submit.resolve_cli_python(str(cli))
    (tmp_path / "python").write_text("")
    assert submit.resolve_cli_python(str(cli)) == str(tmp_path / "python")
    assert submit.resolve_cli_python(str(tmp_path / "missing")) == str(tmp_path / "python")


def fake_picker(monkeypatch, comfy, stdout: str, exit_code: int = 0) -> None:
    script = f"import sys; sys.stdout.write({stdout!r}); sys.exit({exit_code})"
    monkeypatch.setattr(comfy.routes, "picker_command", lambda kind: [sys.executable, "-c", script])


def test_choose_returns_the_chosen_paths(comfy, monkeypatch):
    fake_picker(monkeypatch, comfy, "/a/fox.png\n/a/cat.png\n")
    status, body = run(call(comfy, "/deadline/choose", {"kind": "files"}))
    assert (status, body) == (200, {"paths": [str(Path("/a/fox.png")), str(Path("/a/cat.png"))]})


def test_choose_returns_nothing_when_canceled(comfy, monkeypatch):
    fake_picker(monkeypatch, comfy, "", exit_code=1)
    assert run(call(comfy, "/deadline/choose", {"kind": "folder"})) == (200, {"paths": []})


def test_choose_rejects_unknown_kinds_and_missing_dialogs(comfy, monkeypatch):
    status, _ = run(call(comfy, "/deadline/choose", {"kind": "rm -rf"}))
    assert status == 400
    monkeypatch.setattr(comfy.routes, "picker_command", lambda kind: None)
    status, body = run(call(comfy, "/deadline/choose", {"kind": "files"}))
    assert status == 501
    assert "zenity" in body["error"]


FARM = "farm-" + "1" * 32
QUEUE = "queue-" + "2" * 32
JOB_ID = "job-" + "3" * 32


@pytest.mark.parametrize(
    "value, expected",
    [
        ("fox", "fox"),
        ("fox\nINFO forged", "fox\\nINFO forged"),
        ("fox\r\nINFO forged", "fox\\r\\nINFO forged"),
        ("fox\rINFO forged", "fox\\rINFO forged"),
        (Path("a/b"), str(Path("a/b"))),
    ],
)
def test_log_value_escapes_line_breaks(comfy, value, expected):
    assert comfy.routes._log_value(value) == expected


def test_log_block_indents_continuation_lines(comfy):
    assert comfy.routes._log_block("a\nb\r\nc\rd") == "a\n    b\n    c\\rd"


def test_plans_run_one_at_a_time_off_the_event_loop(comfy, monkeypatch):
    import threading
    import time

    started = threading.Event()
    plan = comfy.routes._plan

    def slow_plan(*args):
        started.set()
        time.sleep(0.5)
        return plan(*args)

    monkeypatch.setattr(comfy.routes, "_plan", slow_plan)

    async def plan_three_times():
        app = web.Application()
        app.add_routes(comfy.server.routes)
        async with TestClient(TestServer(app)) as client:

            async def post_plan():
                response = await client.post("/deadline/plan", json={"workflow": WORKFLOW})
                return response.status

            first = asyncio.ensure_future(post_plan())
            await asyncio.to_thread(started.wait, 5)
            # The server still answers while the first plan works.
            begin = time.monotonic()
            other = await client.post("/deadline/choose", json={"kind": "bad"})
            answered_in = time.monotonic() - begin
            second = asyncio.ensure_future(post_plan())
            await asyncio.sleep(0.05)
            third = asyncio.ensure_future(post_plan())
            return other.status, answered_in, await asyncio.gather(first, second, third)

    other, answered_in, statuses = run(plan_three_times())
    assert other == 400
    assert answered_in < 0.4
    # The second plan waited behind the first, and the third replaced it.
    assert statuses == [200, 409, 200]
    assert comfy.routes._plan_requests == {}


def test_plan_from_another_tab_doesnt_replace_a_waiting_one(comfy, monkeypatch):
    import threading
    import time

    started = threading.Event()
    plan = comfy.routes._plan

    def slow_plan(*args):
        started.set()
        time.sleep(0.3)
        return plan(*args)

    monkeypatch.setattr(comfy.routes, "_plan", slow_plan)

    async def plan_from_two_tabs():
        app = web.Application()
        app.add_routes(comfy.server.routes)
        async with TestClient(TestServer(app)) as client:

            async def post_plan(client_id):
                body = {"workflow": WORKFLOW, "clientId": client_id}
                response = await client.post("/deadline/plan", json=body)
                return response.status

            first = asyncio.ensure_future(post_plan("tab-a"))
            await asyncio.to_thread(started.wait, 5)
            second = asyncio.ensure_future(post_plan("tab-b"))
            await asyncio.sleep(0.05)
            third = asyncio.ensure_future(post_plan("tab-c"))
            return await asyncio.gather(first, second, third)

    # Each tab's plan waits its turn; none replaces another tab's.
    assert run(plan_from_two_tabs()) == [200, 200, 200]
    assert comfy.routes._plan_requests == {}


def test_jobs_report_a_job_that_was_created_before_cancel_reached_it(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    submitted = {"jobId": "job-1", "farmId": "farm-1", "queueId": "queue-1"}
    requests = fake_bridge(monkeypatch, comfy.tmp_path, {"submit": [result(submitted), "hang"]})

    async def submit_then_cancel():
        app = web.Application()
        app.add_routes(comfy.server.routes)
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/deadline/jobs", json=JOB)
            submission_id = (await response.json())["submissionId"]
            # The bridge has created the job and printed its result by the time Cancel arrives.
            for _ in range(200):
                if requests.exists():
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.5)
            await client.post("/deadline/jobs/cancel", json={"submissionId": submission_id})
            await asyncio.gather(*comfy.routes._watchers)

    run(submit_then_cancel())
    event, data = comfy.server.events[-1]
    assert (event, data["status"], data["jobId"]) == ("deadline.submitted", "SUBMITTED", "job-1")


def test_jobs_decline_a_confirmation_nobody_answers(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    fake_bridge(monkeypatch, comfy.tmp_path, {"submit": ["confirm", result({"jobId": "job-1"})]})
    monkeypatch.setattr(comfy.routes, "CONFIRM_TIMEOUT", 0.2)
    status, _ = run(post(comfy, JOB))
    assert status == 202
    event, data = comfy.server.events[-1]
    assert (event, data["status"], data["error"]) == ("deadline.submitted", "FAILED", "Canceled")
    assert comfy.routes._submissions == {}


def test_jobs_cancel_the_unanswered_confirmation_timer_when_the_submission_ends(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    fake_bridge(monkeypatch, comfy.tmp_path, {"submit": ["confirm", result({"jobId": "job-1"})]})
    run(submit_and_confirm(comfy, JOB))
    event, data = comfy.server.events[-1]
    assert (event, data["status"]) == ("deadline.submitted", "SUBMITTED")
    assert comfy.routes._watchers == set()


class _ExitedProcess:
    """A bridge process that exited after it asked for a confirmation."""

    class _Stdin:
        def write(self, data):
            raise BrokenPipeError()

        async def drain(self):
            pass

    stdin = _Stdin()

    def kill(self):
        raise ProcessLookupError()


@pytest.mark.parametrize(
    ("path", "body", "status"),
    [
        ("/deadline/jobs/confirm", {"accept": True}, 404),
        ("/deadline/jobs/cancel", {}, 200),
    ],
)
def test_confirm_and_cancel_handle_a_bridge_that_already_exited(
    comfy, monkeypatch, path, body, status
):
    submission_id = "b" * 32
    monkeypatch.setitem(
        comfy.routes._submissions,
        submission_id,
        comfy.routes._Submission("fox", _ExitedProcess(), confirms=1),
    )
    got, response = run(call(comfy, path, {**body, "submissionId": submission_id}))
    assert got == status
    if status == 404:
        assert "isn't running" in response["error"]


def test_jobs_report_a_bridge_that_exits_before_reading_the_request(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    python = comfy.tmp_path / "python"
    python.write_text("#!/bin/sh\necho 'Traceback: boom' >&2\nexit 1\n")
    python.chmod(0o755)
    monkeypatch.setenv(submit.PYTHON_ENV_VAR, str(python))
    status, _ = run(post(comfy, JOB))
    assert status == 202
    event, data = comfy.server.events[-1]
    assert (event, data["status"]) == ("deadline.submitted", "FAILED")
    assert comfy.routes._submissions == {}


def test_a_hung_cli_version_check_is_killed(comfy, monkeypatch):
    pid_file = comfy.tmp_path / "pid"
    cli = comfy.tmp_path / "deadline"
    cli.write_text(f"#!/bin/sh\necho $$ > {shlex.quote(str(pid_file))}\nexec sleep 60\n")
    cli.chmod(0o755)
    monkeypatch.setattr(comfy.routes, "CLI_VERSION_TIMEOUT", 0.5)
    with pytest.raises(TimeoutError):
        run(comfy.routes._cli_version_output(str(cli)))
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


def test_jobs_submits_when_local_model_staging_fails(comfy, monkeypatch):
    fake_tools(monkeypatch, comfy.tmp_path)
    checkpoint = comfy.tmp_path / "models" / "checkpoints" / "m.safetensors"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"model")

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(comfy.routes, "stage_local_models", fail)
    status, body = run(post(comfy, {"workflow": WORKFLOW}))
    assert status == 202
    assert len(body["warnings"]) == 1
    assert "m.safetensors won't be attached" in body["warnings"][0]


def test_save_asset_references_merges_staged_files_with_the_workflows_inputs(comfy):
    """The scanned input files are kept, models aren't scanned, and staged extras are added once."""
    image = comfy.tmp_path / "input" / "cat.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"png")
    model = comfy.tmp_path / "models" / "checkpoints" / "m.safetensors"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"model")
    workflow = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "cat.png"}},
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "m.safetensors"}},
    }
    bundle = comfy.tmp_path / "bundle"
    bundle.mkdir()
    comfy.routes._save_asset_references(workflow, bundle, extra_filenames=[model, image])
    inputs = yaml.safe_load((bundle / "asset_references.yaml").read_text())["assetReferences"][
        "inputs"
    ]
    assert inputs == {"filenames": sorted([str(image), str(model)]), "directories": []}


def test_jobs_lists_local_models_in_asset_references(comfy, monkeypatch):
    """Every model the workflow loads is attached, whether or not its loader records a URL."""
    requests = fake_tools(monkeypatch, comfy.tmp_path)
    checkpoints = comfy.tmp_path / "models" / "checkpoints"
    checkpoints.mkdir(parents=True)
    local = checkpoints / "local.safetensors"
    local.write_bytes(b"model")
    (checkpoints / "other.safetensors").write_bytes(b"model")
    downloadable = checkpoints / "downloadable.safetensors"
    downloadable.write_bytes(b"model")
    ui_workflow = {
        "nodes": [
            {
                "type": "CheckpointLoaderSimple",
                "properties": {
                    "models": [
                        {"directory": "checkpoints", "name": "local.safetensors"},
                        {
                            "directory": "checkpoints",
                            "name": "downloadable.safetensors",
                            "url": "https://x.test/downloadable.safetensors",
                        },
                    ]
                },
            }
        ]
    }
    workflow = {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "local.safetensors"},
        },
        "5": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "downloadable.safetensors"},
        },
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "comfyui"}},
    }
    status, body = run(
        post(comfy, {"workflow": workflow, "ui_workflow": ui_workflow, "name": "mixed"})
    )
    assert status == 202
    bundle = Path(body["job_bundle"])
    written = json.loads(Path(body["workflow_file"]).read_text())
    assert written["4"]["inputs"]["ckpt_name"] == "local.safetensors"
    staged = [
        bundle / "local_models" / "checkpoints" / name
        for name in ("downloadable.safetensors", "local.safetensors")
    ]
    references = yaml.safe_load((bundle / "asset_references.yaml").read_text())
    assert references["assetReferences"]["inputs"]["filenames"] == [str(path) for path in staged]
    assert references["assetReferences"]["inputs"]["directories"] == []
    assert not (bundle / "local_models" / "checkpoints" / "other.safetensors").exists()
    parameters = sent_parameters(requests)
    assert parameters["ModelDirectory"] == str(bundle / "local_models")
    assert body["warnings"] == []


def test_model_directory_does_not_upload_its_contents():
    template = yaml.safe_load(submit.JOB_TEMPLATE.read_text())
    parameter = next(
        item for item in template["parameterDefinitions"] if item["name"] == "ModelDirectory"
    )
    assert parameter["dataFlow"] == "NONE"
    assert parameter["objectType"] == "DIRECTORY"


def test_jobs_warns_about_models_it_cant_attach(comfy, monkeypatch):
    fake_tools(monkeypatch, comfy.tmp_path)
    status, body = run(post(comfy, {"workflow": WORKFLOW}))
    assert status == 202
    assert len(body["warnings"]) == 1
    assert "m.safetensors won't be attached" in body["warnings"][0]


def _local_checkpoint(comfy, name="m.safetensors"):
    checkpoint = comfy.tmp_path / "models" / "checkpoints" / name
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"model")
    return checkpoint


def test_plan_lists_local_models_and_doesnt_warn_about_them(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    checkpoint = _local_checkpoint(comfy)
    status, body = run(call(comfy, "/deadline/plan", {"workflow": WORKFLOW, "name": "fox"}))
    assert status == 200
    assert body["localModels"] == [
        {
            "id": "checkpoints/m.safetensors",
            "name": "m.safetensors",
            "directory": "checkpoints",
            "path": str(checkpoint),
        }
    ]
    assert body["warnings"] == []
    # Planning doesn't stage anything.
    assert not (comfy.tmp_path / "user" / "deadline_submissions").exists()


def test_plan_warns_about_models_with_no_local_file(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    status, body = run(call(comfy, "/deadline/plan", {"workflow": WORKFLOW, "name": "fox"}))
    assert status == 200
    assert body["localModels"] == []
    assert "m.safetensors won't be attached" in body["warnings"][0]


def test_jobs_attaches_local_models_through_model_directory(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    checkpoint = _local_checkpoint(comfy)
    requests = fake_bridge(monkeypatch, comfy.tmp_path, {"submit": [result({"jobId": "job-1"})]})
    # The panel sends the hidden directory's empty default, and a file of the artist's own.
    body = {
        **JOB,
        "parameters": {"ModelDirectory": ""},
        "attachments": {"filenames": ["/a/fox.png"], "directories": []},
    }
    status, response = run(call(comfy, "/deadline/jobs", body))
    assert status == 202
    assert response["warnings"] == []
    bundle = Path(response["job_bundle"])
    staged = bundle / "local_models" / "checkpoints" / "m.safetensors"
    assert staged.stat().st_ino == checkpoint.stat().st_ino
    references = yaml.safe_load((bundle / "asset_references.yaml").read_text())
    assert references["assetReferences"]["inputs"]["filenames"] == sorted(
        ["/a/fox.png", str(staged)]
    )
    sent = bridge_requests(requests)[0]["request"]["parameters"]
    assert sent["ModelDirectory"] == str(bundle / "local_models")


def test_jobs_doesnt_attach_a_local_model_the_artist_removed(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    _local_checkpoint(comfy)
    requests = fake_bridge(monkeypatch, comfy.tmp_path, {"submit": [result({"jobId": "job-1"})]})
    body = {**JOB, "parameters": {"ModelDirectory": ""}, "excludedModels": ["m.safetensors"]}
    status, response = run(call(comfy, "/deadline/jobs", body))
    assert status == 202
    assert len(response["warnings"]) == 1
    assert "m.safetensors won't be attached" in response["warnings"][0]
    bundle = Path(response["job_bundle"])
    assert not (bundle / "local_models").exists()
    references = yaml.safe_load((bundle / "asset_references.yaml").read_text())
    assert not references["assetReferences"]["inputs"]["filenames"]
    assert bridge_requests(requests)[0]["request"]["parameters"]["ModelDirectory"] == ""


def test_jobs_attaches_the_local_models_the_artist_kept(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    _local_checkpoint(comfy)
    fake_bridge(monkeypatch, comfy.tmp_path, {"submit": [result({"jobId": "job-1"})]})
    body = {**JOB, "parameters": {}, "excludedModels": ["other.safetensors"]}
    status, response = run(call(comfy, "/deadline/jobs", body))
    assert status == 202
    assert response["warnings"] == []
    staged = Path(response["job_bundle"]) / "local_models" / "checkpoints" / "m.safetensors"
    assert staged.is_file()


@pytest.mark.parametrize("excluded", ["m.safetensors", [1], {"m.safetensors": True}])
def test_jobs_rejects_bad_excluded_models(comfy, monkeypatch, excluded):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    fake_bridge(monkeypatch, comfy.tmp_path, {})
    status, response = run(
        call(comfy, "/deadline/jobs", {**JOB, "parameters": {}, "excludedModels": excluded})
    )
    assert status == 400
    assert "excludedModels" in response["error"]


def test_jobs_keeps_the_artists_model_directory_when_nothing_is_attached(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    requests = fake_bridge(monkeypatch, comfy.tmp_path, {"submit": [result({"jobId": "job-1"})]})
    body = {**JOB, "parameters": {"ModelDirectory": "/shared/models"}}
    status, _ = run(call(comfy, "/deadline/jobs", body))
    assert status == 202
    sent = bridge_requests(requests)[0]["request"]["parameters"]
    assert sent["ModelDirectory"] == "/shared/models"


def test_jobs_warns_when_a_planned_local_model_cant_be_staged(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    _local_checkpoint(comfy)
    requests = fake_bridge(monkeypatch, comfy.tmp_path, {"submit": [result({"jobId": "job-1"})]})

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(comfy.routes, "stage_local_models", fail)
    status, response = run(call(comfy, "/deadline/jobs", {**JOB, "parameters": {}}))
    assert status == 202
    assert len(response["warnings"]) == 1
    assert "m.safetensors won't be attached" in response["warnings"][0]
    assert "ModelDirectory" not in bridge_requests(requests)[0]["request"]["parameters"]


def test_templates_without_model_directory_dont_attach_local_models(comfy, monkeypatch):
    template = comfy.tmp_path / "template.yaml"
    template.write_text(
        "specificationVersion: jobtemplate-2023-09\nname: x\n"
        "parameterDefinitions:\n- name: WorkflowFile\n  type: PATH\n"
        "steps:\n- name: s\n  script:\n    actions:\n      onRun:\n        command: 'true'\n"
    )
    monkeypatch.setenv(submit.TEMPLATE_ENV_VAR, str(template))
    requests = fake_tools(monkeypatch, comfy.tmp_path)
    _local_checkpoint(comfy)
    status, body = run(post(comfy, {"workflow": WORKFLOW}))
    assert status == 202
    assert "m.safetensors won't be attached" in body["warnings"][0]
    assert not (Path(body["job_bundle"]) / "local_models").exists()
    assert "ModelDirectory" not in sent_parameters(requests)


@pytest.mark.parametrize(
    ("responses", "kept"), [([result({"jobId": "job-1"})], False), (["crash"], True)]
)
def test_submitted_bundles_are_deleted(comfy, monkeypatch, responses, kept):
    """A job's bundle, with its models, isn't needed once the job exists. A failed one is kept."""
    monkeypatch.setattr(comfy.routes, "_remove_bundle", comfy.remove_bundle)
    fake_cli(monkeypatch, comfy.tmp_path, "")
    checkpoint = _local_checkpoint(comfy)
    fake_bridge(monkeypatch, comfy.tmp_path, {"submit": responses})
    status, body = run(call(comfy, "/deadline/jobs", {**JOB, "parameters": {}}))
    assert status == 202
    assert Path(body["job_bundle"]).exists() is kept
    # Deleting the bundle's link to a model leaves the model.
    assert checkpoint.read_bytes() == b"model"


def test_old_submission_bundles_are_deleted_when_comfyui_starts(comfy):
    """Bundles a submission left behind are deleted after a day, and bundles saved to keep aren't."""
    submissions = comfy.tmp_path / "user" / "deadline_submissions"
    marker = comfy.routes.SUBMISSION_MARKER
    two_days_ago = time.time() - 2 * 24 * 60 * 60
    for name in ("old", "recent", "saved"):
        (submissions / name).mkdir(parents=True)
        (submissions / name / "template.yaml").write_text("x")
        os.utime(submissions / name / "template.yaml", (two_days_ago, two_days_ago))
    for name in ("old", "recent"):
        (submissions / name / marker).write_text("")
    os.utime(submissions / "old" / marker, (two_days_ago, two_days_ago))
    comfy.routes._remove_old_submissions(submissions)
    assert sorted(path.name for path in submissions.iterdir()) == ["recent", "saved"]


@pytest.mark.parametrize(
    ("excluded", "attached"),
    [
        (["loras/shared.safetensors"], ["checkpoints"]),
        ([], ["checkpoints", "loras"]),
        # A panel older than model IDs leaves out every model of the name.
        (["shared.safetensors"], []),
    ],
)
def test_jobs_attach_and_exclude_models_by_folder_and_name(comfy, monkeypatch, excluded, attached):
    """A checkpoint and a LoRA can share a file name, and each is attached or removed on its own."""
    fake_cli(monkeypatch, comfy.tmp_path, "")
    requests = fake_bridge(monkeypatch, comfy.tmp_path, {"submit": [result({"jobId": "job-1"})]})
    for folder in ("checkpoints", "loras"):
        (comfy.tmp_path / "models" / folder).mkdir(parents=True)
        (comfy.tmp_path / "models" / folder / "shared.safetensors").write_bytes(b"model")
    workflow = {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "shared.safetensors"},
        },
        "5": {"class_type": "LoraLoader", "inputs": {"lora_name": "shared.safetensors"}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "fox"}},
    }
    body = {**JOB, "workflow": workflow, "parameters": {}, "excludedModels": excluded}
    status, response = run(call(comfy, "/deadline/jobs", body))
    assert status == 202
    staged = Path(response["job_bundle"]) / "local_models"
    assert sorted(path.parent.name for path in staged.glob("*/shared.safetensors")) == attached
    assert bool(response["warnings"]) is not bool(attached)
    sent = bridge_requests(requests)[0]["request"]["parameters"]
    assert ("ModelDirectory" in sent) is bool(attached)


def test_a_bundle_that_cant_all_be_deleted_is_marked_again(comfy, monkeypatch, caplog):
    """Such as a file an antivirus has open on Windows. A later sweep tries again."""
    bundle = comfy.tmp_path / "bundle"
    bundle.mkdir()
    (bundle / comfy.routes.SUBMISSION_MARKER).write_text("")
    (bundle / "m.safetensors").write_bytes(b"model")

    def partly(path, ignore_errors=False):
        (Path(path) / comfy.routes.SUBMISSION_MARKER).unlink()

    monkeypatch.setattr(comfy.routes.shutil, "rmtree", partly)
    comfy.remove_bundle(bundle)
    assert (bundle / comfy.routes.SUBMISSION_MARKER).is_file()
    assert "Couldn't delete all of the job bundle" in caplog.text


def test_jobs_mark_the_bundle_before_staging_models(comfy, monkeypatch):
    """A bundle left by a crash while copying a model is still deleted later."""
    fake_tools(monkeypatch, comfy.tmp_path)
    _local_checkpoint(comfy)
    marked = []

    def fail(models, directories, dest):
        marked.append((dest.parent / comfy.routes.SUBMISSION_MARKER).is_file())
        raise OSError("disk full")

    monkeypatch.setattr(comfy.routes, "stage_local_models", fail)
    status, _ = run(post(comfy, {"workflow": WORKFLOW}))
    assert status == 202
    assert marked == [True]


def test_jobs_delete_old_bundles_too(comfy, monkeypatch):
    """A ComfyUI server can run for weeks, so old bundles are deleted on submitting too."""
    old = comfy.tmp_path / "user" / "deadline_submissions" / "old"
    old.mkdir(parents=True)
    (old / comfy.routes.SUBMISSION_MARKER).write_text("")
    two_days_ago = time.time() - 2 * 24 * 60 * 60
    os.utime(old / comfy.routes.SUBMISSION_MARKER, (two_days_ago, two_days_ago))
    fake_tools(monkeypatch, comfy.tmp_path)
    status, _ = run(post(comfy, {"workflow": WORKFLOW}))
    assert status == 202
    assert not old.exists()


def test_jobs_dont_read_a_model_id_as_another_models_name(comfy, monkeypatch):
    """A LoRA in a loras/checkpoints/ subfolder has the name checkpoints/x.safetensors."""
    fake_cli(monkeypatch, comfy.tmp_path, "")
    fake_bridge(monkeypatch, comfy.tmp_path, {"submit": [result({"jobId": "job-1"})]})
    for relative in ("checkpoints/x.safetensors", "loras/checkpoints/x.safetensors"):
        (comfy.tmp_path / "models" / relative).parent.mkdir(parents=True, exist_ok=True)
        (comfy.tmp_path / "models" / relative).write_bytes(b"model")
    workflow = {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "x.safetensors"}},
        "5": {"class_type": "LoraLoader", "inputs": {"lora_name": "checkpoints/x.safetensors"}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "fox"}},
    }
    body = {
        **JOB,
        "workflow": workflow,
        "parameters": {},
        "excludedModels": ["checkpoints/x.safetensors"],
    }
    status, response = run(call(comfy, "/deadline/jobs", body))
    assert status == 202
    staged = Path(response["job_bundle"]) / "local_models"
    assert [path.relative_to(staged).as_posix() for path in staged.rglob("*.safetensors")] == [
        "loras/checkpoints/x.safetensors"
    ]


REQUIREMENTS = {
    "attributes": [{"name": "attr.worker.os.family", "anyOf": ["linux", "windows"]}],
    "amounts": [{"name": "amount.worker.memory", "min": 8192}],
}


def _template_steps(bundle_dir: Path) -> list[dict]:
    return yaml.safe_load((bundle_dir / "template.yaml").read_text())["steps"]


def test_plan_starts_host_requirements_from_the_job_template(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    status, body = run(call(comfy, "/deadline/plan", {"workflow": WORKFLOW, "name": "fox"}))
    assert status == 200
    assert body["hostRequirements"] == {
        "attributes": [{"name": "attr.worker.os.family", "anyOf": ["linux"]}],
        "amounts": [{"name": "amount.worker.gpu", "min": 1}],
    }


def test_jobs_replaces_the_host_requirements_and_description(comfy, monkeypatch):
    fake_tools(monkeypatch, comfy.tmp_path)
    body = {**JOB, "hostRequirements": REQUIREMENTS, "description": "Fox lookdev"}
    status, response = run(call(comfy, "/deadline/jobs", body))
    assert status == 202
    template = yaml.safe_load((Path(response["job_bundle"]) / "template.yaml").read_text())
    assert template["description"] == "Fox lookdev"
    assert [step["hostRequirements"] for step in template["steps"]] == [REQUIREMENTS]


def test_jobs_runs_on_any_worker_when_the_requirements_are_cleared(comfy, monkeypatch):
    fake_tools(monkeypatch, comfy.tmp_path)
    body = {**JOB, "hostRequirements": {"attributes": [], "amounts": []}}
    status, response = run(call(comfy, "/deadline/jobs", body))
    assert status == 202
    assert "hostRequirements" not in _template_steps(Path(response["job_bundle"]))[0]


def test_jobs_rejects_bad_host_requirements_before_submitting(comfy, monkeypatch):
    requests = fake_tools(monkeypatch, comfy.tmp_path)
    bad = {"amounts": [{"name": "amount.worker.gpu", "min": 4, "max": 1}]}
    status, response = run(call(comfy, "/deadline/jobs", {**JOB, "hostRequirements": bad}))
    assert status == 400
    assert "more than its max" in response["error"]
    assert not requests.exists()
    assert not (comfy.tmp_path / "user" / "deadline_submissions").exists()


def test_bundle_saves_the_values_without_submitting(comfy, monkeypatch):
    requests = fake_bridge(monkeypatch, comfy.tmp_path, {})
    body = {
        "workflow": WORKFLOW,
        "name": "fox",
        "parameters": {"ComfyUIArgs": ""},
        "settings": {
            "name": "Fox",
            "priority": 80,
            "targetTaskRunStatus": "SUSPENDED",
            "maxRetriesPerTask": 2,
            "maxWorkerCount": "",
        },
        "hostRequirements": REQUIREMENTS,
    }
    status, response = run(call(comfy, "/deadline/bundle", body))
    assert status == 200
    assert not requests.exists()
    bundle = Path(response["job_bundle"])
    saved = yaml.safe_load((bundle / "parameter_values.yaml").read_text())["parameterValues"]
    values = {item["name"]: item["value"] for item in saved}
    assert values["WorkflowFile"] == str(bundle / "fox_api.json")
    assert values["ComfyUIArgs"] == ""
    # deadline bundle submit reads the job settings from deadline:<name> values. An empty setting
    # leaves the queue's default.
    assert {name: value for name, value in values.items() if name.startswith("deadline:")} == {
        "deadline:priority": 80,
        "deadline:targetTaskRunStatus": "SUSPENDED",
        "deadline:maxRetriesPerTask": 2,
    }
    assert (bundle / "parameter_values.yaml").stat().st_mode & 0o777 == 0o600 or os.name == "nt"
    assert _template_steps(bundle)[0]["hostRequirements"] == REQUIREMENTS
    # A saved bundle is submitted later, so it isn't deleted with the submissions.
    assert not (bundle / comfy.routes.SUBMISSION_MARKER).exists()


def test_bundle_rejects_bad_host_requirements(comfy, monkeypatch):
    status, response = run(
        call(comfy, "/deadline/bundle", {"workflow": WORKFLOW, "hostRequirements": "linux"})
    )
    assert status == 400
    assert "hostRequirements" in response["error"]


def test_download_sends_events_only_to_the_tab_that_asked(comfy, monkeypatch):
    lines = [
        {"messageType": "progress", "value": "50"},
        {"messageType": "summary", "value": "Downloaded 2 files", "fileCount": 2},
    ]
    fake_cli(monkeypatch, comfy.tmp_path, "\n".join(map(json.dumps, lines)))
    body = {"farmId": FARM, "queueId": QUEUE, "jobId": JOB_ID, "clientId": "tab-1"}
    status, _ = run(call(comfy, "/deadline/jobs/download", body))
    assert status == 202
    assert comfy.server.sids == ["tab-1", "tab-1"]


def test_download_runs_the_cli_and_reports_progress(comfy, monkeypatch):
    lines = [
        {"messageType": "title", "value": "fox"},
        {"messageType": "progress", "value": "50"},
        {"messageType": "summary", "value": "Downloaded 2 files", "fileCount": 2},
    ]
    args_file = fake_cli(monkeypatch, comfy.tmp_path, "\n".join(map(json.dumps, lines)))
    body = {"farmId": FARM, "queueId": QUEUE, "jobId": JOB_ID}
    status, result = run(call(comfy, "/deadline/jobs/download", body))
    assert (status, result) == (202, {"jobId": JOB_ID})
    args = json.loads(args_file.read_text())
    assert args[:2] == ["job", "download-output"]
    assert args[args.index("--job-id") + 1] == JOB_ID
    assert {"--yes", "json", "create_copy"} <= set(args)
    assert comfy.server.events == [
        ("deadline.download", {"jobId": JOB_ID, "status": "DOWNLOADING", "progress": "50"}),
        (
            "deadline.download",
            {
                "jobId": JOB_ID,
                "status": "DOWNLOADED",
                "message": "Downloaded 2 files",
                "fileCount": 2,
            },
        ),
    ]


def test_download_says_which_folder_it_downloaded_into(comfy, monkeypatch):
    output = comfy.tmp_path / "output"
    files = [str(output / "fox" / "fox_00001_.png"), str(output / "fox_00002_.png")]
    summary = {"messageType": "summary", "value": "Downloaded 2 files", "fileCount": 2}
    fake_cli(monkeypatch, comfy.tmp_path, json.dumps({**summary, "files": files}))
    body = {"farmId": FARM, "queueId": QUEUE, "jobId": JOB_ID}
    run(call(comfy, "/deadline/jobs/download", body))
    assert comfy.server.events[-1][1]["directory"] == str(output)


@pytest.mark.parametrize("files", [None, [], ["a.png", 5], "a.png"])
def test_download_leaves_out_a_folder_it_cant_tell(comfy, monkeypatch, files):
    summary = {"messageType": "summary", "value": "Downloaded 1 file", "fileCount": 1}
    if files is not None:
        summary["files"] = files
    fake_cli(monkeypatch, comfy.tmp_path, json.dumps(summary))
    body = {"farmId": FARM, "queueId": QUEUE, "jobId": JOB_ID}
    run(call(comfy, "/deadline/jobs/download", body))
    assert comfy.server.events[-1][1]["status"] == "DOWNLOADED"
    assert "directory" not in comfy.server.events[-1][1]


def test_download_reads_stderr_while_waiting_for_the_summary(comfy, monkeypatch):
    """A CLI that fills its stderr pipe before printing its summary still finishes."""
    summary = {"messageType": "summary", "value": "Downloaded 1 file", "fileCount": 1}
    fake_cli(monkeypatch, comfy.tmp_path, json.dumps(summary), stderr_noise=600_000)
    body = {"farmId": FARM, "queueId": QUEUE, "jobId": JOB_ID}
    run(asyncio.wait_for(call(comfy, "/deadline/jobs/download", body), 30))
    assert comfy.server.events[-1][1]["status"] == "DOWNLOADED"


def _long_summary() -> dict:
    """A summary line well over asyncio's default 64 KiB line limit, as for a long frame range."""
    files = [
        f"/renders/shot_010/very_long_output_directory_name/fox_{i:05}.png" for i in range(3000)
    ]
    return {
        "messageType": "summary",
        "value": "Downloaded 3000 files",
        "fileCount": 3000,
        "files": files,
    }


def test_download_reports_a_summary_longer_than_the_default_line_limit(comfy, monkeypatch):
    summary = _long_summary()
    assert len(json.dumps(summary)) > 64 * 1024
    fake_cli(monkeypatch, comfy.tmp_path, json.dumps(summary))
    body = {"farmId": FARM, "queueId": QUEUE, "jobId": JOB_ID}
    run(asyncio.wait_for(call(comfy, "/deadline/jobs/download", body), 30))
    assert comfy.server.events[-1][1]["status"] == "DOWNLOADED"
    assert comfy.server.events[-1][1]["fileCount"] == 3000


def test_download_reports_output_it_cant_read(comfy, monkeypatch):
    """The panel stops showing the download as running, whatever went wrong reading it."""
    monkeypatch.setattr(comfy.routes, "DOWNLOAD_LINE_LIMIT", 1024)
    fake_cli(monkeypatch, comfy.tmp_path, json.dumps(_long_summary()))
    body = {"farmId": FARM, "queueId": QUEUE, "jobId": JOB_ID}
    run(asyncio.wait_for(call(comfy, "/deadline/jobs/download", body), 30))
    [(event, data)] = comfy.server.events
    assert (event, data["status"]) == ("deadline.download", "FAILED")
    assert "Couldn't read the CLI's output" in data["error"]


def test_download_reports_failures(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "", exit_code=1, stderr="Credentials are expired")
    body = {"farmId": FARM, "queueId": QUEUE, "jobId": JOB_ID}
    run(call(comfy, "/deadline/jobs/download", body))
    assert comfy.server.events[-1] == (
        "deadline.download",
        {"jobId": JOB_ID, "status": "FAILED", "error": "Credentials are expired"},
    )


def test_download_failures_log_stderr_indented(comfy, monkeypatch, caplog):
    stderr = "Credentials are expired\nERROR forged entry"
    fake_cli(monkeypatch, comfy.tmp_path, "", exit_code=1, stderr=stderr)
    body = {"farmId": FARM, "queueId": QUEUE, "jobId": JOB_ID}
    with caplog.at_level("ERROR", logger=comfy.routes.logger.name):
        run(call(comfy, "/deadline/jobs/download", body))
    message = caplog.records[-1].getMessage()
    assert message.splitlines() == [
        f"Couldn't download the outputs of {JOB_ID}:",
        "    Credentials are expired",
        "    ERROR forged entry",
    ]


def test_download_only_takes_deadline_ids(comfy, monkeypatch):
    body = {"farmId": FARM, "queueId": QUEUE, "jobId": "--profile=evil"}
    status, _ = run(call(comfy, "/deadline/jobs/download", body))
    assert status == 400


def use_basic_template(comfy, monkeypatch) -> Path:
    """A studio template without a custom nodes directory."""
    template = comfy.tmp_path / "studio.yaml"
    template.write_text(
        yaml.safe_dump(
            {
                "specificationVersion": "jobtemplate-2023-09",
                "name": "Studio job",
                "parameterDefinitions": [
                    {"name": name, "type": "PATH"}
                    for name in ("WorkflowFile", "InputDir", "OutputDir", "ModelDirectory")
                ],
                "steps": [{"name": "Run", "script": {"actions": {"onRun": {"command": "true"}}}}],
            }
        )
    )
    monkeypatch.setenv(submit.TEMPLATE_ENV_VAR, str(template))
    return template


def fake_nodes(monkeypatch, modules: dict[str, str], loaded: dict[str, Path]) -> None:
    """Stands in for ComfyUI's nodes module: node type to module, and module to its folder."""
    monkeypatch.setitem(
        sys.modules,
        "nodes",
        types.SimpleNamespace(
            NODE_CLASS_MAPPINGS={
                class_type: types.SimpleNamespace(RELATIVE_PYTHON_MODULE=module)
                for class_type, module in modules.items()
            },
            LOADED_MODULE_DIRS={name: str(path) for name, path in loaded.items()},
        ),
    )


def write_pack(directory: Path, registry: str | None = None) -> Path:
    (directory / "nodes").mkdir(parents=True)
    (directory / "__init__.py").write_text("")
    (directory / "nodes" / "resize.py").write_text("")
    (directory / ".git").mkdir()
    (directory / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    if registry:
        (directory / ".tracking").write_text("")
        (directory / "pyproject.toml").write_text(
            f'[project]\nname = "{registry}"\nversion = "1.5.2"\n'
        )
    return directory


def _studio_pack(comfy, monkeypatch):
    """A custom node pack from a git checkout, and a workflow that runs one of its nodes."""
    custom = comfy.tmp_path / "custom_nodes"
    pack = write_pack(custom / "studio-nodes")
    fake_nodes(
        monkeypatch,
        {"StudioResize": "custom_nodes.studio-nodes"},
        {"studio-nodes": pack},
    )
    return pack, {**WORKFLOW, "5": {"class_type": "StudioResize", "inputs": {}}}


def test_jobs_attaches_every_custom_node_pack_the_workflow_uses(comfy, monkeypatch):
    """A git checkout and a registry install are both attached under CustomNodesDirectory."""
    custom = comfy.tmp_path / "custom_nodes"
    git_pack = write_pack(custom / "studio-nodes")
    write_pack(custom / "comfyui-kjnodes", registry="comfyui-kjnodes")
    fake_nodes(
        monkeypatch,
        {
            "StudioResize": "custom_nodes.studio-nodes",
            "ImageResizeKJv2": "custom_nodes.comfyui-kjnodes",
        },
        {"studio-nodes": git_pack, "comfyui-kjnodes": custom / "comfyui-kjnodes"},
    )
    workflow = {
        **WORKFLOW,
        "5": {"class_type": "StudioResize", "inputs": {}},
        "6": {"class_type": "ImageResizeKJv2", "inputs": {}},
    }
    requests = fake_tools(monkeypatch, comfy.tmp_path)
    status, body = run(post(comfy, {"workflow": workflow, "ui_workflow": {"nodes": []}}))
    assert status == 202
    bundle = Path(body["job_bundle"])
    staged = bundle / "local_nodes" / "studio-nodes"
    assert (staged / "nodes" / "resize.py").is_file()
    assert not (staged / ".git").exists()
    kj = bundle / "local_nodes" / "comfyui-kjnodes"
    assert (kj / "__init__.py").is_file()
    references = yaml.safe_load((bundle / "asset_references.yaml").read_text())
    assert references["assetReferences"]["inputs"]["directories"] == [str(kj), str(staged)]
    parameters = sent_parameters(requests)
    assert "CustomNodes" not in parameters
    assert parameters["CustomNodesDirectory"] == str(bundle / "local_nodes")
    assert not any("custom nodes" in warning for warning in body["warnings"])


def test_jobs_finds_a_pack_in_a_registered_custom_nodes_folder(comfy, monkeypatch):
    """ComfyUI may not report a folder for the module, so the custom_nodes folders are searched."""
    shared = comfy.tmp_path / "shared" / "custom_nodes"
    pack = write_pack(shared / "studio-nodes")
    comfy.routes.folder_paths.folder_names_and_paths["custom_nodes"] = ([str(shared)], set())
    fake_nodes(monkeypatch, {"StudioResize": "custom_nodes.studio-nodes"}, {})
    workflow = {**WORKFLOW, "5": {"class_type": "StudioResize", "inputs": {}}}
    requests = fake_tools(monkeypatch, comfy.tmp_path)
    status, body = run(post(comfy, {"workflow": workflow}))
    assert status == 202
    bundle = Path(body["job_bundle"])
    staged = bundle / "local_nodes" / "studio-nodes"
    assert (staged / "__init__.py").stat().st_ino == (pack / "__init__.py").stat().st_ino
    parameters = sent_parameters(requests)
    assert parameters["CustomNodesDirectory"] == str(bundle / "local_nodes")
    assert "CustomNodes" not in parameters


def test_jobs_finds_a_v3_pack_from_the_class_module_path(comfy, monkeypatch):
    """A V3 extension class without RELATIVE_PYTHON_MODULE resolves from its __module__ path."""
    pack = write_pack(comfy.tmp_path / "custom_nodes" / "my_local_node_pack")
    monkeypatch.setitem(
        sys.modules,
        "nodes",
        types.SimpleNamespace(
            NODE_CLASS_MAPPINGS={
                "CustomTextToList": types.SimpleNamespace(__module__=f"{pack}.nodes")
            },
            LOADED_MODULE_DIRS={"my_local_node_pack": str(pack)},
        ),
    )
    workflow = {**WORKFLOW, "5": {"class_type": "CustomTextToList", "inputs": {}}}
    requests = fake_tools(monkeypatch, comfy.tmp_path)
    status, body = run(post(comfy, {"workflow": workflow}))
    assert status == 202
    bundle = Path(body["job_bundle"])
    staged = bundle / "local_nodes" / "my_local_node_pack"
    assert (staged / "nodes" / "resize.py").is_file()
    references = yaml.safe_load((bundle / "asset_references.yaml").read_text())
    assert references["assetReferences"]["inputs"]["directories"] == [str(staged)]
    parameters = sent_parameters(requests)
    assert parameters["CustomNodesDirectory"] == str(bundle / "local_nodes")
    assert not any("custom nodes" in warning for warning in body["warnings"])


def test_custom_node_pack_name(comfy):
    cases = [
        # V1 and V3 classes that ComfyUI tags with the module it loaded them from.
        (types.SimpleNamespace(RELATIVE_PYTHON_MODULE="custom_nodes.pack"), "pack"),
        # A pack folder's name can contain dots.
        (
            types.SimpleNamespace(RELATIVE_PYTHON_MODULE="custom_nodes.ComfyUI-Foo.v2"),
            "ComfyUI-Foo.v2",
        ),
        # V3 classes on a ComfyUI that doesn't tag them: __module__ is the file path.
        (types.SimpleNamespace(__module__="/ComfyUI/custom_nodes/my_pack.nodes"), "my_pack"),
        (types.SimpleNamespace(__module__="C:\\ComfyUI\\custom_nodes\\my_pack.nodes"), "my_pack"),
        # ComfyUI writes a pack folder's dots as _x_ in the module name.
        (
            types.SimpleNamespace(__module__="/ComfyUI/custom_nodes/ComfyUI-Foo_x_v2.nodes"),
            "ComfyUI-Foo.v2",
        ),
        # A tag that isn't a custom node falls back to the module path.
        (
            types.SimpleNamespace(
                RELATIVE_PYTHON_MODULE="comfy_extras.nodes_x",
                __module__="/ComfyUI/custom_nodes/my_pack",
            ),
            "my_pack",
        ),
        # Built-in nodes and single-file custom nodes have no pack folder.
        (types.SimpleNamespace(RELATIVE_PYTHON_MODULE="", __module__="nodes"), None),
        (types.SimpleNamespace(__module__="/ComfyUI/comfy_extras/nodes_x"), None),
        (None, None),
    ]
    for cls, expected in cases:
        assert comfy.routes._custom_node_pack_name(cls) == expected


def test_jobs_attaches_a_single_file_custom_node(comfy, monkeypatch):
    """A single-file custom node is attached as one file, listed under filenames."""
    custom = comfy.tmp_path / "custom_nodes"
    custom.mkdir()
    source = custom / "single.py"
    source.write_text("NODE_CLASS_MAPPINGS = {}")
    fake_nodes(monkeypatch, {"SingleNode": "custom_nodes.single"}, {"single": custom})
    workflow = {**WORKFLOW, "5": {"class_type": "SingleNode", "inputs": {}}}
    requests = fake_tools(monkeypatch, comfy.tmp_path)
    status, body = run(post(comfy, {"workflow": workflow}))
    assert status == 202
    bundle = Path(body["job_bundle"])
    staged = bundle / "local_nodes" / "single.py"
    assert staged.is_file() and staged.stat().st_ino == source.stat().st_ino
    references = yaml.safe_load((bundle / "asset_references.yaml").read_text())["assetReferences"]
    assert str(staged) in references["inputs"]["filenames"]
    assert references["inputs"]["directories"] == []
    parameters = sent_parameters(requests)
    assert parameters["CustomNodesDirectory"] == str(bundle / "local_nodes")
    assert not any("custom nodes" in warning for warning in body["warnings"])


def test_jobs_finds_a_single_file_node_in_a_registered_custom_nodes_folder(comfy, monkeypatch):
    """Without a LOADED_MODULE_DIRS entry, the custom_nodes folders are searched for <name>.py."""
    shared = comfy.tmp_path / "shared" / "custom_nodes"
    shared.mkdir(parents=True)
    (shared / "single.py").write_text("")
    comfy.routes.folder_paths.folder_names_and_paths["custom_nodes"] = ([str(shared)], set())
    fake_nodes(monkeypatch, {"SingleNode": "custom_nodes.single"}, {})
    workflow = {**WORKFLOW, "5": {"class_type": "SingleNode", "inputs": {}}}
    requests = fake_tools(monkeypatch, comfy.tmp_path)
    status, body = run(post(comfy, {"workflow": workflow}))
    assert status == 202
    bundle = Path(body["job_bundle"])
    assert (bundle / "local_nodes" / "single.py").is_file()
    assert sent_parameters(requests)["CustomNodesDirectory"] == str(bundle / "local_nodes")


def test_jobs_warns_about_custom_nodes_it_cannot_attach(comfy, monkeypatch):
    """A custom node whose pack folder and .py file are both missing can't be attached."""
    custom = comfy.tmp_path / "custom_nodes"
    custom.mkdir()
    fake_nodes(monkeypatch, {"GhostNode": "custom_nodes.ghost"}, {"ghost": custom})
    workflow = {**WORKFLOW, "5": {"class_type": "GhostNode", "inputs": {}}}
    requests = fake_tools(monkeypatch, comfy.tmp_path)
    status, body = run(post(comfy, {"workflow": workflow}))
    assert status == 202
    assert not (Path(body["job_bundle"]) / "local_nodes").exists()
    assert "CustomNodesDirectory" not in sent_parameters(requests)
    assert body["warnings"][-1] == (
        "The files of the custom nodes GhostNode couldn't be attached. The job fails unless "
        "the workers already have them, for example in a custom nodes conda package."
    )


def test_jobs_leaves_out_local_node_packs_for_jobs_without_the_parameter(comfy, monkeypatch):
    pack = write_pack(comfy.tmp_path / "custom_nodes" / "studio-nodes")
    fake_nodes(monkeypatch, {"StudioResize": "custom_nodes.studio-nodes"}, {"studio-nodes": pack})
    workflow = {**WORKFLOW, "5": {"class_type": "StudioResize", "inputs": {}}}
    requests = fake_tools(monkeypatch, comfy.tmp_path)
    use_basic_template(comfy, monkeypatch)
    status, body = run(post(comfy, {"workflow": workflow}))
    assert status == 202
    assert not (Path(body["job_bundle"]) / "local_nodes").exists()
    parameters = sent_parameters(requests)
    assert "CustomNodesDirectory" not in parameters


def test_custom_nodes_directory_does_not_upload_its_contents():
    template = yaml.safe_load(submit.JOB_TEMPLATE.read_text())
    parameter = next(
        item for item in template["parameterDefinitions"] if item["name"] == "CustomNodesDirectory"
    )
    assert parameter["dataFlow"] == "NONE"
    assert parameter["objectType"] == "DIRECTORY"


def test_jobs_accepts_a_non_string_attach_custom_nodes_value(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    _, workflow = _studio_pack(comfy, monkeypatch)
    fake_bridge(monkeypatch, comfy.tmp_path, {"submit": [result({"jobId": "job-1"})]})
    body = {**JOB, "workflow": workflow, "parameters": {"AttachCustomNodes": False}}
    status, response = run(call(comfy, "/deadline/jobs", body))
    assert status == 202
    assert not (Path(response["job_bundle"]) / "local_nodes").exists()


def test_plan_lists_local_custom_nodes_and_doesnt_warn_about_them(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    pack, workflow = _studio_pack(comfy, monkeypatch)
    status, body = run(call(comfy, "/deadline/plan", {"workflow": workflow, "name": "fox"}))
    assert status == 200
    assert body["localNodes"] == [{"name": "studio-nodes", "path": str(pack)}]
    assert body["nodeWarnings"] == []
    assert not any("custom nodes" in warning for warning in body["warnings"])
    # Planning doesn't stage anything.
    assert not (comfy.tmp_path / "user" / "deadline_submissions").exists()


def test_plan_hides_the_custom_nodes_directory_and_offers_the_switch(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    status, body = run(call(comfy, "/deadline/plan", {"workflow": WORKFLOW, "name": "fox"}))
    assert status == 200
    parameters = {parameter["name"]: parameter for parameter in body["parameters"]}
    assert parameters["CustomNodesDirectory"]["userInterface"]["control"] == "HIDDEN"
    assert parameters["AttachCustomNodes"]["userInterface"]["control"] == "CHECK_BOX"
    assert parameters["AttachCustomNodes"]["default"] == "true"


def test_plan_warns_about_custom_nodes_it_cant_find(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    fake_nodes(monkeypatch, {"StudioResize": "custom_nodes.gone"}, {})
    workflow = {**WORKFLOW, "5": {"class_type": "StudioResize", "inputs": {}}}
    status, body = run(call(comfy, "/deadline/plan", {"workflow": workflow, "name": "fox"}))
    assert status == 200
    assert body["localNodes"] == []
    assert len(body["nodeWarnings"]) == 1
    assert "StudioResize" in body["nodeWarnings"][0]
    assert body["nodeWarnings"][0] in body["warnings"]


def test_jobs_attaches_local_custom_nodes_through_their_directory(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    _, workflow = _studio_pack(comfy, monkeypatch)
    requests = fake_bridge(monkeypatch, comfy.tmp_path, {"submit": [result({"jobId": "job-1"})]})
    # The panel sends the hidden directory's empty default, and a folder of the artist's own.
    body = {
        **JOB,
        "workflow": workflow,
        "parameters": {"CustomNodesDirectory": "", "AttachCustomNodes": "true"},
        "attachments": {"filenames": [], "directories": ["/a/folder"]},
    }
    status, response = run(call(comfy, "/deadline/jobs", body))
    assert status == 202
    bundle = Path(response["job_bundle"])
    staged = bundle / "local_nodes" / "studio-nodes"
    assert (staged / "nodes" / "resize.py").is_file()
    assert not (staged / ".git").exists()
    references = yaml.safe_load((bundle / "asset_references.yaml").read_text())
    assert references["assetReferences"]["inputs"]["directories"] == sorted(
        ["/a/folder", str(staged)]
    )
    sent = bridge_requests(requests)[0]["request"]["parameters"]
    assert sent["CustomNodesDirectory"] == str(bundle / "local_nodes")
    assert sent["AttachCustomNodes"] == "true"


def test_jobs_attaches_a_single_file_custom_node_as_a_file(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    custom = comfy.tmp_path / "custom_nodes"
    custom.mkdir()
    (custom / "single.py").write_text("")
    fake_nodes(monkeypatch, {"Single": "custom_nodes.single"}, {"single": custom})
    workflow = {**WORKFLOW, "5": {"class_type": "Single", "inputs": {}}}
    fake_bridge(monkeypatch, comfy.tmp_path, {"submit": [result({"jobId": "job-1"})]})
    body = {**JOB, "workflow": workflow, "parameters": {}}
    status, response = run(call(comfy, "/deadline/jobs", body))
    assert status == 202
    bundle = Path(response["job_bundle"])
    staged = bundle / "local_nodes" / "single.py"
    assert staged.is_file()
    references = yaml.safe_load((bundle / "asset_references.yaml").read_text())
    assert str(staged) in references["assetReferences"]["inputs"]["filenames"]


def test_jobs_doesnt_upload_custom_nodes_with_attach_custom_nodes_off(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    pack, workflow = _studio_pack(comfy, monkeypatch)
    fake_nodes(
        monkeypatch,
        {"StudioResize": "custom_nodes.studio-nodes", "Gone": "custom_nodes.gone"},
        {"studio-nodes": pack},
    )
    workflow = {**workflow, "6": {"class_type": "Gone", "inputs": {}}}
    requests = fake_bridge(monkeypatch, comfy.tmp_path, {"submit": [result({"jobId": "job-1"})]})
    body = {**JOB, "workflow": workflow, "parameters": {"AttachCustomNodes": "false"}}
    status, response = run(call(comfy, "/deadline/jobs", body))
    assert status == 202
    assert not any("custom nodes" in warning for warning in response["warnings"])
    bundle = Path(response["job_bundle"])
    assert not (bundle / "local_nodes").exists()
    references = yaml.safe_load((bundle / "asset_references.yaml").read_text())
    assert not references["assetReferences"]["inputs"]["directories"]
    sent = bridge_requests(requests)[0]["request"]["parameters"]
    assert sent["AttachCustomNodes"] == "false"
    assert "CustomNodesDirectory" not in sent


def test_jobs_warns_when_a_planned_custom_node_pack_cant_be_staged(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    _, workflow = _studio_pack(comfy, monkeypatch)
    requests = fake_bridge(monkeypatch, comfy.tmp_path, {"submit": [result({"jobId": "job-1"})]})

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(comfy.routes, "stage_local_node_packs", fail)
    body = {**JOB, "workflow": workflow, "parameters": {}}
    status, response = run(call(comfy, "/deadline/jobs", body))
    assert status == 202
    assert any("StudioResize" in warning for warning in response["warnings"])
    assert "CustomNodesDirectory" not in bridge_requests(requests)[0]["request"]["parameters"]


def _staged_node_packs(response: dict) -> list[str]:
    bundle = Path(response["job_bundle"])
    references = yaml.safe_load((bundle / "asset_references.yaml").read_text())
    directories = references["assetReferences"]["inputs"]["directories"]
    assert (bundle / "local_nodes").exists() is bool(directories)
    return directories


@pytest.mark.parametrize("value", ["no", "OFF", "0", 0, " False "])
def test_jobs_read_attach_custom_nodes_as_openjd_does(comfy, monkeypatch, value):
    """OpenJD reads each of these as false, so the job wouldn't load the packs."""
    fake_cli(monkeypatch, comfy.tmp_path, "")
    _, workflow = _studio_pack(comfy, monkeypatch)
    fake_bridge(monkeypatch, comfy.tmp_path, {"submit": [result({"jobId": "job-1"})]})
    body = {**JOB, "workflow": workflow, "parameters": {"AttachCustomNodes": value}}
    status, response = run(call(comfy, "/deadline/jobs", body))
    assert status == 202, response
    assert _staged_node_packs(response) == []


def test_jobs_use_the_templates_attach_custom_nodes_default(comfy, monkeypatch):
    """A studio template can turn the switch off by default, and a request can leave it out."""
    template = yaml.safe_load(submit.JOB_TEMPLATE.read_text())
    for parameter in template["parameterDefinitions"]:
        if parameter["name"] == "AttachCustomNodes":
            parameter["default"] = "no"
    studio = comfy.tmp_path / "studio.yaml"
    studio.write_text(yaml.safe_dump(template, sort_keys=False))
    monkeypatch.setenv(submit.TEMPLATE_ENV_VAR, str(studio))
    fake_cli(monkeypatch, comfy.tmp_path, "")
    _, workflow = _studio_pack(comfy, monkeypatch)
    fake_bridge(monkeypatch, comfy.tmp_path, {"submit": [result({"jobId": "job-1"})]})
    status, response = run(call(comfy, "/deadline/jobs", {**JOB, "workflow": workflow}))
    assert status == 202, response
    assert _staged_node_packs(response) == []


@pytest.mark.parametrize(
    ("parameters", "error"),
    [
        ({"AttachCustomNodes": "garbage"}, "Attach Custom Nodes must be true or false"),
        ({"AttachCustomNodes": ""}, "Attach Custom Nodes must be true or false"),
        ({"AttachCustomNodes": 2}, "Attach Custom Nodes must be true or false"),
        ({"WorkflowTimeout": "soon"}, "must be a whole number, not 'soon'"),
        ({"WorkflowTimeout": "30"}, "must be at least 60, not '30'"),
    ],
)
@pytest.mark.parametrize("route", ["/deadline/jobs", "/deadline/bundle"])
def test_bad_parameter_values_are_refused_before_anything_is_staged(
    comfy, monkeypatch, parameters, error, route
):
    """Deadline Cloud would refuse them too, after the models and packs were copied."""
    fake_cli(monkeypatch, comfy.tmp_path, "")
    _, workflow = _studio_pack(comfy, monkeypatch)
    requests = fake_bridge(monkeypatch, comfy.tmp_path, {})
    staged = []
    monkeypatch.setattr(comfy.routes, "_stage_prepared", lambda *args: staged.append(args))
    status, response = run(
        call(comfy, route, {**JOB, "workflow": workflow, "parameters": parameters})
    )
    assert status == 400
    assert error in response["error"]
    assert staged == []
    assert not requests.exists()
    assert not (comfy.tmp_path / "user" / "deadline_submissions").exists()
