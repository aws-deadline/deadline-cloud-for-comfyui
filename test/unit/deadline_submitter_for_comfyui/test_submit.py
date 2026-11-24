# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import copy
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from openjd.model import (
    StepParameterSpaceIterator,
    create_job,
    decode_job_template,
    preprocess_job_parameters,
)

from deadline.comfyui_submitter import submit
from deadline.comfyui_submitter.submit import (
    SubmitError,
    check_cli_version,
    create_job_bundle,
    get_safe_path,
    local_node_packs,
    parse_cli_version,
    plan_task_lists,
    resolve_deadline_cli,
    resolve_job_template,
    safe_name,
    template_defines_parameter,
    template_supports_task_lists,
    validate_api_workflow,
    workflow_models,
)

API_WORKFLOW = {
    "3": {
        "class_type": "KSampler",
        "inputs": {"seed": 156680208700286, "steps": 20, "model": ["4", 0]},
    },
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "v1-5.safetensors"}},
    "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "comfyui", "images": ["8", 0]}},
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for var in (
        submit.TEMPLATE_ENV_VAR,
        submit.CLI_ENV_VAR,
        submit.CONDA_CHANNELS_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    # Never read a developer's real config.
    monkeypatch.setenv(submit.CONFIG_ENV_VAR, str(tmp_path / "config" / submit.CONFIG_FILE_NAME))


def _write_config(tmp_path, config) -> Path:
    path = tmp_path / "config" / submit.CONFIG_FILE_NAME
    path.parent.mkdir(exist_ok=True)
    path.write_text(config if isinstance(config, str) else json.dumps(config))
    return path


def test_load_config(tmp_path):
    _write_config(tmp_path, {"conda_channels": "s3://b/Conda/Default conda-forge"})
    assert submit.load_config() == {"conda_channels": "s3://b/Conda/Default conda-forge"}


@pytest.mark.parametrize("content", ["{not json", "[]"])
def test_load_config_rejects_invalid(tmp_path, content):
    _write_config(tmp_path, content)
    with pytest.raises(SubmitError, match="submitter config"):
        submit.load_config()


def test_default_config_path_is_extension_root():
    root = Path(submit.__file__).resolve().parents[3]
    assert submit.DEFAULT_CONFIG_PATH == root / submit.CONFIG_FILE_NAME
    assert (root / "__init__.py").is_file()


def test_resolve_conda_channels_from_config():
    config = {"conda_channels": "  s3://b/Conda/Default\n conda-forge "}
    assert submit.resolve_conda_channels(config) == "s3://b/Conda/Default conda-forge"


def test_resolve_conda_channels_env_var_wins(monkeypatch):
    monkeypatch.setenv(submit.CONDA_CHANNELS_ENV_VAR, "s3://other conda-forge")
    assert submit.resolve_conda_channels({"conda_channels": "x"}) == "s3://other conda-forge"


def test_resolve_job_template_defaults_to_comfy_cli_job():
    assert resolve_job_template() == submit.JOB_TEMPLATE


def test_job_template_needs_the_comfy_package():
    assert "default: comfy\n" in resolve_job_template().read_text()


def test_resolve_job_template_uses_env_var(monkeypatch, tmp_path):
    template = tmp_path / "studio_comfyui.yaml"
    template.write_text("specificationVersion: 'jobtemplate-2023-09'\n")
    monkeypatch.setenv(submit.TEMPLATE_ENV_VAR, str(template))
    assert resolve_job_template() == template


@pytest.mark.parametrize("name", [None, "template.txt"])
def test_resolve_job_template_rejects_bad_env_var(monkeypatch, tmp_path, name):
    path = tmp_path / "missing.yaml"
    if name:
        path = tmp_path / name
        path.write_text("")
    monkeypatch.setenv(submit.TEMPLATE_ENV_VAR, str(path))
    with pytest.raises(SubmitError, match=submit.TEMPLATE_ENV_VAR):
        resolve_job_template()


def _make_executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits")
def test_resolve_deadline_cli_uses_env_var(monkeypatch, tmp_path):
    cli = _make_executable(tmp_path / "deadline")
    monkeypatch.setenv(submit.CLI_ENV_VAR, str(cli))
    assert resolve_deadline_cli() == str(cli)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits")
def test_resolve_deadline_cli_from_config_relative_to_config(monkeypatch, tmp_path):
    config_dir = _write_config(tmp_path, {}).parent
    (config_dir / ".deadline-cli" / "bin").mkdir(parents=True)
    cli = _make_executable(config_dir / ".deadline-cli" / "bin" / "deadline")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert resolve_deadline_cli({"deadline_cli": ".deadline-cli/bin/deadline"}) == str(cli)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits")
def test_resolve_deadline_cli_env_var_wins_over_config(monkeypatch, tmp_path):
    cli = _make_executable(tmp_path / "deadline")
    monkeypatch.setenv(submit.CLI_ENV_VAR, str(cli))
    assert resolve_deadline_cli({"deadline_cli": "/nope/deadline"}) == str(cli)


def test_resolve_deadline_cli_rejects_missing_config_path(tmp_path):
    with pytest.raises(SubmitError, match="deadline_cli in"):
        resolve_deadline_cli({"deadline_cli": "/nope/deadline"})


def test_resolve_deadline_cli_rejects_missing_env_var_path(monkeypatch, tmp_path):
    monkeypatch.setenv(submit.CLI_ENV_VAR, str(tmp_path / "missing"))
    with pytest.raises(SubmitError, match="isn't an executable"):
        resolve_deadline_cli()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits")
def test_resolve_deadline_cli_searches_path(monkeypatch, tmp_path):
    cli = _make_executable(tmp_path / "deadline")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert resolve_deadline_cli() == str(cli)


def test_resolve_deadline_cli_explains_how_to_install(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(SubmitError, match=r'pip install "deadline\[gui\]>=0.60.8"'):
        resolve_deadline_cli()


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("deadline, version 0.60.10\n", (0, 60, 10)),
        ("deadline, version 1.2.3.dev4+gabc", (1, 2, 3)),
        ("something else", None),
    ],
)
def test_parse_cli_version(output, expected):
    assert parse_cli_version(output) == expected


@pytest.mark.parametrize("version", ["0.60.8", "0.60.10", "0.61.0", "1.0.0"])
def test_check_cli_version_accepts_fixed_versions(version):
    check_cli_version("deadline", f"deadline, version {version}")


@pytest.mark.parametrize("version", ["0.60.3", "0.60.7", "0.59.2"])
def test_check_cli_version_rejects_versions_with_the_parameter_bug(version):
    with pytest.raises(SubmitError, match=r'pip install --upgrade "deadline\[gui\]>=0.60.8"'):
        check_cli_version("deadline", f"deadline, version {version}")


def test_validate_api_workflow_accepts_api_format():
    assert validate_api_workflow(API_WORKFLOW) is API_WORKFLOW


@pytest.mark.parametrize("workflow", [None, {}, [], "workflow"])
def test_validate_api_workflow_rejects_empty(workflow):
    with pytest.raises(SubmitError, match="empty"):
        validate_api_workflow(workflow)


def test_validate_api_workflow_rejects_ui_format():
    with pytest.raises(SubmitError, match="UI format"):
        validate_api_workflow({"nodes": [], "links": [], "version": 0.4})


def test_validate_api_workflow_rejects_node_without_class_type():
    with pytest.raises(SubmitError, match="node 7 because"):
        validate_api_workflow({**API_WORKFLOW, "7": {"inputs": {}}})


def test_validate_api_workflow_names_missing_node_type_and_pack():
    ui_workflow = {
        "nodes": [{"id": 7, "type": "ImageResizeKJv2", "properties": {"cnr_id": "comfyui-kjnodes"}}]
    }
    with pytest.raises(
        SubmitError, match=r"node 7 \(ImageResizeKJv2, from the comfyui-kjnodes node pack\)"
    ):
        validate_api_workflow({**API_WORKFLOW, "7": {"inputs": {}}}, ui_workflow)


def test_validate_api_workflow_finds_missing_node_in_subgraph():
    ui_workflow = {
        "nodes": [],
        "definitions": {"subgraphs": [{"nodes": [{"id": 137, "type": "MissingNode"}]}]},
    }
    with pytest.raises(SubmitError, match=r"node 12:137 \(MissingNode\)"):
        validate_api_workflow({**API_WORKFLOW, "12:137": {"inputs": {}}}, ui_workflow)


def test_workflow_models():
    workflow = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sdxl.safetensors"}},
        "2": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
        "3": {"class_type": "LoraLoader", "inputs": {"lora_name": "styles\\Ink.SAFETENSORS"}},
        "4": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": "flux/q4.gguf"}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cat", "clip": ["1", 1]}},
        "6": {"class_type": "LoadImage", "inputs": {"image": "photo.png"}},
    }
    assert workflow_models(workflow) == [
        "ae.safetensors",
        "flux/q4.gguf",
        "sdxl.safetensors",
        "styles/Ink.SAFETENSORS",
    ]


KJ_API = {
    "1": {"class_type": "KSampler", "inputs": {}},
    "2": {"class_type": "ImageResizeKJv2", "inputs": {}},
    "3": {"class_type": "VHS_VideoCombine", "inputs": {}},
}


def test_local_node_packs_attaches_every_custom_pack_the_workflow_uses(tmp_path):
    """Workers get custom nodes only through job attachments, registry installs included."""
    kj, vhs, unused = (tmp_path / name for name in ("comfyui-kjnodes", "vhs", "unused"))
    # ComfyUI's own nodes, such as KSampler, aren't loaded from custom_nodes, so aren't listed.
    loaded = {"ImageResizeKJv2": kj, "VHS_VideoCombine": vhs, "Unused": unused}
    assert local_node_packs(KJ_API, loaded) == {"comfyui-kjnodes": kj, "vhs": vhs}


def test_local_node_packs_lists_each_folder_once(tmp_path):
    pack = tmp_path / "studio-nodes"
    loaded = {"ImageResizeKJv2": pack, "VHS_VideoCombine": pack}
    assert local_node_packs(KJ_API, loaded) == {"studio-nodes": pack}


def test_local_node_packs_includes_single_file_nodes(tmp_path):
    script = tmp_path / "custom_nodes" / "single.py"
    assert local_node_packs(KJ_API, {"ImageResizeKJv2": script}) == {"single.py": script}


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("parameterDefinitions:\n- name: ModelDirectory\n  type: PATH\n", True),
        ("parameterDefinitions:\n  - name: 'ModelDirectory'\n", True),
        ('{"parameterDefinitions": [{"name": "ModelDirectory", "type": "PATH"}]}', True),
        ("parameterDefinitions:\n- name: ModelDir\n", False),
        ("description: set ModelDirectory to attach models\n", False),
    ],
)
def test_template_defines_parameter(tmp_path, content, expected):
    template = tmp_path / "template.yaml"
    template.write_text(content, encoding="utf-8")
    assert template_defines_parameter(template, "ModelDirectory") is expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("my workflow.json", "my_workflow"),
        ("../../etc/passwd", "etc_passwd"),
        ("Portrait v2", "Portrait_v2"),
        ("", "workflow"),
        ("***", "workflow"),
    ],
)
def test_safe_name(name, expected):
    assert safe_name(name) == expected


def test_get_safe_path_keeps_paths_inside_the_base(tmp_path):
    target = get_safe_path("20261019T123005123456Z_my_workflow", tmp_path)
    assert target == Path(os.path.realpath(tmp_path)) / "20261019T123005123456Z_my_workflow"


@pytest.mark.parametrize("user_input", ["../escape", "sub/../../escape", "/etc/passwd"])
def test_get_safe_path_rejects_escapes(tmp_path, user_input):
    with pytest.raises(ValueError, match="Path injection"):
        get_safe_path(user_input, tmp_path)


def test_get_safe_path_rejects_a_sibling_prefixed_like_the_base(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (tmp_path / "base_evil").mkdir()
    with pytest.raises(ValueError, match="Path injection"):
        get_safe_path("../base_evil", base)


def test_get_safe_path_rejects_a_symlink_that_escapes(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    base = tmp_path / "base"
    base.mkdir()
    (base / "link").symlink_to(outside)
    with pytest.raises(ValueError, match="Path injection"):
        get_safe_path("link", base)


def test_create_job_bundle(tmp_path):
    now = datetime(2026, 10, 19, 12, 30, 5, 123456, tzinfo=UTC)
    template = submit.JOB_TEMPLATE
    bundle_dir, workflow_path = create_job_bundle(
        template, API_WORKFLOW, "my workflow", tmp_path / "submissions", now=now
    )
    assert bundle_dir == tmp_path / "submissions" / "20261019T123005123456Z_my_workflow"
    assert (bundle_dir / "template.yaml").read_text() == template.read_text()
    assert workflow_path == bundle_dir / "my_workflow_api.json"
    assert json.loads(workflow_path.read_text()) == API_WORKFLOW


def test_create_job_bundle_keeps_json_template(tmp_path):
    template = tmp_path / "studio.json"
    template.write_text("{}")
    bundle_dir, _ = create_job_bundle(template, API_WORKFLOW, "w", tmp_path / "submissions")
    assert sorted(path.name for path in bundle_dir.iterdir()) == ["template.json", "w_api.json"]


@pytest.mark.parametrize(
    "requirements",
    [
        {"attributes": [{"name": "attr.worker.os.family", "anyOf": ["linux", "windows"]}]},
        {"attributes": [{"name": "attr.worker.cpu.arch", "anyOf": ["x86_64"]}]},
        {"amounts": [{"name": "amount.worker.vcpu", "min": 4, "max": 16}]},
        {"amounts": [{"name": "amount.worker.memory", "min": 8192}]},
        {"amounts": [{"name": "amount.worker.gpu.memory", "max": 24576}]},
        {"amounts": [{"name": "amount.worker.disk.scratch", "min": 100}]},
        {"amounts": [{"name": "amount.license.nuke", "min": 1}]},
        {"attributes": [{"name": "attr.software.config", "allOf": ["a_b", "c-d"]}]},
    ],
)
def test_host_requirements_openjd_accepts(requirements, tmp_path):
    assert submit.host_requirements(requirements) == requirements
    bundle_dir, _ = create_job_bundle(
        submit.JOB_TEMPLATE, {}, "w", tmp_path, requirements=requirements
    )
    template = yaml.safe_load((bundle_dir / "template.yaml").read_text())
    assert template["steps"][0]["hostRequirements"] == requirements
    decode_job_template(template=template, supported_extensions=["EXPR"])


@pytest.mark.parametrize(
    ("requirements", "message"),
    [
        ("linux", "must be an object"),
        ({"amounts": "gpu"}, "must be a list"),
        ({"amounts": [1]}, "must be an object"),
        ({"attributes": [1]}, "must be an object"),
        ({"amounts": [{"name": "amount.worker.gpu"}]}, "needs a min or a max"),
        ({"amounts": [{"name": "amount.worker.gpu", "min": -1}]}, "positive number"),
        ({"amounts": [{"name": "amount.worker.gpu", "min": True}]}, "positive number"),
        ({"amounts": [{"name": "amount.worker.gpu", "min": "1"}]}, "positive number"),
        ({"amounts": [{"name": "amount.worker.gpu", "min": float("nan")}]}, "positive number"),
        ({"amounts": [{"name": "amount.worker.gpu", "max": float("inf")}]}, "positive number"),
        ({"amounts": [{"name": "amount.worker.gpu", "min": 2, "max": 1}]}, "more than its max"),
        ({"amounts": [{"name": "amount.worker.other", "min": 1}]}, "doesn't start with"),
        ({"amounts": [{"name": "amount.job.x", "min": 1}]}, "doesn't start with"),
        ({"amounts": [{"name": "amount.", "min": 1}]}, "needs a name"),
        ({"amounts": [{"name": "license", "min": 1}]}, "needs a name"),
        ({"amounts": [{"name": "amount.a b", "min": 1}]}, "needs a name"),
        ({"attributes": [{"name": "attr.task.x", "anyOf": ["a"]}]}, "doesn't start with"),
        ({"attributes": [{"name": "attr.software"}]}, "either anyOf or allOf"),
        ({"attributes": [{"name": "attr.a", "anyOf": ["a"], "allOf": ["b"]}]}, "either anyOf"),
        ({"attributes": [{"name": "attr.a", "anyOf": []}]}, "needs values"),
        ({"attributes": [{"name": "attr.a", "anyOf": ["$(id)"]}]}, "needs values"),
    ],
)
def test_host_requirements_rejects(requirements, message):
    with pytest.raises(SubmitError, match=re.escape(message)):
        submit.host_requirements(requirements)


def test_host_requirements_none_keeps_the_templates_and_empty_removes_them():
    assert submit.host_requirements(None) is None
    assert submit.host_requirements({}) == {}
    assert submit.host_requirements({"attributes": [], "amounts": []}) == {}


def test_create_job_bundle_can_remove_host_requirements(tmp_path):
    bundle_dir, _ = create_job_bundle(submit.JOB_TEMPLATE, {}, "w", tmp_path, requirements={})
    template = yaml.safe_load((bundle_dir / "template.yaml").read_text())
    assert "hostRequirements" not in template["steps"][0]
    decode_job_template(template=template, supported_extensions=["EXPR"])


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ({"steps": [{"hostRequirements": {"amounts": []}}]}, {"amounts": []}),
        ({"steps": [{}]}, {}),
        ({"steps": []}, {}),
        ({}, {}),
    ],
)
def test_template_host_requirements(template, expected):
    assert submit.template_host_requirements(template) == expected


def test_job_description():
    assert submit.job_description(None) is None
    assert submit.job_description("") is None
    assert submit.job_description("Fox") == "Fox"
    with pytest.raises(SubmitError, match="must be a string"):
        submit.job_description(1)
    with pytest.raises(SubmitError, match="2048"):
        submit.job_description("x" * 2049)


def test_create_job_bundle_description_is_valid_at_the_limit(tmp_path):
    description = "x" * submit.MAX_JOB_DESCRIPTION
    bundle_dir, _ = create_job_bundle(
        submit.JOB_TEMPLATE, {}, "w", tmp_path, description=description
    )
    template = yaml.safe_load((bundle_dir / "template.yaml").read_text())
    assert template["description"] == description
    decode_job_template(template=template, supported_extensions=["EXPR"])


def _tasks(bundle_dir: Path, tmp_path: Path, **values: str) -> list[dict[str, str]]:
    """Creates the bundle's job with openjd-model, as Deadline Cloud does, and lists its tasks."""
    template = decode_job_template(
        template=yaml.safe_load((bundle_dir / "template.yaml").read_text()),
        supported_extensions=["EXPR"],
    )
    parameters = preprocess_job_parameters(
        job_template=template,
        job_parameter_values={"WorkflowFile": str(bundle_dir / "w_api.json"), **values},
        job_template_dir=bundle_dir,
        current_working_dir=tmp_path,
    )
    job = create_job(job_template=template, job_parameter_values=parameters)
    space = job.steps[0].parameterSpace
    if space is None:
        return [{}]
    return [
        {name: value.value for name, value in task.items()}
        for task in StepParameterSpaceIterator(space=space)
    ]


def test_base_template_runs_once(tmp_path):
    bundle_dir, _ = create_job_bundle(submit.JOB_TEMPLATE, API_WORKFLOW, "w", tmp_path)
    assert _tasks(bundle_dir, tmp_path) == [{}]


def test_create_job_bundle_writes_scripts_as_literal_blocks(tmp_path):
    bundle_dir, _ = create_job_bundle(
        submit.JOB_TEMPLATE, API_WORKFLOW, "w", tmp_path, description="Rewritten"
    )
    text = (bundle_dir / "template.yaml").read_text()
    # Enter, CheckRequirements, Exit and the task's Run script.
    assert text.count("data: |\n") == 4
    assert "\\nimport json" not in text


@pytest.mark.parametrize("content", ["key: [unclosed", "just text"])
def test_create_job_bundle_rejects_unreadable_templates(tmp_path, content):
    template = tmp_path / "studio.yaml"
    template.write_text(content)
    with pytest.raises(SubmitError, match="studio.yaml"):
        create_job_bundle(template, API_WORKFLOW, "w", tmp_path / "s", description="x")
    assert not (tmp_path / "s").exists()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("fox.png", "fox.png"),
        ("fox.png [input]", "fox.png"),
        ("cats/a.png", "cats/a.png"),
        ("fox.png [output]", None),
        ("../secrets.txt", None),
        ("/etc/passwd", None),
        (".", None),
        ("", None),
        (["4", 0], None),
    ],
)
def test_input_path(tmp_path, name, expected):
    path = submit.input_path(tmp_path, name)
    assert path == (tmp_path / expected if expected else None)


def _input_dir(tmp_path: Path) -> Path:
    input_dir = tmp_path / "input"
    (input_dir / "cats").mkdir(parents=True)
    for name in ("fox.png", "unused.png", "cats/a.png", "cats/b.jpg", "cats/a.txt"):
        (input_dir / name).write_text(name)
    return input_dir


def test_input_folders(tmp_path):
    input_dir = _input_dir(tmp_path)
    (tmp_path / "outside").mkdir()
    workflow = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "fox.png"}},
        "2": {"class_type": "LoadImageTextDataSetFromFolder", "inputs": {"folder": "cats"}},
        "3": {"class_type": "LoadImageDataSetFromFolder", "inputs": {"folder": "cats [input]"}},
        # Only folder loaders attach folders, and only folders in the input directory.
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "cats"}},
        "5": {"class_type": "LoadImageDataSetFromFolder", "inputs": {"folder": "../outside"}},
        "6": {"class_type": "LoadImageDataSetFromFolder", "inputs": {"folder": "missing"}},
    }
    assert submit.input_folders(workflow, input_dir) == [input_dir / "cats"]


@pytest.mark.parametrize(
    ("platform", "kind", "program", "expected"),
    [
        ("darwin", "folder", "osascript", "choose folder"),
        ("darwin", "files", "osascript", "multiple selections allowed"),
        ("win32", "folder", "powershell", "FolderBrowserDialog"),
        ("win32", "files", "powershell", "Multiselect=$true"),
    ],
)
def test_picker_command(platform, kind, program, expected):
    command = submit.picker_command(kind, platform) or []
    assert command[0] == program
    assert expected in " ".join(command)


@pytest.mark.parametrize("kind", ["folder", "files"])
def test_windows_picker_writes_utf8(kind):
    """The server decodes the chosen paths as UTF-8, so PowerShell can't use the console code page."""
    script = (submit.picker_command(kind, "win32") or [])[-1]
    assert script.startswith("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;")


def test_picker_command_on_linux(monkeypatch):
    monkeypatch.setattr(submit.shutil, "which", lambda name: "/usr/bin/zenity")
    assert "--directory" in (submit.picker_command("folder", "linux") or [])
    assert "--multiple" in (submit.picker_command("files", "linux") or [])
    monkeypatch.setattr(submit.shutil, "which", lambda name: None)
    assert submit.picker_command("files", "linux") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), ("true", True), ("Yes", True), ("ON", True), ("1", True), (1, True)]
    + [(False, False), ("FALSE", False), ("no", False), ("off", False), ("0", False), (0, False)]
    + [("", None), ("garbage", None), (2, None), ("1.0", None), (None, None)]
    # OpenJD refuses spaces around a BOOL.
    + [(" true", None), ("false ", None), (" ON ", None)],
)
def test_parse_bool_reads_bool_parameters_as_openjd_does(value, expected):
    assert submit.parse_bool(value) is expected


TYPED_PARAMETERS: list[dict[str, Any]] = [
    {"name": "Switch", "type": "BOOL"},
    {"name": "Steps", "type": "INT", "minValue": 1, "maxValue": 50},
    {"name": "Scale", "type": "FLOAT", "minValue": "0.5", "allowedValues": [0.5, 1, 2.5]},
    {"name": "Mode", "type": "STRING", "allowedValues": ["fast", "best"]},
    {"name": "Tag", "type": "STRING", "minLength": 2, "maxLength": 4},
    {"name": "Out", "type": "PATH"},
    {
        "name": "Folder",
        "type": "PATH",
        "minLength": 3,
        "maxLength": 8,
        "allowedValues": ["/a/b", "/shared"],
    },
]


@pytest.mark.parametrize(
    "parameters",
    [
        {"Switch": "off", "Steps": "20", "Scale": "2.5", "Mode": "best", "Tag": "abc"},
        {"Steps": 50, "Scale": 1, "Out": "", "Folder": "/shared"},
        # OpenJD reads spaces around an INT.
        {"Steps": " 20 "},
        # Parameters the template doesn't define, such as the queue's, are the library's to check.
        {"CondaChannels": "deadline-cloud"},
    ],
)
def test_check_parameter_values_accepts_what_openjd_does(parameters):
    submit.check_parameter_values(TYPED_PARAMETERS, parameters)


@pytest.mark.parametrize(
    ("parameters", "error"),
    [
        ({"Switch": "maybe"}, "Switch must be true or false, not 'maybe'."),
        ({"Steps": "1.5"}, "Steps must be a whole number, not '1.5'."),
        ({"Steps": True}, "Steps must be a whole number, not True."),
        ({"Steps": "0"}, "Steps must be at least 1, not '0'."),
        ({"Steps": 51}, "Steps must be at most 50, not 51."),
        ({"Scale": "nan"}, "Scale must be a number, not 'nan'."),
        ({"Scale": "0.25"}, "Scale must be at least 0.5, not '0.25'."),
        ({"Scale": "2"}, "Scale must be one of 0.5, 1, 2.5, not '2'."),
        ({"Mode": "slow"}, "Mode must be one of fast, best, not 'slow'."),
        ({"Tag": "a"}, "Tag must be at least 2 characters long."),
        ({"Tag": "abcde"}, "Tag must be at most 4 characters long."),
        ({"Switch": " true"}, "Switch must be true or false, not ' true'."),
        ({"Steps": "1" * 4301}, "Steps must be a whole number, not '" + "1" * 4301 + "'."),
        ({"Folder": "/a"}, "Folder must be at least 3 characters long."),
        ({"Folder": "/a/b/c/d/e"}, "Folder must be at most 8 characters long."),
        ({"Folder": "/elsewhere"[:8]}, "Folder must be one of /a/b, /shared, not '/elsewhe'."),
    ],
)
def test_check_parameter_values_says_what_is_wrong(parameters, error):
    with pytest.raises(SubmitError) as raised:
        submit.check_parameter_values(TYPED_PARAMETERS, parameters)
    assert str(raised.value) == error


SOURCES = [
    submit.ItemSource("List1", "Create List #1", 4),
    submit.ItemSource(
        "Folder6", "Folder cats (#6)", 3, "folder", values=["a.png", "b.png", "c.png"]
    ),
]


def _create_list(*items):
    return {
        "class_type": "CreateList",
        "inputs": {f"inputs.input{i}": [n, 0] for i, n in enumerate(items)},
    }


def _marked(plan, node_id):
    return plan.workflow[node_id].get("_meta", {})


def _names(plan):
    return [(source.name, source.label, source.count) for source in plan.sources]


def test_plan_task_lists_for_create_list():
    workflow = {
        "1": _create_list("2", "3", "4", "5"),
        "6": {"class_type": "SaveImage", "inputs": {}},
    }
    plan = plan_task_lists(workflow)
    assert _names(plan) == [("List1", "Create List #1", 4)]
    assert _marked(plan, "1") == {submit.ITEM_META: "List1"}
    assert "_meta" not in plan.workflow["6"]
    assert plan.warnings == []
    # The workflow passed in is unchanged.
    assert "_meta" not in workflow["1"]


def test_plan_task_lists_without_the_inputs_prefix():
    workflow = {
        "1": {"class_type": "CreateList", "inputs": {"input0": ["2", 0], "input1": ["3", 0]}}
    }
    assert _names(plan_task_lists(workflow)) == [("List1", "Create List #1", 2)]


@pytest.mark.parametrize(
    "workflow",
    [
        {"1": {"class_type": "SaveImage", "inputs": {}}},
        {"1": _create_list("2")},
    ],
)
def test_plan_task_lists_without_lists(workflow):
    plan = plan_task_lists(workflow)
    assert plan.sources == []
    assert plan.warnings == []
    assert plan.workflow == workflow


def test_plan_task_lists_keeps_lists_of_different_lengths_separate():
    plan = plan_task_lists({"1": _create_list("2", "3"), "4": _create_list("5", "6", "7")})
    assert _names(plan) == [("List1", "Create List #1", 2), ("List4", "Create List #4", 3)]
    assert _marked(plan, "4") == {submit.ITEM_META: "List4"}
    assert plan.warnings == []


def test_plan_task_lists_names_subgraph_nodes():
    plan = plan_task_lists({"57:27": _create_list("2", "3")})
    assert _names(plan) == [("List57_27", "Create List #57:27", 2)]


def test_plan_task_lists_leaves_lists_past_the_most_a_step_takes_whole():
    workflow = {str(n): _create_list("a", "b") for n in range(1, submit.MAX_ITEM_SOURCES + 3)}
    plan = plan_task_lists(workflow)
    assert len(plan.active) == submit.MAX_ITEM_SOURCES
    assert "_meta" not in plan.workflow["17"]
    assert plan.warnings == [
        (
            f"Deadline Cloud splits up to {submit.MAX_ITEM_SOURCES} fields into tasks, so "
            "these run whole in each task: Create List #17, Create List #18."
        )
    ]


def test_bundled_template_splits_lists():
    assert template_supports_task_lists(submit.JOB_TEMPLATE)


def test_template_supports_task_lists_missing_file(tmp_path):
    assert not template_supports_task_lists(tmp_path / "missing.yaml")


def test_add_task_lists():
    base = yaml.safe_load(submit.JOB_TEMPLATE.read_text())
    template = submit.add_task_lists(base, SOURCES)
    names = [parameter["name"] for parameter in template["parameterDefinitions"]]
    # After the workflow parameters.
    at = names.index("OutputDir") + 1
    assert names[at : at + 2] == ["List1Items", "Folder6Items"]
    folder = template["parameterDefinitions"][at + 1]
    assert folder["default"] == "1-3"
    assert folder["userInterface"] == {
        "control": "LINE_EDIT",
        "label": "Folder cats (#6)",
        "groupLabel": "Tasks",
    }
    assert folder["description"].endswith("Items: 1: a.png; 2: b.png; 3: c.png.")
    step = template["steps"][0]
    assert "combination" not in step["parameterSpace"]
    assert step["parameterSpace"]["taskParameterDefinitions"] == [
        {"name": "List1", "type": "INT", "range": "{{Param.List1Items}}"},
        {"name": "Folder6", "type": "INT", "range": "{{Param.Folder6Items}}"},
    ]
    # The task script gets the task's items as arguments, and its code is unchanged.
    assert step["script"]["actions"]["onRun"]["args"] == [
        "{{Task.File.Run}}",
        "List1={{Task.Param.List1}}",
        "Folder6={{Task.Param.Folder6}}",
    ]
    assert step["script"]["embeddedFiles"] == base["steps"][0]["script"]["embeddedFiles"]
    # The template passed in is unchanged.
    assert base == yaml.safe_load(submit.JOB_TEMPLATE.read_text())


def test_add_task_lists_leaves_a_studio_templates_other_steps_alone():
    """A studio step with its own task parameters, and the ITEMS line, keeps its parameter space."""
    base = yaml.safe_load(submit.JOB_TEMPLATE.read_text())
    frames = copy.deepcopy(base["steps"][0])
    frames["name"] = "StudioFrames"
    frames["parameterSpace"] = {
        "taskParameterDefinitions": [{"name": "Frame", "type": "INT", "range": "1-3"}],
        "combination": "Frame",
    }
    base["steps"].append(frames)
    template = submit.add_task_lists(base, SOURCES)
    assert template["steps"][1] == frames
    decode_job_template(template=template, supported_extensions=["EXPR"])


def test_bundle_scripts_have_no_parameter_values_written_into_them(tmp_path):
    """The scripts read job and task parameters from environment variables and arguments."""
    bundle_dir, _ = create_job_bundle(
        submit.JOB_TEMPLATE, API_WORKFLOW, "w", tmp_path, sources=SOURCES
    )
    template = yaml.safe_load((bundle_dir / "template.yaml").read_text())
    scripts = [
        file["data"]
        for container in [*template["jobEnvironments"], *template["steps"]]
        for file in container["script"]["embeddedFiles"]
    ]
    assert len(scripts) == 4
    assert not [script for script in scripts if "{{Param." in script or "{{Task.Param." in script]


def test_add_task_lists_shortens_long_item_lists():
    source = submit.ItemSource(
        "Folder5", "Folder cats (#5)", 12, "folder", [str(n) * 70 for n in range(10)]
    )
    parameter = submit.add_task_lists({"steps": []}, [source])["parameterDefinitions"][0]
    assert f"1: {'0' * 60}...; " in parameter["description"]
    assert parameter["description"].endswith("10: " + "9" * 60 + "...; and 2 more.")


def test_add_task_lists_leaves_out_control_characters_openjd_refuses():
    """A folder name or a list item can hold one, which would make the template invalid."""
    source = submit.ItemSource(
        "Folder5", "Folder shots\x07a (#5)", 2, "folder", ["a\x01b.png", "line\nbreak\ttab\x9f"]
    )
    template = {
        "specificationVersion": "jobtemplate-2023-09",
        "name": "fox",
        "steps": [{"name": "Run", "script": {"actions": {"onRun": {"command": "true"}}}}],
    }
    parameter = submit.add_task_lists(template, [source])["parameterDefinitions"][0]
    assert parameter["userInterface"]["label"] == "Folder shots a (#5)"
    assert parameter["description"].endswith("Items: 1: a b.png; 2: line\nbreak\ttab .")
    decode_job_template(template=submit.add_task_lists(template, [source]))


def test_add_task_lists_lists_ten_items_in_full():
    source = submit.ItemSource(
        "Folder5", "Folder cats (#5)", 10, "folder", [str(n) for n in range(10)]
    )
    parameter = submit.add_task_lists({"steps": []}, [source])["parameterDefinitions"][0]
    assert parameter["description"].endswith(
        "Items: 1: 0; 2: 1; 3: 2; 4: 3; 5: 4; 6: 5; 7: 6; 8: 7; 9: 8; 10: 9."
    )


def test_generated_template_is_valid_and_crosses_every_source(tmp_path):
    bundle_dir, _ = create_job_bundle(
        submit.JOB_TEMPLATE, API_WORKFLOW, "w", tmp_path, sources=SOURCES
    )
    tasks = _tasks(bundle_dir, tmp_path)
    # 4 list items and 3 images.
    assert len(tasks) == 12
    assert tasks[-1] == {"List1": "4", "Folder6": "3"}


def test_create_job_bundle_adds_task_lists_to_json_templates(tmp_path):
    template = tmp_path / "studio.json"
    template.write_text(json.dumps(yaml.safe_load(submit.JOB_TEMPLATE.read_text())))
    bundle_dir, _ = create_job_bundle(template, API_WORKFLOW, "w", tmp_path / "s", sources=SOURCES)
    written = json.loads((bundle_dir / "template.json").read_text())
    assert written == submit.add_task_lists(json.loads(template.read_text()), SOURCES)


def test_image_files(tmp_path):
    for name in ("b.PNG", "a.webp", "c.txt"):
        (tmp_path / name).write_text("x")
    (tmp_path / "d.jpg").mkdir()
    assert submit.image_files(tmp_path) == ["a.webp", "b.PNG"]
    assert submit.image_files(tmp_path / "missing") == []


def test_plan_task_lists_for_folder_loaders(tmp_path):
    input_dir = _input_dir(tmp_path)
    workflow = {"3": {"class_type": "LoadImageDataSetFromFolder", "inputs": {"folder": "cats"}}}
    plan = plan_task_lists(workflow, input_dir)
    assert _names(plan) == [("Folder3", "Folder cats (#3)", 2)]
    assert plan.sources[0].values == ["a.png", "b.jpg"]
    assert _marked(plan, "3") == {
        submit.FILES_META: ["cats/a.png", "cats/b.jpg"],
        submit.ITEM_META: "Folder3",
    }
    # Without the input directory, such as in a test, folders aren't split.
    assert plan_task_lists(workflow).sources == []


def test_plan_task_lists_drops_the_workflows_own_submitter_keys(tmp_path):
    """A shared workflow can't preset the keys the task script follows on a node."""
    input_dir = _input_dir(tmp_path)
    preset = {submit.ITEM_META: "List1", submit.FILES_META: ["../../secret.png"] * 2}
    workflow = {
        "1": _create_list("3", "4"),
        # A folder of one image isn't split. The submitter writes nothing on it.
        "5": {
            "class_type": "LoadImageDataSetFromFolder",
            "inputs": {"folder": "one"},
            "_meta": {**preset, "title": "Plates"},
        },
        "6": {"class_type": "SaveImage", "inputs": {}, "_meta": dict(preset)},
    }
    (input_dir / "one").mkdir()
    (input_dir / "one" / "a.png").write_bytes(b"x")
    plan = plan_task_lists(workflow, input_dir)
    assert [source.name for source in plan.active] == ["List1"]
    assert plan.workflow["5"]["_meta"] == {"title": "Plates"}
    assert "_meta" not in plan.workflow["6"]
    # The workflow passed in is unchanged.
    assert workflow["6"]["_meta"] == preset


def _titles(workflow):
    return [source.title for source in plan_task_lists(workflow).sources]


def test_lists_that_look_like_links_arent_connections():
    workflow = {
        "1": _create_list("3", "4"),
        "2": {"class_type": "Other", "inputs": {"choices": ["1", "2"]}},
    }
    assert _titles(workflow) == ["Create List #1"]


def test_folders_keep_their_label_as_title(tmp_path):
    (tmp_path / "cats").mkdir()
    for name in ("a.png", "b.png"):
        (tmp_path / "cats" / name).write_text("x")
    workflow = {"3": {"class_type": "LoadImageDataSetFromFolder", "inputs": {"folder": "cats"}}}
    sources = plan_task_lists(workflow, tmp_path).sources
    assert [source.title for source in sources] == ["Folder cats (#3)"]


def test_lists_are_named_by_the_title_an_artist_gave_them(tmp_path):
    """The title names the list's column in Deadline Cloud monitor and its output files."""
    (tmp_path / "cats").mkdir()
    for name in ("a.png", "b.png"):
        (tmp_path / "cats" / name).write_text("x")
    workflow = {
        "1": {**_create_list("2", "3"), "_meta": {"title": "Subject"}},
        "4": {**_create_list("5", "6"), "_meta": {"title": "Subject"}},
        "7": {**_create_list("8", "9"), "_meta": {"title": "Create List"}},
        "10": {
            "class_type": "LoadImageDataSetFromFolder",
            "inputs": {"folder": "cats"},
            "_meta": {"title": "Cat photos"},
        },
        "11": {**_create_list("12", "13"), "_meta": {"title": "2nd pass!"}},
    }
    plan = plan_task_lists(workflow, tmp_path)
    assert [(source.name, source.title) for source in plan.sources] == [
        ("Subject", "Subject"),
        # Two lists with the same title keep it, with their node IDs.
        ("Subject_4", "Subject"),
        # ComfyUI's own title isn't a name the artist gave it.
        ("List7", "Create List #7"),
        ("Cat_photos", "Cat photos"),
        ("_2nd_pass", "2nd pass!"),
    ]
    bundle_dir, _ = create_job_bundle(
        submit.JOB_TEMPLATE, API_WORKFLOW, "w", tmp_path / "bundles", sources=plan.sources
    )
    # OpenJD accepts the names as task parameters.
    assert _tasks(bundle_dir, tmp_path)[0] == {
        "Subject": "1",
        "Subject_4": "1",
        "List7": "1",
        "Cat_photos": "1",
        "_2nd_pass": "1",
    }


def test_json_templates_run_task_lists_too(tmp_path):
    """A JSON template keeps its script on one line, with the ITEMS line after an escaped newline."""
    template = yaml.safe_load(submit.JOB_TEMPLATE.read_text())
    json_template = tmp_path / "studio.json"
    json_template.write_text(json.dumps(template))
    assert template_supports_task_lists(json_template)
    for file in template["steps"][0]["script"]["embeddedFiles"]:
        file["data"] = file["data"].replace("ITEMS = {}", "ITEMS = load_items()")
    json_template.write_text(json.dumps(template))
    assert not template_supports_task_lists(json_template)


def test_create_list_items_are_named_for_the_panel():
    workflow = {
        "1": {
            "class_type": "CreateList",
            "inputs": {
                "inputs.input10": ["6", 0],
                "inputs.input2": ["5", 0],
                "inputs.input0": "a red fox",
                "inputs.input1": 7,
            },
        },
        "5": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "a white fox"}},
        "6": {"class_type": "LoadImage", "inputs": {"image": "fox.png"}, "_meta": {"title": "Fox"}},
    }
    [source] = plan_task_lists(workflow).sources
    # In the order the task script numbers them, input10 after input2.
    assert source.values == ["a red fox", "7", "a white fox", "Fox #6"]


def test_lists_with_a_null_meta_are_planned():
    workflow = {"1": {**_create_list("2", "3"), "_meta": None}}
    [source] = plan_task_lists(workflow).sources
    assert source.name == "List1"


def test_renamed_lists_get_unique_names_even_when_a_node_id_is_taken():
    workflow = {
        "3": {**_create_list("a", "b"), "_meta": {"title": "A_7"}},
        "5": {**_create_list("a", "b"), "_meta": {"title": "A"}},
        "7": {**_create_list("a", "b"), "_meta": {"title": "A"}},
    }
    assert [source.name for source in plan_task_lists(workflow).sources] == ["A_7", "A", "A_7_2"]


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("0", True),
        ("1-3", True),
        (" 3, 1 ", True),
        ("1-3:2", True),
        ("4", False),
        ("1-4", False),
        ("0,1", False),
        ("1-3,2", False),
        ("1,,2", False),
        ("1 - 3", True),
        ("x", False),
        ("", False),
        ("1 0", False),
        ("\u0661", False),
    ],
)
def test_check_item_range(value, valid):
    """The submitter checks a list's range again, with the panel's rules."""
    source = submit.ItemSource("List5", "Create List #5", 3)
    if valid:
        submit.check_item_range(source, value)
    else:
        with pytest.raises(SubmitError, match="Create List #5"):
            submit.check_item_range(source, value)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("4", "Create List #5 has 3 items, so it can't run '4'. Read the workflow again"),
        ("1-3,2", "Create List #5: '1-3,2' lists an item more than once."),
        ("x", "Create List #5: use item numbers such as 1-3 or 2,3, not 'x'."),
        ("9" * 5000, "Create List #5: use item numbers such as 1-3 or 2,3"),
    ],
)
def test_check_item_range_says_what_is_wrong(value, message):
    with pytest.raises(SubmitError, match=re.escape(message)):
        submit.check_item_range(submit.ItemSource("List5", "Create List #5", 3), value)


def test_list_names_fit_openjd_even_with_a_node_id_added():
    title = "A" * 60
    workflow = {
        "123:45:67": {**_create_list("a", "b"), "_meta": {"title": title}},
        "123:45:68": {**_create_list("a", "b"), "_meta": {"title": title}},
    }
    names = [source.parameter for source in plan_task_lists(workflow).sources]
    assert names == ["A" * 48 + "Items", "A" * 48 + "_123_45_68Items"]
    assert all(len(name) <= 64 for name in names)


def test_list_names_fit_openjd_for_very_long_node_ids():
    node = ":".join(str(1000 + i) for i in range(20))
    workflow = {
        node: _create_list("a", "b"),
        node + "1": {**_create_list("a", "b"), "_meta": {"title": "List" + node}},
    }
    names = [source.parameter for source in plan_task_lists(workflow).sources]
    assert len(set(names)) == 2
    assert all(len(name) <= 64 for name in names)


def _studio_template(
    tmp_path: Path, task_parameters: bool = False, job_parameter: str = ""
) -> Path:
    """The bundled template, with a step that has its own task parameter or an extra job one."""
    template = yaml.safe_load(submit.JOB_TEMPLATE.read_text())
    if task_parameters:
        template["steps"][0]["parameterSpace"] = {
            "taskParameterDefinitions": [{"name": "Frame", "type": "INT", "range": "1-3"}],
            "combination": "Frame",
        }
    if job_parameter:
        template["parameterDefinitions"].append(
            {"name": job_parameter, "type": "STRING", "default": "x"}
        )
    path = tmp_path / "studio.yaml"
    path.write_text(yaml.safe_dump(template, sort_keys=False))
    return path


def test_templates_whose_step_has_task_parameters_dont_split_lists(tmp_path):
    """Their names, the 16-parameter limit and their combination would all have to fit around."""
    studio = _studio_template(tmp_path, task_parameters=True)
    assert not template_supports_task_lists(studio)
    assert submit.template_has_own_task_parameters(studio)
    assert not submit.template_has_own_task_parameters(submit.JOB_TEMPLATE)


def test_list_job_parameters_steer_clear_of_the_templates_names(tmp_path):
    """A studio template's own SubjectItems job parameter isn't repeated by a list called Subject."""
    workflow = {"1": {**_create_list("a", "b"), "_meta": {"title": "Subject"}}}
    plan = plan_task_lists(workflow, reserved={"SubjectItems", "WorkflowFile"})
    [source] = plan.active
    assert (source.name, source.parameter) == ("Subject_1", "Subject_1Items")
    studio = _studio_template(tmp_path, job_parameter="SubjectItems")
    template = submit.read_job_template(studio, plan.active)
    decode_job_template(template=template, supported_extensions=["EXPR"])


ITEM_RANGES = json.loads(
    (Path(__file__).parent / "panel" / "item_ranges.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", ITEM_RANGES, ids=lambda case: repr(case["value"]))
def test_check_item_range_takes_the_same_ranges_as_the_panel(case):
    """item_ranges.json is shared with test_panel.py, so the submitter and the panel agree."""
    source = submit.ItemSource("List5", "Create List #5", case["items"])
    if case["count"] is None:
        with pytest.raises(SubmitError):
            submit.check_item_range(source, case["value"])
    else:
        submit.check_item_range(source, case["value"])
