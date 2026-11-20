# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

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
    parse_cli_version,
    resolve_deadline_cli,
    resolve_job_template,
    safe_name,
    template_defines_parameter,
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
    decode_job_template(template=template)


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
    decode_job_template(template=template)


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
    decode_job_template(template=template)


def _tasks(bundle_dir: Path, tmp_path: Path, **values: str) -> list[dict[str, str]]:
    """Creates the bundle's job with openjd-model, as Deadline Cloud does, and lists its tasks."""
    template = decode_job_template(
        template=yaml.safe_load((bundle_dir / "template.yaml").read_text()),
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
    assert text.count("data: |\n") == 3
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
