# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Offline installer hooks, run by the installer's bundled Python."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

EXTENSION_NAME = "deadline-cloud-for-comfyui"
CONFIG_NAME = "deadline_cloud_submitter.json"
MARKER_NAME = ".deadline-installer.json"
RECEIPT_NAME = ".deadline-installer-uninstalled.json"
MIN_DEADLINE = (0, 60, 8)
MIN_BOTOCORE = (1, 43, 105)


class InstallError(Exception):
    """An installation error that is safe to present to the customer."""


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".deadline-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise InstallError(f"Cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise InstallError(f"{path} must contain a JSON object.")
    return value


def uninstalled_receipt(destination: Path) -> bool:
    receipt = destination / RECEIPT_NAME
    if not receipt.exists() and not receipt.is_symlink():
        return False
    if receipt.is_symlink() or not receipt.is_file():
        raise InstallError("The preserved extension has an invalid or linked uninstall receipt.")
    value = read_json(receipt)
    if (
        value.get("product") != EXTENSION_NAME
        or value.get("schema") != 1
        or value.get("status") != "uninstalled"
        or value.get("extension") != str(destination.resolve())
    ):
        raise InstallError("The preserved extension has an invalid uninstall receipt.")
    return True


def linked_path(path: Path) -> bool:
    """Treat Windows junctions like symlinks when guarding customer folders."""
    return path.is_symlink() or (os.name == "nt" and path.is_junction())


def direct_path(path: Path) -> Path:
    """Normalize a local path without allowing aliases to bypass ownership guards."""
    expanded = path.expanduser().absolute()
    unsupported = ("\n", "\r", '"', "$", "`", "%") + (("\\",) if os.name != "nt" else ())
    if any(character in str(expanded) for character in unsupported):
        raise InstallError(
            "Folder paths must not contain quotes, dollar signs, backticks, percent signs, or line breaks; backslashes are only supported on Windows."
        )
    if ".." in expanded.parts or any(linked_path(item) for item in (expanded, *expanded.parents)):
        raise InstallError(f"Use an actual folder path without symbolic links or ..: {expanded}")
    return expanded.resolve()


def validate_comfyui(path: Path) -> Path:
    root = direct_path(path)
    nodes = root / "custom_nodes"
    if not (root / "main.py").is_file() or not nodes.is_dir():
        raise InstallError("Choose the ComfyUI folder containing main.py and custom_nodes.")
    if linked_path(nodes):
        raise InstallError(
            "The custom_nodes folder is linked. Choose a normal ComfyUI installation."
        )
    if not os.access(nodes, os.W_OK | os.X_OK):
        raise InstallError(f"The custom_nodes folder is not writable: {nodes}")
    destination = nodes / EXTENSION_NAME
    if linked_path(destination):
        raise InstallError(
            f"{destination} is a development link. Remove the link before installing."
        )
    receipt = uninstalled_receipt(destination)
    if (
        destination.exists()
        and not receipt
        and not (
            (destination / "__init__.py").is_file()
            and (destination / "src/deadline/comfyui_submitter").is_dir()
        )
    ):
        raise InstallError(f"{destination} exists and is not the Deadline extension.")
    return root


def validate_prefix(prefix: Path, source: Path) -> None:
    """Reject unsafe destinations before InstallBuilder copies any product files."""
    if not prefix.expanduser().is_absolute():
        raise InstallError("Use an absolute Deadline Cloud installation directory.")
    prefix = direct_path(prefix)
    if prefix.is_relative_to(source) or source.is_relative_to(prefix):
        raise InstallError(
            "The Deadline Cloud installation directory must be separate from the ComfyUI folder."
        )
    if not prefix.exists():
        return
    if not prefix.is_dir():
        raise InstallError("Choose an empty installation folder or the existing Deadline folder.")
    if not any(prefix.iterdir()):
        return
    marker = prefix / ".deadline-cloud-for-comfyui-installer"
    if (
        linked_path(marker)
        or not marker.is_file()
        or marker.read_text(encoding="utf-8").strip() != "deadline-cloud-for-comfyui-installer-v1"
        or not (prefix / "installer_version.txt").is_file()
    ):
        raise InstallError(
            "This installation directory contains other files. Choose an empty folder "
            "or the existing Deadline Cloud for ComfyUI installation directory."
        )
    for name in (
        "payload",
        "runtime",
        "dependency_bundle",
        "install.py",
        "installation.json",
        "installer_version.txt",
        ".deadline-cloud-for-comfyui-installer",
        "rollbackBackupDirectory",
    ):
        if linked_path(prefix / name):
            raise InstallError("The existing Deadline installation contains linked managed files.")
    state = prefix / "installation.json"
    if state.exists() and read_json(state).get("extension") != str(
        source / "custom_nodes" / EXTENSION_NAME
    ):
        raise InstallError(
            "This installer already manages another ComfyUI installation. "
            "Uninstall it before selecting another folder."
        )


def runtime_paths(runtime: Path) -> tuple[Path, Path]:
    if os.name == "nt":
        return runtime / "python.exe", runtime / "Scripts/deadline.exe"
    return runtime / "bin/python3", runtime / "bin/deadline"


def run_python(python: Path, code: str) -> str:
    result = subprocess.run(
        [str(python), "-I", "-c", code], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise InstallError(
            f"The bundled Deadline runtime could not start:\n{result.stderr.strip()}"
        )
    return result.stdout.strip()


def prepare_runtime(runtime: Path) -> tuple[Path, Path]:
    """Rebuild binary launchers at their final path, including Windows .exe launchers."""
    python, cli = runtime_paths(runtime)
    if not python.is_file():
        raise InstallError(f"The installer is missing its Python runtime: {python}")
    details = json.loads(
        run_python(
            python,
            "import json, sys, importlib.metadata as m; "
            "import ssl, boto3, botocore, deadline.client.api, PySide6.QtWidgets; "
            "ssl.create_default_context(); "
            "print(json.dumps({'prefix':sys.prefix,'python':list(sys.version_info[:2]),"
            "'deadline':m.version('deadline'),'botocore':m.version('botocore')}))",
        )
    )
    if Path(details["prefix"]).resolve() != runtime.resolve():
        raise InstallError(
            "The bundled Python is not relocatable. Rebuild with a portable runtime."
        )
    if tuple(details["python"]) not in ((3, 13), (3, 14)):
        raise InstallError("The bundled runtime must use Python 3.13 or 3.14.")
    for name, minimum in (("deadline", MIN_DEADLINE), ("botocore", MIN_BOTOCORE)):
        if tuple(int(part) for part in details[name].split(".")) < minimum:
            raise InstallError(f"The installer contains an incompatible {name} version.")
    code = (
        "from distlib.scripts import ScriptMaker; import sys; "
        f"maker=ScriptMaker(None, {str(cli.parent)!r}); "
        "maker.variants={''}; maker.clobber=True; "
        "maker.make('deadline = deadline.client.cli:main', "
        "{'interpreter_args':['-I']})"
    )
    run_python(python, code)
    result = subprocess.run([str(cli), "--version"], capture_output=True, text=True, check=False)
    if result.returncode:
        raise InstallError(f"The bundled Deadline CLI could not start:\n{result.stderr.strip()}")
    return python, cli


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_file(root: Path, name: str) -> Path:
    # Manifest names are POSIX relative paths on every platform. Windows rooted and
    # drive-relative paths are dangerous even when Path.is_absolute() is false.
    relative = PurePosixPath(name)
    windows = PureWindowsPath(name)
    if (
        relative.is_absolute()
        or windows.anchor
        or ".." in relative.parts
        or not relative.parts
        or "\\" in name
        or ":" in name
    ):
        raise InstallError("The installation manifest contains an invalid file path.")
    path = root.joinpath(*relative.parts)
    for current in (path, *path.parents):
        if current == root:
            return path
        if linked_path(current):
            raise InstallError(f"Cannot modify a symbolic link: {current}")
    raise InstallError("The installation manifest contains an invalid file path.")


def payload_files(payload: Path) -> dict[str, str]:
    required = (
        "__init__.py",
        "src/deadline/comfyui_submitter/bridge.py",
        "src/deadline/comfyui_submitter/comfy_cli_job_template.yaml",
        "src/deadline/comfyui_submitter/web/deadline_submit.js",
        "src/deadline/comfyui_submitter/web/deadline_ui.js",
    )
    if any(not (payload / name).is_file() for name in required):
        raise InstallError("The installer payload is incomplete. Please use a complete installer.")
    files: dict[str, str] = {}
    for path in payload.rglob("*"):
        if path.is_symlink():
            raise InstallError("The integration payload must not contain symbolic links.")
        if path.is_file():
            name = path.relative_to(payload).as_posix()
            if name in (CONFIG_NAME, MARKER_NAME, RECEIPT_NAME):
                raise InstallError("The installer payload contains customer configuration.")
            files[name] = digest(path)
    return files


def install(
    payload: Path,
    runtime: Path,
    comfyui: Path,
    state: Path,
    version: str,
) -> Path:
    root = validate_comfyui(comfyui)
    nodes = root / "custom_nodes"
    runtime_root = direct_path(runtime)
    if runtime_root.parent.is_relative_to(root) or root.is_relative_to(runtime_root.parent):
        raise InstallError("The Deadline runtime must be separate from the ComfyUI folder.")
    files = payload_files(payload)
    python, cli = prepare_runtime(runtime.resolve())
    destination = nodes / EXTENSION_NAME
    if state.exists():
        previous_state = read_json(state)
        if previous_state.get("extension") != str(destination):
            raise InstallError(
                "This installer already manages another ComfyUI installation. "
                "Uninstall it first, or choose a different installer prefix."
            )
    configuration_path = destination / CONFIG_NAME
    marker_path = destination / MARKER_NAME
    previous_installation = read_json(marker_path) if marker_path.exists() else {}
    configuration = read_json(configuration_path) if configuration_path.exists() else {}
    preserve_configuration = configuration_path.exists() and (
        bool(previous_installation.get("preserve_configuration"))
        or digest(configuration_path) != previous_installation.get("config_hash")
    )
    configuration.update(deadline_cli=str(cli), deadline_python=str(python))
    stage = Path(tempfile.mkdtemp(prefix=".deadline-cloud-install-", dir=destination.parent))
    backup = stage.with_name(stage.name + ".backup")
    moved = False
    try:
        if destination.exists():
            shutil.copytree(destination, stage, dirs_exist_ok=True, symlinks=True)
            old = previous_installation.get("files", {})
            for name, expected in old.items():
                path = safe_file(stage, name)
                if name not in files and path.is_file() and digest(path) == expected:
                    path.unlink()
        for name in files:
            target = safe_file(stage, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(payload / name, target)
        write_json(stage / CONFIG_NAME, configuration)
        safe_file(stage, RECEIPT_NAME).unlink(missing_ok=True)
        manifest = {
            "schema": 1,
            "installation_id": uuid.uuid4().hex,
            "state_file": str(state.resolve()),
            "config_hash": digest(stage / CONFIG_NAME),
            "preserve_configuration": preserve_configuration,
            "extension": str(destination),
            "comfyui_source": str(root),
            "custom_nodes": str(nodes),
            "runtime": str(runtime.resolve()),
            "files": files,
            "version": version,
        }
        write_json(stage / MARKER_NAME, manifest)
        if destination.exists():
            destination.rename(backup)
            moved = True
        stage.rename(destination)
        try:
            write_json(state, manifest)
        except Exception:
            shutil.rmtree(destination)
            if moved:
                backup.rename(destination)
                moved = False
            raise
        if moved:
            shutil.rmtree(backup)
        return destination
    except Exception:
        if moved and backup.exists() and not destination.exists():
            backup.rename(destination)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def module_bytecode(module: Path) -> list[Path]:
    """Find generated caches for one unchanged packaged module without following links."""
    cache = module.parent / "__pycache__"
    if linked_path(cache) or not cache.is_dir():
        return []
    name = re.compile(rf"{re.escape(module.stem)}\.cpython-\d+(?:\.opt-[012])?\.pyc")
    generated = []
    for candidate in cache.iterdir():
        if not name.fullmatch(candidate.name) or candidate.is_symlink() or not candidate.is_file():
            continue
        with candidate.open("rb") as stream:
            header = stream.read(16)
        # CPython 3.13+ caches share this header layout across interpreter versions.
        # A matching filename alone must not claim an arbitrary customer file.
        if (
            len(header) == 16
            and header[2:4] == b"\r\n"
            and int.from_bytes(header[4:8], "little") in (0, 1, 3)
        ):
            generated.append(candidate)
    return generated


def uninstall(state: Path) -> list[str]:
    if not state.exists():
        return []
    manifest = read_json(state)
    destination = Path(str(manifest.get("extension", "")))
    if (
        not destination.is_absolute()
        or destination.name != EXTENSION_NAME
        or destination.parent.name != "custom_nodes"
        or manifest.get("schema") != 1
    ):
        raise InstallError("The installer state has an invalid extension path.")
    marker = destination / MARKER_NAME
    if any(linked_path(path) for path in (destination, *destination.parents)):
        state.unlink()
        return ["extension path now contains a symbolic link"]
    if not marker.is_file() or marker.is_symlink() or read_json(marker) != manifest:
        # A clone, developer copy, or newer installer has taken over this directory.
        # Removing our own runtime must not remove an integration we no longer own.
        state.unlink()
        return ["extension now managed outside this installer"]
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise InstallError("The installation manifest is invalid.")
    # Validate every path before removing any file.
    paths = {name: safe_file(destination, name) for name in files}
    # Only unchanged packaged modules own their generated bytecode. Inspect all
    # eligible caches before deleting files, so a read failure retains retry state.
    unchanged = {
        name: path for name, path in paths.items() if path.is_file() and digest(path) == files[name]
    }
    bytecode = [
        cached
        for path in unchanged.values()
        if path.suffix == ".py"
        for cached in module_bytecode(path)
    ]
    # Keep source ownership evidence until every cache removal succeeds, so a
    # partially completed cache cleanup can still be identified on retry.
    for cached in bytecode:
        cached.unlink()
    preserved = []
    for name, path in paths.items():
        if path.is_file():
            if name in unchanged:
                path.unlink()
            else:
                preserved.append(name)
    configuration = destination / CONFIG_NAME
    if configuration.is_file() and not configuration.is_symlink():
        if not manifest.get("preserve_configuration") and digest(configuration) == manifest.get(
            "config_hash"
        ):
            configuration.unlink()
        else:
            # Preserve studio settings but remove references to the runtime being uninstalled.
            try:
                config = read_json(configuration)
                python, cli = runtime_paths(Path(manifest["runtime"]))
                for key, managed in (("deadline_python", python), ("deadline_cli", cli)):
                    if config.get(key) == str(managed):
                        config.pop(key)
                write_json(configuration, config)
            except InstallError:
                preserved.append(CONFIG_NAME)
    if destination.exists():
        directories = []
        for parent, children, _ in os.walk(destination, topdown=True, followlinks=False):
            current = Path(parent)
            if linked_path(current):
                children.clear()
                continue
            # Junctions can be reported as directories even with followlinks=False.
            # Prune all linked children before the walk can reach customer files.
            children[:] = [name for name in children if not linked_path(current / name)]
            if current != destination:
                directories.append(current)
        for directory in reversed(directories):
            if directory.is_dir() and not linked_path(directory):
                try:
                    directory.rmdir()
                except OSError as error:
                    if error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                        raise InstallError(
                            f"Cannot remove empty integration folder {directory}: {error}"
                        ) from error
                    # Customer files keep their folders; only empty folders are ours to remove.
                    continue
        if any(path.name not in (MARKER_NAME, RECEIPT_NAME) for path in destination.iterdir()):
            # Keep the active marker until this write succeeds, so a handled write
            # failure still permits retry with the original ownership state.
            write_json(
                destination / RECEIPT_NAME,
                {
                    "product": EXTENSION_NAME,
                    "schema": 1,
                    "status": "uninstalled",
                    "extension": str(destination.resolve()),
                },
            )
        else:
            (destination / RECEIPT_NAME).unlink(missing_ok=True)
        marker.unlink()
        try:
            destination.rmdir()
        except OSError as error:
            if error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                # Restore ownership so an unexpected cleanup failure can be retried safely.
                write_json(marker, manifest)
                raise InstallError(
                    f"Cannot remove empty integration folder {destination}: {error}"
                ) from error
            # Preserved customer files and their receipt intentionally keep this folder.
    state.unlink()
    return preserved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set up Deadline Cloud in an existing ComfyUI.")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--comfyui", required=True, type=Path)
    validate.add_argument("--prefix", required=True, type=Path)
    setup = commands.add_parser("install")
    for name in ("payload", "runtime", "comfyui", "state"):
        setup.add_argument(f"--{name}", required=True, type=Path)
    setup.add_argument("--version", default="development")
    remove = commands.add_parser("uninstall")
    remove.add_argument("--state", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            validate_prefix(args.prefix, validate_comfyui(args.comfyui))
        elif args.command == "install":
            extension = install(
                args.payload,
                args.runtime,
                args.comfyui,
                args.state,
                args.version,
            )
            print(
                f"Installed Deadline Cloud in {extension}. Restart ComfyUI to use the Deadline panel."
            )
        else:
            preserved = uninstall(args.state)
            if preserved:
                print("Preserved modified integration files: " + ", ".join(preserved))
        return 0
    except (InstallError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Deadline Cloud setup: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
