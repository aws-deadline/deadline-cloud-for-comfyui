# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from _project import Dependency, get_dependencies, get_git_root, get_project_dict

# The Pythons ComfyUI runs on that the bundle carries compiled artifacts for.
SUPPORTED_PYTHON_VERSIONS = ["3.13", "3.14"]
SUPPORTED_PLATFORMS = ["Windows", "Darwin"]
# Packages with compiled extension modules, fetched once per supported Python version so the
# bundle carries a loadable artifact for each interpreter.
#
# pyyaml is here because it ships a version-specific `_yaml` extension module: resolved only
# in the base environment it lands built for a single interpreter, and pyyaml hides that by
# falling back to its pure-Python parser on the other.
#
# pywin32 isn't here: its modules have the same names for every Python version, so only the build
# Python's copy ships. Job attachments imports it only to set permissions on files that workers
# download, which the panel's output downloads don't do. Deadline's config uses it to set the
# permissions of ~/.deadline when it first creates that folder.
NATIVE_DEPENDENCIES = ["xxhash", "psutil", "awscrt", "pyyaml"]
# Which Pythons and CPU the bundle's compiled modules are for. routes.py reads it, and runs the
# bridge with ComfyUI's Python only when they match.
BUNDLE_DESCRIPTION = "bundle.json"
# The licenses of the bundled packages, which the installer puts in the extension's folder.
THIRD_PARTY_LICENSES = "THIRD_PARTY_LICENSES"
RULE = "=" * 80


def _get_package_version_regex(package: str) -> re.Pattern:
    # Case-insensitive because `pip list` prints the distribution's own casing, which need not
    # match how the requirement is spelled -- `pyyaml` is reported as `PyYAML`. The required
    # whitespace keeps a prefix sibling like `pyyaml-env-tag` from matching.
    return re.compile(rf"^{re.escape(package)}\s+(\S+)\s*$", re.IGNORECASE)


def _get_package_version(package: str, install_path: Path) -> str:
    version_regex = _get_package_version_regex(package)
    pip_args = ["pip", "list", "--path", str(install_path)]
    output = subprocess.run(pip_args, check=True, capture_output=True).stdout.decode("utf-8")
    for line in output.split("\n"):
        match = version_regex.match(line)
        if match:
            return match.group(1)
    raise RuntimeError(f"Could not find version for package {package}")


def _add_console_extra(requirement: str) -> str:
    """Add deadline's `console` extra to a requirement string, preserving its specifier."""
    match = re.fullmatch(
        r"(?P<name>[A-Za-z0-9._-]+)(?:\[(?P<extras>[^\]]*)\])?(?P<spec>.*)", requirement
    )
    if not match or match.group("name").lower() != "deadline":
        return requirement
    extras = [extra for extra in (match.group("extras") or "").split(",") if extra]
    if "console" not in extras:
        extras.append("console")
    return f"{match.group('name')}[{','.join(extras)}]{match.group('spec')}"


def _build_base_environment(working_directory: Path, dependencies: list[Dependency]) -> Path:
    (working_directory / "base_env").mkdir()
    base_env_path = working_directory / "base_env"
    # The submitter needs AWS Console sign-in, which the console extra provides. Requesting
    # the extra rather than installing awscrt directly means the bundle tracks whatever the
    # extra actually requires -- notably a botocore floor, since the console login provider
    # lives in botocore, not in deadline -- and takes awscrt from the exact version botocore's
    # crt extra pins, rather than resolving it independently and drifting.
    dependencies_for_pip = [_add_console_extra(d.for_pip()) for d in dependencies]
    base_env_pip_args = [
        "pip",
        "install",
        "--target",
        str(base_env_path),
        "--only-binary=:all:",
        *dependencies_for_pip,
    ]
    subprocess.run(base_env_pip_args, check=True)
    return base_env_path


def _python_version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _download_native_dependencies(working_directory: Path, base_env: Path) -> list[Path]:
    versioned_native_dependencies = [
        f"{package_name}=={_get_package_version(package_name, base_env)}"
        for package_name in NATIVE_DEPENDENCIES
    ]
    native_dependency_paths = []
    # Ascending order is load-bearing: _copy_native_to_base_env resolves a filename
    # collision in favour of the tree it sees first.
    for version in sorted(SUPPORTED_PYTHON_VERSIONS, key=_python_version_key):
        native_dependency_path = working_directory / "native" / f"{version.replace('.', '_')}"
        native_dependency_paths.append(native_dependency_path)
        native_dependency_path.mkdir(parents=True)
        native_dependency_pip_args = [
            "pip",
            "install",
            "--target",
            str(native_dependency_path),
            "--python-version",
            version,
            "--only-binary=:all:",
            # These trees exist only for their compiled artifacts, and they overwrite the
            # base environment during the merge. Without --no-deps each tree would carry the
            # packages' full transitive closures, resolved independently of the base
            # environment's, and clobber whatever it had resolved for anything they share.
            # If a compiled transitive dependency of one of these packages shows up, add it to
            # NATIVE_DEPENDENCIES.
            "--no-deps",
            *versioned_native_dependencies,
        ]
        subprocess.run(native_dependency_pip_args, check=True)
    return native_dependency_paths


def _copy_native_to_base_env(base_env: Path, native_dependency_paths: list[Path]) -> None:
    """Flatten the per-version native trees into the bundle, lowest version first.

    ``native_dependency_paths`` is ordered by ascending Python version and the first tree
    to supply a path wins, overwriting the base environment. The base environment resolved
    these packages for whatever interpreter the build host happens to run, so it must not
    decide which artifact ships.

    A version-specific name, such as ``xxhash``'s ``_xxhash.cpython-<tag>-<platform>.so``
    (``_xxhash.cp<tag>-<platform>.pyd`` on Windows) or ``pyyaml``'s ``yaml/_yaml`` module, is
    unique per version and so cannot collide. An abi3 name, such as ``psutil``'s or
    ``awscrt``'s, is the same for every version and so collides. abi3 is forward compatible,
    which makes the one built for the lowest supported Python the only copy that loads on all
    of them, and taking the first tree is what keeps it.
    """
    copied: set[Path] = set()
    for native_dependency_path in native_dependency_paths:
        for file in native_dependency_path.rglob("*"):
            if file.is_file():
                relative = file.relative_to(native_dependency_path)
                if relative in copied:
                    continue
                in_base_env = base_env / relative
                in_base_env.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(str(file), str(in_base_env))
                copied.add(relative)


def _describe_bundle(base_env: Path) -> None:
    description = {
        "python_versions": SUPPORTED_PYTHON_VERSIONS,
        "platform": sys.platform,
        "machine": platform.machine(),
    }
    (base_env / BUNDLE_DESCRIPTION).write_text(json.dumps(description, indent=2) + "\n")


def _third_party_licenses(base_env: Path) -> str:
    """Each bundled package's license, from the license files its wheel lists in License-File."""
    sections = [
        (
            "The Windows and macOS installers include these packages in the extension's "
            "dependencies folder.\n"
        )
    ]
    distributions = metadata.distributions(path=[str(base_env)])
    for distribution in sorted(distributions, key=lambda d: d.metadata["Name"].lower()):
        name, version = distribution.metadata["Name"], distribution.version
        license_name = (
            distribution.metadata.get("License-Expression")
            or distribution.metadata.get("License")
            or "See the license files below"
        )
        texts = []
        for license_file in distribution.metadata.get_all("License-File") or []:
            # Wheels with core metadata 2.4 keep them in licenses/, older wheels next to METADATA.
            text = distribution.read_text(f"licenses/{license_file}")
            if text is None:
                text = distribution.read_text(license_file)
            if text is not None:
                texts.append(f"{license_file}\n\n{text.strip()}\n")
        if not texts:
            raise RuntimeError(f"{name} {version} doesn't include its license files.")
        sections.append(f"{RULE}\n{name} {version}\nLicense: {license_name}\n{RULE}\n")
        sections.extend(texts)
    return "\n".join(sections)


def _get_zip_path(working_directory: Path, project_dict: dict[str, Any]) -> Path:
    if "project" not in project_dict:
        raise ValueError("pyproject.toml is missing project section")
    if "name" not in project_dict["project"]:
        raise ValueError("pyproject.toml is missing name section")
    transformed_project_name = (
        f"{project_dict['project']['name'].replace('-', '_')}_submitter-deps.zip"
    )
    return working_directory / transformed_project_name


def _zip_bundle(base_env: Path, zip_path: Path) -> None:
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", str(base_env))


def _write_third_party_licenses(base_env: Path) -> Path:
    destination = get_git_root() / "dependency_bundle" / THIRD_PARTY_LICENSES
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(_third_party_licenses(base_env), encoding="utf-8")
    return destination


def _copy_zip_to_destination(zip_path: Path) -> Path:
    # Where install_builder/deadline-cloud-for-comfyui.xml packs it from, whatever the current folder.
    dependency_bundle_dir = get_git_root() / "dependency_bundle"
    dependency_bundle_dir.mkdir(exist_ok=True)
    zip_destination = dependency_bundle_dir / zip_path.name
    if zip_destination.exists():
        zip_destination.unlink()
    shutil.copy(str(zip_path), str(zip_destination))

    return zip_destination


def build_deps_bundle() -> None:
    with TemporaryDirectory() as working_directory:
        working_directory = Path(working_directory)
        project_dict = get_project_dict()
        dependencies: list[Dependency] = get_dependencies(project_dict)
        base_env = _build_base_environment(working_directory, dependencies)
        native_dependency_paths = _download_native_dependencies(working_directory, base_env)
        _copy_native_to_base_env(base_env, native_dependency_paths)
        _describe_bundle(base_env)
        _write_third_party_licenses(base_env)
        zip_path = _get_zip_path(working_directory, project_dict)
        _zip_bundle(base_env, zip_path)
        print(list(working_directory.glob("*")))
        _copy_zip_to_destination(zip_path)


if __name__ == "__main__":
    build_deps_bundle()
