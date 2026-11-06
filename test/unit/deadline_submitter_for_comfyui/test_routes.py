# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Tests for the /deadline routes. ComfyUI's `server` and `folder_paths` modules are replaced with
stubs, and a fake `deadline` CLI and a fake bridge.py stand in for the Deadline Cloud tools.
"""

import asyncio
import importlib
import json
import os
import shlex
import sys
import textwrap
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

    def send_sync(self, event, data, sid=None):
        self.events.append((event, data))


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
            },
        ),
    )
    monkeypatch.delitem(sys.modules, "deadline.comfyui_submitter.routes", raising=False)
    routes = importlib.import_module("deadline.comfyui_submitter.routes")
    for var in (
        submit.TEMPLATE_ENV_VAR,
        submit.ALLOW_REMOTE_ENV_VAR,
        submit.CONDA_CHANNELS_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"conda_channels": "s3://bucket/Conda/Default conda-forge"}))
    monkeypatch.setenv(submit.CONFIG_ENV_VAR, str(config))
    return types.SimpleNamespace(routes=routes, server=prompt_server, tmp_path=tmp_path)


def fake_cli(
    monkeypatch,
    tmp_path: Path,
    stdout: str,
    exit_code: int = 0,
    stderr: str = "",
    version: str = "0.60.10",
) -> Path:
    args_file = tmp_path / "cli-args.json"
    script = tmp_path / "fake_deadline.py"
    script.write_text(textwrap.dedent(f"""\
            import json, sys
            if sys.argv[1:] == ["--version"]:
                print("deadline, version {version}")
                sys.exit(0)
            json.dump(sys.argv[1:], open({str(args_file)!r}, "w"))
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

    ui_workflow = {
        "nodes": [
            {
                "type": "CheckpointLoaderSimple",
                "properties": {
                    "models": [
                        {
                            "directory": "checkpoints",
                            "name": "m.safetensors",
                            "url": "https://x.test/m.safetensors",
                        }
                    ]
                },
            }
        ]
    }
    checkpoint = comfy.tmp_path / "models" / "checkpoints" / "m.safetensors"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"model")
    status, body = run(
        post(comfy, {"workflow": WORKFLOW, "ui_workflow": ui_workflow, "name": "portrait.json"})
    )

    assert status == 202
    bundle_dir = Path(body["job_bundle"])
    assert bundle_dir.parent == comfy.tmp_path / "user" / "deadline_submissions"
    assert bundle_dir.name.endswith("_portrait")
    assert (bundle_dir / "template.yaml").read_text() == submit.JOB_TEMPLATE.read_text()
    workflow_file = Path(body["workflow_file"])
    assert workflow_file == bundle_dir / "portrait_api.json"
    assert json.loads(workflow_file.read_text()) == WORKFLOW
    # The worker downloads the checkpoint, so it isn't an asset reference.
    assert str(checkpoint) not in (bundle_dir / "asset_references.yaml").read_text()

    request = bridge_requests(requests)[0]["request"]
    assert request["bundleDir"] == str(bundle_dir)
    assert request["settings"]["name"] == "ComfyUI - portrait"
    parameters = request["parameters"]
    assert parameters["WorkflowFile"] == str(workflow_file)
    assert parameters["OutputDir"] == str(comfy.tmp_path / "output")
    assert parameters["InputDir"] == str(comfy.tmp_path / "input")
    assert parameters["ModelDownloads"] == "checkpoints/m.safetensors=https://x.test/m.safetensors"


def test_jobs_attaches_a_local_model_when_the_url_file_does_not_match(comfy, monkeypatch):
    requests = fake_tools(monkeypatch, comfy.tmp_path)
    checkpoint = comfy.tmp_path / "models" / "checkpoints" / "custom.safetensors"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"model")
    ui_workflow = {
        "nodes": [
            {
                "type": "CheckpointLoaderSimple",
                "properties": {
                    "models": [
                        {
                            "directory": "checkpoints",
                            "name": "custom.safetensors",
                            "url": "https://x.test/other.safetensors",
                        }
                    ]
                },
            }
        ]
    }
    workflow = {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "custom.safetensors"},
        },
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "comfyui"}},
    }
    status, body = run(
        post(comfy, {"workflow": workflow, "ui_workflow": ui_workflow, "name": "local"})
    )
    assert status == 202
    bundle = Path(body["job_bundle"])
    written = json.loads(Path(body["workflow_file"]).read_text())
    assert written["4"]["inputs"]["ckpt_name"] == "custom.safetensors"
    staged = bundle / "local_models" / "checkpoints" / "custom.safetensors"
    assert staged.is_file() and staged.stat().st_ino == checkpoint.stat().st_ino
    references = yaml.safe_load((bundle / "asset_references.yaml").read_text())
    assert references["assetReferences"]["inputs"]["filenames"] == [str(staged)]
    parameters = sent_parameters(requests)
    assert "ModelDownloads" not in parameters
    assert parameters["ModelDirectory"] == str(bundle / "local_models")
    assert body["warnings"] == []


def test_jobs_warns_when_a_mismatched_url_has_no_local_file(comfy, monkeypatch):
    fake_tools(monkeypatch, comfy.tmp_path)
    ui_workflow = {
        "nodes": [
            {
                "type": "CheckpointLoaderSimple",
                "properties": {
                    "models": [
                        {
                            "directory": "checkpoints",
                            "name": "m.safetensors",
                            "url": "https://x.test/other.safetensors",
                        }
                    ]
                },
            }
        ]
    }
    status, body = run(post(comfy, {"workflow": WORKFLOW, "ui_workflow": ui_workflow}))
    assert status == 202
    assert body["warnings"] == [
        (
            "The URL for m.safetensors names a different file, and there is no local file with "
            "that name. Add it to Model Downloads in the panel, or the job fails unless the "
            "workers already have it in the model cache or an extra model path."
        )
    ]


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
    assert "No download link for m.safetensors" in body["warnings"][0]


def test_jobs_lists_local_models_in_asset_references(comfy, monkeypatch):
    """A model with no URL is attached; one with a valid URL is downloaded on the worker."""
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
    staged = bundle / "local_models" / "checkpoints" / "local.safetensors"
    references = yaml.safe_load((bundle / "asset_references.yaml").read_text())
    assert references["assetReferences"]["inputs"]["filenames"] == [str(staged)]
    assert references["assetReferences"]["inputs"]["directories"] == []
    assert not (bundle / "local_models" / "checkpoints" / "other.safetensors").exists()
    parameters = sent_parameters(requests)
    assert parameters["ModelDownloads"] == (
        "checkpoints/downloadable.safetensors=https://x.test/downloadable.safetensors"
    )
    assert parameters["ModelDirectory"] == str(bundle / "local_models")
    assert body["warnings"] == []


def test_model_directory_does_not_upload_its_contents():
    template = yaml.safe_load(submit.JOB_TEMPLATE.read_text())
    parameter = next(
        item for item in template["parameterDefinitions"] if item["name"] == "ModelDirectory"
    )
    assert parameter["dataFlow"] == "NONE"
    assert parameter["objectType"] == "DIRECTORY"


def use_basic_template(comfy, monkeypatch) -> Path:
    """A studio template without work items, custom nodes or a custom nodes directory."""
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


REGISTRY_NODE_UI = {
    "nodes": [{"type": "KSampler", "properties": {"cnr_id": "comfyui-kjnodes", "ver": "1.1.9"}}]
}


def test_jobs_passes_custom_nodes_to_comfy_cli_job(comfy, monkeypatch):
    requests = fake_tools(monkeypatch, comfy.tmp_path)
    run(post(comfy, {"workflow": WORKFLOW, "ui_workflow": REGISTRY_NODE_UI}))
    assert sent_parameters(requests)["CustomNodes"] == "comfyui-kjnodes@1.1.9"


def test_jobs_finds_custom_nodes_from_the_packs_comfyui_loaded(comfy, monkeypatch):
    pack_dir = comfy.tmp_path / "custom_nodes" / "comfyui-kjnodes"
    pack_dir.mkdir(parents=True)
    (pack_dir / ".tracking").write_text("")
    (pack_dir / "pyproject.toml").write_text(
        '[project]\nname = "comfyui-kjnodes"\nversion = "1.5.2"\n'
    )
    node_class = types.SimpleNamespace(RELATIVE_PYTHON_MODULE="custom_nodes.comfyui-kjnodes")
    monkeypatch.setitem(
        sys.modules,
        "nodes",
        types.SimpleNamespace(
            NODE_CLASS_MAPPINGS={"ImageResizeKJv2": node_class},
            LOADED_MODULE_DIRS={"comfyui-kjnodes": str(pack_dir)},
        ),
    )
    workflow = {**WORKFLOW, "5": {"class_type": "ImageResizeKJv2", "inputs": {}}}
    requests = fake_tools(monkeypatch, comfy.tmp_path)
    run(post(comfy, {"workflow": workflow, "ui_workflow": {"nodes": []}}))
    assert sent_parameters(requests)["CustomNodes"] == "comfyui-kjnodes@1.5.2"


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


def test_jobs_attaches_a_custom_node_pack_that_is_not_from_the_registry(comfy, monkeypatch):
    """A git checkout is attached under CustomNodesDirectory; a registry install is not."""
    custom = comfy.tmp_path / "custom_nodes"
    git_pack = write_pack(custom / "studio-nodes")
    write_pack(custom / "comfyui-kjnodes", registry="comfyui-kjnodes")
    fake_nodes(
        monkeypatch,
        {
            "StudioResize": "custom_nodes.studio-nodes.nodes",
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
    assert not (bundle / "local_nodes" / "comfyui-kjnodes").exists()
    references = yaml.safe_load((bundle / "asset_references.yaml").read_text())
    assert references["assetReferences"]["inputs"]["directories"] == [str(staged)]
    parameters = sent_parameters(requests)
    assert parameters["CustomNodes"] == "comfyui-kjnodes@1.5.2"
    assert parameters["CustomNodesDirectory"] == str(bundle / "local_nodes")
    assert not any("Comfy Registry" in warning for warning in body["warnings"])


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
    assert not any("Comfy Registry" in warning for warning in body["warnings"])


def test_custom_node_pack_name(comfy):
    cases = [
        # V1 and V3 classes that ComfyUI tags with the module it loaded them from.
        (types.SimpleNamespace(RELATIVE_PYTHON_MODULE="custom_nodes.pack"), "pack"),
        (types.SimpleNamespace(RELATIVE_PYTHON_MODULE="custom_nodes.pack.nodes"), "pack"),
        # V3 classes on a ComfyUI that doesn't tag them: __module__ is the file path.
        (types.SimpleNamespace(__module__="/ComfyUI/custom_nodes/my_pack.nodes"), "my_pack"),
        (types.SimpleNamespace(__module__="C:\\ComfyUI\\custom_nodes\\my_pack.nodes"), "my_pack"),
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
    assert not any("Comfy Registry" in warning for warning in body["warnings"])


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
        "The custom nodes GhostNode aren't from the Comfy Registry, and their files couldn't "
        "be attached. The job fails unless the workers already have them, for example in a "
        "custom nodes conda package."
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


def test_jobs_leaves_out_custom_nodes_for_jobs_without_the_parameter(comfy, monkeypatch):
    requests = fake_tools(monkeypatch, comfy.tmp_path)
    use_basic_template(comfy, monkeypatch)
    run(post(comfy, {"workflow": WORKFLOW, "ui_workflow": REGISTRY_NODE_UI}))
    parameters = sent_parameters(requests)
    assert "CustomNodes" not in parameters


def test_jobs_warns_about_models_without_download_links(comfy, monkeypatch):
    fake_tools(monkeypatch, comfy.tmp_path)
    status, body = run(post(comfy, {"workflow": WORKFLOW}))
    assert status == 202
    assert len(body["warnings"]) == 1
    assert "No download link for m.safetensors" in body["warnings"][0]


def _task_parameters(bundle_dir: Path) -> list[str]:
    template = yaml.safe_load((bundle_dir / "template.yaml").read_text())
    space = template["steps"][0]["parameterSpace"]["taskParameterDefinitions"]
    return [parameter["name"] for parameter in space]


def test_jobs_runs_list_items_as_tasks(comfy, monkeypatch):
    workflow = {
        **WORKFLOW,
        "10": {
            "class_type": "CreateList",
            "inputs": {"inputs.input0": ["11", 0], "inputs.input1": ["12", 0]},
        },
    }
    requests = fake_tools(monkeypatch, comfy.tmp_path)
    _, body = run(post(comfy, {"workflow": workflow}))
    assert _task_parameters(Path(body["job_bundle"])) == ["List10"]
    assert "List10" not in sent_parameters(requests)

    studio_template = use_basic_template(comfy, monkeypatch)
    _, body = run(post(comfy, {"workflow": workflow}))
    template = Path(body["job_bundle"]) / "template.yaml"
    assert template.read_text() == studio_template.read_text()


def test_jobs_restores_dynamic_prompts(comfy, monkeypatch):
    workflow = {**WORKFLOW, "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a red fox"}}}
    ui_workflow = {"nodes": [{"id": 6, "widgets_values": ["a {red|white} fox"]}]}
    fake_tools(monkeypatch, comfy.tmp_path)
    _, body = run(post(comfy, {"workflow": workflow, "ui_workflow": ui_workflow}))
    assert _task_parameters(Path(body["job_bundle"])) == ["Prompt6"]
    bundled = json.loads(Path(body["workflow_file"]).read_text())
    assert bundled["6"]["inputs"]["text"] == "a {red|white} fox"


def test_jobs_runs_lists_of_different_lengths_as_separate_task_parameters(comfy, monkeypatch):
    workflow = {
        **WORKFLOW,
        "10": {"class_type": submit.STRING_NODE, "inputs": {"values": "a\nb\nc"}},
        "11": {"class_type": "CreateList", "inputs": {"input0": ["1", 0], "input1": ["2", 0]}},
    }
    fake_tools(monkeypatch, comfy.tmp_path)
    _, body = run(post(comfy, {"workflow": workflow}))
    assert _task_parameters(Path(body["job_bundle"])) == ["String10", "List11"]
    assert not any("different numbers of items" in warning for warning in body["warnings"])


def test_jobs_reports_an_unreadable_job_template(comfy, monkeypatch):
    template = comfy.tmp_path / "studio.yaml"
    template.write_text("steps: [\n  ITEMS = {}\n")
    monkeypatch.setenv(submit.TEMPLATE_ENV_VAR, str(template))
    workflow = {
        **WORKFLOW,
        "11": {"class_type": "CreateList", "inputs": {"input0": 1, "input1": 2}},
    }
    fake_tools(monkeypatch, comfy.tmp_path)
    status, body = run(post(comfy, {"workflow": workflow}))
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
    assert submit.ALLOW_REMOTE_ENV_VAR in body["error"]


def test_jobs_allows_remote_clients_when_configured(comfy, monkeypatch):
    fake_tools(monkeypatch, comfy.tmp_path)
    monkeypatch.setenv(submit.ALLOW_REMOTE_ENV_VAR, "1")
    status, _ = run(post(comfy, {"workflow": WORKFLOW}, remote="10.0.0.5"))
    assert status == 202


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


def test_jobs_reports_the_task_count(comfy, monkeypatch):
    workflow = {
        **WORKFLOW,
        "10": {"class_type": submit.INTEGER_NODE, "inputs": {"values": "1-4"}},
        "11": {"class_type": "CreateList", "inputs": {"input0": 1, "input1": 2, "input2": 3}},
    }
    fake_cli(monkeypatch, comfy.tmp_path, "")
    _, body = run(call(comfy, "/deadline/plan", {"workflow": workflow}))
    assert body["tasks"] == "12 tasks: Integer10 4 × List11 3"

    use_basic_template(comfy, monkeypatch)
    _, body = run(call(comfy, "/deadline/plan", {"workflow": workflow}))
    assert body["tasks"] is None


def test_jobs_rejects_value_nodes_the_job_cant_run(comfy, monkeypatch):
    fake_tools(monkeypatch, comfy.tmp_path)
    workflow = {**WORKFLOW, "10": {"class_type": submit.STRING_NODE, "inputs": {"values": ""}}}
    status, body = run(post(comfy, {"workflow": workflow}))
    assert status == 400
    assert body["error"] == "Deadline String Values 10 has no values."


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


async def call(comfy, path, body, remote="127.0.0.1"):
    @web.middleware
    async def set_remote(request, handler):
        return await handler(request.clone(remote=remote))

    app = web.Application(middlewares=[set_remote])
    app.add_routes(comfy.server.routes)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(path, json=body)
        result = response.status, await response.json()
        await asyncio.gather(*comfy.routes._watchers)
        return result


def result(data):
    return {"event": "result", "data": data}


def test_plan_lists_parameters_work_items_and_attachments(comfy, monkeypatch):
    input_dir = comfy.tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "fox.png").write_text("png")
    workflow = {
        **WORKFLOW,
        "2": {"class_type": "LoadImage", "inputs": {"image": "fox.png"}},
        "10": {"class_type": submit.INTEGER_NODE, "inputs": {"values": "1-4", "preview_item": 1}},
    }
    status, body = run(call(comfy, "/deadline/plan", {"workflow": workflow, "name": "fox.json"}))
    assert status == 200
    assert body["name"] == "fox"
    assert body["template"] == "comfy_cli_job_template.yaml"
    names = [parameter["name"] for parameter in body["parameters"]]
    assert {"WorkflowFile", "OutputDir", "InputDir", "Integer10Items"} <= set(names)
    assert body["values"]["OutputDir"] == str(comfy.tmp_path / "output")
    assert body["values"]["InputDir"] == str(input_dir)
    assert body["queueValues"] == {"CondaChannels": "s3://bucket/Conda/Default conda-forge"}
    assert body["sources"] == [
        {
            "name": "Integer10",
            "parameter": "Integer10Items",
            "label": "Deadline Integer Values 10",
            "title": "Deadline Integer Values 10",
            "kind": "integer",
            "values": [],
            "count": 4,
            "range": "1-4",
            "pairWith": "",
        }
    ]
    assert body["tasks"] == "4 tasks: Integer10 4"
    assert body["attachments"] == {"filenames": [str(input_dir / "fox.png")], "directories": []}
    # Planning doesn't create a job bundle.
    assert not (comfy.tmp_path / "user" / "deadline_submissions").exists()


def test_plan_reports_workflows_it_cant_submit(comfy):
    workflow = {**WORKFLOW, "10": {"class_type": submit.STRING_NODE, "inputs": {"values": ""}}}
    status, body = run(call(comfy, "/deadline/plan", {"workflow": workflow}))
    assert (status, body) == (400, {"error": "Deadline String Values 10 has no values."})


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
    "parameters": {"ModelDownloads": "", "CondaChannels": "s3://bucket/Conda/Default"},
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
        # The artist cleared the model downloads.
        "ModelDownloads": "",
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
    assert events[2] == ("deadline.submitted", {**submission, **submitted, "status": "SUBMITTED"})
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
        {"name": "m.safetensors", "directory": "checkpoints", "path": str(checkpoint)}
    ]
    assert body["warnings"] == []
    # Planning doesn't stage anything.
    assert not (comfy.tmp_path / "user" / "deadline_submissions").exists()


def test_plan_warns_about_models_with_no_link_or_local_file(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    status, body = run(call(comfy, "/deadline/plan", {"workflow": WORKFLOW, "name": "fox"}))
    assert status == 200
    assert body["localModels"] == []
    assert "No download link for m.safetensors" in body["warnings"][0]


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
    assert "No download link for m.safetensors" in response["warnings"][0]
    bundle = Path(response["job_bundle"])
    assert not (bundle / "local_models").exists()
    references = yaml.safe_load((bundle / "asset_references.yaml").read_text())
    assert not references["assetReferences"]["inputs"]["filenames"]
    assert bridge_requests(requests)[0]["request"]["parameters"]["ModelDirectory"] == ""


def test_jobs_doesnt_warn_about_a_removed_model_the_artist_added_a_link_for(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    _local_checkpoint(comfy)
    fake_bridge(monkeypatch, comfy.tmp_path, {"submit": [result({"jobId": "job-1"})]})
    downloads = "checkpoints/m.safetensors=https://x.test/m.safetensors"
    body = {
        **JOB,
        "parameters": {"ModelDownloads": downloads},
        "excludedModels": ["m.safetensors"],
    }
    status, response = run(call(comfy, "/deadline/jobs", body))
    assert status == 202
    assert response["warnings"] == []
    assert not (Path(response["job_bundle"]) / "local_models").exists()


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


def test_jobs_accepts_a_non_string_attach_custom_nodes_value(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    _, workflow = _studio_pack(comfy, monkeypatch)
    fake_bridge(monkeypatch, comfy.tmp_path, {"submit": [result({"jobId": "job-1"})]})
    body = {**JOB, "workflow": workflow, "parameters": {"AttachCustomNodes": False}}
    status, response = run(call(comfy, "/deadline/jobs", body))
    assert status == 202
    assert not (Path(response["job_bundle"]) / "local_nodes").exists()


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
    assert "No download link for m.safetensors" in response["warnings"][0]
    assert "ModelDirectory" not in bridge_requests(requests)[0]["request"]["parameters"]


def _studio_pack(comfy, monkeypatch):
    """A custom node pack from a git checkout, and a workflow that runs one of its nodes."""
    custom = comfy.tmp_path / "custom_nodes"
    pack = write_pack(custom / "studio-nodes")
    fake_nodes(
        monkeypatch,
        {"StudioResize": "custom_nodes.studio-nodes.nodes"},
        {"studio-nodes": pack},
    )
    return pack, {**WORKFLOW, "5": {"class_type": "StudioResize", "inputs": {}}}


def test_plan_lists_local_custom_nodes_and_doesnt_warn_about_them(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    pack, workflow = _studio_pack(comfy, monkeypatch)
    status, body = run(call(comfy, "/deadline/plan", {"workflow": workflow, "name": "fox"}))
    assert status == 200
    assert body["localNodes"] == [{"name": "studio-nodes", "path": str(pack)}]
    assert body["nodeWarnings"] == []
    assert not any("Comfy Registry" in warning for warning in body["warnings"])
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


def test_plan_doesnt_attach_this_extension_for_its_value_nodes(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "")
    extension = write_pack(comfy.tmp_path / "custom_nodes" / "deadline-cloud-for-comfyui")
    fake_nodes(
        monkeypatch,
        {"DeadlineIntegerValues": "custom_nodes.deadline-cloud-for-comfyui.nodes"},
        {"deadline-cloud-for-comfyui": extension},
    )
    workflow = {
        **WORKFLOW,
        "5": {
            "class_type": "DeadlineIntegerValues",
            "inputs": {"values": "1-2", "preview_item": 1, "pair_with": ""},
        },
    }
    status, body = run(call(comfy, "/deadline/plan", {"workflow": workflow, "name": "fox"}))
    assert status == 200
    assert body["localNodes"] == []
    assert body["nodeWarnings"] == []


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
        {"StudioResize": "custom_nodes.studio-nodes.nodes", "Gone": "custom_nodes.gone"},
        {"studio-nodes": pack},
    )
    workflow = {**workflow, "6": {"class_type": "Gone", "inputs": {}}}
    requests = fake_bridge(monkeypatch, comfy.tmp_path, {"submit": [result({"jobId": "job-1"})]})
    body = {**JOB, "workflow": workflow, "parameters": {"AttachCustomNodes": "false"}}
    status, response = run(call(comfy, "/deadline/jobs", body))
    assert status == 202
    assert not any("Comfy Registry" in warning for warning in response["warnings"])
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


def test_jobs_keeps_the_templates_host_requirements_by_default(comfy, monkeypatch):
    requests = fake_tools(monkeypatch, comfy.tmp_path)
    status, response = run(call(comfy, "/deadline/jobs", {**JOB, "parameters": {}}))
    assert status == 202
    assert bridge_requests(requests)
    bundle = Path(response["job_bundle"])
    assert (bundle / "template.yaml").read_text() == submit.JOB_TEMPLATE.read_text()


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


def test_jobs_passes_require_paths_exist_to_the_library(comfy, monkeypatch):
    requests = fake_tools(monkeypatch, comfy.tmp_path)
    body = {**JOB, "settings": {"requirePathsExist": True}}
    status, _ = run(call(comfy, "/deadline/jobs", body))
    assert status == 202
    assert bridge_requests(requests)[0]["request"]["settings"]["requirePathsExist"] is True


def test_bundle_saves_the_values_without_submitting(comfy, monkeypatch):
    requests = fake_bridge(monkeypatch, comfy.tmp_path, {})
    _, workflow = _studio_pack(comfy, monkeypatch)
    body = {
        "workflow": workflow,
        "name": "fox",
        "parameters": {"ModelDownloads": ""},
        "hostRequirements": REQUIREMENTS,
    }
    status, response = run(call(comfy, "/deadline/bundle", body))
    assert status == 200
    assert not requests.exists()
    bundle = Path(response["job_bundle"])
    saved = yaml.safe_load((bundle / "parameter_values.yaml").read_text())["parameterValues"]
    values = {item["name"]: item["value"] for item in saved}
    assert values["WorkflowFile"] == str(bundle / "fox_api.json")
    assert values["CustomNodesDirectory"] == str(bundle / "local_nodes")
    assert (bundle / "local_nodes" / "studio-nodes" / "__init__.py").is_file()
    assert _template_steps(bundle)[0]["hostRequirements"] == REQUIREMENTS


def test_bundle_rejects_bad_host_requirements(comfy, monkeypatch):
    status, response = run(
        call(comfy, "/deadline/bundle", {"workflow": WORKFLOW, "hostRequirements": "linux"})
    )
    assert status == 400
    assert "hostRequirements" in response["error"]


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
    assert "No download link for m.safetensors" in body["warnings"][0]
    assert not (Path(body["job_bundle"]) / "local_models").exists()
    assert "ModelDirectory" not in sent_parameters(requests)


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


def test_download_reports_failures(comfy, monkeypatch):
    fake_cli(monkeypatch, comfy.tmp_path, "", exit_code=1, stderr="Credentials are expired")
    body = {"farmId": FARM, "queueId": QUEUE, "jobId": JOB_ID}
    run(call(comfy, "/deadline/jobs/download", body))
    assert comfy.server.events[-1] == (
        "deadline.download",
        {"jobId": JOB_ID, "status": "FAILED", "error": "Credentials are expired"},
    )


def test_download_only_takes_deadline_ids(comfy, monkeypatch):
    body = {"farmId": FARM, "queueId": QUEUE, "jobId": "--profile=evil"}
    status, _ = run(call(comfy, "/deadline/jobs/download", body))
    assert status == 400


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
