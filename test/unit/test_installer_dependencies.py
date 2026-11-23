# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Check installer dependency resolution and SDK packaging without network access."""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import os
import stat
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "installer_dependency_builder", ROOT / "scripts/build_installer.py"
)
assert spec is not None and spec.loader is not None
builder: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.mark.parametrize("offline", [False, True])
def test_sdk_build_uses_binary_wheels_and_installer_requirements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline: bool
) -> None:
    commands = []
    monkeypatch.setattr(builder, "run", lambda command: commands.append(command))
    wheelhouse = tmp_path / "Wheels with spaces" if offline else None
    target = tmp_path / "SDK staging with spaces"
    builder.install_dependencies(Path("python"), wheelhouse, target, version="3.14")
    assert len(commands) == 1
    command = commands[0]
    assert {"--force-reinstall", "--only-binary=:all:"} <= set(command)
    with (ROOT / "pyproject.toml").open("rb") as stream:
        requirements = tomllib.load(stream)["dependency-groups"]["installer"]
    assert any(requirement.startswith("deadline[gui,console]") for requirement in requirements)
    assert command[-len(requirements) :] == requirements
    assert ("--no-index" in command) == offline
    assert command[command.index("--target") + 1] == str(target.resolve())
    assert command[command.index("--python-version") + 1] == "3.14"
    if wheelhouse is not None:
        assert command[command.index("--find-links") + 1] == str(wheelhouse.resolve())


def wheel(
    folder: Path, name: str, dependency: str | None = None, *, tag: str = "py3-none-any"
) -> Path:
    module = name.replace("-", "_")
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n"
    if dependency is not None:
        metadata += f"Requires-Dist: {dependency}\n"
    info = f"{module}-1.0.dist-info"
    files = {
        f"{module}/__init__.py": b"# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.\nVALUE = 'fixture'\n",
        f"{info}/METADATA": (metadata + "\n").encode(),
        f"{info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: installer-tests\nRoot-Is-Purelib: true\n"
            f"Tag: {tag}\n"
        ).encode(),
    }
    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for relative, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        writer.writerow([relative, "sha256=" + digest, str(len(content))])
    writer.writerow([f"{info}/RECORD", "", ""])
    files[f"{info}/RECORD"] = record.getvalue().encode()
    output = folder / f"{module}-1.0-{tag}.whl"
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative, content in files.items():
            archive.writestr(relative, content)
    return output


@pytest.fixture
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    runtime = tmp_path / "Private runtime"
    subprocess.run(
        [sys.executable, "-I", "-m", "venv", str(runtime)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    (tmp_path / "pyproject.toml").write_text(
        '[dependency-groups]\ninstaller = ["sdk-parent==1.0"]\n', encoding="utf-8"
    )
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    original = builder.run
    environment = {
        **{name: value for name, value in os.environ.items() if not name.startswith("PIP_")},
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }

    def run(command: list[str]) -> str:
        return original(command, env=environment, timeout=120)

    monkeypatch.setattr(builder, "run", run)
    return python


def test_sdk_staging_resolves_transitive_wheels_outside_runtime(
    tmp_path: Path, isolated_runtime: Path
) -> None:
    wheels = tmp_path / "Wheelhouse"
    wheels.mkdir()
    wheel(wheels, "sdk-parent", "sdk-child>=1.0")
    wheel(wheels, "sdk-child")
    target = tmp_path / "SDK staging with spaces"
    builder.install_dependencies(isolated_runtime, wheels, target, version="3.13")
    assert (target / "sdk_parent/__init__.py").is_file()
    assert (target / "sdk_child/__init__.py").is_file()
    result = subprocess.run(
        [
            str(isolated_runtime),
            "-I",
            "-c",
            "import importlib.util; print(importlib.util.find_spec('sdk_parent'))",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout.strip() == "None"
    result = subprocess.run(
        [
            str(isolated_runtime),
            "-I",
            "-c",
            "import sys; sys.path.insert(0,sys.argv[1]); import sdk_parent; print(sdk_parent.VALUE)",
            str(target),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout.strip() == "fixture"


@pytest.mark.parametrize("python_version", ["3.13", "3.14"])
def test_windows_target_resolves_matching_wheels_offline(
    tmp_path: Path, isolated_runtime: Path, python_version: str
) -> None:
    wheels = tmp_path / "Windows wheels with spaces"
    wheels.mkdir()
    abi = "cp" + python_version.replace(".", "")
    parent = wheel(wheels, "sdk-parent", "sdk-child==1.0", tag=f"{abi}-{abi}-win_amd64")
    child = wheel(wheels, "sdk-child")
    # An otherwise suitable wheel for the other Python version must never be chosen.
    other_abi = "cp314" if abi == "cp313" else "cp313"
    wheel(wheels, "sdk-parent", "sdk-child==1.0", tag=f"{other_abi}-{other_abi}-win_amd64")
    output = tmp_path / "Resolved target wheels"
    environment = {
        **{name: value for name, value in os.environ.items() if not name.startswith("PIP_")},
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    result = subprocess.run(
        [
            str(isolated_runtime),
            "-I",
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--no-index",
            "--find-links",
            str(wheels),
            "--platform",
            "win_amd64",
            "--python-version",
            python_version,
            "--implementation",
            "cp",
            "--abi",
            abi,
            "--dest",
            str(output),
            "sdk-parent==1.0",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert {artifact.name for artifact in output.iterdir()} == {parent.name, child.name}


def test_dependency_bundle_is_deterministic_and_contains_raw_site_packages(tmp_path: Path) -> None:
    source = tmp_path / "SDK site-packages with spaces"
    files = {
        "locked_sdk/__init__.py": b"# Synthetic SDK fixture.\n",
        "locked_sdk/native.cp313-win_amd64.pyd": b"synthetic Windows binary fixture",
        "locked_sdk/native.cpython-313-darwin.so": b"synthetic Mac binary fixture",
        "locked_sdk-1.0.dist-info/METADATA": b"Name: locked-sdk\nVersion: 1.0\n",
        "locked_sdk-1.0.dist-info/licenses/LICENSE": b"synthetic package license fixture",
    }
    for relative, content in files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    caches = source / "locked_sdk/__pycache__"
    caches.mkdir()
    (caches / "__init__.cpython-313.pyc").write_bytes(b"excluded generated bytecode")
    archive = tmp_path / builder.BUNDLE_RELATIVE
    builder.create_dependency_bundle(source, archive)
    second = tmp_path / "Second build" / archive.name
    builder.create_dependency_bundle(source, second)
    assert archive.read_bytes() == second.read_bytes()
    assert archive.name == "deadline_cloud_for_comfyui_submitter-deps.zip"
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        assert {entry.filename for entry in entries if not entry.is_dir()} == files.keys()
        folders = [entry for entry in entries if entry.is_dir()]
        assert {entry.filename for entry in folders} == {
            "locked_sdk/",
            "locked_sdk-1.0.dist-info/",
            "locked_sdk-1.0.dist-info/licenses/",
        }
        assert all(stat.S_IMODE(entry.external_attr >> 16) == 0o755 for entry in folders)
        assert all(
            stat.S_IMODE(entry.external_attr >> 16) in (0o644, 0o755)
            for entry in entries
            if not entry.is_dir()
        )
        assert all(entry.date_time == (1980, 1, 1, 0, 0, 0) for entry in entries)
    destination = tmp_path / "Runtime site-packages with spaces"
    builder.extract_dependency_bundle(archive, destination)
    assert {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    } == files
    if os.name != "nt":
        assert all(
            stat.S_IMODE(path.stat().st_mode) == 0o755
            for path in destination.rglob("*")
            if path.is_dir()
        )


@pytest.mark.parametrize(
    "unsafe",
    [
        "../customer.txt",
        "/customer.txt",
        "C:/customer.txt",
        "C:customer.txt",
        "locked_sdk\\customer.txt",
    ],
)
def test_dependency_bundle_rejects_paths_outside_platform_site_packages(
    tmp_path: Path, unsafe: str
) -> None:
    archive = tmp_path / "Untrusted SDK.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        entry = zipfile.ZipInfo("placeholder")
        # Preserve malformed separators; Windows normalizes strings in ZipInfo.__init__.
        entry.filename = unsafe
        bundle.writestr(entry, "synthetic unsafe entry")
    customer = tmp_path / "customer.txt"
    customer.write_text("keep unrelated file", encoding="utf-8")
    destination = tmp_path / "site-packages"
    with pytest.raises(ValueError, match="unsafe path"):
        builder.extract_dependency_bundle(archive, destination)
    assert customer.read_text(encoding="utf-8") == "keep unrelated file"
    assert not list(destination.rglob("*"))
