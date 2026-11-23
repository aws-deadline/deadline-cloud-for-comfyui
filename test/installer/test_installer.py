# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Exercise real Mac/Windows installers using disposable hosts, never ComfyUI itself."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPONENT = "deadline_cloud_for_comfyui"
EXTENSION = "deadline-cloud-for-comfyui"
CONFIG = "deadline_cloud_submitter.json"
SUPPORTED_MAC = sys.platform == "darwin" and platform.machine().lower() in ("arm64", "aarch64")
SUPPORTED_WINDOWS = sys.platform == "win32" and platform.machine().lower() in ("amd64", "x86_64")
PRODUCTION_BUILD = (
    os.environ.get("CODEBUILD_SRC_DIR") is not None
    and os.environ.get("IS_DEV_BUILD", "false").lower() != "true"
)
pytestmark = pytest.mark.skipif(
    not (SUPPORTED_MAC or SUPPORTED_WINDOWS), reason="Requires macOS arm64 or Windows x64"
)


@dataclass(frozen=True)
class Layout:
    root: Path
    source: Path
    nodes: Path
    prefix: Path
    environment: dict[str, str] = field(repr=False)
    launcher: Path | None = None
    portable_root: Path | None = None

    @property
    def extension(self) -> Path:
        return self.nodes / EXTENSION

    @property
    def selection(self) -> Path:
        return self.launcher or self.portable_root or self.source

    @property
    def python(self) -> Path:
        return self.prefix / (
            "runtime/python.exe" if sys.platform == "win32" else "runtime/bin/python3"
        )

    @property
    def cli(self) -> Path:
        return self.prefix / (
            "runtime/Scripts/deadline.exe" if sys.platform == "win32" else "runtime/bin/deadline"
        )

    @property
    def uninstaller(self) -> Path:
        relative = (
            "uninstall.exe"
            if sys.platform == "win32"
            else "uninstall.app/Contents/MacOS/installbuilder.sh"
        )
        return self.prefix / relative


def make_layout(root: Path, *, split: bool = False, portable: bool = False) -> Layout:
    root = root.resolve()
    portable_root = root / "ComfyUI portable with spaces" if portable else None
    source = portable_root / "ComfyUI" if portable_root else root / "ComfyUI source with spaces"
    (source / "custom_nodes").mkdir(parents=True)
    (source / "main.py").write_text(
        "# Synthetic host; no ComfyUI code or assets.\n", encoding="utf-8"
    )
    nodes = root / "Active base with spaces/custom_nodes" if split else source / "custom_nodes"
    nodes.mkdir(parents=True, exist_ok=True)
    (nodes / "studio-node").mkdir()
    (nodes / "studio-node/customer.txt").write_text("keep studio node", encoding="utf-8")
    if portable_root:
        outer_nodes = portable_root / "custom_nodes"
        outer_nodes.mkdir()
        (outer_nodes / "customer.txt").write_text("keep outer portable folder", encoding="utf-8")
    launcher = None
    if split:
        (source / "custom_nodes/source-sentinel.txt").write_text(
            "inactive source", encoding="utf-8"
        )
        if sys.platform != "win32":
            launcher = root / "Selected launcher with spaces"
            launcher.mkdir()
            (launcher / "comfyui-source.txt").write_text(str(source), encoding="utf-8")
            (launcher / "launch.sh").write_text(
                f'exec python "{source / "main.py"}" --base-directory "{nodes.parent}"\n',
                encoding="utf-8",
            )
    configuration = root / "Isolated configuration"
    configuration.mkdir()
    (configuration / "aws-config").write_text("[default]\nregion=us-west-2\n", encoding="utf-8")
    (configuration / "aws-credentials").write_text(
        "[default]\naws_access_key_id=testing\naws_secret_access_key=testing\n", encoding="utf-8"
    )
    (configuration / "deadline-config").write_text("", encoding="utf-8")
    environment = {
        **{key: value for key, value in os.environ.items() if not key.startswith("AWS_")},
        "XDG_CONFIG_HOME": str(configuration),
        "AWS_CONFIG_FILE": str(configuration / "aws-config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(configuration / "aws-credentials"),
        "AWS_EC2_METADATA_DISABLED": "true",
        "DEADLINE_CONFIG_FILE_PATH": str(configuration / "deadline-config"),
        "QT_QPA_PLATFORM": "offscreen",
    }
    return Layout(
        root,
        source,
        nodes,
        root / "Deadline runtime with spaces",
        environment,
        launcher,
        portable_root,
    )


def run_program(
    executable: Path,
    arguments: list[str],
    *,
    layout: Layout | None = None,
    input_text: str | None = None,
    timeout: int = 240,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(executable), *arguments],
        cwd=layout.root if layout else ROOT,
        env=layout.environment if layout else None,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def assert_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stdout + result.stderr


def install(
    installer: Path,
    layout: Layout,
    extra: list[str] | None = None,
    *,
    source: Path | None = None,
) -> None:
    result = run_program(
        installer,
        [
            "--mode",
            "unattended",
            "--unattendedmodeui",
            "none",
            "--prefix",
            str(layout.prefix),
            "--enable-components",
            COMPONENT,
            "--comfyui-directory",
            str(source or layout.selection),
            *(extra or []),
        ],
        layout=layout,
    )
    assert_success(result)


def remaining_prefix_entries(prefix: Path) -> list[Path]:
    """Accept an absent or empty product folder without following replacement links."""
    if prefix.is_symlink() or prefix.is_junction():
        return [prefix]
    try:
        return list(prefix.iterdir())
    except FileNotFoundError:
        # Windows may remove the folder between the completion checks.
        return []
    except NotADirectoryError:
        return [prefix]


def uninstall(layout: Layout) -> None:
    assert layout.uninstaller.is_file()
    assert_success(
        run_program(
            layout.uninstaller,
            ["--mode", "unattended", "--unattendedmodeui", "none"],
            layout=layout,
        )
    )
    if sys.platform == "win32":
        # Windows starts a separate cleanup process so it can remove uninstall.exe itself.
        deadline = time.monotonic() + 100
        while remaining_prefix_entries(layout.prefix) and time.monotonic() < deadline:
            time.sleep(0.25)
    remaining = remaining_prefix_entries(layout.prefix)
    assert not remaining, "The uninstaller left private runtime files behind:\n" + "\n".join(
        str(path) for path in remaining
    )
    assert (layout.source / "main.py").read_text(encoding="utf-8").startswith("# Synthetic host")
    assert (layout.nodes / "studio-node/customer.txt").read_text(
        encoding="utf-8"
    ) == "keep studio node"
    if layout.portable_root:
        assert (layout.portable_root / "custom_nodes/customer.txt").read_text(
            encoding="utf-8"
        ) == "keep outer portable folder"
        assert not (layout.portable_root / "custom_nodes" / EXTENSION).exists()


@pytest.fixture(scope="session")
def installer_path() -> Path:
    configured = os.environ.get("COMFYUI_INSTALLER_PATH")
    path = (
        Path(configured).expanduser()
        if configured
        else ROOT
        / "dist/installers"
        / (
            "DeadlineCloudForComfyUISubmitter-windows-x64-installer.exe"
            if sys.platform == "win32"
            else "DeadlineCloudForComfyUISubmitter-osx-arm64-installer.app"
        )
    )
    if path.suffix == ".app":
        path /= "Contents/MacOS/installbuilder.sh"
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(
            f"Installer not found: {path}. Build it first or set COMFYUI_INSTALLER_PATH."
        )
    if not os.access(path, os.X_OK):
        raise PermissionError(f"Installer is not executable: {path}")
    return path


@pytest.fixture(scope="session")
def installer_help(installer_path: Path) -> str | None:
    if sys.platform == "win32":
        # Windows has GUI help, so the native lifecycle tests exercise its CLI options.
        return None
    result = run_program(installer_path, ["--mode", "text", "--help"], timeout=30)
    assert_success(result)
    return result.stdout


@pytest.fixture(scope="session")
def user_installation(
    installer_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Layout]:
    layout = make_layout(tmp_path_factory.mktemp("native-user-install"))
    install(installer_path, layout)
    try:
        yield layout
    finally:
        if layout.uninstaller.is_file():
            uninstall(layout)


@pytest.mark.parametrize(
    "option,expected",
    [
        ("comfyui-directory", Path.home() / "ComfyUI"),
        ("comfyui-base-directory", None),
    ],
)
def test_default_locations(installer_help: str | None, option: str, expected: Path | None) -> None:
    if installer_help is None:
        # Inspect the shared picker contract without opening a blocking Windows help dialog.
        component = ET.parse(ROOT / "install_builder/deadline-cloud-for-comfyui.xml").getroot()
        parameter = next(
            item
            for item in component.iter("directoryParameter")
            if item.findtext("cliOptionName") == option
        )
        default = parameter.findtext("default", "").replace(
            "${user_home_directory}", str(Path.home())
        )
        assert (Path(default) if default else None) == expected
        assert parameter.findtext("ask", "1") == "1"
        return
    match = re.search(
        rf"(?m)^[ \t]*--{re.escape(option)}\b[^\n]*\n[ \t]*Default:[ \t]*([^\n]*)",
        installer_help,
    )
    assert match, f"No documented default for --{option}:\n{installer_help}"
    assert match.group(1).strip() == (str(expected) if expected else "")


def test_component_and_selected_folder_cli(installer_help: str | None) -> None:
    if installer_help is None:
        component = ET.parse(ROOT / "install_builder/deadline-cloud-for-comfyui.xml").getroot()
        assert component.findtext("name") == COMPONENT
        assert component.findtext("show") == "1" and component.findtext("canBeEdited") == "1"
        options = {
            item.findtext("cliOptionName") or item.get("cliOptionName")
            for item in component.iter()
            if item.tag.endswith("Parameter")
        }
        assert {
            "comfyui-directory",
            "comfyui-base-directory",
            "comfyui-custom-nodes-directory",
        } <= options
        assert {"installscope", "comfyui-discovery-home", "discover"}.isdisjoint(options)
        return
    assert "--enable-components" in installer_help and COMPONENT in installer_help
    assert "--comfyui-directory" in installer_help
    assert "--comfyui-base-directory" in installer_help
    for removed in ("--installscope", "--comfyui-discovery-home", "--discover"):
        assert removed not in installer_help


def test_installed_files_and_dependencies(user_installation: Layout) -> None:
    layout = user_installation
    assert (layout.prefix / "installer_version.txt").read_text(encoding="utf-8").strip()
    assert layout.uninstaller.is_file()
    assert (layout.prefix / ".deadline-cloud-for-comfyui-installer").is_file()
    for staging in ("payload", "bootstrap", "dependency_bundle"):
        assert not (layout.prefix / staging).exists(), staging
    assert not (layout.prefix / "rollbackBackupDirectory").exists()
    for relative in (
        "__init__.py",
        "src/deadline/comfyui_submitter/bridge.py",
        "src/deadline/comfyui_submitter/comfy_cli_job_template.yaml",
        "src/deadline/comfyui_submitter/web/deadline_submit.js",
        "src/deadline/comfyui_submitter/web/deadline_ui.js",
        "THIRD_PARTY_LICENSES",
    ):
        assert (layout.extension / relative).is_file(), relative
    inventory = json.loads(
        (layout.extension / "software-inventory.json").read_text(encoding="utf-8")
    )
    names = {re.sub(r"[-_.]+", "-", entry["name"]).lower() for entry in inventory}
    assert {"deadline", "boto3", "botocore", "pyside6-essentials", "awscrt"} <= names
    python, cli = layout.python, layout.cli
    code = (
        "import json,sys,ssl,importlib.util,deadline.client,boto3,botocore,awscrt; "
        "import psutil,yaml,xxhash,qtpy,distlib; from yaml import _yaml; "
        "from PySide6 import QtCore,QtWidgets; "
        "modules=('deadline.client','boto3','botocore','awscrt','PySide6.QtCore',"
        "'shiboken6','psutil','yaml','xxhash','distlib','qtpy'); "
        "print(json.dumps({'prefix':sys.prefix,'version':list(sys.version_info[:2]),"
        "'tls':bool(ssl.create_default_context().get_ca_certs()),"
        "'modules':{name:importlib.util.find_spec(name).origin for name in modules}}))"
    )
    result = run_program(python, ["-I", "-c", code], layout=layout)
    assert_success(result)
    details = json.loads(result.stdout)
    assert Path(details["prefix"]).resolve() == layout.prefix / "runtime"
    assert details["version"] in ([3, 13], [3, 14]) and details["tls"]
    version = ".".join(str(part) for part in details["version"])
    site_packages = (
        layout.prefix
        / "runtime"
        / ("Lib/site-packages" if sys.platform == "win32" else f"lib/python{version}/site-packages")
    )
    assert all(
        Path(path).resolve().is_relative_to(site_packages) for path in details["modules"].values()
    ), details
    if sys.platform == "win32":
        # Imports of these native modules verify Windows conditional dependencies too.
        assert_success(run_program(python, ["-I", "-c", "import colorama,win32api"], layout=layout))
    result = run_program(cli, ["--version"], layout=layout)
    assert_success(result)
    assert "deadline" in result.stdout.lower()
    configuration = json.loads((layout.extension / CONFIG).read_text(encoding="utf-8"))
    assert configuration["deadline_python"] == str(python)
    assert configuration["deadline_cli"] == str(cli)


def test_user_ownership_and_permissions(user_installation: Layout) -> None:
    if sys.platform != "darwin":
        pytest.skip("User ownership and permissions require macOS.")
    failures = []
    for root in (user_installation.prefix, user_installation.extension):
        for path in (root, *root.rglob("*")):
            details = path.lstat()
            if path.is_symlink():
                continue
            mode = stat.S_IMODE(details.st_mode)
            valid = (
                details.st_uid == os.getuid()
                and not mode & (stat.S_IWGRP | stat.S_IWOTH)
                and bool(mode & stat.S_IRUSR)
                and (not path.is_dir() or bool(mode & stat.S_IXUSR))
            )
            if not valid:
                failures.append(f"{path}: uid={details.st_uid}, mode={mode:o}")
    assert not failures, "\n".join(failures[:20])
    assert stat.S_IMODE((user_installation.extension / CONFIG).stat().st_mode) == 0o600


def compile_installed_modules(layout: Layout) -> None:
    # Compile without importing or executing the integration or any customer module.
    python = layout.python
    assert_success(
        run_program(
            python, ["-I", "-m", "compileall", "-q", "-f", str(layout.extension)], layout=layout
        )
    )
    assert list(layout.extension.rglob("*.pyc")), "The bytecode regression must create real caches"


@pytest.mark.parametrize("portable", [False, True], ids=["source-folder", "portable-parent"])
def test_clean_uninstall(installer_path: Path, tmp_path: Path, portable: bool) -> None:
    layout = make_layout(tmp_path, portable=portable)
    install(installer_path, layout)
    compile_installed_modules(layout)
    uninstall(layout)
    assert not layout.extension.exists()


def test_upgrade_uninstall_reinstall_preserves_split_customer_settings(
    installer_path: Path, tmp_path: Path
) -> None:
    layout = make_layout(tmp_path, split=True)
    initial_options = (
        ["--comfyui-base-directory", str(layout.nodes.parent)] if sys.platform == "win32" else None
    )
    install(installer_path, layout, initial_options)
    configuration = layout.extension / CONFIG
    settings = json.loads(configuration.read_text(encoding="utf-8"))
    settings.update(conda_channels="studio-channel", custom_setting="keep")
    configuration.write_text(json.dumps(settings), encoding="utf-8")
    (layout.extension / "customer.txt").write_text("keep customer file", encoding="utf-8")
    original_state = json.loads((layout.prefix / "installation.json").read_text(encoding="utf-8"))
    install(
        installer_path,
        layout,
        ["--comfyui-base-directory", str(layout.nodes.parent)],
        source=layout.source,
    )
    upgraded_state = json.loads((layout.prefix / "installation.json").read_text(encoding="utf-8"))
    assert original_state["installation_id"] != upgraded_state["installation_id"]
    assert upgraded_state["extension"] == str(layout.extension)
    assert upgraded_state["comfyui_source"] == str(layout.source)
    assert upgraded_state["custom_nodes"] == str(layout.nodes)
    assert not (layout.prefix / "rollbackBackupDirectory").exists()
    customer_module = layout.extension / "src/deadline/comfyui_submitter/customer.py"
    customer_module.write_text("# Customer customization.\nVALUE = 1\n", encoding="utf-8")
    compile_installed_modules(layout)
    customer_caches = {
        cached: cached.read_bytes()
        for cached in customer_module.parent.joinpath("__pycache__").glob("customer.*.pyc")
    }
    assert customer_caches
    uninstall(layout)
    assert all(cached.read_bytes() == expected for cached, expected in customer_caches.items())
    assert not list(customer_module.parent.joinpath("__pycache__").glob("bridge.*.pyc"))
    preserved = json.loads(configuration.read_text(encoding="utf-8"))
    assert preserved["conda_channels"] == "studio-channel" and preserved["custom_setting"] == "keep"
    assert "deadline_python" not in preserved and "deadline_cli" not in preserved
    assert (layout.extension / ".deadline-installer-uninstalled.json").is_file()
    install(
        installer_path,
        layout,
        ["--comfyui-custom-nodes-directory", str(layout.nodes)],
        source=layout.source,
    )
    assert not (layout.extension / ".deadline-installer-uninstalled.json").exists()
    restored = json.loads(configuration.read_text(encoding="utf-8"))
    assert restored["conda_channels"] == "studio-channel"
    uninstall(layout)
    assert (layout.extension / "customer.txt").read_text(encoding="utf-8") == "keep customer file"
    assert customer_module.is_file()
    assert all(cached.read_bytes() == expected for cached, expected in customer_caches.items())
    assert (layout.source / "custom_nodes/source-sentinel.txt").read_text(
        encoding="utf-8"
    ) == "inactive source"
    assert not (layout.source / "custom_nodes" / EXTENSION).exists()


@pytest.mark.parametrize(
    "case",
    [
        "missing-source",
        "unrelated-prefix",
        "source-overlap",
        "nodes-overlap",
        "linked-nodes",
        "dollar-source",
        "backtick-source",
    ],
)
def test_invalid_targets_are_rejected_before_installing(
    installer_path: Path, tmp_path: Path, case: str
) -> None:
    layout = make_layout(tmp_path)
    source, prefix = layout.source, layout.prefix
    if case == "missing-source":
        source = layout.root / "Missing ComfyUI"
    elif case == "unrelated-prefix":
        prefix.mkdir()
        (prefix / "customer.txt").write_text("occupied prefix", encoding="utf-8")
    elif case == "source-overlap":
        prefix = source / "Deadline runtime"
    elif case == "nodes-overlap":
        prefix = layout.nodes / "Deadline runtime"
    elif case == "linked-nodes":
        target = layout.root / "External customer nodes"
        layout.nodes.rename(target)
        if sys.platform == "win32":
            powershell = shutil.which("powershell.exe")
            assert powershell, "PowerShell is required for the junction guard regression"
            environment = {
                **layout.environment,
                "COMFYUI_TEST_LINK_PATH": str(layout.nodes),
                "COMFYUI_TEST_LINK_TARGET": str(target),
            }
            assert_success(
                subprocess.run(
                    [
                        powershell,
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:COMFYUI_TEST_LINK_PATH -Target $env:COMFYUI_TEST_LINK_TARGET | Out-Null",
                    ],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
            )
        else:
            layout.nodes.symlink_to(target, target_is_directory=True)
    elif case in ("dollar-source", "backtick-source"):
        expansion = (
            "$(touch shell-sentinel)" if case == "dollar-source" else "`touch shell-sentinel`"
        )
        source = layout.root / ("ComfyUI " + expansion)
        layout.source.rename(source)
    result = run_program(
        installer_path,
        [
            "--mode",
            "unattended",
            "--unattendedmodeui",
            "none",
            "--prefix",
            str(prefix),
            "--enable-components",
            COMPONENT,
            "--comfyui-directory",
            str(source),
        ],
        layout=layout,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert not (prefix / "runtime").exists()
    assert not (source / "custom_nodes" / EXTENSION).exists()
    assert not (layout.root / "shell-sentinel").exists()
    if case == "unrelated-prefix":
        assert (prefix / "customer.txt").read_text(encoding="utf-8") == "occupied prefix"
    if case == "linked-nodes":
        assert (layout.root / "External customer nodes/studio-node/customer.txt").read_text(
            encoding="utf-8"
        ) == "keep studio node"


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows installers have no text-mode evaluation banner"
)
@pytest.mark.skipif(
    not PRODUCTION_BUILD, reason="Only licensed production builds must omit the evaluation banner"
)
def test_production_installer_is_not_evaluation(installer_path: Path, tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    result = run_program(
        installer_path,
        [
            "--mode",
            "text",
            "--prefix",
            str(layout.prefix),
            "--comfyui-directory",
            str(layout.source),
        ],
        layout=layout,
        input_text="n\n",
        timeout=60,
    )
    output = (result.stdout + result.stderr).lower()
    assert "welcome" in output, result.stdout + result.stderr
    assert "evaluation version of installbuilder" not in output
    assert not layout.prefix.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS application signing only")
@pytest.mark.skipif(
    not PRODUCTION_BUILD, reason="Only production artifacts must be signed and notarized"
)
def test_production_macos_signing(installer_path: Path) -> None:
    bundle = next((path for path in installer_path.parents if path.suffix == ".app"), None)
    assert bundle, "macOS production installers must be application bundles"
    for executable, arguments in (
        (Path("/usr/bin/codesign"), ["--verify", "--deep", "--strict", "--verbose", str(bundle)]),
        (Path("/usr/sbin/spctl"), ["--verbose", "--assess", "--type", "execute", str(bundle)]),
    ):
        assert_success(run_program(executable, arguments, timeout=60))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Authenticode signing only")
@pytest.mark.skipif(not PRODUCTION_BUILD, reason="Only production Windows artifacts must be signed")
def test_production_windows_signing(installer_path: Path) -> None:
    powershell = shutil.which("powershell.exe")
    assert powershell, "PowerShell is required to verify Authenticode"
    environment = {**os.environ, "COMFYUI_TEST_SIGNED_ARTIFACT": str(installer_path)}
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$ErrorActionPreference='Stop'; "
                "$signature=Get-AuthenticodeSignature -LiteralPath $env:COMFYUI_TEST_SIGNED_ARTIFACT; "
                "if($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate){"
                "Write-Error ('Invalid Authenticode: '+$signature.Status); exit 1}; "
                "$signature.Status.ToString()"
            ),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert_success(result)
    assert result.stdout.strip() == "Valid"
