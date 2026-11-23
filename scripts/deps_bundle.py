# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Create and verify a standalone SDK site-packages archive, as in the Cinema4D installer."""

from __future__ import annotations

import os
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

BUNDLE_FILENAME = "deadline_cloud_for_comfyui_submitter-deps.zip"
BUNDLE_RELATIVE = "dependency_bundle/" + BUNDLE_FILENAME


def create_dependency_bundle(site_packages: Path, archive: Path) -> Path:
    """Archive installed, hash-verified wheels with stable names, timestamps and permissions."""
    source = site_packages.resolve()
    if not source.is_dir():
        raise ValueError(f"The SDK site-packages directory does not exist: {source}")
    if archive.resolve().is_relative_to(source):
        raise ValueError("The dependency archive must be outside its source directory.")
    archive.parent.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    directories: list[Path] = []
    visited: set[Path] = set()
    for directory, folders, names in os.walk(source, followlinks=True):
        resolved = Path(directory).resolve()
        if not resolved.is_relative_to(source) or resolved in visited:
            raise ValueError(
                f"The dependency bundle contains an external or cyclic link: {directory}"
            )
        visited.add(resolved)
        if Path(directory) != source:
            directories.append(Path(directory))
        folders[:] = sorted(name for name in folders if name != "__pycache__")
        for name in sorted(names):
            path = Path(directory) / name
            if name.endswith(".pyc"):
                continue
            if not path.resolve().is_relative_to(source) or not path.is_file():
                raise ValueError(f"The dependency bundle contains an unsupported file: {path}")
            files.append(path)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        # InstallBuilder invents mode0777 parents when the ZIP omits directory entries.
        for folder in sorted(directories, key=lambda item: item.relative_to(source).as_posix()):
            info = zipfile.ZipInfo(
                folder.relative_to(source).as_posix() + "/", (1980, 1, 1, 0, 0, 0)
            )
            info.create_system = 3
            info.external_attr = ((stat.S_IFDIR | 0o755) << 16) | 0x10
            output.writestr(info, b"")
        for path in sorted(files, key=lambda item: item.relative_to(source).as_posix()):
            info = zipfile.ZipInfo(path.relative_to(source).as_posix(), (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            permissions = 0o755 if path.stat().st_mode & 0o111 else 0o644
            info.external_attr = (stat.S_IFREG | permissions) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with path.open("rb") as content, output.open(info, "w") as destination:
                shutil.copyfileobj(content, destination)
    return archive


def extract_dependency_bundle(archive: Path, site_packages: Path) -> None:
    """Extract only ordinary files below site-packages; reject traversal, links and duplicates."""
    root = site_packages.resolve()
    root.mkdir(parents=True, exist_ok=True)
    names: set[str] = set()
    with zipfile.ZipFile(archive) as source:
        for info in source.infolist():
            relative = PurePosixPath(info.filename)
            if (
                not info.filename
                or "\\" in info.filename
                or ":" in info.filename
                or relative.is_absolute()
                or ".." in relative.parts
                or info.filename in names
            ):
                raise ValueError(f"The dependency archive contains an unsafe path: {info.filename}")
            names.add(info.filename)
            mode = info.external_attr >> 16
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError(f"The dependency archive contains a special file: {info.filename}")
            destination = root.joinpath(*relative.parts)
            if not destination.resolve().is_relative_to(root):
                raise ValueError(f"The dependency archive leaves site-packages: {info.filename}")
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                if mode & 0o777:
                    destination.chmod(mode & 0o777)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.open(info) as content, destination.open("wb") as output:
                shutil.copyfileobj(content, output)
            if mode & 0o777:
                destination.chmod(mode & 0o777)
