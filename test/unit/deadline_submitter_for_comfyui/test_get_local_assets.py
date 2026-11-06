# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import shutil
from pathlib import Path

import pytest
import yaml

from deadline.comfyui_submitter.get_local_assets import (
    LocalAssets,
    find_local_model,
    find_workflow_local_models,
    get_local_assets,
    save_asset_references,
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


def test_save_asset_references_skips_models(comfy, tmp_path):
    touch(comfy["checkpoints"] / "a.safetensors")
    image = touch(comfy["input"] / "cat.png")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    saved = save_asset_references(
        {"1": node(ckpt_name="a.safetensors", image="cat.png")},
        bundle,
        input_dir=comfy["input"],
    )
    data = yaml.safe_load(saved.read_text())
    assert data["assetReferences"]["inputs"]["filenames"] == [str(image)]


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
    assert found == {"custom.safetensors": path}
    assert directories == {"custom.safetensors": "checkpoints"}
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
        {"kept.safetensors": kept, "dropped.safetensors": dropped},
        {"kept.safetensors": "checkpoints", "dropped.safetensors": "loras"},
        dest,
    )
    assert staged == {"kept.safetensors": dest / "checkpoints" / "kept.safetensors"}
    assert not (dest / "loras" / "dropped.safetensors").exists()


def test_stage_local_models_uses_comfy_folder_layout(comfy, tmp_path):
    source = touch(comfy["checkpoints"] / "nested" / "custom.safetensors")
    secret = touch(tmp_path / "secret.safetensors")
    dest = tmp_path / "local_models"
    staged = stage_local_models(
        {"nested/custom.safetensors": source, "../../secret.safetensors": secret},
        {"nested/custom.safetensors": "checkpoints", "../../secret.safetensors": "checkpoints"},
        dest,
    )
    expected = dest / "checkpoints" / "nested" / "custom.safetensors"
    assert staged == {"nested/custom.safetensors": expected}
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


def test_save_asset_references_includes_extra_directories(comfy, tmp_path):
    pack = tmp_path / "local_nodes" / "studio-nodes"
    pack.mkdir(parents=True)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    saved = save_asset_references({}, bundle, input_dir=comfy["input"], extra_directories=[pack])
    inputs = yaml.safe_load(saved.read_text())["assetReferences"]["inputs"]
    assert inputs == {"filenames": [], "directories": [str(pack)]}


def test_save_asset_references_includes_extra_model_files(comfy, tmp_path):
    image = touch(comfy["input"] / "cat.png")
    model = touch(comfy["checkpoints"] / "custom.safetensors")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    saved = save_asset_references(
        {"1": node(image="cat.png", ckpt_name=str(model))},
        bundle,
        input_dir=comfy["input"],
        extra_filenames=[model],
    )
    names = yaml.safe_load(saved.read_text())["assetReferences"]["inputs"]["filenames"]
    assert names == sorted([str(image), str(model)])


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
    assert found == {name: checkpoints / name for name in names}
    assert directories == {name: "checkpoints" for name in names}


def test_find_workflow_local_models_reads_properties_on_a_node_inside_a_subgraph(tmp_path):
    """A subgraph node's properties name the folder. A matching URL is not staged."""
    skipped = touch(tmp_path / "checkpoints" / "sd.safetensors")
    kept = touch(tmp_path / "loras" / "style.safetensors")
    folders = {
        "checkpoints": ([str(skipped.parent)], {".safetensors"}),
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
    assert found == {"style.safetensors": kept}
    assert directories == {"style.safetensors": "loras"}
    dest = tmp_path / "local_models"
    staged = stage_local_models(found, directories, dest)
    assert staged == {"style.safetensors": dest / "loras" / "style.safetensors"}
    assert not (dest / "checkpoints" / "sd.safetensors").exists()


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
    assert found == {"other.ckpt": other, "style.pt": path}
    assert directories == {"other.ckpt": "checkpoints", "style.pt": "loras"}


def test_save_asset_references(comfy, tmp_path):
    image = touch(comfy["input"] / "cat.png")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    saved = save_asset_references({"1": node(image="cat.png")}, bundle, input_dir=comfy["input"])
    assert saved == bundle / "asset_references.yaml"
    data = yaml.safe_load(saved.read_text())
    assert data["assetReferences"]["inputs"]["filenames"] == [str(image)]
