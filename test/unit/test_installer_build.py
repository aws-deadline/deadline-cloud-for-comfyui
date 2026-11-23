# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Verify platform build contracts and safe SDK/tool archives without native executables."""

from __future__ import annotations

import importlib.util
import json
import stat
import zipfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "platform_installer_builder", ROOT / "scripts/build_installer.py"
)
assert spec is not None and spec.loader is not None
builder: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

from scripts.deps_bundle import create_dependency_bundle, extract_dependency_bundle
from scripts.find_installbuilder import extract_builder_archive, find_builder


@pytest.mark.parametrize(
    "target,system,machine,artifact,arch",
    [
        (
            "Windows",
            "Windows",
            "AMD64",
            "DeadlineCloudForComfyUISubmitter-windows-x64-installer.exe",
            "x64",
        ),
        (
            "MacOS",
            "Darwin",
            "arm64",
            "DeadlineCloudForComfyUISubmitter-osx-arm64-installer.app",
            "arm64",
        ),
    ],
)
def test_platform_build_produces_expected_artifact(
    tmp_path, monkeypatch, target, system, machine, artifact, arch
):
    payload = tmp_path / "Payload with spaces"
    payload.mkdir()
    (payload / "build.json").write_text(
        json.dumps(
            {
                "system": system,
                "machine": machine,
            }
        )
    )
    executable = tmp_path / "builder"
    executable.write_text("synthetic builder")
    output = tmp_path / "Output with spaces"
    commands = []

    def run(command):
        commands.append(command)
        destination = output / artifact
        if target == "Windows":
            destination.write_text("synthetic installer")
        else:
            destination.mkdir()
        return "Build complete"

    monkeypatch.setattr(builder, "run", run)
    result = builder.build(payload, executable, output, target, "0.1.0", True)
    assert result == output / artifact
    assert commands[0][3] == {"Windows": "windows-x64", "MacOS": "osx"}[target]
    assert f"project.parameter(installer_architecture).value={arch}" in commands[0]
    assert "project.version=0.1.0" in commands[0]


def test_release_builder_rejects_an_evaluation_artifact(tmp_path, monkeypatch):
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "build.json").write_text(
        json.dumps(
            {
                "system": "Windows",
                "machine": "AMD64",
            }
        )
    )
    executable = tmp_path / "builder"
    executable.write_text("synthetic")
    monkeypatch.setattr(
        builder, "run", lambda command: "Built with an evaluation version of InstallBuilder"
    )
    with pytest.raises(ValueError, match="licensed"):
        builder.build(payload, executable, tmp_path / "output", "Windows", "0.1.0", False)


@pytest.mark.parametrize(
    "target,system,machine",
    [
        ("Windows", "Darwin", "arm64"),
        ("Windows", "Windows", "ARM64"),
        ("MacOS", "Windows", "AMD64"),
    ],
)
def test_build_rejects_an_sdk_for_the_wrong_native_target(target, system, machine):
    with pytest.raises(ValueError):
        builder.validate_architecture({"system": system, "machine": machine}, target)


def test_sdk_bundle_round_trip_is_deterministic_and_excludes_bytecode(tmp_path):
    packages = tmp_path / "site-packages"
    (packages / "sdk/__pycache__").mkdir(parents=True)
    (packages / "sdk/__init__.py").write_text("# synthetic SDK\nVALUE = 1\n")
    (packages / "sdk/__pycache__/module.pyc").write_bytes(b"cache")
    first = create_dependency_bundle(packages, tmp_path / "first.zip")
    second = create_dependency_bundle(packages, tmp_path / "second.zip")
    assert first.read_bytes() == second.read_bytes()
    installed = tmp_path / "runtime/Lib/site-packages"
    extract_dependency_bundle(first, installed)
    assert (installed / "sdk/__init__.py").read_bytes() == (
        packages / "sdk/__init__.py"
    ).read_bytes()
    assert not (installed / "sdk/__pycache__").exists()


def test_merge_sdk_packages_keeps_both_tagged_native_modules(tmp_path):
    base = tmp_path / "3.13"
    alternate = tmp_path / "3.14"
    for packages, abi in [(base, "313"), (alternate, "314")]:
        (packages / "xxhash").mkdir(parents=True)
        (packages / "xxhash/__init__.py").write_text("from ._xxhash import xxh128\n")
        (packages / f"xxhash/_xxhash.cpython-{abi}-darwin.so").write_bytes(abi.encode())
        (packages / "xxhash-1.0.dist-info").mkdir()
        (packages / "xxhash-1.0.dist-info/WHEEL").write_text(f"Tag: cp{abi}\n")

    builder.merge_sdk_packages(base, alternate)

    assert (base / "xxhash/_xxhash.cpython-313-darwin.so").read_bytes() == b"313"
    assert (base / "xxhash/_xxhash.cpython-314-darwin.so").read_bytes() == b"314"


def test_merge_sdk_packages_rejects_conflicting_code(tmp_path):
    base = tmp_path / "3.13"
    alternate = tmp_path / "3.14"
    base.mkdir()
    alternate.mkdir()
    (base / "_native.abi3.so").write_bytes(b"base")
    (alternate / "_native.abi3.so").write_bytes(b"alternate")

    with pytest.raises(ValueError, match=r"conflicting files at _native\.abi3\.so"):
        builder.merge_sdk_packages(base, alternate)
    assert (base / "_native.abi3.so").read_bytes() == b"base"


@pytest.mark.parametrize(
    "name", ["../outside", "/outside", "C:/outside", "C:outside", "sdk\\outside"]
)
def test_sdk_and_tool_archives_reject_unsafe_paths(tmp_path, name):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        entry = zipfile.ZipInfo("placeholder")
        # Preserve malformed separators; Windows normalizes strings in ZipInfo.__init__.
        entry.filename = name
        output.writestr(entry, "untrusted")
    with pytest.raises(ValueError, match="unsafe path"):
        extract_dependency_bundle(archive, tmp_path / "sdk")
    with pytest.raises(ValueError, match="unsafe path"):
        extract_builder_archive(archive, tmp_path / "tools")
    assert not (tmp_path / "outside").exists()


def test_sdk_and_tool_archives_reject_symbolic_link_entries(tmp_path):
    archive = tmp_path / "linked.zip"
    info = zipfile.ZipInfo("sdk/link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(info, "../../outside")
    with pytest.raises(ValueError, match="special file"):
        extract_dependency_bundle(archive, tmp_path / "sdk")
    with pytest.raises(ValueError, match="special file"):
        extract_builder_archive(archive, tmp_path / "tools")


@pytest.mark.parametrize("target,name", [("Windows", "builder-cli.exe"), ("MacOS", "builder")])
def test_installbuilder_resolver_accepts_peer_directory_and_legacy_executable(
    tmp_path, target, name
):
    installation = tmp_path / "InstallBuilder with spaces"
    executable = installation / "bin" / name
    executable.parent.mkdir(parents=True)
    executable.write_text("synthetic tool")
    assert find_builder(installation, target) == executable.resolve()
    assert find_builder(executable, target) == executable.resolve()


def test_invalid_s3_cli_contract_fails_before_staging(monkeypatch, capsys):
    monkeypatch.setattr(
        builder, "stage_payload", lambda *args: pytest.fail("must validate options first")
    )
    assert (
        builder.main(
            ["--platform", "Windows", "--stage-only", "--install-builder-s3-key", "tool.zip"]
        )
        == 1
    )
    assert "requires --install-builder-s3-bucket" in capsys.readouterr().err


@pytest.mark.parametrize(
    "exact,merged,nearest,expected",
    [
        ("v1.2.3", "", "", "1.2.3"),
        ("experiment\nv1.2.3-rc.1", "", "", "1.2.3-rc.1"),
        ("experiment", "v1.2.2\nv1.2.3", "v1.2.3", "1.2.3.abc1234"),
    ],
)
def test_installer_version_uses_release_tags_and_identifies_development_commits(
    monkeypatch, exact, merged, nearest, expected
):
    monkeypatch.delenv("INSTALLER_VERSION", raising=False)

    def git_output(*arguments):
        if arguments[0] == "rev-parse":
            return "abc1234"
        if arguments[:2] == ("tag", "--points-at"):
            return exact
        if arguments[:2] == ("tag", "--merged"):
            return merged
        return nearest

    monkeypatch.setattr(builder, "git_output", git_output)
    assert builder.installer_version() == expected


def test_installer_version_honors_build_runner_override_without_git(monkeypatch):
    monkeypatch.setenv("INSTALLER_VERSION", "1.2.3")
    monkeypatch.setattr(
        builder, "git_output", lambda *arguments: pytest.fail("override must not read git")
    )
    assert builder.installer_version() == "1.2.3"
