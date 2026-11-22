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
def test_installer_lock_pins_and_hashes_every_requirement(python_version: str) -> None:
    environment: dict[str, str] = {
        **cast(dict[str, str], default_environment()),
        "python_version": python_version,
        "python_full_version": python_version + ".0",
        "sys_platform": "darwin",
        "platform_system": "Darwin",
        "platform_machine": "arm64",
        "os_name": "posix",
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
    } <= active.keys()


@pytest.mark.parametrize("offline", [False, True])
def test_hash_verification_is_required_for_index_and_wheelhouse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline: bool
) -> None:
    commands = []
    monkeypatch.setattr(builder, "run", lambda command: commands.append(command))
    wheelhouse = tmp_path / "Wheels with spaces" if offline else None
    builder.install_dependencies(Path("portable-python"), wheelhouse)
    assert len(commands) == 1
    command = commands[0]
    assert {"--require-hashes", "--force-reinstall", "--only-binary=:all:"} <= set(command)
    assert ("--no-index" in command) == offline
    if wheelhouse is not None:
        assert command[command.index("--find-links") + 1] == str(wheelhouse.resolve())


def wheel(folder: Path, name: str, dependency: str | None = None) -> Path:
    module = name.replace("-", "_")
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n"
    if dependency is not None:
        metadata += f"Requires-Dist: {dependency}\n"
    info = f"{module}-1.0.dist-info"
    files = {
        f"{module}/__init__.py": b"# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.\nVALUE = 'trusted'\n",
        f"{info}/METADATA": (metadata + "\n").encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: installer-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for relative, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        writer.writerow([relative, "sha256=" + digest, str(len(content))])
    writer.writerow([f"{info}/RECORD", "", ""])
    files[f"{info}/RECORD"] = record.getvalue().encode()
    output = folder / f"{module}-1.0-py3-none-any.whl"
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


def test_locked_local_wheel_is_installed_without_an_index(
    tmp_path: Path, isolated_runtime: Path
) -> None:
    wheels = tmp_path / "Wheelhouse"
    wheels.mkdir()
    artifact = wheel(wheels, "locked-parent")
    lock(tmp_path, artifact)
    builder.install_dependencies(isolated_runtime, wheels)
    result = subprocess.run(
        [str(isolated_runtime), "-I", "-c", "import locked_parent; print(locked_parent.VALUE)"],
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
    builder.install_dependencies(isolated_runtime, wheels)
    with zipfile.ZipFile(artifact, "a") as archive:
        archive.writestr("locked_parent/unverified.py", "VALUE = 'modified'\n")
    with pytest.raises(subprocess.CalledProcessError) as failure:
        builder.install_dependencies(isolated_runtime, wheels)
    output = failure.value.stdout + failure.value.stderr
    assert "DO NOT MATCH THE HASHES" in output
    result = subprocess.run(
        [str(isolated_runtime), "-I", "-c", "import locked_parent; print(locked_parent.VALUE)"],
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
        builder.install_dependencies(isolated_runtime, wheels)
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
