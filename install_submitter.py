#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Sets up the Deadline Cloud Submitter for a ComfyUI install.

    python install_submitter.py --comfyui /path/to/ComfyUI

1. Copies this extension into ComfyUI's custom_nodes directory. If this checkout is already in
   custom_nodes, for example cloned there by ComfyUI-Manager, it's used where it is.
2. Finds the Deadline Cloud CLI. It uses the `deadline` on the PATH when it's new enough, for
   example the one from the Deadline Cloud Submitter installer. Otherwise it creates a Python
   environment inside the extension and installs deadline[gui,console] into it. Use
   --deadline-cli to choose a CLI, or --private-cli to always install a private copy.
3. Writes deadline_cloud_submitter.json, which tells the extension which CLI to run, and
   optionally which conda channels to fill in. By default, jobs use the queue's channels.

Run it again to update the extension or the CLI. It only needs Python 3.9 or later and its
standard library, and it doesn't prompt, so it can run from configuration management.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from deadline.comfyui_submitter.submit import (
    CONFIG_FILE_NAME,
    MIN_BOTOCORE_VERSION,
    MIN_CLI_VERSION,
    SubmitError,
    parse_cli_version,
    resolve_cli_python,
)

EXTENSION_DIR_NAME = "deadline-cloud-for-comfyui"
CLI_ENV_DIR_NAME = ".deadline-cli"
CLI_REQUIREMENT = "deadline[gui,console]>={},<0.61".format(".".join(map(str, MIN_CLI_VERSION)))
BOTO3_REQUIREMENT = "boto3>={}".format(".".join(map(str, MIN_BOTOCORE_VERSION)))

COPY_IGNORE = shutil.ignore_patterns(
    ".git",
    ".github",
    ".venv",
    "venv",
    "build",
    "dist",
    "test",
    "__pycache__",
    "*.pyc",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".coverage*",
    CLI_ENV_DIR_NAME,
    CONFIG_FILE_NAME,
)


class InstallError(Exception):
    pass


def log(message: str) -> None:
    print(f"[deadline-comfyui] {message}", flush=True)


def find_custom_nodes(comfyui: Path) -> Path:
    custom_nodes = comfyui.expanduser() / "custom_nodes"
    if not custom_nodes.is_dir():
        raise InstallError(
            f"{comfyui} has no custom_nodes directory. Pass the ComfyUI directory that contains "
            "custom_nodes, the one with main.py. For Comfy Desktop, that's the ComfyUI folder "
            "inside the installation, for example ~/ComfyUI-Installs/ComfyUI/ComfyUI."
        )
    return custom_nodes.resolve()


def _is_this_extension(path: Path) -> bool:
    return (path / "__init__.py").is_file() and (
        path / "src" / "deadline" / "comfyui_submitter"
    ).is_dir()


def install_extension(
    source: Path, custom_nodes: Path, link: bool = False, force: bool = False
) -> Path:
    """An existing copy keeps its CLI environment and config, so updates don't reinstall the CLI."""
    source = source.resolve()
    if source.parent == custom_nodes:
        log(f"Using the extension in place: {source}")
        return source

    dest = custom_nodes / EXTENSION_DIR_NAME
    if dest.is_symlink():
        if dest.resolve() == source and link:
            log(f"Already linked: {dest} -> {source}")
            return dest
        dest.unlink()
    elif dest.exists() and not _is_this_extension(dest) and not force:
        raise InstallError(
            f"{dest} exists and isn't this extension. Remove it or pass --force to replace it."
        )

    if link:
        if dest.exists():
            shutil.rmtree(dest)
        dest.symlink_to(source, target_is_directory=True)
        log(f"Linked {dest} -> {source}")
        return dest

    staging = custom_nodes / f".{EXTENSION_DIR_NAME}.installing"
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(source, staging, ignore=COPY_IGNORE)
    if dest.exists():
        for kept in (CLI_ENV_DIR_NAME, CONFIG_FILE_NAME):
            if (dest / kept).exists():
                shutil.move(str(dest / kept), str(staging / kept))
        shutil.rmtree(dest)
    staging.rename(dest)
    log(f"Installed the extension to {dest}")
    return dest


def cli_env_executable(env_dir: Path, name: str) -> Path:
    if os.name == "nt":
        return env_dir / "Scripts" / f"{name}.exe"
    return env_dir / "bin" / name


def install_cli(extension_dir: Path) -> Path:
    env_dir = extension_dir / CLI_ENV_DIR_NAME
    python = cli_env_executable(env_dir, "python")
    if not python.exists():
        log(f"Creating a Python environment for the Deadline Cloud CLI in {env_dir}")
        # venv.create() copies the interpreter by default, and a copied python-build-standalone
        # interpreter (used by mise, uv and others) aborts when it starts.
        venv.create(env_dir, with_pip=True, clear=True, symlinks=os.name != "nt")
    log(f"Installing {CLI_REQUIREMENT} {BOTO3_REQUIREMENT}")
    subprocess.run(
        [str(python), "-m", "pip", "install", "--upgrade", "--disable-pip-version-check", "--quiet"]
        + [CLI_REQUIREMENT, BOTO3_REQUIREMENT],
        check=True,
    )
    return cli_env_executable(env_dir, "deadline")


def check_cli_sdk(cli: Path) -> None:
    """Rejects a CLI whose AWS SDK is too old. A version it can't read is left to submitting."""
    try:
        python = resolve_cli_python(str(cli))
    except SubmitError:
        return
    result = subprocess.run(
        [python, "-I", "-c", "import botocore; print(botocore.__version__)"],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        version = tuple(int(part) for part in result.stdout.strip().split("."))
    except ValueError:
        return
    if result.returncode == 0 and version < MIN_BOTOCORE_VERSION:
        raise InstallError(
            f"{cli} uses botocore {result.stdout.strip()}. The submitter needs "
            f"{'.'.join(map(str, MIN_BOTOCORE_VERSION))} or later. Upgrade it with "
            f"{python} -m pip install --upgrade boto3, or leave out --deadline-cli to install a "
            "private copy."
        )


def check_cli(cli: Path) -> str:
    if not (cli.is_file() and os.access(cli, os.X_OK)):
        raise InstallError(f"{cli} isn't an executable file.")
    result = subprocess.run([str(cli), "--version"], capture_output=True, text=True, check=False)
    version = parse_cli_version(result.stdout)
    if result.returncode != 0 or version is None:
        raise InstallError(f"{cli} --version failed:\n{result.stdout}{result.stderr}")
    if version < MIN_CLI_VERSION:
        raise InstallError(
            f"{cli} is version {'.'.join(map(str, version))}. The submitter needs "
            f"{'.'.join(map(str, MIN_CLI_VERSION))} or later. Upgrade it, or leave out "
            "--deadline-cli to install a private copy."
        )
    check_cli_sdk(cli)
    return ".".join(map(str, version))


def find_cli(extension_dir: Path, deadline_cli: Path | None, private: bool) -> Path:
    if deadline_cli:
        return deadline_cli.expanduser().resolve()
    if not private:
        found = shutil.which("deadline")
        if found:
            # Not resolved, so shims still work. Stored in full, as ComfyUI may have another PATH.
            on_path = Path(found).absolute()
            try:
                check_cli(on_path)
                log(f"Using the Deadline Cloud CLI on the PATH: {on_path}")
                return on_path
            except InstallError as e:
                log(f"Not using the Deadline Cloud CLI on the PATH. {e}")
    return install_cli(extension_dir)


def write_config(extension_dir: Path, deadline_cli: Path, conda_channels: str | None) -> Path:
    """A private CLI is stored relative to the extension, so the folder can be moved."""
    path = extension_dir / CONFIG_FILE_NAME
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    try:
        config["deadline_cli"] = deadline_cli.relative_to(extension_dir).as_posix()
    except ValueError:
        config["deadline_cli"] = str(deadline_cli)
    if conda_channels is not None:
        config["conda_channels"] = " ".join(conda_channels.split())
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    log(f"Wrote {path}")
    return path


def cli_setting(cli: Path, name: str) -> str:
    result = subprocess.run(
        [str(cli), "config", "get", name], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Set up the Deadline Cloud Submitter for a ComfyUI install."
    )
    parser.add_argument(
        "--comfyui",
        required=True,
        type=Path,
        help="The ComfyUI directory that contains custom_nodes.",
    )
    parser.add_argument(
        "--conda-channels",
        help=(
            "Optional. Conda Channels to fill in for every job instead of the queue's default, "
            "as a space-separated list. Pass an empty string to go back to the queue's default."
        ),
    )
    parser.add_argument(
        "--deadline-cli",
        type=Path,
        help="Use this Deadline Cloud CLI, 0.60.8 or later, instead of looking for one.",
    )
    parser.add_argument(
        "--private-cli",
        action="store_true",
        help=(
            "Install a private copy of the Deadline Cloud CLI in the extension, even if one is on "
            "the PATH."
        ),
    )
    parser.add_argument(
        "--link",
        action="store_true",
        help="Symlink this checkout into custom_nodes instead of copying it, for development.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=f"Replace a custom_nodes/{EXTENSION_DIR_NAME} directory that isn't this extension.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        custom_nodes = find_custom_nodes(args.comfyui)
        extension_dir = install_extension(REPO_ROOT, custom_nodes, link=args.link, force=args.force)
        cli = find_cli(extension_dir, args.deadline_cli, args.private_cli)
        version = check_cli(cli)
        log(f"Deadline Cloud CLI {version}: {cli}")
        write_config(extension_dir, cli, args.conda_channels)
    except (InstallError, OSError, subprocess.CalledProcessError) as e:
        log(f"Setup failed: {e}")
        return 1

    log("Done. Restart ComfyUI, then use Workflow > Submit to Deadline Cloud.")
    if not (cli_setting(cli, "defaults.farm_id") and cli_setting(cli, "defaults.queue_id")):
        log(
            f'No default farm and queue are set. Run "{cli}" config gui to sign in and choose them.'
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
