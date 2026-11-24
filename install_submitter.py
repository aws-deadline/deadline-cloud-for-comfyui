#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Sets up the Deadline Cloud Submitter for a ComfyUI install.

    python install_submitter.py --comfyui /path/to/ComfyUI

1. Builds the panel: runs npm ci and npm run build in frontend/, which compiles its TypeScript
   into the extension's web folder.
2. Copies the extension's runtime files, listed in RUNTIME_FILES, and the compiled panel into
   ComfyUI's custom_nodes directory. Nothing else in the checkout is copied. If this checkout is
   already in custom_nodes, for example cloned there by ComfyUI-Manager, it's used where it is.
3. Finds the Deadline Cloud CLI. It uses the `deadline` on the PATH when it's new enough and
   runs on a Python the extension can find, since the panel runs that Python. Otherwise it
   creates a Python environment inside the extension and installs deadline[gui,console] into it. Use
   --deadline-cli to choose a CLI, or --private-cli to always install a private copy.
4. Writes deadline_cloud_submitter.json, which tells the extension which CLI to run, and
   optionally which conda channels to fill in. By default, jobs use the queue's channels.

Run it again to update the extension or the CLI. It needs Python 3.13 or later, with only its
standard library, and Node.js 22 or later with npm to build the panel. It doesn't prompt, so it
can run from configuration management.
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
    write_private_text,
)

EXTENSION_DIR_NAME = "deadline-cloud-for-comfyui"
CLI_ENV_DIR_NAME = ".deadline-cli"
CLI_REQUIREMENT = "deadline[gui,console]>={},<0.61".format(".".join(map(str, MIN_CLI_VERSION)))
BOTO3_REQUIREMENT = "boto3>={}".format(".".join(map(str, MIN_BOTOCORE_VERSION)))

FRONTEND_DIR = Path("frontend")
PACKAGE_DIR = Path("src", "deadline", "comfyui_submitter")
WEB_DIR = PACKAGE_DIR / "web"
PANEL_FILES = ("deadline_submit.js", "deadline_ui.js")
MIN_NODE_VERSION = (22,)

# Everything a copied install has, relative to the checkout. A checkout can also hold local
# settings, credentials, logs, old builds and experiments, and other tools and users may be able to
# read custom_nodes, so only these files are copied. ComfyUI imports the root __init__.py, which
# imports the package under src/. src/deadline has no __init__.py, as it's a namespace package.
# install_submitter.py is copied so the installed copy can run it again to update its CLI. A module
# added to the package has to be added here too, which a unit test checks.
RUNTIME_FILES = (
    Path("__init__.py"),
    Path("install_submitter.py"),
    Path("LICENSE"),
    Path("NOTICE"),
    Path("README.md"),
    PACKAGE_DIR / "__init__.py",
    PACKAGE_DIR / "bridge.py",
    PACKAGE_DIR / "comfy_cli_job_template.yaml",
    PACKAGE_DIR / "get_local_assets.py",
    PACKAGE_DIR / "routes.py",
    PACKAGE_DIR / "submit.py",
    # The panel that build_panel compiles. ComfyUI loads every script in the web folder, so other
    # JavaScript there, such as from an older build, isn't copied.
    *(WEB_DIR / name for name in PANEL_FILES),
)


class InstallError(Exception):
    pass


def log(message: str) -> None:
    print(f"[deadline-comfyui] {message}", flush=True)


def _node_version(node: str) -> tuple[int, ...] | None:
    result = subprocess.run([node, "--version"], capture_output=True, text=True, check=False)
    try:
        return tuple(int(part) for part in result.stdout.strip().lstrip("v").split("."))
    except ValueError:
        return None


def build_panel(source: Path) -> None:
    """
    Compiles the panel's TypeScript in frontend/ into the web folder that ComfyUI serves. A copy
    without frontend/, such as an installed extension, already has the compiled panel.
    """
    frontend = source / FRONTEND_DIR
    if not (frontend / "package.json").is_file():
        if all((source / WEB_DIR / name).is_file() for name in PANEL_FILES):
            return
        raise InstallError(
            f"{source} has neither the panel's TypeScript in {FRONTEND_DIR} nor its compiled "
            f"JavaScript in {WEB_DIR.as_posix()}. Run this script from a complete checkout."
        )
    required = ".".join(map(str, MIN_NODE_VERSION))
    node, npm = shutil.which("node"), shutil.which("npm")
    if not (node and npm):
        raise InstallError(
            f"Building the panel needs Node.js {required} or later, with npm, on the PATH. "
            "Install it from https://nodejs.org, then run this script again."
        )
    version = _node_version(node)
    if version is None or version < MIN_NODE_VERSION:
        found = ".".join(map(str, version)) if version else "an unknown version"
        raise InstallError(
            f"{node} is {found}. Building the panel needs Node.js {required} or later."
        )
    log(f"Building the panel in {frontend}")
    for args in (["ci", "--no-audit", "--no-fund"], ["run", "build"]):
        result = subprocess.run(
            [npm, *args], cwd=frontend, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            raise InstallError(
                f"npm {' '.join(args)} failed in {frontend}:\n{result.stdout}{result.stderr}"
            )
    missing = [name for name in PANEL_FILES if not (source / WEB_DIR / name).is_file()]
    if missing:
        raise InstallError(f"npm run build didn't create {', '.join(missing)} in {WEB_DIR}.")


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


# What a source checkout has and an installed copy doesn't, since the copy leaves them out.
_CHECKOUT_MARKERS = (".git", ".hg", ".svn")


def _is_installed_copy(path: Path) -> bool:
    """
    Whether a folder is a copy this installer made, which it can replace. A checkout of the
    extension, such as one cloned into custom_nodes, has the same files, but replacing it would
    delete its history and uncommitted changes.
    """
    return _is_this_extension(path) and not any(
        os.path.lexists(path / marker) for marker in _CHECKOUT_MARKERS
    )


def _link_on_the_way_to(source: Path, relative: Path) -> Path | None:
    """
    The first folder or file from source down to source / relative that's a symlink or a Windows
    junction, if any. os.path.islink is False for a junction, so it's checked for separately.
    """
    path = source
    for part in relative.parts:
        path = path / part
        if os.path.islink(path) or os.path.isjunction(path):
            return path
    return None


def _check_runtime_files(source: Path) -> None:
    """
    Fails if a file in RUNTIME_FILES is missing from source, or is reached through a link, before
    anything in custom_nodes is replaced. A link can point anywhere, such as at a models folder or
    ~/.ssh, and copying it would copy its target.
    """
    links: list[str] = []
    missing: list[str] = []
    for relative in RUNTIME_FILES:
        link = _link_on_the_way_to(source, relative)
        if link is not None:
            name = link.relative_to(source).as_posix()
            if name not in links:
                links.append(name)
        elif not (source / relative).is_file():
            missing.append(relative.as_posix())
    if links:
        raise InstallError(
            f"{source} has links in place of files the extension needs: {', '.join(links)}. "
            "Links and Windows junctions can point anywhere, so the installer doesn't copy them. "
            "Replace them with the files from the repository."
        )
    if missing:
        raise InstallError(
            f"{source} is missing {', '.join(missing)}, which the extension needs. Run this "
            "script from a complete checkout."
        )


def _copy_runtime_files(source: Path, staging: Path) -> None:
    # Made new, so the copy can't be written through a link left at the staging folder's path.
    staging.mkdir()
    for relative in RUNTIME_FILES:
        (staging / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, staging / relative)


def install_extension(
    source: Path, custom_nodes: Path, link: bool = False, force: bool = False
) -> Path:
    """
    Copies the files in RUNTIME_FILES from source into custom_nodes, or with link, links to
    source. An existing copy keeps its CLI environment and config, so updates don't reinstall the
    CLI. Replacing a copy with a link keeps its config.
    """
    source = source.resolve()
    if source.parent == custom_nodes:
        log(f"Using the extension in place: {source}")
        return source
    if not link:
        _check_runtime_files(source)

    dest = custom_nodes / EXTENSION_DIR_NAME
    if dest.is_symlink():
        if dest.resolve() == source and link:
            log(f"Already linked: {dest} -> {source}")
            return dest
        dest.unlink()
    elif dest.exists() and not _is_installed_copy(dest) and not force:
        if _is_this_extension(dest):
            raise InstallError(
                f"{dest} is a checkout of the extension. Run the installer from it to use it in "
                "place, or pass --force to replace it, which deletes its history and changes."
            )
        raise InstallError(
            f"{dest} exists and isn't this extension. Remove it or pass --force to replace it."
        )

    if link:
        if dest.exists():
            _keep_config_when_linking(dest, source)
            shutil.rmtree(dest)
        dest.symlink_to(source, target_is_directory=True)
        log(f"Linked {dest} -> {source}")
        return dest

    staging = custom_nodes / f".{EXTENSION_DIR_NAME}.installing"
    if staging.exists():
        shutil.rmtree(staging)
    _copy_runtime_files(source, staging)
    if dest.exists():
        for kept in (CLI_ENV_DIR_NAME, CONFIG_FILE_NAME):
            if (dest / kept).exists():
                shutil.move(str(dest / kept), str(staging / kept))
        shutil.rmtree(dest)
    staging.rename(dest)
    log(f"Installed the extension to {dest}")
    return dest


def _keep_config_when_linking(copy: Path, source: Path) -> None:
    """
    Moves a copied install's config, with its CLI and conda channels, into the checkout that
    replaces it, unless the checkout has its own. A Python environment breaks when it's moved, so
    the copy's private CLI is installed again in the checkout instead.
    """
    config = copy / CONFIG_FILE_NAME
    if config.exists() and not (source / CONFIG_FILE_NAME).exists():
        shutil.move(str(config), str(source / CONFIG_FILE_NAME))
        log(f"Moved {CONFIG_FILE_NAME} from {copy} to {source}")
    if (copy / CLI_ENV_DIR_NAME).exists():
        log(f"Replacing {copy}, and its private Deadline Cloud CLI, with a link to {source}")


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
    """
    Rejects a CLI without a Python the panel can run, or whose AWS SDK is too old. A version it
    can't read is left to submitting.
    """
    try:
        python = resolve_cli_python(str(cli))
    except SubmitError as e:
        # A standalone build of the CLI has no Python of its own, and the panel needs one.
        raise InstallError(f"{e} Or leave out --deadline-cli to install a private copy.") from e
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
    # It names the queue's S3 conda channel, so like job bundles only its owner can read it.
    write_private_text(path, json.dumps(config, indent=2) + "\n")
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
        build_panel(REPO_ROOT)
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
