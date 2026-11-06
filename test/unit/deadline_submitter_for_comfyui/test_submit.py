# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import json
import os
import re
import stat
from datetime import datetime, timezone
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
    custom_nodes,
    expand_dynamic_prompt,
    get_safe_path,
    local_node_packs,
    model_downloads,
    models_with_mismatched_links,
    models_without_links,
    parse_cli_version,
    plan_work_items,
    read_node_pack,
    resolve_deadline_cli,
    resolve_job_template,
    safe_name,
    string_values,
    template_defines_parameter,
    template_supports_work_items,
    validate_api_workflow,
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
        submit.ALLOW_REMOTE_ENV_VAR,
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


HF = "https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files"
SHA = "E9476A13728CD75D8279F6EC8BAD753A66A1957CA375A1464DC63B37DB6E3916"


def _loader(class_type, models):
    return {"type": class_type, "properties": {"models": models}}


def _model(directory, name, url, **extra):
    return {"directory": directory, "name": name, "url": url, **extra}


Z_IMAGE_API = {
    "62": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_3_4b.safetensors"}},
    "63": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
    "66": {"class_type": "UNETLoader", "inputs": {"unet_name": "z_image_turbo_bf16.safetensors"}},
}
Z_IMAGE_UI: dict[str, Any] = {
    "nodes": [
        _loader(
            "CLIPLoader",
            [
                _model(
                    "text_encoders",
                    "qwen_3_4b.safetensors",
                    f"{HF}/text_encoders/qwen_3_4b.safetensors",
                )
            ],
        ),
        _loader("VAELoader", [_model("vae", "ae.safetensors", f"{HF}/vae/ae.safetensors")]),
    ],
    # Templates often put loaders inside a subgraph.
    "definitions": {
        "subgraphs": [
            {
                "nodes": [
                    _loader(
                        "UNETLoader",
                        [
                            _model(
                                "diffusion_models",
                                "z_image_turbo_bf16.safetensors",
                                f"{HF}/diffusion_models/z_image_turbo_bf16.safetensors?download=true",
                            )
                        ],
                    )
                ]
            }
        ]
    },
}


def test_model_downloads_from_template_links():
    assert model_downloads(Z_IMAGE_UI, Z_IMAGE_API) == " ".join(
        [
            f"text_encoders/qwen_3_4b.safetensors={HF}/text_encoders/qwen_3_4b.safetensors",
            f"vae/ae.safetensors={HF}/vae/ae.safetensors",
            f"diffusion_models/z_image_turbo_bf16.safetensors={HF}/diffusion_models/z_image_turbo_bf16.safetensors",
        ]
    )


def test_model_downloads_adds_sha256():
    ui = {
        "nodes": [
            _loader(
                "CheckpointLoaderSimple",
                [
                    _model(
                        "checkpoints",
                        "sd15.safetensors",
                        "https://x.test/sd15.safetensors",
                        hash=SHA,
                        hash_type="SHA256",
                    )
                ],
            )
        ]
    }
    api = {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd15.safetensors"}}
    }
    assert (
        model_downloads(ui, api)
        == f"checkpoints/sd15.safetensors=https://x.test/sd15.safetensors#sha256={SHA.lower()}"
    )


def test_model_downloads_matches_filename_in_subdir():
    model = _model("vae", "subdir/ae.safetensors", "https://x.test/split_files/vae/ae.safetensors")
    api = {"1": {"class_type": "VAELoader", "inputs": {"vae_name": "subdir/ae.safetensors"}}}
    assert (
        model_downloads({"nodes": [_loader("VAELoader", [model])]}, api)
        == "vae/subdir/ae.safetensors=https://x.test/split_files/vae/ae.safetensors"
    )


def test_model_downloads_skips_models_the_workflow_doesnt_load():
    ui = {
        "nodes": [
            *Z_IMAGE_UI["nodes"],
            _loader(
                "LoraLoader",
                [_model("loras", "bypassed.safetensors", "https://x.test/bypassed.safetensors")],
            ),
        ]
    }
    assert "bypassed" not in (model_downloads(ui, Z_IMAGE_API) or "")


def test_model_downloads_lists_each_model_once():
    ui = {"nodes": Z_IMAGE_UI["nodes"] * 2}
    assert (model_downloads(ui, Z_IMAGE_API) or "").count("vae/ae.safetensors=") == 1


@pytest.mark.parametrize(
    "model",
    [
        _model("vae", "ae.safetensors", "https://x.test/ae.safetensors?token=$(id)"),
        _model("vae", "ae.safetensors", "http://x.test/ae.safetensors"),
        _model("vae", "ae.safetensors", "https://x.test/a e.safetensors"),
        _model("../vae", "ae.safetensors", "https://x.test/ae.safetensors"),
        _model("vae", "ae.safetensors;id", "https://x.test/ae.safetensors"),
        {"directory": "vae", "name": "ae.safetensors"},
    ],
)
def test_model_downloads_skips_entries_unsafe_for_the_job_script(model):
    api = {"1": {"class_type": "VAELoader", "inputs": {"vae_name": model["name"]}}}
    assert model_downloads({"nodes": [_loader("VAELoader", [model])]}, api) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://civitai.com/api/download/models/128713",
        "https://civitai.com/api/download/models/128713?type=Model&format=SafeTensor&fp=fp16",
    ],
)
def test_model_downloads_takes_civitai_urls_without_the_query(url):
    model = _model("checkpoints", "dreamshaper_8.safetensors", url)
    api = {"1": {"class_type": "L", "inputs": {"ckpt_name": "dreamshaper_8.safetensors"}}}
    ui = {"nodes": [_loader("L", [model])]}
    assert model_downloads(ui, api) == (
        "checkpoints/dreamshaper_8.safetensors=https://civitai.com/api/download/models/128713"
    )
    assert models_with_mismatched_links(ui, api) == []


def test_model_downloads_adds_sha256_to_civitai_urls():
    model = _model(
        "loras",
        "style.safetensors",
        "https://civitai.com/api/download/models/32988?type=Model",
        hash=SHA,
        hash_type="SHA256",
    )
    api = {"1": {"class_type": "L", "inputs": {"lora_name": "style.safetensors"}}}
    assert model_downloads({"nodes": [_loader("L", [model])]}, api) == (
        "loras/style.safetensors=https://civitai.com/api/download/models/32988"
        f"#sha256={SHA.lower()}"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://civitai.com/models/4384?modelVersionId=128713",
        "https://civitai.com/api/download/models/abc",
        "https://civitai.com.evil.test/api/download/models/128713?a=$(id)",
        "https://evil.test/https://civitai.com/api/download/models/128713?a=$(id)",
        "https://x.test/api/download/models/128713",
    ],
)
def test_model_downloads_only_relaxes_rules_for_civitai_download_urls(url):
    model = _model("checkpoints", "m.safetensors", url)
    api = {"1": {"class_type": "L", "inputs": {"ckpt_name": "m.safetensors"}}}
    assert model_downloads({"nodes": [_loader("L", [model])]}, api) is None


def test_model_downloads_ignores_bad_hash():
    model = _model(
        "vae", "ae.safetensors", "https://x.test/ae.safetensors", hash="nope", hash_type="SHA256"
    )
    api = {"1": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}}}
    assert (
        model_downloads({"nodes": [_loader("VAELoader", [model])]}, api)
        == "vae/ae.safetensors=https://x.test/ae.safetensors"
    )


def test_models_without_links():
    workflow = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sdxl.safetensors"}},
        "2": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
        "3": {"class_type": "LoraLoader", "inputs": {"lora_name": "styles\\Ink.SAFETENSORS"}},
        "4": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": "flux/q4.gguf"}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cat", "clip": ["1", 1]}},
        "6": {"class_type": "LoadImage", "inputs": {"image": "photo.png"}},
    }
    downloads = "vae/ae.safetensors=https://x/ae diffusion_models/flux/q4.gguf=https://x/q4"
    assert models_without_links(workflow, downloads) == [
        "sdxl.safetensors",
        "styles/Ink.SAFETENSORS",
    ]


def _node(class_type, cnr_id=None, ver=None):
    properties: dict[str, Any] = {}
    if cnr_id is not None:
        properties["cnr_id"] = cnr_id
    if ver is not None:
        properties["ver"] = ver
    return {"type": class_type, "properties": properties}


KJ_API = {
    "1": {"class_type": "KSampler", "inputs": {}},
    "2": {"class_type": "ImageResizeKJv2", "inputs": {}},
    "3": {"class_type": "VHS_VideoCombine", "inputs": {}},
}


def test_custom_nodes_from_node_properties():
    ui = {
        "nodes": [
            _node("KSampler", "comfy-core", "0.38.0"),
            _node("ImageResizeKJv2", "comfyui-kjnodes", "1.1.9"),
        ],
        # Nodes inside subgraphs count too.
        "definitions": {
            "subgraphs": [{"nodes": [_node("VHS_VideoCombine", "comfyui-videohelpersuite")]}]
        },
    }
    assert custom_nodes(ui, KJ_API) == "comfyui-kjnodes@1.1.9 comfyui-videohelpersuite"


def test_custom_nodes_skips_nodes_the_workflow_doesnt_run():
    ui = {"nodes": [_node("BypassedNode", "comfyui-bypassed", "1.0.0")]}
    assert custom_nodes(ui, KJ_API) is None


def test_custom_nodes_lists_each_pack_once():
    ui = {
        "nodes": [
            _node("ImageResizeKJv2", "comfyui-kjnodes", "1.1.9"),
            _node("ImageResizeKJv2", "comfyui-kjnodes", "1.1.9"),
        ]
    }
    assert custom_nodes(ui, KJ_API) == "comfyui-kjnodes@1.1.9"


@pytest.mark.parametrize(
    ("cnr_id", "ver", "expected"),
    [
        ("comfyui-kjnodes", "$(id)", "comfyui-kjnodes"),
        ("comfyui-kjnodes", "abc1234", "comfyui-kjnodes"),
        ("../evil", "1.0.0", None),
        ("pack;id", "1.0.0", None),
        ("Upper", "1.0.0", None),
    ],
)
def test_custom_nodes_skips_values_unsafe_for_the_job_script(cnr_id, ver, expected):
    ui = {"nodes": [_node("ImageResizeKJv2", cnr_id, ver)]}
    assert custom_nodes(ui, KJ_API) == expected


def test_custom_nodes_falls_back_to_installed_packs():
    # ComfyUI-Manager's new UI doesn't save cnr_id, so the node has no properties to read.
    ui = {"nodes": [_node("ImageResizeKJv2")]}
    installed = {
        "ImageResizeKJv2": ("comfyui-kjnodes", "1.5.2"),
        "VHS_VideoCombine": ("comfyui-videohelpersuite", None),
        "NotInWorkflow": ("comfyui-other", "1.0.0"),
    }
    assert custom_nodes(ui, KJ_API, installed) == "comfyui-kjnodes@1.5.2 comfyui-videohelpersuite"


def test_custom_nodes_prefers_node_properties_to_installed_packs():
    ui = {"nodes": [_node("ImageResizeKJv2", "comfyui-kjnodes", "1.1.9")]}
    installed = {"ImageResizeKJv2": ("comfyui-kjnodes", "1.5.2")}
    assert custom_nodes(ui, KJ_API, installed) == "comfyui-kjnodes@1.1.9"


def test_custom_nodes_checks_installed_packs_for_unsafe_values():
    installed = {"ImageResizeKJv2": ("pack;id", "1.0.0"), "VHS_VideoCombine": ("vhs", "$(id)")}
    assert custom_nodes(None, KJ_API, installed) == "vhs"


def test_local_node_packs_attaches_packs_without_a_registry_identity(tmp_path):
    """The registry pack stays in Custom Nodes; only the git checkout is attached."""
    git_pack = tmp_path / "ComfyUI-VideoHelperSuite"
    ui = {"nodes": [_node("ImageResizeKJv2", "comfyui-kjnodes", "1.1.9"), _node("KSampler")]}
    loaded = {
        "ImageResizeKJv2": tmp_path / "comfyui-kjnodes",
        "VHS_VideoCombine": git_pack,
    }
    packs, unregistered = local_node_packs(ui, KJ_API, {}, loaded)
    assert custom_nodes(ui, KJ_API) == "comfyui-kjnodes@1.1.9"
    assert packs == {git_pack.name: git_pack}
    assert unregistered == ["KSampler", "VHS_VideoCombine"]


@pytest.mark.parametrize("class_type", ["DeadlineStringValues", "DeadlineIntegerValues"])
def test_local_node_packs_never_attaches_this_extensions_value_nodes(tmp_path, class_type):
    """The job replaces value nodes, so workers don't need this extension."""
    api = {"1": {"class_type": class_type, "inputs": {}}}
    loaded = {class_type: tmp_path / "deadline-cloud-for-comfyui"}
    assert local_node_packs({"nodes": []}, api, {}, loaded) == ({}, [])


def test_local_node_packs_skips_registry_installs_and_built_in_nodes(tmp_path):
    ui = {"nodes": [_node("KSampler", "comfy-core", "0.38.0")]}
    installed = {
        "ImageResizeKJv2": ("comfyui-kjnodes", "1.5.2"),
        "VHS_VideoCombine": ("comfyui-videohelpersuite", None),
    }
    loaded = {class_type: tmp_path / class_type for class_type in ("KSampler", *installed)}
    assert local_node_packs(ui, KJ_API, installed, loaded) == ({}, [])


def test_local_node_packs_attaches_a_pack_with_an_unsafe_cnr_id(tmp_path):
    ui = {"nodes": [_node("ImageResizeKJv2", "pack;id", "1.0.0")]}
    pack = tmp_path / "kj"
    packs, _ = local_node_packs(ui, KJ_API, {}, {"ImageResizeKJv2": pack})
    assert packs == {"kj": pack}


def test_local_node_packs_lists_each_folder_once(tmp_path):
    pack = tmp_path / "studio-nodes"
    loaded = {"ImageResizeKJv2": pack, "VHS_VideoCombine": pack}
    packs, _ = local_node_packs(None, KJ_API, {}, loaded)
    assert packs == {"studio-nodes": pack}


def test_local_node_packs_includes_single_file_nodes(tmp_path):
    script = tmp_path / "custom_nodes" / "single.py"
    packs, _ = local_node_packs(None, KJ_API, {}, {"ImageResizeKJv2": script})
    assert packs == {"single.py": script}


def _pack(tmp_path: Path, pyproject: str | None, tracking: bool = True) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    if pyproject is not None:
        (tmp_path / "pyproject.toml").write_text(pyproject)
    if tracking:
        (tmp_path / ".tracking").write_text("pyproject.toml\n")
    return tmp_path


KJ_PYPROJECT = """[project]
name = "comfyui-kjnodes"
description = "Various nodes"
version = "1.5.2"

[tool.comfy]
PublisherId = "kijai"
name = "not-this"
"""


def test_read_node_pack_reads_registry_install(tmp_path):
    assert read_node_pack(_pack(tmp_path, KJ_PYPROJECT)) == ("comfyui-kjnodes", "1.5.2")


def test_read_node_pack_without_version(tmp_path):
    assert read_node_pack(_pack(tmp_path, "[project]\nname = 'pack'\n")) == ("pack", None)


@pytest.mark.parametrize(
    ("pyproject", "tracking"),
    [
        (KJ_PYPROJECT, False),
        (None, True),
        ("[tool.comfy]\nname = 'pack'\n", True),
        ("[project]\nversion = '1.0.0'\n", True),
    ],
)
def test_read_node_pack_skips_packs_not_from_the_registry(tmp_path, pyproject, tracking):
    assert read_node_pack(_pack(tmp_path, pyproject, tracking)) is None


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("parameterDefinitions:\n- name: CustomNodes\n  type: STRING\n", True),
        ("parameterDefinitions:\n  - name: 'CustomNodes'\n", True),
        ('{"parameterDefinitions": [{"name": "CustomNodes", "type": "STRING"}]}', True),
        ("parameterDefinitions:\n- name: CustomNodesDir\n", False),
        ("description: set CustomNodes to install nodes\n", False),
    ],
)
def test_template_defines_parameter(tmp_path, content, expected):
    template = tmp_path / "template.yaml"
    template.write_text(content, encoding="utf-8")
    assert template_defines_parameter(template, "CustomNodes") is expected


def test_bundled_template_custom_nodes():
    assert template_defines_parameter(resolve_job_template(), "CustomNodes")


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
    now = datetime(2026, 10, 19, 12, 30, 5, 123456, tzinfo=timezone.utc)
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


def _create_list(*items):
    return {
        "class_type": "CreateList",
        "inputs": {f"inputs.input{i}": [n, 0] for i, n in enumerate(items)},
    }


def _marked(plan, node_id):
    return plan.workflow[node_id].get("_meta", {})


def _names(plan):
    return [(source.name, source.label, source.count) for source in plan.sources]


def test_plan_work_items_for_create_list():
    workflow = {
        "1": _create_list("2", "3", "4", "5"),
        "6": {"class_type": "SaveImage", "inputs": {}},
    }
    plan = plan_work_items(workflow, None)
    assert _names(plan) == [("List1", "Create List 1", 4)]
    assert _marked(plan, "1") == {submit.ITEM_META: "List1"}
    assert "_meta" not in plan.workflow["6"]
    assert plan.warnings == []
    # The workflow passed in is unchanged.
    assert "_meta" not in workflow["1"]


def test_plan_work_items_without_the_inputs_prefix():
    workflow = {
        "1": {"class_type": "CreateList", "inputs": {"input0": ["2", 0], "input1": ["3", 0]}}
    }
    assert _names(plan_work_items(workflow, None)) == [("List1", "Create List 1", 2)]


@pytest.mark.parametrize(
    "workflow",
    [
        {"1": {"class_type": "SaveImage", "inputs": {}}},
        {"1": _create_list("2")},
    ],
)
def test_plan_work_items_without_lists(workflow):
    plan = plan_work_items(workflow, None)
    assert plan.sources == []
    assert plan.warnings == []
    assert plan.workflow == workflow


def test_plan_work_items_keeps_lists_of_different_lengths_separate():
    strings = {"class_type": submit.STRING_NODE, "inputs": {"values": "lora_a\n\n lora_b \n"}}
    plan = plan_work_items(
        {"1": _create_list("2", "3"), "4": _create_list("5", "6", "7"), "8": strings}, None
    )
    assert _names(plan) == [
        ("List1", "Create List 1", 2),
        ("List4", "Create List 4", 3),
        ("String8", "Deadline String Values 8", 2),
    ]
    assert plan.sources[2].values == ["lora_a", "lora_b"]
    assert _marked(plan, "8") == {submit.ITEM_META: "String8", submit.PREVIEW_META: "lora_a"}
    assert plan.warnings == []


def test_plan_work_items_names_subgraph_nodes():
    plan = plan_work_items({"57:27": _create_list("2", "3")}, None)
    assert _names(plan) == [("List57_27", "Create List 57:27", 2)]


def test_plan_work_items_leaves_lists_past_the_most_a_step_takes_whole():
    workflow = {str(n): _create_list("a", "b") for n in range(1, submit.MAX_ITEM_SOURCES + 3)}
    plan = plan_work_items(workflow, None)
    assert len(plan.active) == submit.MAX_ITEM_SOURCES
    assert "_meta" not in plan.workflow["17"]
    assert plan.warnings == [
        (
            f"Deadline Cloud runs up to {submit.MAX_ITEM_SOURCES} lists as separate tasks, so "
            "these run whole in each task: Create List 17, Create List 18."
        )
    ]


def test_plan_work_items_restores_dynamic_prompts():
    workflow = {
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a white fox", "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "a red fox", "clip": ["4", 1]}},
    }
    ui_workflow = {
        "nodes": [
            {"id": 6, "type": "CLIPTextEncode", "widgets_values": ["a {red|white|black} fox"]},
            # Another node's plain prompt isn't mistaken for one of node 6's choices.
            {"id": 7, "type": "CLIPTextEncode", "widgets_values": ["a red fox"]},
        ]
    }
    plan = plan_work_items(workflow, ui_workflow)
    assert _names(plan) == [("Prompt6", "Prompt 6", 3)]
    assert plan.sources[0].values == ["a red fox", "a white fox", "a black fox"]
    assert plan.workflow["6"]["inputs"]["text"] == "a {red|white|black} fox"
    # By default, each task chooses at random, as ComfyUI does.
    assert plan.sources[0].range == "0"
    assert _marked(plan, "6") == {submit.DYNAMIC_PROMPTS_META: {"text": "Prompt6"}}
    assert plan.workflow["7"] == workflow["7"]


def test_plan_work_items_names_each_dynamic_prompt_of_a_node():
    workflow = {"6": {"class_type": "CLIPTextEncodeSDXL", "inputs": {"text_g": "a", "text_l": "x"}}}
    ui_workflow = {"nodes": [{"id": 6, "widgets_values": ["{a|b}", "{x|y|z}"]}]}
    plan = plan_work_items(workflow, ui_workflow)
    assert _names(plan) == [
        ("Prompt6_text_g", "Prompt 6 (text_g)", 2),
        ("Prompt6_text_l", "Prompt 6 (text_l)", 3),
    ]


def test_plan_work_items_finds_dynamic_prompts_in_subgraphs():
    workflow = {"57:27": {"class_type": "CLIPTextEncode", "inputs": {"text": "b d"}}}
    ui_workflow = {
        "nodes": [{"id": 57, "type": "subgraph-uuid", "widgets_values": []}],
        "definitions": {"subgraphs": [{"nodes": [{"id": 27, "widgets_values": ["{a|b} {c|d}"]}]}]},
    }
    plan = plan_work_items(workflow, ui_workflow)
    assert _names(plan) == [("Prompt57_27", "Prompt 57:27", 4)]
    assert plan.workflow["57:27"]["inputs"]["text"] == "{a|b} {c|d}"


def test_plan_work_items_finds_dynamic_prompts_the_frontend_didnt_choose():
    workflow = {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a {red|white} fox"}}}
    ui_workflow = {"nodes": [{"id": 6, "widgets_values": ["a {red|white} fox"]}]}
    plan = plan_work_items(workflow, ui_workflow)
    assert _names(plan) == [("Prompt6", "Prompt 6", 2)]
    assert _marked(plan, "6") == {submit.DYNAMIC_PROMPTS_META: {"text": "Prompt6"}}


def test_plan_work_items_ignores_text_that_isnt_a_choice():
    workflow = {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a fox"}}}
    ui_workflow = {"nodes": [{"id": 6, "widgets_values": ['{"json": 1}', "{only}"]}]}
    plan = plan_work_items(workflow, ui_workflow)
    assert plan.sources == []
    assert plan.workflow == workflow


def test_plan_work_items_with_too_many_prompt_combinations():
    template = "{a|b|c|d|e|f|g|h|i|j}" * 4
    workflow = {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "aaaa"}}}
    plan = plan_work_items(workflow, {"nodes": [{"id": 6, "widgets_values": [template]}]})
    assert plan.sources == []
    assert plan.workflow["6"]["inputs"]["text"] == template
    # The task script chooses at random for a prompt without a task parameter.
    assert _marked(plan, "6") == {submit.DYNAMIC_PROMPTS_META: {"text": ""}}
    assert "more than 1000 combinations" in plan.warnings[0]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a {red|white|black} fox", ["a red fox", "a white fox", "a black fox"]),
        ("{a|b} {c|d}", ["a c", "a d", "b c", "b d"]),
        ("{x|{y|z}}", ["x", "y", "z"]),
        ("no choices", ["no choices"]),
        ("{only}", ["only"]),
        ("{a|}", ["a", ""]),
        ("literal \\{a|b\\}", ["literal {a|b}"]),
        ("{a|b} // {c|d}", ["a ", "b "]),
        ("{a|b /* c|d */}", ["a", "b "]),
        ("{unclosed|end", ["unclosed", "end"]),
    ],
)
def test_expand_dynamic_prompt(text, expected):
    assert expand_dynamic_prompt(text) == expected


def test_string_values():
    assert string_values("  a red fox \n\n2\r\n 0.5") == ["a red fox", "2", "0.5"]
    assert string_values("") == []


def test_bundled_template_splits_lists():
    assert template_supports_work_items(submit.JOB_TEMPLATE)


def test_template_supports_work_items_missing_file(tmp_path):
    assert not template_supports_work_items(tmp_path / "missing.yaml")


SOURCES = [
    submit.ItemSource("List1", "Create List 1", 4),
    submit.ItemSource(
        "Prompt6", "Prompt 6", 3, "prompt", values=["a red fox", "a white fox", "a black fox"]
    ),
]


def test_add_work_items():
    base = yaml.safe_load(submit.JOB_TEMPLATE.read_text())
    template = submit.add_work_items(base, SOURCES)
    names = [parameter["name"] for parameter in template["parameterDefinitions"]]
    # After the workflow parameters.
    at = names.index("OutputDir") + 1
    assert names[at : at + 2] == ["List1Items", "Prompt6Items"]
    prompts = template["parameterDefinitions"][at + 1]
    assert prompts["default"] == "1-3"
    assert prompts["userInterface"] == {
        "control": "LINE_EDIT",
        "label": "Prompt 6",
        "groupLabel": "Work Items",
    }
    assert prompts["description"].endswith("Items: 1: a red fox; 2: a white fox; 3: a black fox.")
    step = template["steps"][0]
    assert "combination" not in step["parameterSpace"]
    assert step["parameterSpace"]["taskParameterDefinitions"] == [
        {"name": "List1", "type": "INT", "range": "{{Param.List1Items}}"},
        {"name": "Prompt6", "type": "INT", "range": "{{Param.Prompt6Items}}"},
    ]
    script = step["script"]["embeddedFiles"][0]["data"]
    assert 'ITEMS = {"List1": {{Task.Param.List1}}, "Prompt6": {{Task.Param.Prompt6}}}\n' in script
    # The template passed in is unchanged.
    assert base == yaml.safe_load(submit.JOB_TEMPLATE.read_text())


def test_add_work_items_shortens_long_item_lists():
    source = submit.ItemSource(
        "String5", "Deadline String Values 5", 12, "string", [str(n) * 70 for n in range(10)]
    )
    parameter = submit.add_work_items({"steps": []}, [source])["parameterDefinitions"][0]
    assert f"1: {'0' * 60}...; " in parameter["description"]
    assert parameter["description"].endswith("10: " + "9" * 60 + "...; and 2 more.")


def test_add_work_items_lists_ten_items_in_full():
    source = submit.ItemSource(
        "String5", "Deadline String Values 5", 10, "string", [str(n) for n in range(10)]
    )
    parameter = submit.add_work_items({"steps": []}, [source])["parameterDefinitions"][0]
    assert parameter["description"].endswith(
        "Items: 1: 0; 2: 1; 3: 2; 4: 3; 5: 4; 6: 5; 7: 6; 8: 7; 9: 8; 10: 9."
    )


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


def test_generated_template_is_valid_and_crosses_every_source(tmp_path):
    bundle_dir, _ = create_job_bundle(
        submit.JOB_TEMPLATE, API_WORKFLOW, "w", tmp_path, sources=SOURCES
    )
    tasks = _tasks(bundle_dir, tmp_path)
    # 4 list items and 3 prompts.
    assert len(tasks) == 12
    assert tasks[-1] == {"List1": "4", "Prompt6": "3"}


def test_base_template_runs_once(tmp_path):
    bundle_dir, _ = create_job_bundle(submit.JOB_TEMPLATE, API_WORKFLOW, "w", tmp_path)
    assert _tasks(bundle_dir, tmp_path) == [{}]


def test_generated_template_pairs_value_nodes(tmp_path):
    workflow = {
        "2": {"class_type": submit.STRING_NODE, "inputs": {"values": "a\nb\nc", "pair_with": "p"}},
        "3": {"class_type": submit.INTEGER_NODE, "inputs": {"values": "1-5:2", "pair_with": "p"}},
        "4": {"class_type": submit.INTEGER_NODE, "inputs": {"values": "7, 40-41"}},
        "5": {"class_type": "SaveImage", "inputs": {"filename_prefix": "fox"}},
    }
    workflow["4"]["_meta"] = {"title": "Seed"}
    plan = plan_work_items(workflow, None)
    assert [(source.name, source.range) for source in plan.active] == [
        ("String2", "1-3"),
        ("Integer3", "1-5:2"),
        ("Seed", "7,40-41"),
    ]
    assert submit.combination(plan.active) == "(String2, Integer3) * Seed"
    assert submit.task_count(plan.active) == 9
    bundle_dir, _ = create_job_bundle(
        submit.JOB_TEMPLATE, plan.workflow, "w", tmp_path, sources=plan.active
    )
    tasks = _tasks(bundle_dir, tmp_path)
    assert len(tasks) == 9
    assert tasks[:4] == [
        {"String2": "1", "Integer3": "1", "Seed": "7"},
        {"String2": "1", "Integer3": "1", "Seed": "40"},
        {"String2": "1", "Integer3": "1", "Seed": "41"},
        {"String2": "2", "Integer3": "3", "Seed": "7"},
    ]


def test_create_job_bundle_writes_scripts_as_literal_blocks(tmp_path):
    bundle_dir, _ = create_job_bundle(
        submit.JOB_TEMPLATE, API_WORKFLOW, "w", tmp_path, sources=SOURCES
    )
    text = (bundle_dir / "template.yaml").read_text()
    assert text.count("data: |\n") == 3
    assert "\\nimport json" not in text


def test_create_job_bundle_adds_work_items_to_json_templates(tmp_path):
    template = tmp_path / "studio.json"
    template.write_text(json.dumps(yaml.safe_load(submit.JOB_TEMPLATE.read_text())))
    bundle_dir, _ = create_job_bundle(template, API_WORKFLOW, "w", tmp_path / "s", sources=SOURCES)
    written = json.loads((bundle_dir / "template.json").read_text())
    assert written == submit.add_work_items(json.loads(template.read_text()), SOURCES)


@pytest.mark.parametrize("content", ["key: [unclosed", "just text"])
def test_create_job_bundle_rejects_unreadable_templates(tmp_path, content):
    template = tmp_path / "studio.yaml"
    template.write_text(content)
    with pytest.raises(SubmitError, match="studio.yaml"):
        create_job_bundle(template, API_WORKFLOW, "w", tmp_path / "s", sources=SOURCES)
    assert not (tmp_path / "s").exists()


@pytest.mark.parametrize(
    ("text", "expected", "expression"),
    [
        ("1-4", [1, 2, 3, 4], "1-4"),
        (" 7, 42 ,99 ", [7, 42, 99], "7,42,99"),
        ("1-9:4,20", [1, 5, 9, 20], "1-9:4,20"),
        ("-2-0", [-2, -1, 0], "-2-0"),
        ("5-5", [5], "5"),
        ("", [], ""),
    ],
)
def test_integer_values(text, expected, expression):
    assert submit.integer_values(text) == expected
    assert submit._integer_range_expression(text) == expression


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("one", "isn't a number"),
        ("4-1", "count up"),
        ("1-4:0", "count up"),
        ("1-3,2", "more than once"),
        ("1-20000", "more than 10000"),
    ],
)
def test_integer_values_rejects(text, match):
    with pytest.raises(ValueError, match=match):
        submit.integer_values(text)


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


def test_image_files(tmp_path):
    for name in ("b.PNG", "a.webp", "c.txt"):
        (tmp_path / name).write_text("x")
    (tmp_path / "d.jpg").mkdir()
    assert submit.image_files(tmp_path) == ["a.webp", "b.PNG"]
    assert submit.image_files(tmp_path / "missing") == []


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
        "3": {"class_type": submit.IMAGE_NODE, "inputs": {"folder": "cats [input]"}},
        # Only folder loaders attach folders, and only folders in the input directory.
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "cats"}},
        "5": {"class_type": "LoadImageDataSetFromFolder", "inputs": {"folder": "../outside"}},
        "6": {"class_type": "LoadImageDataSetFromFolder", "inputs": {"folder": "missing"}},
    }
    assert submit.input_folders(workflow, input_dir) == [input_dir / "cats"]


def test_plan_work_items_for_folder_loaders(tmp_path):
    input_dir = _input_dir(tmp_path)
    workflow = {"3": {"class_type": "LoadImageDataSetFromFolder", "inputs": {"folder": "cats"}}}
    plan = plan_work_items(workflow, None, input_dir)
    assert _names(plan) == [("Folder3", "Folder cats (3)", 2)]
    assert plan.sources[0].values == ["a.png", "b.jpg"]
    assert _marked(plan, "3") == {
        submit.FILES_META: ["cats/a.png", "cats/b.jpg"],
        submit.ITEM_META: "Folder3",
    }
    # Without the input directory, such as in a test, folders aren't split.
    assert plan_work_items(workflow, None).sources == []


def test_plan_work_items_for_image_values(tmp_path):
    workflow = {
        "3": {
            "class_type": submit.IMAGE_NODE,
            "inputs": {"folder": "cats", "preview_item": 2},
            "_meta": {"title": "Deadline Image Values"},
        }
    }
    plan = plan_work_items(workflow, None, _input_dir(tmp_path))
    assert _names(plan) == [("Image3", "Deadline Image Values 3", 2)]
    assert _marked(plan, "3") == {
        "title": "Deadline Image Values",
        submit.FILES_META: ["cats/a.png", "cats/b.jpg"],
        submit.PREVIEW_META: "cats/b.jpg",
        submit.ITEM_META: "Image3",
    }


def test_plan_work_items_marks_value_nodes_with_one_item(tmp_path):
    workflow = {"5": {"class_type": submit.INTEGER_NODE, "inputs": {"values": "42"}}}
    plan = plan_work_items(workflow, None)
    assert plan.sources == []
    assert _marked(plan, "5") == {submit.PREVIEW_META: 42}


@pytest.mark.parametrize(
    ("node", "message"),
    [
        (
            {"class_type": submit.STRING_NODE, "inputs": {"values": ["4", 0]}},
            "Deadline String Values 5 needs its values typed in, not connected.",
        ),
        (
            {"class_type": submit.STRING_NODE, "inputs": {"values": " \n"}},
            "Deadline String Values 5 has no values.",
        ),
        (
            {"class_type": submit.INTEGER_NODE, "inputs": {"values": "1-3", "preview_item": 4}},
            "Deadline Integer Values 5 has 3 items, so it can't preview item 4.",
        ),
        (
            {"class_type": submit.INTEGER_NODE, "inputs": {"values": "x"}},
            "Deadline Integer Values 5: 'x' isn't a number",
        ),
        (
            {
                "class_type": submit.INTEGER_NODE,
                "inputs": {"values": ",".join(map(str, range(0, 600, 2)))},
            },
            "Deadline Integer Values 5 has too many numbers and ranges.",
        ),
        (
            {"class_type": submit.IMAGE_NODE, "inputs": {"folder": "dogs"}},
            "Deadline Image Values 5 has no images in the folder 'dogs' of the input directory.",
        ),
    ],
)
def test_plan_work_items_rejects_value_nodes_the_job_cant_run(tmp_path, node, message):
    with pytest.raises(SubmitError, match=re.escape(message)):
        plan_work_items({"5": node}, None, _input_dir(tmp_path))


def test_task_summary():
    sources = [
        submit.ItemSource("List64", "Create List 64", 3),
        submit.ItemSource("Seed", "Seed", 4, "integer", range="1-4"),
        submit.ItemSource("A", "A", 2, "string", pair_with="p"),
        submit.ItemSource("B", "B", 2, "image", pair_with="p"),
        # A dynamic prompt chooses at random by default, so it doesn't add tasks.
        submit.ItemSource("Prompt6", "Prompt 6", 9, "prompt", range="0"),
    ]
    assert submit.task_count(sources) == 24
    assert submit.task_summary(sources) == "24 tasks: List64 3 × Seed 4 × A + B 2"
    assert submit.task_summary(sources[-1:]) == "1 task"


def test_dynamic_prompt_parameters_choose_at_random_by_default():
    source = submit.ItemSource("Prompt6", "Prompt 6", 2, "prompt", ["a", "b"], range="0")
    parameter = submit.add_work_items({"steps": []}, [source])["parameterDefinitions"][0]
    assert parameter["default"] == "0"
    assert '"0", the default, chooses one at random for each task' in parameter["description"]


def test_plan_work_items_names_value_nodes_by_title():
    workflow = {
        "5": {
            "class_type": submit.INTEGER_NODE,
            "inputs": {"values": "1-2"},
            "_meta": {"title": "Seed (main)"},
        },
        "6": {
            "class_type": submit.INTEGER_NODE,
            "inputs": {"values": "3-4"},
            "_meta": {"title": "Seed main"},
        },
        "7": {
            "class_type": submit.STRING_NODE,
            "inputs": {"values": "a\nb"},
            "_meta": {"title": "9"},
        },
    }
    assert [source.name for source in plan_work_items(workflow, None).sources] == [
        "Seed_main",
        "Seed_main_6",
        "_9",
    ]


def test_plan_work_items_unpairs_value_nodes_with_different_counts():
    workflow = {
        "2": {"class_type": submit.STRING_NODE, "inputs": {"values": "a\nb", "pair_with": "p"}},
        "3": {"class_type": submit.INTEGER_NODE, "inputs": {"values": "1-3", "pair_with": " p "}},
    }
    plan = plan_work_items(workflow, None)
    assert [source.pair_with for source in plan.active] == ["", ""]
    assert plan.warnings == [
        (
            "The value nodes paired as 'p' have different numbers of items (Deadline String "
            "Values 2: 2, Deadline Integer Values 3: 3), so every combination of them runs."
        )
    ]
    assert submit.combination(plan.active) is None
    assert submit.task_count(plan.active) == 6


def test_dynamic_prompts_look_up_each_node_once(monkeypatch):
    # Scanning the UI workflow for each API node makes planning a large graph take seconds.
    scans = []
    real = submit._index_ui_nodes

    def counting_index(ui):
        scans.append(ui)
        return real(ui)

    monkeypatch.setattr(submit, "_index_ui_nodes", counting_index)
    count = 50
    workflow = {
        str(i): {"class_type": "CLIPTextEncode", "inputs": {"text": "a"}} for i in range(count)
    }
    ui_workflow = {"nodes": [{"id": i, "widgets_values": ["{a|b}"]} for i in range(count)]}
    plan = plan_work_items(workflow, ui_workflow)
    assert len(scans) == 1
    assert len(plan.sources) == count


@pytest.mark.parametrize("ui_workflow", [None, "text", {}, {"nodes": None}, {"nodes": ["x"]}])
def test_dynamic_prompts_ignore_malformed_ui_workflows(ui_workflow):
    workflow = {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a"}}}
    assert plan_work_items(workflow, ui_workflow).sources == []


def _titles(workflow, ui_workflow=None):
    return [source.title for source in plan_work_items(workflow, ui_workflow).sources]


def test_value_nodes_are_titled_by_the_name_an_artist_gave_them():
    workflow = {
        "1": {
            "class_type": submit.INTEGER_NODE,
            "inputs": {"values": "1-2"},
            "_meta": {"title": "Width"},
        },
        "2": {"class_type": "ImageScale", "inputs": {"width": ["1", 0]}},
    }
    assert _titles(workflow) == ["Width"]
    # The label still names the node, for errors and job parameters.
    assert plan_work_items(workflow, None).sources[0].label == "Deadline Integer Values 1"


def test_value_nodes_are_titled_by_what_they_connect_to():
    workflow: dict[str, Any] = {
        "1": {
            "class_type": submit.INTEGER_NODE,
            "inputs": {"values": "1-2"},
            "_meta": {"title": "Deadline Integer Values"},
        },
        "2": {
            "class_type": "ImageScale",
            "inputs": {"width": ["1", 0], "height": ["1", 0]},
            "_meta": {"title": "Scale"},
        },
    }
    assert _titles(workflow) == ["Scale · width +1"]
    del workflow["2"]["inputs"]["height"]
    del workflow["2"]["_meta"]
    assert _titles(workflow) == ["ImageScale · width"]


def test_value_nodes_without_a_name_or_connection_are_titled_by_their_label():
    workflow = {"1": {"class_type": submit.STRING_NODE, "inputs": {"values": "a\nb"}}}
    assert _titles(workflow) == ["Deadline String Values 1"]


def test_lists_that_look_like_links_arent_connections():
    workflow = {
        "1": {"class_type": submit.STRING_NODE, "inputs": {"values": "a\nb"}},
        "2": {"class_type": "Other", "inputs": {"choices": ["1", "2"]}},
    }
    assert _titles(workflow) == ["Deadline String Values 1"]


def test_dynamic_prompts_are_titled_by_their_text():
    long_text = "{a|b} " + "x" * 60
    workflow = {
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a"}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": long_text}},
    }
    ui_workflow = {
        "nodes": [
            {"id": 6, "widgets_values": ["{a|b}"]},
            {"id": 7, "widgets_values": [long_text]},
        ]
    }
    assert _titles(workflow, ui_workflow) == [
        'Prompt 6: "{a|b}"',
        f'Prompt 7: "{long_text[:submit.MAX_PROMPT_TITLE]}…"',
    ]


def test_folders_keep_their_label_as_title(tmp_path):
    (tmp_path / "cats").mkdir()
    for name in ("a.png", "b.png"):
        (tmp_path / "cats" / name).write_text("x")
    workflow = {"3": {"class_type": "LoadImageDataSetFromFolder", "inputs": {"folder": "cats"}}}
    sources = plan_work_items(workflow, None, tmp_path).sources
    assert [source.title for source in sources] == ["Folder cats (3)"]


def test_integer_parameters_hold_values():
    source = submit.ItemSource("Seed", "Seed", 3, "integer", ["1", "2", "3"], range="1-3")
    parameter = submit.add_work_items({"steps": []}, [source])["parameterDefinitions"][0]
    assert parameter["default"] == "1-3"
    assert parameter["description"].startswith("The values of Seed to run as separate tasks")
    assert "Items:" not in parameter["description"]


def test_paired_parameters_explain_the_pair():
    source = submit.ItemSource("A", "A", 2, "string", ["x", "y"], pair_with="p")
    parameter = submit.add_work_items({"steps": []}, [source])["parameterDefinitions"][0]
    assert "paired as 'p' run their items together" in parameter["description"]


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


def test_picker_command_on_linux(monkeypatch):
    monkeypatch.setattr(submit.shutil, "which", lambda name: "/usr/bin/zenity")
    assert "--directory" in (submit.picker_command("folder", "linux") or [])
    assert "--multiple" in (submit.picker_command("files", "linux") or [])
    monkeypatch.setattr(submit.shutil, "which", lambda name: None)
    assert submit.picker_command("files", "linux") is None
