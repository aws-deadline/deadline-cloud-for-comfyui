# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Offline installer hooks, run by the installer's bundled Python."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
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


def direct_path(path: Path) -> Path:
    """Normalize a local path without allowing aliases to bypass ownership guards."""
    expanded = path.expanduser().absolute()
    unsupported = ("\n", "\r", '"', "$", "`") + (("\\",) if os.name != "nt" else ())
    if any(character in str(expanded) for character in unsupported):
        raise InstallError(
            "Folder paths must not contain quotes, dollar signs, backticks, backslashes, or line breaks."
        )
    if ".." in expanded.parts or any(item.is_symlink() for item in (expanded, *expanded.parents)):
        raise InstallError(f"Use an actual folder path without symbolic links or ..: {expanded}")
    return expanded.resolve()


def metadata_text(path: Path, limit: int = 65536) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise InstallError(f"Invalid ComfyUI metadata: {path}")
    return path.read_text(encoding="utf-8")


def launcher_source(folder: Path) -> Path:
    source_text = metadata_text(folder / "comfyui-source.txt", 4096).strip()
    if not source_text or "\n" in source_text:
        raise InstallError("comfyui-source.txt must contain one existing source folder.")
    source = Path(source_text).expanduser()
    return source if source.is_absolute() else folder / source


def python_script_operand(args: list[str]) -> int | None:
    """Find Python's actual script operand, excluding -c/-m and script arguments."""
    if not args or not Path(args[0]).name.startswith("python"):
        return None
    index = 1
    while index < len(args):
        arg = args[index]
        if arg == "--":
            return index + 1 if index + 1 < len(args) else None
        if not arg.startswith("-"):
            return index
        if arg in ("-c", "-m") or arg.startswith(("-c", "-m")):
            return None
        if arg in ("-X", "-W"):
            index += 2
        elif arg.startswith(("-X", "-W")) or arg in (
            "-I",
            "-B",
            "-E",
            "-s",
            "-S",
            "-u",
            "-O",
            "-OO",
            "-q",
            "-v",
            "-vv",
        ):
            index += 1
        else:
            return None
    return None


def base_argument(args: list[str]) -> str | None:
    bases = []
    for index, arg in enumerate(args):
        if arg == "--base-directory":
            if index + 1 >= len(args) or args[index + 1].startswith("--"):
                raise InstallError("ComfyUI has no base-directory value.")
            bases.append(args[index + 1])
        elif arg.startswith("--base-directory="):
            bases.append(arg.partition("=")[2])
    if len(set(bases)) > 1 or bases and not bases[0]:
        raise InstallError("ComfyUI names ambiguous base directories.")
    return bases[0] if bases else None


def launcher_value(value: str, folder: Path, source: Path, text: str) -> str:
    anchored_expressions = (
        '"$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"',
        '"$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        '"$(cd -- "$(dirname -- "$0")" && pwd)"',
        '"$(cd "$(dirname "$0")" && pwd)"',
    )
    variables = re.findall(r"\$(?:\{([A-Za-z_][A-Za-z_0-9]*)\}|([A-Za-z_][A-Za-z_0-9]*))", value)
    for variable in variables:
        name = variable[0] or variable[1]
        assignments = re.findall(rf"(?m)^{re.escape(name)}=([^\n]+)$", text)
        replacement = None
        if len(assignments) == 1:
            expression = assignments[0].strip()
            if expression in anchored_expressions:
                replacement = str(folder)
            else:
                # A source variable may only read this launcher's source pointer.
                match = re.fullmatch(
                    r'"\$\(cat "(\$[A-Za-z_][A-Za-z_0-9]*|\$\{[A-Za-z_][A-Za-z_0-9]*\})/comfyui-source\.txt"\)"',
                    expression,
                )
                if match:
                    anchor_name = match[1].removeprefix("$").strip("{}")
                    anchor_assignments = re.findall(
                        rf"(?m)^{re.escape(anchor_name)}=([^\n]+)$", text
                    )
                    if (
                        len(anchor_assignments) == 1
                        and anchor_assignments[0].strip() in anchored_expressions
                    ):
                        replacement = str(source)
        if replacement is None:
            raise InstallError(
                "Choose the source and base folders manually; the launcher uses dynamic paths."
            )
        value = re.sub(
            r"\$(?:\{" + re.escape(name) + r"\}|" + re.escape(name) + r"(?![A-Za-z_0-9]))",
            lambda _match, replacement=replacement: replacement,
            value,
        )
    unsupported = ("$", "`", "\n") + (("\\",) if os.name != "nt" else ())
    if any(character in value for character in unsupported):
        raise InstallError(
            "Choose the source and base folders manually; the launcher uses dynamic paths."
        )
    return value


def launcher_layout(folder: Path) -> tuple[Path, Path]:
    """Read one literal Python main.py exec, never evaluating or running shell code."""
    source = direct_path(launcher_source(folder))
    text = metadata_text(folder / "launch.sh").replace("\\\n", " ")
    if re.search(
        r"(?m)^\s*(if|then|else|elif|fi|case|esac|for|while|until|function|exit|return)\b|^\s*[A-Za-z_][A-Za-z_0-9]*\s*\(\)",
        text,
    ):
        raise InstallError(
            "Choose the source and base folders manually; the launcher uses shell control flow."
        )
    commands = re.findall(r"(?m)^\s*exec\s+([^\n]+)$", text)
    if len(commands) != 1:
        raise InstallError(
            "Choose the source and base folders manually; the launcher has no single Python exec."
        )
    try:
        args = shlex.split(commands[0], comments=True)
    except ValueError as error:
        raise InstallError("The launcher metadata cannot be read safely.") from error
    if not args or any(token in (";", "&&", "||", "|", "&", ">", "<") for token in args):
        raise InstallError("The launcher has no single safe Python command.")
    args[0] = launcher_value(args[0], folder, source, text)
    index = python_script_operand(args)
    if index is None or Path(args[index]).name != "main.py":
        raise InstallError(
            "Choose the source and base folders manually; the launcher is not a Python main.py exec."
        )
    script = Path(launcher_value(args[index], folder, source, text)).expanduser()
    if not script.is_absolute():
        raise InstallError(
            "Choose the source and base folders manually; the launcher uses a relative main.py path."
        )
    if direct_path(script.parent) != source:
        raise InstallError("The launcher's main.py does not match comfyui-source.txt.")
    base = base_argument(args[index + 1 :])
    nodes = source / "custom_nodes"
    if base is not None:
        base_path = Path(launcher_value(base, folder, source, text)).expanduser()
        if not base_path.is_absolute():
            raise InstallError(
                "Choose the source and base folders manually; the launcher uses a relative base path."
            )
        nodes = base_path / "custom_nodes"
    return source, nodes


def resolve_layout(
    path: Path, custom_nodes: Path | None = None, base_directory: Path | None = None
) -> tuple[Path, Path]:
    if custom_nodes is not None and base_directory is not None:
        raise InstallError("Choose a custom_nodes folder or a base directory, not both.")
    root = direct_path(path)
    if (root / "comfyui-source.txt").exists():
        if custom_nodes is not None or base_directory is not None:
            root = launcher_source(root)
            inferred_nodes = root / "custom_nodes"
        else:
            root, inferred_nodes = launcher_layout(root)
    else:
        inferred_nodes = root / "custom_nodes"
    root = direct_path(root)
    selected_nodes = custom_nodes or (
        base_directory / "custom_nodes" if base_directory else inferred_nodes
    )
    if selected_nodes.is_symlink():
        raise InstallError("The custom_nodes folder is a symbolic link. Choose its actual folder.")
    nodes = direct_path(selected_nodes)
    if not (root / "main.py").is_file() or not nodes.is_dir() or nodes.name != "custom_nodes":
        raise InstallError(
            "Choose your existing ComfyUI source folder containing main.py, or its launcher "
            "folder containing comfyui-source.txt. If ComfyUI uses --base-directory, choose "
            "that base folder so Deadline is added to its active custom_nodes."
        )
    if not os.access(nodes, os.W_OK | os.X_OK):
        raise InstallError(f"The custom_nodes folder is not writable: {nodes}")
    destination = nodes / EXTENSION_NAME
    if destination.is_symlink():
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
    return root, nodes


def validate_comfyui(path: Path, custom_nodes: Path | None = None) -> Path:
    root, nodes = resolve_layout(path, custom_nodes)
    try:
        with tempfile.NamedTemporaryFile(prefix=".deadline-write-check-", dir=nodes):
            pass
    except OSError as error:
        raise InstallError(f"Cannot write to {nodes}: {error}") from error
    return root


def layout_output(directory: Path, source: Path, nodes: Path) -> None:
    """Write only the selected source/active-node pair for the native wizard."""
    directory.mkdir(parents=True, exist_ok=True)
    write_json(
        directory / "layout.json", {"schema": 1, "source": str(source), "custom_nodes": str(nodes)}
    )
    for name, value in (("source", source), ("custom_nodes", nodes)):
        (directory / (name + ".txt")).write_text(str(value), encoding="utf-8")


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
        if current.is_symlink():
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
    custom_nodes: Path | None = None,
) -> Path:
    root, nodes = resolve_layout(comfyui, custom_nodes)
    validate_comfyui(root, nodes)
    runtime_root = direct_path(runtime)
    for host in (root, nodes):
        if runtime_root.parent.is_relative_to(host) or host.is_relative_to(runtime_root.parent):
            raise InstallError(
                "The Deadline runtime must be separate from ComfyUI source and custom_nodes."
            )
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
    if any(path.is_symlink() for path in (destination, *destination.parents)):
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
    preserved = []
    for name, path in paths.items():
        if path.is_file():
            if digest(path) == files[name]:
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
        for directory in sorted(
            destination.rglob("*"), key=lambda item: len(item.parts), reverse=True
        ):
            if directory.is_dir() and not directory.is_symlink():
                try:
                    directory.rmdir()
                except OSError:
                    pass
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
        except OSError:
            pass
    state.unlink()
    return preserved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set up Deadline Cloud in an existing ComfyUI.")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--comfyui", required=True, type=Path)
    validate.add_argument("--custom-nodes", type=Path)
    resolve = commands.add_parser("resolve")
    resolve.add_argument("--comfyui", required=True, type=Path)
    resolve.add_argument("--custom-nodes", type=Path)
    resolve.add_argument("--base-directory", type=Path)
    resolve.add_argument("--output-dir", type=Path)
    setup = commands.add_parser("install")
    for name in ("payload", "runtime", "comfyui", "state"):
        setup.add_argument(f"--{name}", required=True, type=Path)
    setup.add_argument("--version", default="development")
    setup.add_argument("--custom-nodes", type=Path)
    remove = commands.add_parser("uninstall")
    remove.add_argument("--state", required=True, type=Path)
    configure = commands.add_parser("configure")
    configure.add_argument("--runtime", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            validate_comfyui(args.comfyui, args.custom_nodes)
        elif args.command == "resolve":
            source, nodes = resolve_layout(args.comfyui, args.custom_nodes, args.base_directory)
            result = {"schema": 1, "source": str(source), "custom_nodes": str(nodes)}
            if args.output_dir:
                layout_output(args.output_dir, source, nodes)
            print(json.dumps(result, indent=2))
        elif args.command == "install":
            extension = install(
                args.payload,
                args.runtime,
                args.comfyui,
                args.state,
                args.version,
                args.custom_nodes,
            )
            print(
                f"Installed Deadline Cloud in {extension}. Restart ComfyUI to use the Deadline panel."
            )
        elif args.command == "uninstall":
            preserved = uninstall(args.state)
            if preserved:
                print("Preserved modified integration files: " + ", ".join(preserved))
        else:
            _, cli = runtime_paths(args.runtime)
            subprocess.run([str(cli), "config", "gui"], check=True)
        return 0
    except (InstallError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Deadline Cloud setup: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
