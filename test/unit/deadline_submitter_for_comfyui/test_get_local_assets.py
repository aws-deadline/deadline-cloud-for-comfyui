# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from deadline.comfyui_submitter.get_local_assets import (
    LocalAssets,
    NodePackSize,
    _search_local_models,
    find_local_model,
    find_workflow_local_models,
    get_local_assets,
    node_pack_size,
    stage_local_models,
    stage_local_node_packs,
    write_asset_references,
)
from deadline.comfyui_submitter.submit import MODEL_EXTENSIONS


@pytest.fixture
def comfy(tmp_path):
    """ComfyUI's input, output, and model directories."""
    root = tmp_path / "ComfyUI"
    dirs = {
        "input": root / "input",
        "output": root / "output",
        "checkpoints": root / "models" / "checkpoints",
        "loras": root / "models" / "loras",
        "diffusers": root / "models" / "diffusers",
        "custom_nodes": root / "custom_nodes",
    }
    for directory in dirs.values():
        directory.mkdir(parents=True)
    return dirs


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def assets(workflow, comfy):
    return get_local_assets(workflow, input_dir=comfy["input"], output_dir=comfy["output"])


def node(**inputs):
    return {"class_type": "Node", "inputs": inputs}


def test_finds_input_files_and_skips_models(comfy):
    touch(comfy["checkpoints"] / "sd" / "v1-5.safetensors")
    touch(comfy["loras"] / "style.safetensors")
    image = touch(comfy["input"] / "cat.png")
    video = touch(comfy["input"] / "clips" / "run.mp4")
    workflow = {
        "1": node(ckpt_name="sd/v1-5.safetensors"),
        "2": node(lora_name="style.safetensors", strength=0.5, model=["1", 0]),
        "3": node(image="cat.png"),
        "4": node(video="clips/run.mp4"),
        "5": node(text="a cat riding a horse"),
    }
    assert assets(workflow, comfy) == LocalAssets(sorted([image, video]), [])


def test_ignores_missing_files_and_unknown_extensions(comfy):
    touch(comfy["checkpoints"] / "notes.txt")
    touch(comfy["input"] / "data.csv")
    workflow = {
        "1": node(ckpt_name="notes.txt"),
        "2": node(image="missing.png"),
        "3": node(ckpt_name="gone.ckpt"),
        "4": node(file="data.csv"),
        "5": node(image="notes.png"),
    }
    assert not assets(workflow, comfy)


def test_annotated_values(comfy):
    image = touch(comfy["input"] / "a.png")
    out = touch(comfy["output"] / "b.png")
    workflow = {"1": node(image="a.png [input]"), "2": node(image="b.png [output]")}
    assert assets(workflow, comfy).filenames == sorted([image, out])


def test_invalid_annotated_value(comfy):
    assert not assets({"1": node(image="a.png [temp]")}, comfy)


def test_skips_model_files_and_directories(comfy, tmp_path):
    touch(comfy["checkpoints"] / "a.safetensors")
    (comfy["diffusers"] / "sd-turbo").mkdir()
    absolute = touch(tmp_path / "vae" / "ae.safetensors")
    workflow = {
        "1": node(ckpt_name="a.safetensors", model_path="sd-turbo", vae=str(absolute)),
    }
    assert not assets(workflow, comfy)


def test_absolute_paths(comfy, tmp_path):
    video = touch(tmp_path / "elsewhere" / "v.mov")
    other = touch(tmp_path / "elsewhere" / "notes.txt")
    workflow = {"1": node(video=str(video), file=str(other), folder=str(tmp_path))}
    assert assets(workflow, comfy) == LocalAssets([video], [])


@pytest.mark.parametrize(
    "value",
    [
        r"\\attacker\share\a.png",
        "//attacker/share/a.png",
        r"\\attacker@SSL\DavWWWRoot\a.png",
        r"\\?\UNC\attacker\share\a.png",
        r"\\.\pipe\a.png",
        r"/\attacker\share\a.png",
    ],
)
def test_never_checks_network_or_device_paths(comfy, monkeypatch, value):
    # On Windows, checking these paths contacts the server they name.
    checked: list[Path] = []

    def check(self: Path) -> bool:
        checked.append(self)
        return True

    monkeypatch.setattr(Path, "is_file", check)
    monkeypatch.setattr(Path, "is_dir", check)
    assert not assets({"1": node(image=value)}, comfy)
    assert not checked


def test_refuses_an_absolute_path_that_starts_with_two_separators(comfy, tmp_path):
    video = touch(tmp_path / "elsewhere" / "v.mov")
    # POSIX treats //path as /path, but on Windows it's a UNC path.
    assert not assets({"1": node(video="/" + video.as_posix())}, comfy)


def test_rejects_paths_that_leave_the_directory(comfy, tmp_path):
    touch(tmp_path / "secret.png")
    workflow = {
        "1": node(image="../../secret.png"),
        "2": node(image="../ComfyUI/input/../../x.png"),
    }
    assert not assets(workflow, comfy)


def test_tolerates_odd_values(comfy):
    workflow = {
        "1": node(a="", b="x" * 5000, c="line\nbreak.png", d="nul\0.png", e=None, f=3, g=[1, 2]),
        "2": "not a node",
        "3": {"class_type": "X"},
    }
    assert not assets(workflow, comfy)


def test_deduplicates(comfy):
    image = touch(comfy["input"] / "cat.png")
    workflow = {"1": node(image="cat.png"), "2": node(image="cat.png [input]")}
    assert assets(workflow, comfy).filenames == [image]


def test_asset_references_yaml_format(tmp_path):
    assets = LocalAssets(
        [tmp_path / "a b.png", tmp_path / 'q"uote: é.ckpt'], [tmp_path / "models" / "dir"]
    )
    path = write_asset_references(assets, tmp_path / "asset_references.yaml")
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {
        "assetReferences": {
            "inputs": {
                "filenames": [str(p) for p in assets.filenames],
                "directories": [str(tmp_path / "models" / "dir")],
            },
            "outputs": {"directories": []},
            "referencedPaths": [],
        }
    }


def test_find_local_model_searches_registered_directories(comfy, tmp_path):
    extra = tmp_path / "extra" / "vae"
    expected = touch(extra / "nested" / "ae.safetensors")
    touch(comfy["checkpoints"] / "ae.safetensors")
    folders = {
        "vae": ([str(comfy["checkpoints"]), str(extra)], {".safetensors"}),
    }
    assert find_local_model(folders, "vae", "nested/ae.safetensors") == expected
    assert find_local_model(folders, "vae", "../secret.safetensors") is None
    assert find_local_model(folders, "loras", "ae.safetensors") is None


def test_find_workflow_local_models_skips_missing_files(comfy, caplog):
    path = touch(comfy["checkpoints"] / "custom.safetensors")
    folders = {"checkpoints": ([str(comfy["checkpoints"])], {".safetensors"})}
    ui = {
        "nodes": [
            {
                "properties": {
                    "models": [
                        {"directory": "checkpoints", "name": "custom.safetensors"},
                        {"directory": "checkpoints", "name": "gone.safetensors"},
                    ]
                }
            }
        ]
    }
    api = {
        "1": {
            "inputs": {"ckpt_name": "custom.safetensors", "lora_name": "gone.safetensors"},
        }
    }
    found, directories = find_workflow_local_models(ui, api, folders)
    assert found == {"checkpoints/custom.safetensors": path}
    assert directories == {"checkpoints/custom.safetensors": "checkpoints"}
    assert "gone.safetensors" in caplog.text


def test_stage_local_models_skips_a_file_that_cannot_be_placed(comfy, tmp_path, monkeypatch):
    kept = touch(comfy["checkpoints"] / "kept.safetensors")
    dropped = touch(comfy["loras"] / "dropped.safetensors")

    copy_file = shutil.copy2

    def fail_link(source, target):
        raise OSError("link")

    def fail_copy(source, target):
        if Path(source).name == "dropped.safetensors":
            raise OSError("disk full")
        copy_file(source, target)

    monkeypatch.setattr("deadline.comfyui_submitter.get_local_assets.os.link", fail_link)
    monkeypatch.setattr("deadline.comfyui_submitter.get_local_assets.shutil.copy2", fail_copy)
    dest = tmp_path / "local_models"
    staged = stage_local_models(
        {"checkpoints/kept.safetensors": kept, "loras/dropped.safetensors": dropped},
        {"checkpoints/kept.safetensors": "checkpoints", "loras/dropped.safetensors": "loras"},
        dest,
    )
    assert staged == {"checkpoints/kept.safetensors": dest / "checkpoints" / "kept.safetensors"}
    assert not (dest / "loras" / "dropped.safetensors").exists()


def test_stage_local_models_uses_comfy_folder_layout(comfy, tmp_path):
    source = touch(comfy["checkpoints"] / "nested" / "custom.safetensors")
    secret = touch(tmp_path / "secret.safetensors")
    dest = tmp_path / "local_models"
    staged = stage_local_models(
        {
            "checkpoints/nested/custom.safetensors": source,
            "checkpoints/../../secret.safetensors": secret,
        },
        {
            "checkpoints/nested/custom.safetensors": "checkpoints",
            "checkpoints/../../secret.safetensors": "checkpoints",
        },
        dest,
    )
    expected = dest / "checkpoints" / "nested" / "custom.safetensors"
    assert staged == {"checkpoints/nested/custom.safetensors": expected}
    assert expected.read_bytes() == source.read_bytes()
    assert expected.stat().st_ino == source.stat().st_ino
    assert not (dest / "checkpoints" / "secret.safetensors").exists()


def test_stage_local_node_packs_copies_the_pack_code(comfy, tmp_path):
    pack = comfy["custom_nodes"] / "studio-nodes"
    init = touch(pack / "__init__.py")
    touch(pack / "nodes" / "resize.py")
    touch(pack / "requirements.txt")
    for skipped in (
        ".git/HEAD",
        "__pycache__/x.pyc",
        "nodes/old.pyc",
        ".venv/lib/x.py",
        "node_modules/pkg/index.js",
    ):
        touch(pack / skipped)
    touch(pack / ".venv" / "pyvenv.cfg")
    dest = tmp_path / "local_nodes"
    staged = stage_local_node_packs({"studio-nodes": pack}, dest)
    assert staged == {"studio-nodes": dest / "studio-nodes"}
    files = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert files == [
        "studio-nodes/__init__.py",
        "studio-nodes/nodes/resize.py",
        "studio-nodes/requirements.txt",
    ]
    assert (dest / "studio-nodes" / "__init__.py").stat().st_ino == init.stat().st_ino


def test_stage_local_node_packs_links_a_single_file_node(comfy, tmp_path):
    source = touch(comfy["custom_nodes"] / "single.py")
    dest = tmp_path / "local_nodes"
    staged = stage_local_node_packs({"single.py": source}, dest)
    assert staged == {"single.py": dest / "single.py"}
    assert (dest / "single.py").stat().st_ino == source.stat().st_ino


def test_stage_local_node_packs_refuses_unsafe_names_and_missing_folders(comfy, tmp_path):
    pack = comfy["custom_nodes"] / "pack"
    touch(pack / "__init__.py")
    script = touch(comfy["custom_nodes"] / "single.py")
    notes = touch(comfy["custom_nodes"] / "notes.txt")
    dest = tmp_path / "local_nodes"
    staged = stage_local_node_packs(
        {
            "..": pack,
            "a/b": pack,
            "": pack,
            "gone": comfy["custom_nodes"] / "gone",
            "renamed.py": script,  # The key must be the file's own name.
            "notes.txt": notes,  # Only .py files are attached.
        },
        dest,
    )
    assert staged == {}
    assert not dest.exists()


def test_stage_local_node_packs_skips_a_pack_that_cannot_be_copied(comfy, tmp_path, monkeypatch):
    kept = comfy["custom_nodes"] / "kept"
    touch(kept / "__init__.py")
    dropped = comfy["custom_nodes"] / "dropped"
    touch(dropped / "__init__.py")
    copy_tree = shutil.copytree

    def fail_copy(source, target, **kwargs):
        if Path(source).name == "dropped":
            raise OSError("disk full")
        return copy_tree(source, target, **kwargs)

    monkeypatch.setattr("deadline.comfyui_submitter.get_local_assets.shutil.copytree", fail_copy)
    dest = tmp_path / "local_nodes"
    staged = stage_local_node_packs({"kept": kept, "dropped": dropped}, dest)
    assert staged == {"kept": dest / "kept"}
    assert not (dest / "dropped").exists()


def test_find_workflow_local_models_reads_extensions_under_nodes(tmp_path):
    checkpoints = tmp_path / "checkpoints"
    folders = {"checkpoints": ([str(checkpoints)], set[str]())}
    names = [f"m{extension}" for extension in MODEL_EXTENSIONS]
    for name in names:
        touch(checkpoints / name)
    touch(checkpoints / "photo.png")
    ui = {
        "nodes": [
            {
                "type": "CheckpointLoaderSimple",
                "properties": {
                    "models": [{"directory": "checkpoints", "name": name} for name in names]
                },
                "widgets_values": [*names, "photo.png"],
            }
        ]
    }
    api = {"1": {"inputs": {name: name for name in [*names, "photo.png"]}}}
    found, directories = find_workflow_local_models(ui, api, folders)
    assert found == {f"checkpoints/{name}": checkpoints / name for name in names}
    assert directories == {f"checkpoints/{name}": "checkpoints" for name in names}


def test_find_workflow_local_models_reads_properties_on_a_node_inside_a_subgraph(tmp_path):
    """A subgraph node's properties name the folder. A URL there doesn't stop it being staged."""
    checkpoint = touch(tmp_path / "checkpoints" / "sd.safetensors")
    kept = touch(tmp_path / "loras" / "style.safetensors")
    folders = {
        "checkpoints": ([str(checkpoint.parent)], {".safetensors"}),
        "loras": ([str(kept.parent)], {".safetensors"}),
    }
    ui = {
        "nodes": [
            {
                "type": "sg",
                "properties": {"cnr_id": "comfy-core", "ver": "0.3.64"},
                "inputs": [
                    {"name": "ckpt_name", "widget": {"name": "ckpt_name"}},
                    {"name": "lora_name", "widget": {"name": "lora_name"}},
                ],
                "widgets_values_named": {
                    "ckpt_name": "sd.safetensors",
                    "lora_name": "style.safetensors",
                },
            }
        ],
        "definitions": {
            "subgraphs": [
                {
                    "id": "sg",
                    "inputs": [
                        {"name": "ckpt_name", "linkIds": [1]},
                        {"name": "lora_name", "linkIds": [2]},
                    ],
                    "nodes": [
                        {
                            "type": "CheckpointLoaderSimple",
                            "inputs": [
                                {
                                    "name": "ckpt_name",
                                    "widget": {"name": "ckpt_name"},
                                    "link": 1,
                                }
                            ],
                            "properties": {
                                "models": [
                                    {
                                        "directory": "checkpoints",
                                        "name": "sd.safetensors",
                                        "url": "https://x.test/sd.safetensors",
                                    }
                                ]
                            },
                        },
                        {
                            "type": "LoraLoader",
                            "inputs": [
                                {"name": "lora_name", "widget": {"name": "lora_name"}, "link": 2}
                            ],
                            "properties": {
                                "models": [{"directory": "loras", "name": "style.safetensors"}]
                            },
                        },
                    ],
                }
            ]
        },
    }
    api = {
        "1": {"inputs": {"ckpt_name": "sd.safetensors"}},
        "2": {"inputs": {"lora_name": "style.safetensors"}},
    }
    found, directories = find_workflow_local_models(ui, api, folders)
    assert found == {"checkpoints/sd.safetensors": checkpoint, "loras/style.safetensors": kept}
    assert directories == {
        "checkpoints/sd.safetensors": "checkpoints",
        "loras/style.safetensors": "loras",
    }
    dest = tmp_path / "local_models"
    staged = stage_local_models(found, directories, dest)
    assert staged == {
        "checkpoints/sd.safetensors": dest / "checkpoints" / "sd.safetensors",
        "loras/style.safetensors": dest / "loras" / "style.safetensors",
    }


def test_find_workflow_local_models_searches_registered_folders_without_a_directory(tmp_path):
    path = touch(tmp_path / "loras" / "style.pt")
    other = touch(tmp_path / "checkpoints" / "other.ckpt")
    folders = {
        "checkpoints": ([str(tmp_path / "checkpoints")], set[str]()),
        "loras": ([str(path.parent)], set[str]()),
    }
    # other.ckpt is only an API input. models_without_links still counts it, so it is attached.
    ui = {"nodes": [{"widgets_values": ["style.pt", "notes.txt"]}]}
    api = {"1": {"inputs": {"lora_name": "style.pt", "ckpt_name": "other.ckpt"}}}
    found, directories = find_workflow_local_models(ui, api, folders)
    assert found == {"checkpoints/other.ckpt": other, "loras/style.pt": path}
    assert directories == {"checkpoints/other.ckpt": "checkpoints", "loras/style.pt": "loras"}


@pytest.mark.parametrize("declared", [None, "custom_nodes"])
def test_find_workflow_local_models_never_attaches_files_from_custom_nodes(tmp_path, declared):
    """A pack's cache or settings file isn't a model, even when a workflow names it as one."""
    touch(tmp_path / "custom_nodes" / "SomePack" / "cache" / "session.pkl")
    folders = {
        "checkpoints": ([str(tmp_path / "checkpoints")], set[str]()),
        "custom_nodes": ([str(tmp_path / "custom_nodes")], set[str]()),
    }
    name = "SomePack/cache/session.pkl"
    models = [{"directory": declared, "name": name}] if declared else []
    ui = {"nodes": [{"properties": {"models": models}, "widgets_values": [name]}]}
    api = {"1": {"inputs": {"ckpt_name": name}}}
    assert find_workflow_local_models(ui, api, folders) == ({}, {})


@pytest.mark.parametrize(
    ("hint", "folder"), [("clip", "text_encoders"), ("unet", "diffusion_models")]
)
def test_find_workflow_local_models_maps_legacy_folder_names(tmp_path, hint, folder):
    """ComfyUI loads clip and unet models from text_encoders and diffusion_models."""
    path = touch(tmp_path / folder / "m.safetensors")
    folders = {folder: ([str(path.parent)], set[str]())}
    ui = {"nodes": [{"properties": {"models": [{"directory": hint, "name": "m.safetensors"}]}}]}
    api = {"1": {"inputs": {"clip_name": "m.safetensors"}}}
    assert find_workflow_local_models(ui, api, folders) == (
        {f"{folder}/m.safetensors": path},
        {f"{folder}/m.safetensors": folder},
    )


def test_find_workflow_local_models_trusts_the_folder_a_loader_declares(tmp_path, caplog):
    """The worker's loader only reads its folder, so a file elsewhere wouldn't help it."""
    touch(tmp_path / "vae" / "m.safetensors")
    folders = {
        "checkpoints": ([str(tmp_path / "checkpoints")], set[str]()),
        "vae": ([str(tmp_path / "vae")], set[str]()),
    }
    ui = {
        "nodes": [
            {"properties": {"models": [{"directory": "checkpoints", "name": "m.safetensors"}]}}
        ]
    }
    api = {"1": {"inputs": {"ckpt_name": "m.safetensors"}}}
    assert find_workflow_local_models(ui, api, folders) == ({}, {})
    assert "m.safetensors" in caplog.text


@pytest.mark.parametrize("hint", [("checkpoints", False), ("my_models", True)])
def test_find_workflow_local_models_searches_every_folder_when_an_unsure_hint_misses(
    tmp_path, hint
):
    """A guess from a subgraph, or a folder ComfyUI doesn't register, doesn't hide a model."""
    path = touch(tmp_path / "vae" / "m.safetensors")
    folders = {
        "checkpoints": ([str(tmp_path / "checkpoints")], set[str]()),
        "vae": ([str(path.parent)], set[str]()),
    }
    assert _search_local_models(folders, "m.safetensors", [hint]) == [(path, "vae")]


def test_stage_local_models_warns_when_it_copies_a_model(comfy, tmp_path, monkeypatch, caplog):
    """A copy, such as of a model on another drive, takes time and space, so it's logged."""
    source = touch(comfy["checkpoints"] / "m.safetensors")

    def fail_link(source, target):
        raise OSError("Invalid cross-device link")

    monkeypatch.setattr("deadline.comfyui_submitter.get_local_assets.os.link", fail_link)
    dest = tmp_path / "local_models"
    key = "checkpoints/m.safetensors"
    staged = stage_local_models({key: source}, {key: "checkpoints"}, dest)
    assert staged == {key: dest / "checkpoints" / "m.safetensors"}
    assert "Copying model 'm.safetensors' into the job bundle" in caplog.text
    assert "Invalid cross-device link" in caplog.text


def test_models_with_the_same_name_in_different_folders_are_each_attached(tmp_path):
    """A checkpoint and a LoRA can share a file name. Each loader needs its own."""
    checkpoint = touch(tmp_path / "checkpoints" / "shared.safetensors")
    lora = touch(tmp_path / "loras" / "shared.safetensors")
    folders = {
        "checkpoints": ([str(checkpoint.parent)], set[str]()),
        "loras": ([str(lora.parent)], set[str]()),
        "vae": ([str(tmp_path / "vae")], set[str]()),
    }
    api = {
        "1": {"inputs": {"ckpt_name": "shared.safetensors"}},
        "2": {"inputs": {"lora_name": "shared.safetensors"}},
    }
    expected = (
        {"checkpoints/shared.safetensors": checkpoint, "loras/shared.safetensors": lora},
        {"checkpoints/shared.safetensors": "checkpoints", "loras/shared.safetensors": "loras"},
    )
    # With the loaders' folders.
    ui = {
        "nodes": [
            {"properties": {"models": [{"directory": folder, "name": "shared.safetensors"}]}}
            for folder in ("checkpoints", "loras")
        ]
    }
    assert find_workflow_local_models(ui, api, folders) == expected
    # Without them, every folder with the name.
    assert find_workflow_local_models({"nodes": []}, api, folders) == expected
    # With one folder, only that one.
    ui = {
        "nodes": [
            {"properties": {"models": [{"directory": "loras", "name": "shared.safetensors"}]}}
        ]
    }
    assert find_workflow_local_models(ui, api, folders) == (
        {"loras/shared.safetensors": lora},
        {"loras/shared.safetensors": "loras"},
    )
    # Each is staged in its own folder.
    dest = tmp_path / "local_models"
    staged = stage_local_models(*expected, dest)
    assert staged == {
        "checkpoints/shared.safetensors": dest / "checkpoints" / "shared.safetensors",
        "loras/shared.safetensors": dest / "loras" / "shared.safetensors",
    }


def test_stage_local_node_packs_leaves_out_files_that_can_hold_credentials(comfy, tmp_path, caplog):
    """An API-backed pack can keep its keys in its folder, and anyone who can use the queue can
    read job attachments."""
    pack = comfy["custom_nodes"] / "api-nodes"
    # The pack's own code stays, even when it's named like a credentials file.
    kept_files = (
        "__init__.py",
        "config.json",
        "keys/README.md",
        ".env.py/__init__.py",
        "credentials.py",
        "auth/Credentials.py",
        "auth/.env.py",
        "web/credentials.js",
        "token.py",
        "tokenizer.json",
        "models/tokenizer_config.json",
    )
    for kept in kept_files:
        touch(pack / kept)
    for secret in (
        ".env",
        ".env.local",
        "certs/client.pem",
        "api.KEY",
        "credentials",
        "credentials.json",
        "credentials.cfg",
        "auth/Credentials.TXT",
        "secrets.json",
        ".git-credentials",
        ".npmrc",
        ".pypirc",
        "token",
        "hf_token",
        "api_key.txt",
        "putty.ppk",
        "id_dsa",
        "service_account.json",
        "client_secret_1234.json",
        "prod.env",
        ".envrc",
        ".boto",
        ".s3cfg",
        "kubeconfig",
        "keys/release.jks",
        ".htpasswd",
        "nested/deeper/hf_token",
    ):
        touch(pack / secret)
    dest = tmp_path / "local_nodes"
    stage_local_node_packs({"api-nodes": pack}, dest)
    files = sorted(
        p.relative_to(dest / "api-nodes").as_posix() for p in dest.rglob("*") if p.is_file()
    )
    assert files == sorted(kept_files)
    assert "can hold credentials" in caplog.text


def _symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError:
        pytest.skip("This system can't create symbolic links.")


def _staged_files(dest: Path, name: str) -> list[str]:
    return sorted(
        p.relative_to(dest / name).as_posix() for p in (dest / name).rglob("*") if p.is_file()
    )


def test_stage_local_node_packs_keeps_links_inside_the_pack_only(comfy, tmp_path, caplog):
    """A link's content is attached, so a link out of the pack, or to a credentials file, isn't."""
    pack = comfy["custom_nodes"] / "linked-nodes"
    touch(pack / "__init__.py")
    touch(pack / "data" / "styles.csv")
    touch(pack / ".env")
    outside = touch(tmp_path / "home" / "secret.txt")
    touch(tmp_path / "shared" / "models" / "big.safetensors")
    _symlink(pack / "notes.txt", outside)
    _symlink(pack / "models", tmp_path / "shared" / "models")
    _symlink(pack / "config.txt", pack / ".env")
    _symlink(pack / "styles.csv", pack / "data" / "styles.csv")
    _symlink(pack / "data" / "loop", pack)
    dest = tmp_path / "local_nodes"
    stage_local_node_packs({"linked-nodes": pack}, dest)
    assert _staged_files(dest, "linked-nodes") == ["__init__.py", "data/styles.csv", "styles.csv"]
    assert caplog.text.count("it links outside the pack") == 2
    assert "config.txt out of the job: it can hold credentials" in caplog.text
    assert "loop out of the job: it links to a folder in the pack" in caplog.text


def test_stage_local_node_packs_keeps_relative_links(comfy, tmp_path, monkeypatch):
    """A relative link is read from its own folder, not from the server's working directory."""
    pack = comfy["custom_nodes"] / "linked-nodes"
    touch(pack / "nodes" / "real.py")
    try:
        (pack / "nodes" / "alias.py").symlink_to("real.py")
    except OSError:
        pytest.skip("This system can't create symbolic links.")
    monkeypatch.chdir(tmp_path)
    dest = tmp_path / "local_nodes"
    stage_local_node_packs({"linked-nodes": pack}, dest)
    assert _staged_files(dest, "linked-nodes") == ["nodes/alias.py", "nodes/real.py"]


def test_stage_local_node_packs_leaves_out_links_into_left_out_folders(comfy, tmp_path, caplog):
    """A link can't bring back what's left out, such as .git/config, which can hold a token."""
    pack = comfy["custom_nodes"] / "linked-nodes"
    touch(pack / "__init__.py")
    touch(pack / ".git" / "config")
    touch(pack / "__pycache__" / "nodes.cpython-312.pyc")
    _symlink(pack / "repo", pack / ".git")
    _symlink(pack / "git_config.txt", pack / ".git" / "config")
    _symlink(pack / "cache.bin", pack / "__pycache__" / "nodes.cpython-312.pyc")
    dest = tmp_path / "local_nodes"
    stage_local_node_packs({"linked-nodes": pack}, dest)
    assert _staged_files(dest, "linked-nodes") == ["__init__.py"]
    assert "repo out of the job: it links to a folder in the pack" in caplog.text
    assert caplog.text.count("it links to a file that's left out") == 2


def test_stage_local_node_packs_stops_at_links_between_sibling_folders(comfy, tmp_path, caplog):
    """Folders that link to each other would otherwise be copied into each other again and again."""
    pack = comfy["custom_nodes"] / "linked-nodes"
    touch(pack / "a" / "a.py")
    touch(pack / "b" / "b.py")
    _symlink(pack / "a" / "to_b", pack / "b")
    _symlink(pack / "b" / "to_a", pack / "a")
    dest = tmp_path / "local_nodes"
    stage_local_node_packs({"linked-nodes": pack}, dest)
    assert _staged_files(dest, "linked-nodes") == ["a/a.py", "b/b.py"]
    assert caplog.text.count("it links to a folder in the pack") == 2


def test_stage_local_node_packs_refuses_names_that_differ_only_in_case(comfy, tmp_path, caplog):
    """On a file system that ignores case, both packs would be merged into one folder."""
    first = touch(tmp_path / "root1" / "MyPack" / "first.py").parent
    second = touch(tmp_path / "root2" / "mypack" / "second.py").parent
    dest = tmp_path / "local_nodes"
    assert stage_local_node_packs({"MyPack": first, "mypack": second}, dest) == {
        "MyPack": dest / "MyPack"
    }
    assert _staged_files(dest, "MyPack") == ["first.py"]
    assert "its name and 'MyPack' differ in case alone" in caplog.text


def test_node_pack_size_counts_what_staging_attaches(comfy, tmp_path):
    pack = comfy["custom_nodes"] / "studio-nodes"
    (pack / "nodes").mkdir(parents=True)
    (pack / "__init__.py").write_bytes(b"x" * 10)
    (pack / "nodes" / "big.bin").write_bytes(b"x" * 1000)
    touch(pack / ".git" / "objects" / "pack.idx")
    touch(pack / ".env")
    _symlink(pack / "nodes" / "loop", pack)
    assert node_pack_size(pack) == NodePackSize(bytes=1010, files=2, complete=True)
    assert node_pack_size(touch(tmp_path / "single.py")) == NodePackSize(1, 1, True)


def test_node_pack_size_stops_counting_a_huge_pack(comfy, monkeypatch):
    """A pack that downloads models into its own folder can store thousands of files."""
    monkeypatch.setattr("deadline.comfyui_submitter.get_local_assets.MAX_COUNTED_NODE_FILES", 3)
    pack = comfy["custom_nodes"] / "studio-nodes"
    for index in range(5):
        touch(pack / "ckpts" / f"{index}.bin")
    assert node_pack_size(pack) == NodePackSize(bytes=3, files=3, complete=False)


@pytest.mark.skipif(sys.platform == "win32", reason="Named pipes are POSIX only.")
def test_stage_local_node_packs_leaves_out_special_files(comfy, tmp_path, caplog):
    """Copying a named pipe would wait for a writer forever."""
    pack = comfy["custom_nodes"] / "pipe-nodes"
    touch(pack / "__init__.py")
    # Windows has no os.mkfifo, and mypy checks this file for Windows too.
    if sys.platform != "win32":
        os.mkfifo(pack / "events")
    dest = tmp_path / "local_nodes"
    stage_local_node_packs({"pipe-nodes": pack}, dest)
    assert _staged_files(dest, "pipe-nodes") == ["__init__.py"]
    assert "events out of the job: it isn't a file or a folder" in caplog.text


def test_stage_local_node_packs_links_the_file_a_link_points_to(comfy, tmp_path, monkeypatch):
    """Windows would hard link the link itself, so the bundle would hold a link, not the file."""
    pack = comfy["custom_nodes"] / "linked-nodes"
    nodes = touch(pack / "nodes.py")
    _symlink(pack / "nodes_link.py", nodes)
    linked = []
    real_link = os.link

    def link(source, target):
        linked.append((Path(source), Path(target).name))
        real_link(source, target)

    monkeypatch.setattr("deadline.comfyui_submitter.get_local_assets.os.link", link)
    stage_local_node_packs({"linked-nodes": pack}, tmp_path / "local_nodes")
    assert sorted(linked, key=lambda item: item[1]) == [
        (nodes.resolve(), "nodes.py"),
        (nodes.resolve(), "nodes_link.py"),
    ]


@pytest.mark.skipif(sys.platform != "win32", reason="Directory junctions are Windows only.")
def test_stage_local_node_packs_leaves_out_junctions_outside_the_pack(comfy, tmp_path, caplog):
    """Python doesn't count a directory junction as a symbolic link, so it's resolved as well."""
    pack = comfy["custom_nodes"] / "junction-nodes"
    touch(pack / "__init__.py")
    shared = tmp_path / "shared" / "models"
    touch(shared / "big.safetensors")
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(shared), str(pack / "models"))
    dest = tmp_path / "local_nodes"
    stage_local_node_packs({"junction-nodes": pack}, dest)
    assert _staged_files(dest, "junction-nodes") == ["__init__.py"]
    assert "models out of the job: it links outside the pack" in caplog.text


def test_stage_local_node_packs_follows_a_pack_that_is_a_link(comfy, tmp_path):
    """A pack installed as a link to a checkout elsewhere is attached from the checkout."""
    checkout = tmp_path / "dev" / "studio-nodes"
    touch(checkout / "__init__.py")
    touch(checkout / "nodes" / "fox.py")
    _symlink(comfy["custom_nodes"] / "studio-nodes", checkout)
    dest = tmp_path / "local_nodes"
    stage_local_node_packs({"studio-nodes": comfy["custom_nodes"] / "studio-nodes"}, dest)
    assert _staged_files(dest, "studio-nodes") == ["__init__.py", "nodes/fox.py"]


def test_stage_local_node_packs_warns_when_it_copies_a_file(comfy, tmp_path, monkeypatch, caplog):
    """A copy, such as of a pack on another drive, takes time and space, so it's logged."""
    pack = comfy["custom_nodes"] / "studio-nodes"
    touch(pack / "__init__.py")
    touch(pack / "nodes.py")

    def fail_link(source, target):
        raise OSError("Invalid cross-device link")

    monkeypatch.setattr("deadline.comfyui_submitter.get_local_assets.os.link", fail_link)
    dest = tmp_path / "local_nodes"
    assert stage_local_node_packs({"studio-nodes": pack}, dest) == {
        "studio-nodes": dest / "studio-nodes"
    }
    assert (dest / "studio-nodes" / "__init__.py").is_file()
    # Once for the pack, not for each file.
    [record] = [r for r in caplog.records if "Copied" in r.getMessage()]
    assert "Copied 2 files of custom node pack 'studio-nodes'" in record.getMessage()
