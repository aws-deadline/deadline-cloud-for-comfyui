# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Resolve a supplied, locally installed, or private S3 InstallBuilder distribution."""

from __future__ import annotations

import os
import shutil
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


def builder_candidates(installation: Path, target: str) -> list[Path]:
    names = ("builder-cli.exe", "builder.exe") if target == "Windows" else ("builder",)
    return [installation / "bin" / name for name in names]


def find_builder(installation: Path, target: str) -> Path:
    """Accept the Cinema4D directory contract and older direct-executable invocations."""
    installation = installation.expanduser().resolve()
    if installation.is_file():
        return installation
    for executable in builder_candidates(installation, target):
        if executable.is_file():
            return executable
    raise ValueError(f"InstallBuilder has no builder executable for {target}: {installation}")


def search_builder(target: str) -> Path:
    names = ("builder-cli.exe", "builder.exe") if target == "Windows" else ("builder",)
    for name in names:
        executable = shutil.which(name)
        if executable:
            return Path(executable).resolve()
    roots = (
        [Path(os.environ.get("ProgramFiles", "C:/Program Files"))]
        if target == "Windows"
        else [Path("/Applications")]
    )
    for root in roots:
        for directory in sorted(root.glob("*InstallBuilder*"), reverse=True):
            try:
                return find_builder(directory, target)
            except ValueError:
                continue
    raise ValueError("Specify --install-builder-path or --install-builder-s3-bucket.")


def _archive_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or "\\" in name or ":" in name or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"The InstallBuilder archive contains an unsafe path: {name}")
    return path


def extract_builder_archive(archive: Path, destination: Path) -> None:
    """Extract a trusted tool archive without allowing archive members to escape its directory."""
    destination.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as source:
            for info in source.infolist():
                path = _archive_path(info.orig_filename)
                mode = info.external_attr >> 16
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError(
                        f"The InstallBuilder ZIP contains a special file: {info.filename}"
                    )
                output = destination.joinpath(*path.parts)
                if info.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                else:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(info) as content, output.open("wb") as target:
                        shutil.copyfileobj(content, target)
                    if mode & 0o777:
                        output.chmod(mode & 0o777)
    elif tarfile.is_tarfile(archive):
        with tarfile.open(archive) as source:
            for member in source.getmembers():
                _archive_path(member.name)
                if member.issym() or member.islnk():
                    _archive_path(member.linkname)
            source.extractall(destination, filter="data")
    else:
        raise ValueError("The S3 InstallBuilder artifact must be a ZIP or tar archive.")


def builder_from_s3(bucket: str, key: str | None, directory: Path, target: str) -> Path:
    """Use the configured AWS credential chain and private tool bucket; never download runtimes."""
    try:
        import boto3
    except ImportError as error:
        raise ValueError(
            "S3 InstallBuilder provisioning requires boto3 in the build environment."
        ) from error
    client = boto3.client("s3")
    if key is None:
        markers = ("windows", "win64") if target == "Windows" else ("macos", "osx", "darwin")
        objects: list[dict[str, Any]] = []
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            objects.extend(
                item
                for item in page.get("Contents", [])
                if item["Key"].lower().endswith((".zip", ".tar.gz", ".tgz"))
                and any(marker in item["Key"].lower() for marker in markers)
            )
        if not objects:
            raise ValueError(
                "No matching InstallBuilder archive found; specify --install-builder-s3-key."
            )
        key = max(objects, key=lambda item: (item["LastModified"], item["Key"]))["Key"]
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / "installbuilder.archive"
    client.download_file(bucket, key, str(archive))
    extracted = directory / "extracted"
    extract_builder_archive(archive, extracted)
    candidates = builder_candidates(extracted, target)
    names = ("builder-cli.exe", "builder.exe") if target == "Windows" else ("builder",)
    candidates += [
        path for name in names for path in extracted.rglob(name) if path.parent.name == "bin"
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise ValueError("The S3 archive does not contain the requested InstallBuilder executable.")


def resolve_builder(
    installation: Path | None,
    bucket: str | None,
    key: str | None,
    license_path: Path | None,
    working: Path,
    target: str,
) -> Path:
    if installation is not None and bucket is not None:
        raise ValueError(
            "--install-builder-path and --install-builder-s3-bucket are mutually exclusive."
        )
    if key is not None and bucket is None:
        raise ValueError("--install-builder-s3-key requires --install-builder-s3-bucket.")
    builder = (
        find_builder(installation, target)
        if installation is not None
        else builder_from_s3(bucket, key, working, target) if bucket else search_builder(target)
    )
    if license_path is not None:
        if not license_path.is_file():
            raise ValueError(f"InstallBuilder license file does not exist: {license_path}")
        destination = builder.parent.parent / "license.xml"
        if license_path.resolve() != destination.resolve():
            shutil.copy2(license_path, destination)
    return builder
