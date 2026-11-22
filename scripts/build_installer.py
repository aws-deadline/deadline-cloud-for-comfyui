# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Stage an offline ComfyUI submitter installer and optionally build it with InstallBuilder."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {"MacOS": "osx"}


def run(args: list[str], **kwargs: Any) -> str:
    result = subprocess.run(args, check=True, capture_output=True, text=True, **kwargs)
    if result.stdout:
        print(result.stdout, end="")
    return result.stdout.strip()


def runtime_python(runtime: Path) -> Path:
    return runtime / ("python.exe" if os.name == "nt" else "bin/python3")


def validate_native_path(path: Path) -> None:
    if any(
        character in str(path.expanduser().absolute())
        for character in ('"', "$", "`", "\\", "\n", "\r")
    ):
        raise ValueError(
            "Installer build paths must not contain quotes, dollar signs, backticks, backslashes, or line breaks."
        )


def copy_runtime(source: Path, destination: Path) -> None:
    source = source.resolve()
    if (source / "pyvenv.cfg").exists():
        raise ValueError("Supply a portable CPython distribution, not a virtual environment.")
    if destination.resolve().is_relative_to(source):
        raise ValueError("The payload directory must not be inside the Python runtime.")
    for path in source.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(source):
            raise ValueError(f"The runtime has a symbolic link outside its directory: {path}")
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    # Absolute internal links must point at the relocated copy, not the builder's installation.
    for path in destination.rglob("*"):
        if path.is_symlink() and Path(os.readlink(path)).is_absolute():
            target = source / path.relative_to(destination)
            relative = target.resolve().relative_to(source)
            path.unlink()
            path.symlink_to(os.path.relpath(destination / relative, path.parent))


def runtime_metadata(python: Path) -> dict[str, Any]:
    return json.loads(
        run(
            [
                str(python),
                "-I",
                "-c",
                (
                    "import json,sys,platform; print(json.dumps({'prefix':sys.prefix,"
                    "'version':list(sys.version_info[:2]),'system':platform.system(),"
                    "'machine':platform.machine()}))"
                ),
            ]
        )
    )


def stage_bootstrap(runtime: Path, output: Path) -> None:
    """Bundle only portable Python and its standard library for selected-folder validation."""
    if output.exists():
        raise ValueError(f"Bootstrap output already exists: {output}")
    copied = output / "runtime"
    copied.mkdir(parents=True)
    (copied / "bin").mkdir()
    for path in (runtime / "bin").glob("python3*"):
        if not path.name.endswith("-config"):
            shutil.copy2(path, copied / "bin" / path.name, follow_symlinks=False)
    (copied / "lib").mkdir()
    for path in (runtime / "lib").glob("*.dylib"):
        shutil.copy2(path, copied / "lib" / path.name, follow_symlinks=False)
    for library in (runtime / "lib").glob("python3.*"):
        if library.is_dir():
            shutil.copytree(
                library,
                copied / "lib" / library.name,
                symlinks=True,
                ignore=shutil.ignore_patterns(
                    "site-packages", "__pycache__", "*.pyc", "test", "tests", "idlelib", "ensurepip"
                ),
            )
    # Preserve portable runtime license notices in both copies.
    for path in runtime.iterdir():
        if path.is_file() and any(
            word in path.name.lower() for word in ("license", "notice", "copying")
        ):
            shutil.copy2(path, copied / path.name)
    for path in copied.rglob("*"):
        if path.is_symlink():
            original = runtime / path.relative_to(copied)
            target = original.resolve()
            if not target.is_relative_to(runtime.resolve()):
                raise ValueError(f"Bootstrap runtime link leaves its directory: {original}")
            relative = target.relative_to(runtime.resolve())
            path.unlink()
            path.symlink_to(os.path.relpath(copied / relative, path.parent))
            if not path.exists():
                raise ValueError(f"Bootstrap runtime is missing a link target: {relative}")
    shutil.copy2(ROOT / "installer/install.py", output / "install.py")
    info = runtime_metadata(runtime_python(copied))
    if Path(info["prefix"]).resolve() != copied.resolve():
        raise ValueError("The bootstrap Python is not relocatable.")
    run([str(runtime_python(copied)), "-I", str(output / "install.py"), "--help"])


def build_frontend(extension: Path) -> None:
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        raise ValueError("Building an installer requires Node.js 22 or later and npm.")
    version = run([node, "--version"]).lstrip("v")
    if int(version.split(".")[0]) < 22:
        raise ValueError("Building an installer requires Node.js 22 or later.")
    frontend = ROOT / "frontend"
    run([npm, "ci", "--no-audit", "--no-fund"], cwd=frontend)
    output = extension / "src/deadline/comfyui_submitter/web"
    output.mkdir(parents=True, exist_ok=True)
    # Build directly into the payload; generated JavaScript never changes the checkout.
    run([npm, "run", "build", "--", "--outDir", str(output)], cwd=frontend)


def attribution(python: Path, output: Path) -> None:
    code = r"""
import importlib.metadata as m, json, pathlib, sys
inventory = []
notices = []
for distribution in sorted(m.distributions(), key=lambda d: d.metadata['Name'].lower()):
    name = distribution.metadata['Name']
    inventory.append({'name': name, 'version': distribution.version})
    notices.append('\n' + name + ' ' + distribution.version + '\n')
    notices.append(distribution.metadata.get('License-Expression') or distribution.metadata.get('License') or 'See bundled package license files.')
    for file in distribution.files or []:
        if ('license' in str(file).lower() or 'copying' in pathlib.Path(file).name.lower()) and pathlib.Path(file).suffix.lower() in ('', '.txt', '.md', '.rst'):
            path = distribution.locate_file(file)
            if path.is_file():
                try:
                    notices.append('\n' + str(file) + '\n' + path.read_text(encoding='utf-8'))
                except UnicodeError:
                    pass
root = pathlib.Path(sys.argv[1])
(root / 'software-inventory.json').write_text(json.dumps(inventory, indent=2) + '\n', encoding='utf-8')
(root / 'THIRD_PARTY_LICENSES').write_text('\n'.join(notices) + '\n', encoding='utf-8')
"""
    run([str(python), "-I", "-c", code, str(output)])


def validate_architecture(info: dict[str, Any], target: str) -> None:
    if target != "MacOS":
        raise ValueError("Installer builds currently support macOS only.")
    expected_system = "Darwin"
    if info["system"] != expected_system:
        raise ValueError("The supplied runtime is for a different operating system.")
    machine = info["machine"].lower()
    allowed = ("arm64", "aarch64", "x86_64", "amd64")
    if machine not in allowed:
        raise ValueError("macOS installers require an arm64 or x64 runtime.")


def stage_payload(python_runtime: Path, output: Path, target: str, wheelhouse: Path | None) -> Path:
    for path in (ROOT, python_runtime, output):
        validate_native_path(path)
    if wheelhouse is not None:
        validate_native_path(wheelhouse)
    if target != "MacOS":
        raise ValueError("Installer builds currently support macOS only.")
    host = "MacOS" if platform.system() == "Darwin" else platform.system()
    if target != host:
        raise ValueError("Build on the target operating system; dependencies contain native code.")
    if output.exists():
        raise ValueError(f"Payload output already exists: {output}. Choose a new build directory.")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".comfyui-installer-", dir=output.parent))
    try:
        runtime = temporary / "runtime"
        copy_runtime(python_runtime, runtime)
        python = runtime_python(runtime)
        info = runtime_metadata(python)
        validate_architecture(info, target)
        if tuple(info["version"]) not in ((3, 13), (3, 14)):
            raise ValueError("Supply a portable CPython 3.13 or 3.14 distribution.")
        if Path(info["prefix"]).resolve() != runtime.resolve():
            raise ValueError("The supplied Python distribution cannot run after relocation.")
        run([str(python), "-I", "-m", "ensurepip", "--upgrade"])
        command = [str(python), "-I", "-m", "pip", "install", "--only-binary=:all:", "--upgrade"]
        if wheelhouse:
            command += ["--no-index", "--find-links", str(wheelhouse.resolve())]
        command += ["-r", str(ROOT / "requirements-installer.txt")]
        run(command)
        extension = temporary / "extension"
        extension.mkdir()
        for name in ("__init__.py", "LICENSE", "NOTICE"):
            shutil.copy2(ROOT / name, extension / name)
        shutil.copytree(
            ROOT / "src/deadline/comfyui_submitter",
            extension / "src/deadline/comfyui_submitter",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "web"),
        )
        build_frontend(extension)
        shutil.copy2(ROOT / "installer/install.py", temporary / "install.py")
        stage_bootstrap(runtime, temporary / "bootstrap")
        attribution(python, extension)
        metadata = {**info, "platform": target}
        metadata.pop("prefix")
        (temporary / "build.json").write_text(json.dumps(metadata, indent=2) + "\n")
        temporary.rename(output)
        # Exercise the installed interpreter after a second relocation and regenerate launchers.
        verify_payload(output, target)
        return output
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        if output.exists():
            shutil.rmtree(output)
        raise


def verify_payload(payload: Path, target: str) -> None:
    validate_native_path(payload)
    info = runtime_metadata(runtime_python(payload / "runtime"))
    validate_architecture(info, target)
    recorded = json.loads((payload / "build.json").read_text())
    if any(info[key] != recorded[key] for key in ("version", "system", "machine")):
        raise ValueError("The staged runtime does not match its build metadata.")
    spec = importlib.util.spec_from_file_location("installer_hooks", payload / "install.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load installer hooks.")
    hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hooks)
    hooks.prepare_runtime(payload / "runtime")
    hooks.payload_files(payload / "extension")
    bootstrap = payload / "bootstrap"
    if not bootstrap.is_dir():
        raise ValueError("The staged payload has no validation bootstrap; stage it again.")
    if (bootstrap / "install.py").read_bytes() != (payload / "install.py").read_bytes():
        raise ValueError("The validation and installation helpers do not match.")
    bootstrap_info = runtime_metadata(runtime_python(bootstrap / "runtime"))
    if any(bootstrap_info[key] != info[key] for key in ("version", "system", "machine")):
        raise ValueError("The validation runtime does not match the installer runtime.")


def build(
    payload: Path, builder: Path, output: Path, target: str, version: str, local_dev: bool
) -> None:
    for path in (ROOT, payload, builder, output):
        validate_native_path(path)
    if not builder.is_file():
        raise ValueError(f"InstallBuilder executable does not exist: {builder}")
    output.mkdir(parents=True, exist_ok=True)
    python_relative = "bin/python3"
    cli_relative = "bin/deadline"
    info = json.loads((payload / "build.json").read_text())
    validate_architecture(info, target)
    suffix = "app"
    platform_name = PLATFORMS[target]
    architecture = "arm64" if info["machine"].lower() in ("arm64", "aarch64") else "x86_64"
    platform_name += "-" + architecture
    filename = f"DeadlineCloudForComfyUISubmitter-{platform_name}-installer.{suffix}"
    text = run(
        [
            str(builder),
            "build",
            str(ROOT / "installer/DeadlineCloudForComfyUISubmitter.xml"),
            PLATFORMS[target],
            "--setvars",
            f"project.outputDirectory={output.resolve()}",
            f"project.version={version}",
            f"project.installerFilename={filename}",
            f"project.parameter(payload_directory).value={payload.resolve()}",
            f"project.parameter(runtime_python_relative).value={python_relative}",
            f"project.parameter(runtime_cli_relative).value={cli_relative}",
            f"project.parameter(runtime_architecture).value={architecture}",
        ]
    )
    if "Built with an evaluation version of InstallBuilder" in text and not local_dev:
        raise ValueError("Release installers require a licensed InstallBuilder build.")
    artifact = output / filename
    if not artifact.exists():
        raise ValueError(f"InstallBuilder did not produce the expected installer: {artifact}")
    print(f"Installer: {artifact}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-runtime", type=Path)
    parser.add_argument("--reuse-payload", action="store_true")
    parser.add_argument("--payload-dir", type=Path, default=ROOT / "build/installer-payload")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist/installers")
    parser.add_argument("--platform", choices=PLATFORMS, default="MacOS")
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--install-builder-path", type=Path)
    parser.add_argument("--stage-only", action="store_true")
    parser.add_argument("--local-dev", action="store_true")
    parser.add_argument(
        "--version", default=os.environ.get("INSTALLER_VERSION", f"development-{date.today()}")
    )
    args = parser.parse_args(argv)
    try:
        if not args.stage_only and not args.install_builder_path:
            raise ValueError(
                "Specify --install-builder-path (the builder executable), or --stage-only."
            )
        if args.reuse_payload:
            payload = args.payload_dir
            if not (payload / "build.json").is_file():
                raise ValueError("--reuse-payload requires an existing verified payload directory.")
            verify_payload(payload, args.platform)
        else:
            if args.python_runtime is None:
                raise ValueError("Specify --python-runtime to stage a new payload.")
            payload = stage_payload(
                args.python_runtime, args.payload_dir, args.platform, args.wheelhouse
            )
        print(f"Verified offline payload: {payload}")
        if not args.stage_only:
            build(
                payload,
                args.install_builder_path,
                args.output_dir,
                args.platform,
                args.version,
                args.local_dev,
            )
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Installer build failed: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stdout or "", file=sys.stderr)
            print(error.stderr or "", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
