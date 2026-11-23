# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Installer transaction and ownership tests; no third-party assets are required."""

from __future__ import annotations

import errno
import importlib.util
import json
import os
import py_compile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hooks = load_script("installer_hooks", ROOT / "installer/install.py")
builder = load_script("installer_builder", ROOT / "scripts/build_installer.py")


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path, Path]:
    comfy = tmp_path / "Artist folder" / "ComfyUI"
    (comfy / "custom_nodes").mkdir(parents=True)
    (comfy / "main.py").write_text("# customer supplied\n")
    payload = tmp_path / "payload"
    for name in (
        "__init__.py",
        "src/deadline/comfyui_submitter/bridge.py",
        "src/deadline/comfyui_submitter/comfy_cli_job_template.yaml",
        "src/deadline/comfyui_submitter/web/deadline_submit.js",
        "src/deadline/comfyui_submitter/web/deadline_ui.js",
    ):
        path = payload / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"packaged {name}\n")
    runtime = tmp_path / "Deadline installation" / "runtime"
    python, cli = hooks.runtime_paths(runtime)
    python.parent.mkdir(parents=True, exist_ok=True)
    cli.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("runtime")
    cli.write_text("cli")
    monkeypatch.setattr(hooks, "prepare_runtime", lambda path: hooks.runtime_paths(path))
    return comfy, payload, runtime, runtime.parent / "installation.json"


def test_offline_install_preserves_comfyui_and_records_runtime(setup):
    comfy, payload, runtime, state = setup
    result = hooks.install(payload, runtime, comfy, state, "1.0")
    assert result == comfy / "custom_nodes" / hooks.EXTENSION_NAME
    assert (comfy / "main.py").read_text() == "# customer supplied\n"
    config = json.loads((result / hooks.CONFIG_NAME).read_text())
    python, cli = hooks.runtime_paths(runtime)
    assert config["deadline_python"] == str(python)
    assert config["deadline_cli"] == str(cli)
    assert json.loads(state.read_text())["files"]["__init__.py"] == hooks.digest(
        payload / "__init__.py"
    )
    if os.name != "nt":
        assert (result / hooks.CONFIG_NAME).stat().st_mode & 0o777 == 0o600


def test_upgrade_preserves_customer_config_and_extra_files(setup):
    comfy, payload, runtime, state = setup
    destination = hooks.install(payload, runtime, comfy, state, "1.0")
    config = destination / hooks.CONFIG_NAME
    config.write_text(json.dumps({"conda_channels": "studio-channel", "custom": True}))
    (destination / "customer.txt").write_text("keep")
    (payload / "__init__.py").write_text("new plugin")
    hooks.install(payload, runtime, comfy, state, "2.0")
    assert (destination / "__init__.py").read_text() == "new plugin"
    assert (destination / "customer.txt").read_text() == "keep"
    assert json.loads(config.read_text())["conda_channels"] == "studio-channel"
    assert json.loads(config.read_text())["custom"] is True


def test_runtime_failure_leaves_existing_extension_untouched(setup, monkeypatch):
    comfy, payload, runtime, state = setup
    destination = hooks.install(payload, runtime, comfy, state, "1.0")
    before = (destination / "__init__.py").read_text()
    (payload / "__init__.py").write_text("new")

    def fail(_runtime):
        raise hooks.InstallError("runtime broken")

    monkeypatch.setattr(hooks, "prepare_runtime", fail)
    with pytest.raises(hooks.InstallError, match="runtime broken"):
        hooks.install(payload, runtime, comfy, state, "2.0")
    assert (destination / "__init__.py").read_text() == before
    assert json.loads(state.read_text())["version"] == "1.0"


def test_state_write_failure_rolls_back_extension(setup, monkeypatch):
    comfy, payload, runtime, state = setup
    destination = hooks.install(payload, runtime, comfy, state, "1.0")
    (payload / "__init__.py").write_text("new")
    original = hooks.write_json

    def fail(path, data):
        if path == state:
            raise OSError("disk full")
        return original(path, data)

    monkeypatch.setattr(hooks, "write_json", fail)
    with pytest.raises(OSError, match="disk full"):
        hooks.install(payload, runtime, comfy, state, "2.0")
    assert (destination / "__init__.py").read_text() == "packaged __init__.py\n"
    assert not list(destination.parent.glob(".deadline-cloud-install-*"))


def test_uninstall_preserves_settings_modified_files_and_other_nodes(setup):
    comfy, payload, runtime, state = setup
    destination = hooks.install(payload, runtime, comfy, state, "1.0")
    (destination / "__init__.py").write_text("customer modification")
    (destination / "customer.txt").write_text("keep")
    other = comfy / "custom_nodes" / "other-node"
    other.mkdir()
    (other / "__init__.py").write_text("keep")
    assert hooks.uninstall(state) == ["__init__.py"]
    assert (destination / "__init__.py").read_text() == "customer modification"
    assert (destination / "customer.txt").read_text() == "keep"
    assert not (destination / hooks.CONFIG_NAME).exists()
    assert (other / "__init__.py").read_text() == "keep"
    assert not (destination / "src/deadline/comfyui_submitter/bridge.py").exists()
    assert not state.exists()
    assert hooks.uninstall(state) == []


@pytest.mark.skipif(os.name != "nt", reason="Directory junctions require Windows")
def test_uninstall_does_not_enter_customer_directory_junction(setup, tmp_path):
    import subprocess

    comfy, payload, runtime, state = setup
    destination = hooks.install(payload, runtime, comfy, state, "1.0")
    outside = tmp_path / "Customer directory outside integration"
    empty = outside / "Customer empty folder" / "Nested empty folder"
    empty.mkdir(parents=True)
    sentinel = outside / "customer.txt"
    sentinel.write_text("keep customer files", encoding="utf-8")
    junction = destination / "Customer linked folder"
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$ErrorActionPreference='Stop'; New-Item -ItemType Junction "
                "-Path $env:COMFYUI_TEST_JUNCTION_PATH "
                "-Target $env:COMFYUI_TEST_JUNCTION_TARGET | Out-Null"
            ),
        ],
        env={
            **os.environ,
            "COMFYUI_TEST_JUNCTION_PATH": str(junction),
            "COMFYUI_TEST_JUNCTION_TARGET": str(outside),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert junction.is_junction()
    try:
        assert hooks.uninstall(state) == []
        assert empty.is_dir()
        assert sentinel.read_text(encoding="utf-8") == "keep customer files"
        assert junction.is_junction()
        assert hooks.uninstalled_receipt(destination)
        assert not state.exists()
    finally:
        junction.rmdir()


def test_uninstall_rejects_traversal_before_deleting_anything(setup):
    comfy, payload, runtime, state = setup
    destination = hooks.install(payload, runtime, comfy, state, "1.0")
    manifest = json.loads(state.read_text())
    manifest["files"]["../outside"] = "bad"
    state.write_text(json.dumps(manifest))
    (destination / hooks.MARKER_NAME).write_text(json.dumps(manifest))
    with pytest.raises(hooks.InstallError, match="invalid file path"):
        hooks.uninstall(state)
    assert (destination / "__init__.py").exists()


def test_install_rejects_incomplete_payload(setup):
    comfy, payload, runtime, state = setup
    (payload / "src/deadline/comfyui_submitter/web/deadline_submit.js").unlink()
    with pytest.raises(hooks.InstallError, match="incomplete"):
        hooks.install(payload, runtime, comfy, state, "1.0")
    assert not state.exists()
    assert not (comfy / "custom_nodes" / hooks.EXTENSION_NAME).exists()


def test_install_rejects_unknown_destination(setup):
    comfy, payload, runtime, state = setup
    destination = comfy / "custom_nodes" / hooks.EXTENSION_NAME
    destination.mkdir()
    (destination / "user.txt").write_text("keep")
    with pytest.raises(hooks.InstallError, match="not the Deadline extension"):
        hooks.install(payload, runtime, comfy, state, "1.0")
    assert (destination / "user.txt").read_text() == "keep"


def test_second_comfyui_cannot_orphan_managed_install(setup):
    comfy, payload, runtime, state = setup
    hooks.install(payload, runtime, comfy, state, "1.0")
    other = comfy.parent / "second"
    (other / "custom_nodes").mkdir(parents=True)
    (other / "main.py").write_text("existing")
    with pytest.raises(hooks.InstallError, match="another ComfyUI"):
        hooks.install(payload, runtime, other, state, "2.0")
    assert not (other / "custom_nodes" / hooks.EXTENSION_NAME).exists()


@pytest.mark.skipif(os.name == "nt", reason="Symlink permissions differ on Windows")
def test_upgrade_rejects_link_that_would_modify_outside_directory(setup, tmp_path):
    comfy, payload, runtime, state = setup
    destination = hooks.install(payload, runtime, comfy, state, "1.0")
    outside = tmp_path / "outside.py"
    outside.write_text("keep")
    (destination / "__init__.py").unlink()
    (destination / "__init__.py").symlink_to(outside)
    with pytest.raises(hooks.InstallError, match="symbolic link"):
        hooks.install(payload, runtime, comfy, state, "2.0")
    assert outside.read_text() == "keep"


@pytest.mark.skipif(os.name == "nt", reason="Symlink permissions differ on Windows")
def test_runtime_copy_rewrites_absolute_internal_links(tmp_path):
    runtime = tmp_path / "original"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin/python3.13").write_text("python")
    (runtime / "bin/python3").symlink_to(runtime / "bin/python3.13")
    copied = tmp_path / "relocated"
    builder.copy_runtime(runtime, copied)
    assert not Path(os.readlink(copied / "bin/python3")).is_absolute()
    assert (copied / "bin/python3").resolve() == copied / "bin/python3.13"


@pytest.mark.skipif(os.name == "nt", reason="Symlink permissions differ on Windows")
def test_runtime_copy_rejects_external_links(tmp_path):
    runtime = tmp_path / "original"
    runtime.mkdir()
    (runtime / "python3").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="outside"):
        builder.copy_runtime(runtime, tmp_path / "output")


def test_runtime_copy_rejects_nonportable_venv(tmp_path):
    runtime = tmp_path / "venv"
    runtime.mkdir()
    (runtime / "pyvenv.cfg").write_text("home=/somewhere")
    with pytest.raises(ValueError, match="virtual environment"):
        builder.copy_runtime(runtime, tmp_path / "output")


@pytest.mark.parametrize(
    "name", ["../outside", "/outside", "\\outside", "C:outside", "C:\\outside", "file:stream", ""]
)
def test_manifest_rejects_windows_and_posix_escape_names(tmp_path, name):
    with pytest.raises(hooks.InstallError, match="invalid file path"):
        hooks.safe_file(tmp_path, name)


def test_clean_uninstall_removes_generated_config_and_directory(setup):
    comfy, payload, runtime, state = setup
    destination = hooks.install(payload, runtime, comfy, state, "1.0")
    assert hooks.uninstall(state) == []
    assert not destination.exists()


def test_old_uninstaller_preserves_customer_replacement(setup):
    comfy, payload, runtime, state = setup
    destination = hooks.install(payload, runtime, comfy, state, "1.0")
    (destination / hooks.MARKER_NAME).unlink()
    assert hooks.uninstall(state) == ["extension now managed outside this installer"]
    assert (destination / "__init__.py").exists()
    assert not state.exists()


def test_old_uninstaller_preserves_newer_ownership(setup):
    comfy, payload, runtime, state = setup
    destination = hooks.install(payload, runtime, comfy, state, "1.0")
    marker = destination / hooks.MARKER_NAME
    changed = json.loads(marker.read_text())
    changed["installation_id"] = "another installer"
    marker.write_text(json.dumps(changed))
    hooks.uninstall(state)
    assert (destination / "__init__.py").exists()


@pytest.mark.parametrize(
    "target,system,machine",
    [("Linux", "Linux", "aarch64"), ("Windows", "Windows", "arm64"), ("MacOS", "Linux", "x86_64")],
)
def test_builder_rejects_mislabeled_runtime_architecture(target, system, machine):
    with pytest.raises(ValueError):
        builder.validate_architecture({"system": system, "machine": machine}, target)


@pytest.mark.skipif(os.name == "nt", reason="macOS installer symlink validation")
def test_install_rejects_linked_custom_nodes_before_changing_files(setup):
    comfy, payload, runtime, state = setup
    custom_nodes = comfy / "custom_nodes"
    custom_nodes.rmdir()
    target = runtime.parent / "payload"
    target.mkdir()
    (target / "customer.txt").write_text("keep")
    custom_nodes.symlink_to(target, target_is_directory=True)
    with pytest.raises(hooks.InstallError, match="custom_nodes folder is a symbolic link"):
        hooks.install(payload, runtime, comfy, state, "1.0")
    assert (target / "customer.txt").read_text() == "keep"
    assert not state.exists()
    assert not (target / hooks.EXTENSION_NAME).exists()


@pytest.mark.skipif(os.name == "nt", reason="macOS installer symlink validation")
def test_old_uninstaller_preserves_extension_behind_changed_custom_nodes_link(setup):
    comfy, payload, runtime, state = setup
    destination = hooks.install(payload, runtime, comfy, state, "1.0")
    custom_nodes = comfy / "custom_nodes"
    moved = comfy / "customer-managed-nodes"
    custom_nodes.rename(moved)
    custom_nodes.symlink_to(moved, target_is_directory=True)
    assert hooks.uninstall(state) == ["extension path now contains a symbolic link"]
    assert (destination / "__init__.py").exists()
    assert not state.exists()


@pytest.mark.parametrize("origin", ["edited-native", "existing-source"])
def test_uninstall_preserves_customer_config_after_upgrade(setup, origin):
    comfy, payload, runtime, state = setup
    destination = comfy / "custom_nodes" / hooks.EXTENSION_NAME
    if origin == "edited-native":
        hooks.install(payload, runtime, comfy, state, "1.0")
    else:
        destination.mkdir()
        (destination / "__init__.py").write_text("source setup")
        (destination / "src/deadline/comfyui_submitter").mkdir(parents=True)
    configuration = destination / hooks.CONFIG_NAME
    configuration.write_text(json.dumps({"conda_channels": "studio", "custom": True}))
    hooks.install(payload, runtime, comfy, state, "2.0")
    hooks.install(payload, runtime, comfy, state, "3.0")
    assert json.loads(state.read_text())["preserve_configuration"] is True
    hooks.uninstall(state)
    assert json.loads(configuration.read_text()) == {"conda_channels": "studio", "custom": True}
    assert not (destination / "src/deadline/comfyui_submitter/bridge.py").exists()


def test_unchanged_generated_config_still_removed_after_upgrade(setup):
    comfy, payload, runtime, state = setup
    destination = hooks.install(payload, runtime, comfy, state, "1.0")
    hooks.install(payload, runtime, comfy, state, "2.0")
    assert json.loads(state.read_text())["preserve_configuration"] is False
    assert hooks.uninstall(state) == []
    assert not destination.exists()


def test_reinstall_preserved_settings_and_files_after_edited_upgrade(setup, tmp_path):
    comfy, payload, runtime, state = setup
    destination = hooks.install(payload, runtime, comfy, state, "1.0")
    config = destination / hooks.CONFIG_NAME
    config.write_text(json.dumps({"conda_channels": "studio", "custom": True}))
    (destination / "customer.txt").write_text("keep")
    hooks.install(payload, runtime, comfy, state, "2.0")
    old_state = tmp_path / "old-installer-state.json"
    old_state.write_text(state.read_text())
    hooks.uninstall(state)
    receipt = destination / hooks.RECEIPT_NAME
    assert json.loads(receipt.read_text()) == {
        "product": hooks.EXTENSION_NAME,
        "schema": 1,
        "status": "uninstalled",
        "extension": str(destination.resolve()),
    }
    hooks.install(payload, runtime, comfy, state, "3.0")
    assert not receipt.exists()
    assert json.loads(config.read_text())["conda_channels"] == "studio"
    assert json.loads(config.read_text())["custom"] is True
    assert (destination / "customer.txt").read_text() == "keep"
    assert hooks.uninstall(old_state) == ["extension now managed outside this installer"]
    assert (destination / "__init__.py").exists()
    assert state.exists()


@pytest.mark.parametrize(
    "key,value",
    [("product", "other"), ("schema", 2), ("status", "installed"), ("extension", "/different")],
)
def test_reinstall_rejects_invalid_uninstall_receipt(setup, key, value):
    comfy, payload, runtime, state = setup
    destination = comfy / "custom_nodes" / hooks.EXTENSION_NAME
    destination.mkdir()
    receipt = {
        "product": hooks.EXTENSION_NAME,
        "schema": 1,
        "status": "uninstalled",
        "extension": str(destination.resolve()),
    }
    receipt[key] = value
    (destination / hooks.RECEIPT_NAME).write_text(json.dumps(receipt))
    (destination / "customer.txt").write_text("keep")
    with pytest.raises(hooks.InstallError, match="invalid uninstall receipt"):
        hooks.install(payload, runtime, comfy, state, "1.0")
    assert (destination / "customer.txt").read_text() == "keep"
    assert not state.exists()


@pytest.mark.skipif(os.name == "nt", reason="macOS installer symlink validation")
def test_reinstall_rejects_linked_uninstall_receipt(setup, tmp_path):
    comfy, payload, runtime, state = setup
    destination = comfy / "custom_nodes" / hooks.EXTENSION_NAME
    destination.mkdir()
    target = tmp_path / "receipt.json"
    target.write_text(
        json.dumps(
            {
                "product": hooks.EXTENSION_NAME,
                "schema": 1,
                "status": "uninstalled",
                "extension": str(destination.resolve()),
            }
        )
    )
    (destination / hooks.RECEIPT_NAME).symlink_to(target)
    with pytest.raises(hooks.InstallError, match="linked uninstall receipt"):
        hooks.install(payload, runtime, comfy, state, "1.0")
    assert target.exists()
    assert not state.exists()


def test_uninstall_receipt_write_failure_retains_retry_ownership(setup, monkeypatch):
    comfy, payload, runtime, state = setup
    destination = hooks.install(payload, runtime, comfy, state, "1.0")
    (destination / "customer.txt").write_text("keep")
    original = hooks.write_json

    def fail(path, data):
        if path.name == hooks.RECEIPT_NAME:
            raise OSError("disk full")
        return original(path, data)

    monkeypatch.setattr(hooks, "write_json", fail)
    with pytest.raises(OSError, match="disk full"):
        hooks.uninstall(state)
    assert json.loads((destination / hooks.MARKER_NAME).read_text()) == json.loads(
        state.read_text()
    )
    monkeypatch.setattr(hooks, "write_json", original)
    hooks.uninstall(state)
    assert not state.exists()
    assert hooks.uninstalled_receipt(destination)
    assert (destination / "customer.txt").read_text() == "keep"


def make_launcher(home: Path, source: Path, name: str = "ComfyUI Preview") -> tuple[Path, Path]:
    folder = home / name
    nodes = folder / "runtime/custom_nodes"
    nodes.mkdir(parents=True)
    (folder / "comfyui-source.txt").write_text(str(source), encoding="utf-8")
    (folder / "launch.sh").write_text(
        'ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"\n'
        'COMFYUI_SOURCE="$(cat "$ROOT/comfyui-source.txt")"\n'
        'exec "$ROOT/venv/bin/python" "$COMFYUI_SOURCE/main.py" '
        '--base-directory "$ROOT/runtime" "$@"\n',
        encoding="utf-8",
    )
    return folder, nodes


def test_selected_launcher_validation_is_read_only_with_split_paths(setup, tmp_path, monkeypatch):
    comfy, _, _, _ = setup
    launcher, nodes = make_launcher(tmp_path / "Customer home", comfy)

    def forbid_access(*args, **kwargs):
        raise AssertionError(
            "Selected-folder validation must not scan folders or probe by writing."
        )

    monkeypatch.setattr(hooks.tempfile, "NamedTemporaryFile", forbid_access)
    monkeypatch.setattr(hooks.os, "scandir", forbid_access)
    assert hooks.resolve_layout(launcher) == (comfy, nodes)
    assert not (nodes / hooks.EXTENSION_NAME).exists()


@pytest.mark.parametrize("base", ['"$(touch sentinel)/runtime"', '"$OTHER/runtime"'])
def test_selected_launcher_dynamic_paths_are_rejected_without_execution(setup, tmp_path, base):
    comfy, _, _, _ = setup
    folder, _ = make_launcher(tmp_path / "Home", comfy)
    (folder / "launch.sh").write_text(
        'exec python "' + str(comfy / "main.py") + '" --base-directory ' + base
    )
    with pytest.raises(hooks.InstallError, match="dynamic paths"):
        hooks.resolve_layout(folder)
    assert not (folder / "sentinel").exists()


def test_split_layout_install_upgrade_uninstall_reinstall_preserves_customer_files(setup, tmp_path):
    comfy, payload, runtime, state = setup
    source_nodes = comfy / "custom_nodes"
    (source_nodes / "unrelated.txt").write_text("source stays untouched")
    launcher, nodes = make_launcher(tmp_path / "Home", comfy)
    destination = hooks.install(payload, runtime, launcher, state, "1.0")
    assert destination == nodes / hooks.EXTENSION_NAME
    manifest = json.loads(state.read_text())
    assert manifest["comfyui_source"] == str(comfy)
    assert manifest["custom_nodes"] == str(nodes)
    config = destination / hooks.CONFIG_NAME
    config.write_text(json.dumps({"conda_channels": "studio"}))
    (destination / "customer.txt").write_text("keep")
    hooks.install(payload, runtime, comfy, state, "2.0", custom_nodes=nodes)
    hooks.uninstall(state)
    assert hooks.uninstalled_receipt(destination)
    hooks.install(payload, runtime, launcher, state, "3.0")
    assert json.loads(config.read_text())["conda_channels"] == "studio"
    assert not (destination / hooks.RECEIPT_NAME).exists()
    hooks.uninstall(state)
    assert (destination / "customer.txt").read_text() == "keep"
    assert (source_nodes / "unrelated.txt").read_text() == "source stays untouched"
    assert not (source_nodes / hooks.EXTENSION_NAME).exists()


def test_split_source_without_local_nodes_uses_explicit_base(setup, tmp_path):
    comfy, _, _, _ = setup
    (comfy / "custom_nodes").rmdir()
    base = tmp_path / "Active base"
    (base / "custom_nodes").mkdir(parents=True)
    assert hooks.resolve_layout(comfy, base_directory=base) == (comfy, base / "custom_nodes")
    with pytest.raises(hooks.InstallError, match="active custom_nodes"):
        hooks.resolve_layout(comfy)


def test_runtime_overlap_with_separate_active_nodes_rejected_before_install(setup):
    comfy, payload, runtime, state = setup
    nodes = runtime.parent / "custom_nodes"
    nodes.mkdir()
    with pytest.raises(hooks.InstallError, match="separate"):
        hooks.install(payload, runtime, comfy, state, "1.0", custom_nodes=nodes)
    assert not state.exists()
    assert not (nodes / hooks.EXTENSION_NAME).exists()


@pytest.mark.skipif(os.name == "nt", reason="macOS path alias guard")
def test_split_nodes_symlink_ancestor_rejected(setup, tmp_path):
    comfy, payload, runtime, state = setup
    target = tmp_path / "Customer nodes"
    (target / "custom_nodes").mkdir(parents=True)
    link = tmp_path / "Alias"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(hooks.InstallError, match="symbolic links"):
        hooks.install(payload, runtime, comfy, state, "1.0", custom_nodes=link / "custom_nodes")
    assert not state.exists()
    assert not (target / "custom_nodes" / hooks.EXTENSION_NAME).exists()


def test_layout_output_matches_selected_pair(setup, tmp_path, capsys):
    comfy, _, _, _ = setup
    launcher, nodes = make_launcher(tmp_path / "Home", comfy)
    output = tmp_path / "Private validation output"
    assert hooks.main(["resolve", "--comfyui", str(launcher), "--output-dir", str(output)]) == 0
    result = {"schema": 1, "source": str(comfy), "custom_nodes": str(nodes)}
    assert json.loads(capsys.readouterr().out) == result
    assert json.loads((output / "layout.json").read_text()) == result
    assert (output / "source.txt").read_text() == str(comfy)
    assert (output / "custom_nodes.txt").read_text() == str(nodes)
    assert {item.name for item in output.iterdir()} == {
        "layout.json",
        "source.txt",
        "custom_nodes.txt",
    }


def test_launcher_ignores_incidental_base_flags_and_honors_explicit_override(setup, tmp_path):
    comfy, _, _, _ = setup
    launcher, nodes = make_launcher(tmp_path / "Home", comfy)
    with (launcher / "launch.sh").open("a") as stream:
        stream.write('echo --base-directory "/unrelated/base"\n')
    assert hooks.resolve_layout(launcher) == (comfy, nodes)
    (launcher / "launch.sh").write_text(
        'exec python "$(unknown)/main.py" --base-directory "$UNKNOWN"'
    )
    assert hooks.resolve_layout(launcher, custom_nodes=nodes) == (comfy, nodes)
    assert hooks.resolve_layout(launcher, base_directory=nodes.parent) == (comfy, nodes)


@pytest.mark.parametrize(
    "args,expected",
    [
        (["python3", "-I", "/ComfyUI/main.py", "--base-directory", "/base"], 2),
        (["python3", "other.py", "--file", "/ComfyUI/main.py"], 1),
        (["python3", "-c", "print('main.py')"], None),
        (["python3", "-m", "other", "/ComfyUI/main.py"], None),
    ],
)
def test_launcher_parser_identifies_actual_python_script_operand(args, expected):
    assert hooks.python_script_operand(args) == expected


def test_launcher_refuses_mismatched_source_and_missing_exec(setup, tmp_path):
    comfy, _, _, _ = setup
    launcher, _ = make_launcher(tmp_path / "Home", comfy)
    other_main = (tmp_path / "Other source" / "main.py").as_posix()
    (launcher / "launch.sh").write_text(
        f'exec python "{other_main}" --base-directory "$ROOT/runtime"'
    )
    with pytest.raises(hooks.InstallError, match="does not match"):
        hooks.resolve_layout(launcher)
    (launcher / "launch.sh").write_text('echo --base-directory "runtime"\n')
    with pytest.raises(hooks.InstallError, match="no single Python exec"):
        hooks.resolve_layout(launcher)


@pytest.mark.parametrize(
    "script",
    [
        "exec python main.py --base-directory runtime",
        'if false; then\nexec python "{source}/main.py" --base-directory "{base}"\nfi',
        'ROOT="$(cat "$ROOT/comfyui-source.txt")"\nexec "$ROOT/python" "$ROOT/main.py"',
    ],
)
def test_selected_untrusted_launcher_requires_explicit_base(setup, tmp_path, script):
    comfy, _, _, _ = setup
    launcher, nodes = make_launcher(tmp_path / "Home", comfy)
    (launcher / "launch.sh").write_text(script.format(source=comfy, base=nodes.parent))
    with pytest.raises(hooks.InstallError, match="Choose the source and base folders manually"):
        hooks.resolve_layout(launcher)
    assert hooks.resolve_layout(launcher, custom_nodes=nodes) == (comfy, nodes)
    assert hooks.resolve_layout(launcher, base_directory=nodes.parent) == (comfy, nodes)


def test_launcher_variable_expansion_does_not_replace_other_variable_prefix(setup, tmp_path):
    comfy, _, _, _ = setup
    launcher, _ = make_launcher(tmp_path / "Home", comfy)
    text = (launcher / "launch.sh").read_text().replace("COMFYUI_SOURCE", "ROOT_SOURCE")
    assert hooks.launcher_value("$ROOT/$ROOT_SOURCE", launcher, comfy, text) == str(
        launcher
    ) + "/" + str(comfy)


@pytest.mark.parametrize(
    "name",
    [
        'ComfyUI "quote"',
        "ComfyUI $variable",
        "ComfyUI $(command)",
        "ComfyUI `command`",
        "ComfyUI %variable%",
        "ComfyUI\nline",
    ],
)
def test_selected_folder_rejects_native_shell_expansion_paths(tmp_path, name):
    with pytest.raises(hooks.InstallError, match="Folder paths must not contain"):
        hooks.resolve_layout(tmp_path / name)


@pytest.mark.skipif(os.name == "nt", reason="Backslash is a Windows directory separator")
def test_macos_selected_folder_rejects_literal_backslash_paths(tmp_path):
    with pytest.raises(hooks.InstallError, match="Folder paths must not contain"):
        hooks.resolve_layout(tmp_path / "ComfyUI\\literal")


@pytest.mark.parametrize(
    "path",
    ['build "quote"', "build $variable", "build `command`", "build %variable%", "build\nline"],
)
def test_builder_rejects_paths_unsupported_by_native_command_interpolation(tmp_path, path):
    with pytest.raises(ValueError, match="Installer build paths must not contain"):
        builder.validate_native_path(tmp_path / path)


@pytest.mark.skipif(os.name != "nt", reason="Windows path separators require a Windows host")
def test_windows_direct_path_accepts_normal_directory_separators(tmp_path):
    source = tmp_path / "Artist folder" / "ComfyUI"
    assert hooks.direct_path(source) == source.resolve()


def valid_python_payload(payload: Path) -> None:
    for module in payload.rglob("*.py"):
        module.write_text("# Synthetic packaged module.\nVALUE = 1\n", encoding="utf-8")


def compile_module(module: Path, optimization: int = 0) -> Path:
    cached = py_compile.compile(str(module), doraise=True, optimize=optimization)
    assert cached is not None
    return Path(cached)


def test_clean_uninstall_removes_generated_packaged_module_bytecode(setup):
    comfy, payload, runtime, state = setup
    valid_python_payload(payload)
    destination = hooks.install(payload, runtime, comfy, state, "1.0")
    cached = [
        compile_module(module, optimization)
        for module in destination.rglob("*.py")
        for optimization in (0, 1, 2)
    ]
    # ComfyUI and the supplied installer may use different CPython versions.
    alternate = cached[0].with_name("__init__.cpython-999.pyc")
    alternate_content = bytearray(cached[0].read_bytes())
    alternate_content[0] ^= 1  # Synthetic alternate magic; never require this interpreter's magic.
    alternate.write_bytes(alternate_content)
    assert all(path.is_file() for path in cached)
    assert hooks.uninstall(state) == []
    assert not destination.exists()
    assert not state.exists()


def test_uninstall_preserves_edited_and_customer_module_bytecode(setup):
    comfy, payload, runtime, state = setup
    valid_python_payload(payload)
    destination = hooks.install(payload, runtime, comfy, state, "1.0")
    edited = destination / "__init__.py"
    edited.write_text("# Customer changes.\nVALUE = 2\n", encoding="utf-8")
    edited_cache = compile_module(edited)
    packaged = destination / "src/deadline/comfyui_submitter/bridge.py"
    packaged_cache = compile_module(packaged)
    customer = packaged.with_name("customer.py")
    customer.write_text("# Customer module.\nVALUE = 3\n", encoding="utf-8")
    customer_cache = compile_module(customer)
    note = packaged_cache.parent / "customer.txt"
    note.write_text("keep customer cache extras", encoding="utf-8")
    misleading = packaged_cache.with_name("bridge.cpython-999.pyc")
    misleading.write_text("customer file with a cache-like name", encoding="utf-8")
    keep = {
        path: path.read_bytes()
        for path in (edited_cache, customer, customer_cache, note, misleading)
    }
    assert hooks.uninstall(state) == ["__init__.py"]
    assert not packaged_cache.exists()
    assert all(path.read_bytes() == content for path, content in keep.items())
    assert hooks.uninstalled_receipt(destination)


@pytest.mark.skipif(os.name == "nt", reason="Symlink permissions differ on Windows")
@pytest.mark.parametrize("link_directory", [False, True])
def test_uninstall_does_not_follow_bytecode_symlinks(setup, tmp_path, link_directory):
    comfy, payload, runtime, state = setup
    valid_python_payload(payload)
    destination = hooks.install(payload, runtime, comfy, state, "1.0")
    module = destination / "src/deadline/comfyui_submitter/bridge.py"
    cached = compile_module(module)
    outside = tmp_path / "Customer bytecode outside integration"
    outside.mkdir()
    external = outside / cached.name
    external.write_bytes(cached.read_bytes())
    expected = external.read_bytes()
    cached.unlink()
    if link_directory:
        cached.parent.rmdir()
        cached.parent.symlink_to(outside, target_is_directory=True)
    else:
        cached.symlink_to(external)
    assert hooks.uninstall(state) == []
    assert external.read_bytes() == expected
    assert (cached.parent if link_directory else cached).is_symlink()
    assert hooks.uninstalled_receipt(destination)


def test_uninstall_cache_removal_failure_preserves_source_ownership_for_retry(setup, monkeypatch):
    comfy, payload, runtime, state = setup
    valid_python_payload(payload)
    destination = hooks.install(payload, runtime, comfy, state, "1.0")
    manifest = json.loads(state.read_text())
    owned_files = {
        destination / name: (destination / name).read_bytes() for name in manifest["files"]
    }
    caches = [compile_module(module) for module in destination.rglob("*.py")]
    assert len(caches) >= 2
    marker = destination / hooks.MARKER_NAME
    marker_content = marker.read_bytes()
    state_content = state.read_bytes()
    configuration = destination / hooks.CONFIG_NAME
    configuration_content = configuration.read_bytes()
    original = Path.unlink
    cache_unlinks = 0

    def deny_second_cache_once(path, missing_ok=False):
        nonlocal cache_unlinks
        if path.suffix == ".pyc":
            cache_unlinks += 1
            if cache_unlinks == 2:
                raise PermissionError(errno.EACCES, "cache access denied", str(path))
        return original(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", deny_second_cache_once)
    with pytest.raises(PermissionError, match="cache access denied"):
        hooks.uninstall(state)
    # Partial cache progress must leave all ownership evidence and source intact.
    assert sum(cached.exists() for cached in caches) == len(caches) - 1
    assert all(path.read_bytes() == expected for path, expected in owned_files.items())
    assert marker.read_bytes() == marker_content
    assert state.read_bytes() == state_content
    assert configuration.read_bytes() == configuration_content
    assert hooks.uninstall(state) == []
    assert not destination.exists()
    assert not state.exists()


@pytest.mark.parametrize("root_folder", [False, True])
def test_uninstall_reports_unexpected_directory_cleanup_errors_and_allows_retry(
    setup, monkeypatch, root_folder
):
    comfy, payload, runtime, state = setup
    destination = hooks.install(payload, runtime, comfy, state, "1.0")
    target = destination if root_folder else destination / "src/deadline/comfyui_submitter"
    original = Path.rmdir
    failed = False

    def fail_once(path):
        nonlocal failed
        if path == target and not failed:
            failed = True
            raise PermissionError(errno.EACCES, "access denied", str(path))
        return original(path)

    monkeypatch.setattr(Path, "rmdir", fail_once)
    with pytest.raises(hooks.InstallError, match="Cannot remove empty integration folder"):
        hooks.uninstall(state)
    assert state.is_file()
    assert json.loads((destination / hooks.MARKER_NAME).read_text()) == json.loads(
        state.read_text()
    )
    assert hooks.uninstall(state) == []
    assert not destination.exists()


@pytest.mark.skipif(os.name == "nt", reason="Backslash is a Windows directory separator")
def test_macos_builder_rejects_literal_backslash_paths(tmp_path):
    with pytest.raises(ValueError, match="Installer build paths must not contain"):
        builder.validate_native_path(tmp_path / "build\\literal")


def test_portable_parent_resolves_only_nested_source_and_active_nodes(tmp_path):
    portable = tmp_path / "ComfyUI portable with spaces"
    source = portable / "ComfyUI"
    (source / "custom_nodes").mkdir(parents=True)
    (source / "main.py").write_text("# customer source\n")
    (portable / "custom_nodes").mkdir()
    sentinel = portable / "custom_nodes/customer.txt"
    sentinel.write_text("keep")
    assert hooks.resolve_layout(portable) == (source.resolve(), source / "custom_nodes")
    assert sentinel.read_text() == "keep"


def test_direct_source_takes_precedence_over_a_nested_source(setup):
    source, _, _, _ = setup
    (source / "ComfyUI/custom_nodes").mkdir(parents=True)
    (source / "ComfyUI/main.py").write_text("# nested customer source\n")
    assert hooks.resolve_layout(source) == (source.resolve(), source / "custom_nodes")


def test_resolve_rejects_unowned_prefix_before_writing_layout(setup, tmp_path, capsys):
    source, _, _, _ = setup
    prefix = tmp_path / "Customer unrelated data"
    prefix.mkdir()
    sentinel = prefix / "customer.txt"
    sentinel.write_text("keep")
    output = tmp_path / "resolved"
    assert (
        hooks.main(
            [
                "resolve",
                "--comfyui",
                str(source),
                "--prefix",
                str(prefix),
                "--output-dir",
                str(output),
            ]
        )
        == 1
    )
    assert "contains other files" in capsys.readouterr().err
    assert not output.exists()
    assert sentinel.read_text() == "keep"


@pytest.mark.parametrize("target", ["source", "nodes", "ancestor"])
def test_prefix_overlap_is_rejected_before_payload_copy(setup, target):
    source, _, _, _ = setup
    nodes = source / "custom_nodes"
    prefix = {"source": source, "nodes": nodes / "Deadline", "ancestor": source.parent}[target]
    with pytest.raises(hooks.InstallError, match="must be separate"):
        hooks.validate_prefix(prefix, source, nodes)


def test_existing_managed_prefix_can_only_target_its_original_host(setup, tmp_path):
    source, _, runtime, state = setup
    prefix = runtime.parent
    (prefix / ".deadline-cloud-for-comfyui-installer").write_text(
        "deadline-cloud-for-comfyui-installer-v1"
    )
    (prefix / "installer_version.txt").write_text("1.0")
    state.write_text(json.dumps({"extension": str(source / "custom_nodes" / hooks.EXTENSION_NAME)}))
    hooks.validate_prefix(prefix, source, source / "custom_nodes")
    other = tmp_path / "Other ComfyUI"
    (other / "custom_nodes").mkdir(parents=True)
    with pytest.raises(hooks.InstallError, match="another ComfyUI installation"):
        hooks.validate_prefix(prefix, other, other / "custom_nodes")


def test_prefix_rejects_relative_paths(setup):
    source, _, _, _ = setup
    with pytest.raises(hooks.InstallError, match="absolute"):
        hooks.validate_prefix(Path("Deadline Runtime"), source, source / "custom_nodes")


@pytest.mark.skipif(os.name != "nt", reason="Directory junctions require Windows")
def test_windows_junction_aliases_cannot_bypass_prefix_or_host_guards(setup, tmp_path):
    import subprocess

    source, _, _, _ = setup
    alias = tmp_path / "ComfyUI junction"
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(alias), str(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    try:
        with pytest.raises(hooks.InstallError, match="symbolic links"):
            hooks.resolve_layout(alias)
        with pytest.raises(hooks.InstallError, match="symbolic links"):
            hooks.validate_prefix(alias / "Deadline Runtime", source, source / "custom_nodes")
    finally:
        alias.rmdir()
