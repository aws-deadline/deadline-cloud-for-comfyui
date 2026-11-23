# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Provision pinned evaluation tools for unprivileged native installer CI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

# Publisher checksums: https://installbuilder.com/download-step-2.
# Python assets: astral-sh/python-build-standalone release 20261110.
# Changes to these executable tools require reviewing their versions and hashes.
TOOLS = {
    "MacOS": {
        "python": "cpython-3.13.18%2B20261110-aarch64-apple-darwin-install_only.tar.gz",
        "python_sha256": "1afd3983237c2a10277345f235be730e9cb49a123e0574ed252dbf3a841a24e4",
        "builder": "installbuilder-enterprise-26.8.0-osx-installer.dmg",
        "builder_sha256": "5d6a3b607d0fad113986e35cab7f83a38034ef6d47163541efc721daf4fa06c1",
    },
    "Windows": {
        "python": "cpython-3.13.18%2B20261110-x86_64-pc-windows-msvc-install_only.tar.gz",
        "python_sha256": "7e93813bd362c6413f30788b248590e2f169df2d17dcac81aafbcc7a6eeb542c",
        "builder": "installbuilder-enterprise-26.8.0-windows-x64-installer.exe",
        "builder_sha256": "1024a41c08f4278e52a28b6e87af34e879bb80d6f6e8f7419b1dabc560aaff23",
    },
}
PYTHON_RELEASE = "https://github.com/astral-sh/python-build-standalone/releases/download/20261110/"
BUILDER_RELEASE = "https://releases.installbuilder.com/installbuilder/"


def download(url: str, destination: Path, expected: str) -> None:
    """Verify immutable release bytes before extracting or executing anything."""
    if not destination.exists():
        temporary = destination.with_suffix(destination.suffix + ".part")
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with (
                urllib.request.urlopen(request, timeout=90) as response,
                temporary.open("wb") as out,
            ):
                shutil.copyfileobj(response, out)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    with destination.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        raise ValueError(
            f"SHA256 mismatch for {destination.name}: expected {expected}, got {actual}"
        )
    print(f"Verified {destination.name}: sha256:{actual}")


def run(command: list[str], timeout: int = 600) -> None:
    subprocess.run(command, check=True, timeout=timeout)


def provision(target: str, output: Path) -> tuple[Path, Path]:
    system = platform.system()
    machine = platform.machine().lower()
    if target == "MacOS":
        supported = system == "Darwin" and machine in ("arm64", "aarch64")
    else:
        supported = system == "Windows" and machine in ("amd64", "x86_64")
    if not supported:
        raise ValueError(f"{target} native CI tools cannot run on {system}/{machine}")
    output.mkdir(parents=True, exist_ok=True)
    tools = TOOLS[target]
    python_archive = output / tools["python"].replace("%2B", "+")
    download(PYTHON_RELEASE + tools["python"], python_archive, tools["python_sha256"])
    python_folder = output / "python"
    if python_folder.exists():
        raise ValueError("Use a fresh tools folder: the portable Python already exists")
    with tarfile.open(python_archive, "r:gz") as archive:
        archive.extractall(output, filter="data")
    python = python_folder / ("python.exe" if target == "Windows" else "bin/python3")
    run([str(python), "-I", "-c", "import ssl, encodings; print(ssl.OPENSSL_VERSION)"])
    setup = output / tools["builder"]
    download(BUILDER_RELEASE + tools["builder"], setup, tools["builder_sha256"])
    builder = output / "installbuilder"
    if builder.exists():
        raise ValueError("Use a fresh tools folder: InstallBuilder already exists")
    arguments = ["--mode", "unattended", "--unattendedmodeui", "none", "--prefix", str(builder)]
    if target == "Windows":
        run([str(setup), *arguments])
    else:
        mount = output / "mount"
        mount.mkdir()
        run(["hdiutil", "attach", str(setup), "-mountpoint", str(mount), "-readonly", "-nobrowse"])
        try:
            executable = (
                mount
                / "installbuilder-enterprise-26.8.0-osx-installer.app/Contents/MacOS/osx-arm64"
            )
            run([str(executable), *arguments])
        finally:
            run(["hdiutil", "detach", str(mount)], timeout=90)
    executable = builder / ("bin/builder-cli.exe" if target == "Windows" else "bin/builder")
    run([str(executable), "--version"])
    provenance = {"platform": target, "python_release": PYTHON_RELEASE, **tools}
    (output / "tool-versions.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
    )
    return python_folder, builder


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=TOOLS)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    python, builder = provision(args.platform, args.output.resolve())
    values = {"python-runtime": str(python), "install-builder-path": str(builder)}
    print(json.dumps(values))
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with Path(github_output).open("a", encoding="utf-8") as stream:
            stream.writelines(f"{name}={value}\n" for name, value in values.items())


if __name__ == "__main__":
    main()
