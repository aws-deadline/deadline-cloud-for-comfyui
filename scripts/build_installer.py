# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Stage an offline ComfyUI submitter installer and optionally build it with InstallBuilder."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

# Direct execution and importlib-based tests must both resolve these repository build tools.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.deps_bundle import (
    BUNDLE_RELATIVE,
    create_dependency_bundle,
    extract_dependency_bundle,
)
from scripts.find_installbuilder import resolve_builder

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {"MacOS": "osx", "Windows": "windows-x64"}


def run(args: list[str], **kwargs: Any) -> str:
    result = subprocess.run(args, check=True, capture_output=True, text=True, **kwargs)
    if result.stdout:
        print(result.stdout, end="")
    return result.stdout.strip()


def runtime_python(runtime: Path) -> Path:
    return runtime / ("python.exe" if os.name == "nt" else "bin/python3")


def validate_native_path(path: Path) -> None:
    unsafe = ('"', "$", "%", "`", "\n", "\r") + (("\\",) if os.name != "nt" else ())
    if any(character in str(path.expanduser().absolute()) for character in unsafe):
        raise ValueError(
            "Installer build paths must not contain quotes, dollar or percent signs, backticks, "
            "literal backslashes on macOS, or line breaks."
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
                    "import json,sys,platform,sysconfig,os; "
                    "print(json.dumps({'prefix':sys.prefix,"
                    "'version':list(sys.version_info[:2]),'system':platform.system(),"
                    "'machine':platform.machine(),'site_packages_relative':"
                    "os.path.relpath(sysconfig.get_path('purelib'),sys.prefix).replace(os.sep,'/')}))"
                ),
            ]
        )
    )


def runtime_site_packages(runtime: Path, info: dict[str, Any]) -> Path:
    relative = PurePosixPath(info["site_packages_relative"])
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or "\\" in str(relative)
        or ":" in str(relative)
    ):
        raise ValueError("The runtime site-packages directory must be inside the portable runtime.")
    output = runtime.joinpath(*relative.parts)
    if not output.resolve().is_relative_to(runtime.resolve()):
        raise ValueError("The runtime site-packages directory leaves its portable runtime.")
    expected = (
        "Lib/site-packages"
        if info["system"] == "Windows"
        else f"lib/python{info['version'][0]}.{info['version'][1]}/site-packages"
    )
    if str(relative) != expected:
        raise ValueError(
            f"The portable runtime requires a standard site-packages layout: {expected}"
        )
    return output


def strip_runtime_dependencies(runtime: Path, info: dict[str, Any]) -> None:
    """Keep CPython separate from the SDK, including when the supplied seed has build tools."""
    site_packages = runtime_site_packages(runtime, info)
    if site_packages.exists():
        shutil.rmtree(site_packages)
    scripts = runtime / ("Scripts" if info["system"] == "Windows" else "bin")
    if scripts.is_dir():
        for path in scripts.iterdir():
            if path.name.lower().startswith("python"):
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()


def validate_bare_runtime(runtime: Path, info: dict[str, Any]) -> None:
    site_packages = runtime_site_packages(runtime, info)
    if not site_packages.exists():
        return
    build_tools = {
        "pip",
        "setuptools",
        "wheel",
        "_distutils_hack",
        "distutils-precedence.pth",
        "__pycache__",
    }
    for path in site_packages.iterdir():
        if path.name not in build_tools and not re.fullmatch(
            r"(?:pip|setuptools|wheel)-.+\.dist-info", path.name
        ):
            raise ValueError(f"The packed runtime must not contain SDK dependencies: {path}")


def build_frontend(extension: Path) -> None:
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        raise ValueError("Building an installer requires Node.js 22 or later and npm.")
    version = run([node, "--version"]).lstrip("v")
    if int(version.split(".")[0]) < 22:
        raise ValueError("Building an installer requires Node.js 22 or later.")
    frontend = ROOT / "frontend"
    # npm.cmd needs the Windows command processor; use npm's JS entry point through Node.
    command = [npm]
    if os.name == "nt":
        npm_cli = Path(node).parent / "node_modules/npm/bin/npm-cli.js"
        if not npm_cli.is_file():
            raise ValueError("The Windows Node installation is missing npm-cli.js.")
        command = [node, str(npm_cli)]
    output = extension / "src/deadline/comfyui_submitter/web"
    output.mkdir(parents=True, exist_ok=True)
    # Use the same source and package lock without mutating checkout tooling or racing panel tests.
    with tempfile.TemporaryDirectory(prefix="comfyui-installer-frontend-") as directory:
        staged = Path(directory) / "frontend"
        shutil.copytree(frontend, staged, ignore=shutil.ignore_patterns("node_modules"))
        run([*command, "ci", "--no-audit", "--no-fund"], cwd=staged)
        run([*command, "run", "build", "--", "--outDir", str(output.resolve())], cwd=staged)


def install_dependencies(python: Path, wheelhouse: Path | None, target: Path | None = None) -> None:
    """Install only pinned, hash-verified wheels; a target stages the separate SDK bundle."""
    command = [
        str(python),
        "-I",
        "-m",
        "pip",
        "install",
        "--only-binary=:all:",
        "--require-hashes",
        "--force-reinstall",
        "--no-compile",
    ]
    if target is not None:
        command += ["--target", str(target.resolve())]
    if wheelhouse is not None:
        command += ["--no-index", "--find-links", str(wheelhouse.resolve())]
    command += ["-r", str(ROOT / "requirements-installer.txt")]
    run(command)


def attribution(python: Path, output: Path, site_packages: Path | None = None) -> None:
    code = r"""
import importlib.metadata as m, json, pathlib, sys
inventory = []
notices = []
distributions = m.distributions(path=[sys.argv[2]]) if len(sys.argv) > 2 else m.distributions()
for distribution in sorted(distributions, key=lambda d: d.metadata['Name'].lower()):
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
    command = [str(python), "-I", "-c", code, str(output)]
    if site_packages is not None:
        command.append(str(site_packages))
    run(command)


def validate_architecture(info: dict[str, Any], target: str) -> None:
    if target not in PLATFORMS:
        raise ValueError("Installer builds support macOS and Windows only.")
    if info["system"] != {"MacOS": "Darwin", "Windows": "Windows"}[target]:
        raise ValueError("The supplied runtime is for a different operating system.")
    allowed = (
        ("x86_64", "amd64") if target == "Windows" else ("arm64", "aarch64", "x86_64", "amd64")
    )
    if info["machine"].lower() not in allowed:
        raise ValueError("Windows installers require an x64 runtime; macOS supports arm64 or x64.")


def stage_payload(python_runtime: Path, output: Path, target: str, wheelhouse: Path | None) -> Path:
    for path in (ROOT, python_runtime, output):
        validate_native_path(path)
    if wheelhouse is not None:
        validate_native_path(wheelhouse)
    host = "MacOS" if platform.system() == "Darwin" else platform.system()
    if target not in PLATFORMS or target != host:
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
        strip_runtime_dependencies(runtime, info)
        run([str(python), "-I", "-m", "ensurepip", "--upgrade"])
        validate_bare_runtime(runtime, info)
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
        with tempfile.TemporaryDirectory(prefix=".sdk-", dir=temporary) as directory:
            dependencies = Path(directory) / "site-packages"
            install_dependencies(python, wheelhouse, dependencies)
            attribution(python, extension, dependencies)
            create_dependency_bundle(dependencies, temporary / BUNDLE_RELATIVE)
        metadata = {**info, "platform": target, "dependency_bundle": BUNDLE_RELATIVE}
        metadata.pop("prefix")
        (temporary / "build.json").write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )
        temporary.rename(output)
        verify_payload(output, target)
        return output
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        if output.exists():
            shutil.rmtree(output)
        raise


def verify_payload(payload: Path, target: str) -> None:
    """Exercise the relocated, extracted SDK without putting SDK bytes into the packed runtime."""
    validate_native_path(payload)
    runtime = payload / "runtime"
    info = runtime_metadata(runtime_python(runtime))
    validate_architecture(info, target)
    recorded = json.loads((payload / "build.json").read_text(encoding="utf-8"))
    if any(
        info[key] != recorded[key]
        for key in ("version", "system", "machine", "site_packages_relative")
    ):
        raise ValueError("The staged runtime does not match its build metadata.")
    if Path(info["prefix"]).resolve() != runtime.resolve():
        raise ValueError("The staged runtime is not relocatable.")
    validate_bare_runtime(runtime, info)
    if recorded.get("dependency_bundle") != BUNDLE_RELATIVE:
        raise ValueError(
            "The staged payload has no separate SDK dependency archive; stage it again."
        )
    spec = importlib.util.spec_from_file_location("installer_hooks", payload / "install.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load installer hooks.")
    hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hooks)
    with tempfile.TemporaryDirectory(
        prefix=".comfyui-verification-", dir=payload.parent
    ) as directory:
        verification = Path(directory) / "runtime"
        copy_runtime(runtime, verification)
        extract_dependency_bundle(
            payload / BUNDLE_RELATIVE, runtime_site_packages(verification, info)
        )
        hooks.prepare_runtime(verification)
    hooks.payload_files(payload / "extension")


def build(
    payload: Path,
    builder: Path,
    output: Path,
    target: str,
    version: str,
    local_dev: bool,
    installer_source_path: Path | None = None,
) -> Path:
    source = installer_source_path or ROOT / "installer/DeadlineCloudForComfyUISubmitter.xml"
    for path in (ROOT, payload, builder, output, source):
        validate_native_path(path)
    if not builder.is_file():
        raise ValueError(f"InstallBuilder executable does not exist: {builder}")
    if not source.is_file():
        raise ValueError(f"InstallBuilder project does not exist: {source}")
    output.mkdir(parents=True, exist_ok=True)
    info = json.loads((payload / "build.json").read_text(encoding="utf-8"))
    validate_architecture(info, target)
    if target == "Windows":
        python_relative = "python.exe"
        architecture, filename = "x64", "DeadlineCloudForComfyUISubmitter-windows-x64-installer.exe"
    else:
        python_relative = "bin/python3"
        architecture = "arm64" if info["machine"].lower() in ("arm64", "aarch64") else "x86_64"
        filename = f"DeadlineCloudForComfyUISubmitter-osx-{architecture}-installer.app"
    text = run(
        [
            str(builder),
            "build",
            str(source),
            PLATFORMS[target],
            "--setvars",
            f"project.outputDirectory={output.resolve()}",
            f"project.version={version}",
            f"project.installerFilename={filename}",
            f"project.parameter(payload_directory).value={payload.resolve()}",
            f"project.parameter(runtime_python_relative).value={python_relative}",
            f"project.parameter(runtime_site_packages_relative).value={info['site_packages_relative']}",
            f"project.parameter(runtime_architecture).value={architecture}",
        ]
    )
    if "Built with an evaluation version of InstallBuilder" in text and not local_dev:
        raise ValueError("Release installers require a licensed InstallBuilder build.")
    artifact = output / filename
    if (
        not artifact.exists()
        or (target == "Windows" and not artifact.is_file())
        or (target == "MacOS" and not artifact.is_dir())
    ):
        raise ValueError(f"InstallBuilder did not produce the expected installer: {artifact}")
    print(f"Installer: {artifact}")
    return artifact


def git_output(*arguments: str) -> str:
    """Read local Git metadata without modifying the checkout or fetching tags."""
    try:
        return subprocess.check_output(
            ["git", *arguments], cwd=ROOT, text=True, stderr=subprocess.DEVNULL, timeout=15
        ).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


def semantic_version(tag: str) -> str | None:
    match = re.fullmatch(
        r"v?((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
        r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)",
        tag,
    )
    if match is None:
        return None
    version = match.group(1)
    release = version.split("+", 1)[0]
    if "-" in release:
        prerelease = release.split("-", 1)[1]
        if any(
            part.isdigit() and len(part) > 1 and part.startswith("0")
            for part in prerelease.split(".")
        ):
            return None
    return version


def installer_version() -> str:
    """Use release tags for licensed builds and identify untagged development checkouts."""
    override = os.environ.get("INSTALLER_VERSION")
    if override:
        return override
    short_head = git_output("rev-parse", "--short", "HEAD") or "unknown"
    for tag in git_output("tag", "--points-at", "HEAD", "--sort=-version:refname").splitlines():
        version = semantic_version(tag)
        if version is not None:
            return version
    tags = [
        tag
        for tag in git_output("tag", "--merged", "HEAD").splitlines()
        if semantic_version(tag) is not None
    ]
    if tags:
        patterns = [argument for tag in tags for argument in ("--match", tag)]
        nearest = git_output("describe", "--tags", "--abbrev=0", *patterns, "HEAD")
        version = semantic_version(nearest)
        if version is not None:
            return version + "." + short_head
    return f"development-{date.today()}.{short_head}"


def normalize_platform(value: str) -> str:
    for name in PLATFORMS:
        if value.lower() == name.lower():
            return name
    raise argparse.ArgumentTypeError("Supported platforms are MacOS and Windows.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python-runtime", type=Path, default=os.environ.get("COMFYUI_INSTALLER_PYTHON_RUNTIME")
    )
    parser.add_argument("--reuse-payload", action="store_true")
    parser.add_argument("--payload-dir", type=Path, default=ROOT / "build/installer-payload")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist/installers")
    host = "MacOS" if platform.system() == "Darwin" else platform.system()
    parser.add_argument("--platform", type=normalize_platform, default=host)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--install-builder-path", type=Path)
    parser.add_argument("--install-builder-s3-bucket")
    parser.add_argument("--install-builder-s3-key")
    parser.add_argument("--install-builder-license-path", type=Path)
    parser.add_argument("--installer-source-path", type=Path)
    parser.add_argument("--stage-only", action="store_true")
    development = parser.add_mutually_exclusive_group()
    development.add_argument("--dev", action="store_true")
    development.add_argument("--local-dev", action="store_true")
    parser.add_argument(
        "--override-installer-version",
        "--version",
        dest="version",
        default=installer_version(),
    )
    args = parser.parse_args(argv)
    try:
        if args.platform not in PLATFORMS:
            raise ValueError("Specify --platform MacOS or Windows on a supported native host.")
        if args.local_dev and "CODEBUILD_BUILD_ID" in os.environ:
            raise ValueError(
                "--local-dev cannot be used when CODEBUILD_BUILD_ID is set; use --dev."
            )
        if args.install_builder_path is not None and args.install_builder_s3_bucket is not None:
            raise ValueError(
                "--install-builder-path and --install-builder-s3-bucket are mutually exclusive."
            )
        if args.install_builder_s3_key is not None and args.install_builder_s3_bucket is None:
            raise ValueError("--install-builder-s3-key requires --install-builder-s3-bucket.")
        with tempfile.TemporaryDirectory(prefix="comfyui-installbuilder-") as directory:
            builder = None
            if not args.stage_only:
                for path in (args.install_builder_path, args.install_builder_license_path):
                    if path is not None:
                        validate_native_path(path)
                builder = resolve_builder(
                    args.install_builder_path,
                    args.install_builder_s3_bucket,
                    args.install_builder_s3_key,
                    args.install_builder_license_path,
                    Path(directory),
                    args.platform,
                )
                if (
                    not (args.dev or args.local_dev)
                    and args.install_builder_license_path is None
                    and not (builder.parent.parent / "license.xml").is_file()
                ):
                    raise ValueError(
                        "Release builds require --install-builder-license-path or a licensed InstallBuilder installation."
                    )
            if args.reuse_payload:
                payload = args.payload_dir
                if not (payload / "build.json").is_file():
                    raise ValueError(
                        "--reuse-payload requires an existing verified payload directory."
                    )
                verify_payload(payload, args.platform)
            else:
                if args.python_runtime is None:
                    raise ValueError(
                        "Specify --python-runtime or COMFYUI_INSTALLER_PYTHON_RUNTIME to stage a new payload."
                    )
                payload = stage_payload(
                    Path(args.python_runtime), args.payload_dir, args.platform, args.wheelhouse
                )
            print(f"Verified offline payload: {payload}")
            if builder is not None:
                build(
                    payload,
                    builder,
                    args.output_dir,
                    args.platform,
                    args.version,
                    args.dev or args.local_dev,
                    args.installer_source_path,
                )
        return 0
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.CalledProcessError,
        zipfile.BadZipFile,
    ) as error:
        print(f"Installer build failed: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stdout or "", file=sys.stderr)
            print(error.stderr or "", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
