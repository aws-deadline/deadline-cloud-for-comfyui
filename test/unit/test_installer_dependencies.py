# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Check installer dependency locks and reject unverified wheels without network access."""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import os
import re
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "installer_dependency_builder", ROOT / "scripts/build_installer.py"
)
assert spec is not None and spec.loader is not None
builder: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def requirements(path: Path) -> list[tuple[Requirement, list[str]]]:
    text = re.sub(r"\\\r?\n[ \t]*", " ", path.read_text(encoding="utf-8"))
    parsed = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        requirement, *hashes = line.split(" --hash=")
        parsed.append((Requirement(requirement.strip()), [item.strip() for item in hashes]))
    return parsed


@pytest.mark.parametrize("python_version", ["3.13", "3.14"])
@pytest.mark.parametrize(
    "system,sys_platform,machine,os_name",
    [("Darwin", "darwin", "arm64", "posix"), ("Windows", "win32", "AMD64", "nt")],
    ids=["macos-arm64", "win-amd64"],
)
def test_installer_lock_pins_and_hashes_every_requirement(
    python_version: str, system: str, sys_platform: str, machine: str, os_name: str
) -> None:
    environment: dict[str, str] = {
        **cast(dict[str, str], default_environment()),
        "python_version": python_version,
        "python_full_version": python_version + ".0",
        "sys_platform": sys_platform,
        "platform_system": system,
        "platform_machine": machine,
        "os_name": os_name,
    }
    locked = requirements(ROOT / "requirements-installer.txt")
    assert locked
    active = {}
    for requirement, hashes in locked:
        pins = list(requirement.specifier)
        assert len(pins) == 1 and pins[0].operator == "==" and "*" not in pins[0].version
        assert not requirement.url
        assert hashes and all(re.fullmatch(r"sha256:[0-9a-f]{64}", item) for item in hashes)
        if requirement.marker is None or requirement.marker.evaluate(environment):
            name = canonicalize_name(requirement.name)
            assert name not in active, f"Duplicate active requirement: {name}"
            active[name] = requirement
    for direct, _ in requirements(ROOT / "requirements-installer.in"):
        if direct.marker is None or direct.marker.evaluate(environment):
            assert active[canonicalize_name(direct.name)].specifier == direct.specifier
    assert {
        "awscrt",
        "deadline-job-attachments",
        "shiboken6",
        "s3transfer",
        "qtpy",
        "psutil",
        "pyyaml",
        "xxhash",
    } <= active.keys()
    # Click and deadline-job-attachments require these only on Windows. A lock
    # generated exclusively on macOS otherwise appears complete on every host.
    windows_only = {"colorama", "pywin32"}
    if sys_platform == "win32":
        assert windows_only <= active.keys()
        assert active[canonicalize_name("colorama")].specifier.contains("0.4.6")
        assert active[canonicalize_name("pywin32")].specifier.contains("312")
    else:
        assert windows_only.isdisjoint(active)


@pytest.mark.parametrize("offline", [False, True])
def test_hash_verification_is_required_for_index_and_wheelhouse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline: bool
) -> None:
    commands = []
    monkeypatch.setattr(builder, "run", lambda command: commands.append(command))
    wheelhouse = tmp_path / "Wheels with spaces" if offline else None
    target = tmp_path / "SDK staging with spaces"
    builder.install_dependencies(Path("python"), wheelhouse, target, version="3.14")
    assert len(commands) == 1
    command = commands[0]
    assert {"--require-hashes", "--force-reinstall", "--only-binary=:all:"} <= set(command)
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
        f"{module}/__init__.py": b"# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.\nVALUE = 'trusted'\n",
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


def lock(tmp_path: Path, artifact: Path) -> None:
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (tmp_path / "requirements-installer.txt").write_text(
        f"locked-parent==1.0 --hash=sha256:{digest}\n", encoding="utf-8"
    )


def test_sdk_target_staging_does_not_install_dependencies_into_runtime(
    tmp_path: Path, isolated_runtime: Path
) -> None:
    wheels = tmp_path / "Wheelhouse"
    wheels.mkdir()
    artifact = wheel(wheels, "locked-parent")
    lock(tmp_path, artifact)
    target = tmp_path / "SDK staging with spaces"
    builder.install_dependencies(isolated_runtime, wheels, target, version="3.13")
    assert (target / "locked_parent/__init__.py").is_file()
    result = subprocess.run(
        [
            str(isolated_runtime),
            "-I",
            "-c",
            "import importlib.util; print(importlib.util.find_spec('locked_parent'))",
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
            "import sys; sys.path.insert(0,sys.argv[1]); import locked_parent; print(locked_parent.VALUE)",
            str(target),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout.strip() == "trusted"


def test_modified_local_wheel_is_rejected_even_when_version_is_installed(
    tmp_path: Path, isolated_runtime: Path
) -> None:
    wheels = tmp_path / "Wheelhouse"
    wheels.mkdir()
    artifact = wheel(wheels, "locked-parent")
    lock(tmp_path, artifact)
    target = tmp_path / "SDK staging"
    builder.install_dependencies(isolated_runtime, wheels, target, version="3.13")
    with zipfile.ZipFile(artifact, "a") as archive:
        archive.writestr("locked_parent/unverified.py", "VALUE = 'modified'\n")
    with pytest.raises(subprocess.CalledProcessError) as failure:
        builder.install_dependencies(
            isolated_runtime, wheels, tmp_path / "SDK staging", version="3.13"
        )
    output = failure.value.stdout + failure.value.stderr
    assert "DO NOT MATCH THE HASHES" in output
    result = subprocess.run(
        [
            str(isolated_runtime),
            "-IBS",
            "-c",
            "import sys; sys.path.insert(0,sys.argv[1]); import locked_parent; print(locked_parent.VALUE)",
            str(target),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout.strip() == "trusted"


@pytest.mark.parametrize("dependency", ["locked-child>=1.0", "locked-child==1.0"])
def test_transitive_dependency_without_a_complete_hash_lock_is_rejected(
    tmp_path: Path, isolated_runtime: Path, dependency: str
) -> None:
    wheels = tmp_path / "Wheelhouse"
    wheels.mkdir()
    artifact = wheel(wheels, "locked-parent", dependency)
    wheel(wheels, "locked-child")
    lock(tmp_path, artifact)
    with pytest.raises(subprocess.CalledProcessError) as failure:
        builder.install_dependencies(
            isolated_runtime, wheels, tmp_path / "SDK staging", version="3.13"
        )
    output = failure.value.stdout + failure.value.stderr
    assert "--require-hashes" in output and "locked-child" in output
    result = subprocess.run(
        [
            str(isolated_runtime),
            "-I",
            "-c",
            "import importlib.util; print(importlib.util.find_spec('locked_parent'))",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout.strip() == "None"


@pytest.mark.parametrize("python_version", ["3.13", "3.14"])
@pytest.mark.parametrize(
    "complete_lock", [False, True], ids=["missing-child-hash", "complete-lock"]
)
def test_windows_target_wheel_closure_is_hash_verified_offline(
    tmp_path: Path, isolated_runtime: Path, python_version: str, complete_lock: bool
) -> None:
    wheels = tmp_path / "Windows wheels with spaces"
    wheels.mkdir()
    abi = "cp" + python_version.replace(".", "")
    parent = wheel(wheels, "locked-parent", "locked-child==1.0", tag=f"{abi}-{abi}-win_amd64")
    child = wheel(wheels, "locked-child")
    # An otherwise suitable wheel for the other Python version must never be chosen.
    other_abi = "cp314" if abi == "cp313" else "cp313"
    wheel(wheels, "locked-parent", "locked-child==1.0", tag=f"{other_abi}-{other_abi}-win_amd64")
    locked = [parent, child] if complete_lock else [parent]
    lock_path = tmp_path / "windows-target-lock.txt"
    lock_path.write_text(
        "".join(
            f"{artifact.name.split('-')[0].replace('_', '-')}==1.0 "
            f"--hash=sha256:{hashlib.sha256(artifact.read_bytes()).hexdigest()}\n"
            for artifact in locked
        ),
        encoding="utf-8",
    )
    output = tmp_path / "Verified target wheels"
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
            "--require-hashes",
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
            "-r",
            str(lock_path),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if complete_lock:
        assert result.returncode == 0, result.stdout + result.stderr
        assert {artifact.name for artifact in output.iterdir()} == {parent.name, child.name}
        assert (output / parent.name).read_bytes() == parent.read_bytes()
    else:
        assert result.returncode != 0
        assert "--require-hashes" in result.stdout + result.stderr
        assert "locked-child" in result.stdout + result.stderr


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
