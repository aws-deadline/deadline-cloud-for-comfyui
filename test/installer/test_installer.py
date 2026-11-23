# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Runs a built installer against a stand-in ComfyUI folder. Run `hatch run test-installer` from the
folder that contains the installer, or set COMFYUI_INSTALLER_PATH to it.
"""

import glob
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform not in ("darwin", "win32"), reason="The installers are for macOS and Windows"
)

EXTENSION_NAME = "deadline-cloud-for-comfyui"
COMPONENT_NAME = "deadline_cloud_for_comfyui"
# Mirrors scripts/deps_bundle.py's SUPPORTED_PYTHON_VERSIONS; not imported since
# test/installer does not otherwise reach into scripts/.
SUPPORTED_PYTHON_VERSIONS = ["3.13", "3.14"]


@dataclass
class Installation:
    prefix: Path
    comfyui: Path

    @property
    def extension(self) -> Path:
        return self.comfyui / "custom_nodes" / EXTENSION_NAME


@pytest.fixture(scope="session")
def installer_path() -> Path:
    path = "DeadlineCloudForComfyUISubmitter-{platform}-installer.{ext}"
    if platform.system() == "Darwin":
        path = path.format(platform="osx", ext="app")
    else:
        path = path.format(platform="windows-x64", ext="exe")

    installer = Path(os.environ.get("COMFYUI_INSTALLER_PATH") or path)
    if installer.suffix == ".app":
        installer = installer / "Contents" / "MacOS" / "installbuilder.sh"

    if not installer.is_file():
        raise FileNotFoundError(f"Installer not found at '{installer}'")

    if not os.access(installer, os.X_OK) and not platform.system() == "Darwin":
        raise PermissionError(f"Installer at '{installer}' is not executable")

    return installer.absolute()


def _make_comfyui(path: Path) -> Path:
    """A stand-in ComfyUI folder, with another extension that the installer must leave alone."""
    comfyui = path / "ComfyUI"
    (comfyui / "custom_nodes" / "other-node").mkdir(parents=True)
    (comfyui / "main.py").write_text("")
    (comfyui / "custom_nodes" / "other-node" / "__init__.py").write_text("")
    return comfyui


def _installer_args(installer_path: Path, prefix: Path, comfyui: Path) -> list[str]:
    return [
        str(installer_path),
        # The Deadline Cloud submitter installer runs this installer with these options.
        "--mode",
        "unattended",
        "--unattendedmodeui",
        "none",
        "--installscope",
        "user",
        "--prefix",
        str(prefix),
        "--comfyui-directory",
        str(comfyui),
        "--enable-components",
        COMPONENT_NAME,
    ]


def _run_installer(installer_path: Path, tmp_path: Path) -> Installation:
    # use a path that does not exist
    installation = Installation(prefix=tmp_path / "dne", comfyui=_make_comfyui(tmp_path))
    subprocess.run(
        _installer_args(installer_path, installation.prefix, installation.comfyui), check=True
    )
    return installation


def _validate_files(installation: Installation) -> None:
    if platform.system() == "Darwin":
        uninstaller = "uninstall.app"
    else:
        uninstaller = "uninstall.exe"

    # THEN
    top_level_dir = [f.name for f in installation.prefix.iterdir()]
    assert "installer_version.txt" in top_level_dir
    assert uninstaller in top_level_dir

    for name in (
        "__init__.py",
        "LICENSE",
        "NOTICE",
        "src/deadline/comfyui_submitter/routes.py",
        "src/deadline/comfyui_submitter/bridge.py",
        "src/deadline/comfyui_submitter/web/deadline_submit.js",
    ):
        assert (installation.extension / name).is_file(), f"the extension is missing {name}"

    # Just check that we have dependencies in this folder
    dependencies = installation.extension / "dependencies"
    dependency_names = [f.name for f in dependencies.iterdir()]
    for package in ("deadline", "boto3", "psutil", "yaml"):
        assert package in dependency_names
    # awscrt reaches the bundle via deadline's console extra, requested by deps_bundle.py.
    # Without it botocore reports CRT as unavailable and AWS Console sign-in fails.
    assert "awscrt" in dependency_names
    # The panel is a web page, so the bundle doesn't need Qt.
    assert "PySide6" not in dependency_names

    # xxhash and pyyaml ship a version-specific compiled extension per supported version, and a
    # missing one is a silent failure: pyyaml falls back to its pure-Python parser, and a
    # missing xxhash artifact only surfaces as an ImportError under that Python. So check every
    # supported version by name.
    for package, module in (("xxhash", "_xxhash"), ("yaml", "_yaml")):
        artifact_names = {f.name for f in (dependencies / package).glob("*")}
        for version in SUPPORTED_PYTHON_VERSIONS:
            tag = version.replace(".", "")
            # POSIX wheels name the module cpython-<tag>-<platform>.so; Windows wheels name
            # it cp<tag>-<platform>.pyd -- CPython does not use the "cpython-" spelling there.
            prefixes = (f"{module}.cpython-{tag}-", f"{module}.cp{tag}-")
            assert any(
                name.startswith(prefixes) for name in artifact_names
            ), f"the bundle carries no compiled {module} artifact for Python {version}"


@pytest.fixture(scope="session")
def user_installation(installer_path: Path, tmp_path_factory) -> Installation:
    """Used for tests that just want to assert some facts around the install but do not modify"""
    return _run_installer(installer_path, tmp_path_factory.mktemp("install"))


@pytest.fixture(scope="function")
def per_test_user_installation(installer_path: Path, tmp_path: Path) -> Installation:
    """Used for tests that modify the installation"""
    return _run_installer(installer_path, tmp_path)


@pytest.fixture(scope="function")
def uninstaller_path() -> Path:
    if platform.system() == "Darwin":
        return Path("uninstall.app", "Contents", "MacOS", "installbuilder.sh")
    return Path("uninstall.exe")


def test_default_location(installer_path: Path):
    """Ensures that the default output location reported by the installer is accurate.
       Example help output:

    --prefix <prefix>                           Installation Directory
                                                Default: /Users/<user>/DeadlineCloudForComfyUISubmitter
    """
    # GIVEN
    default_install_location = Path("~/DeadlineCloudForComfyUISubmitter").expanduser()
    default_pattern = r"Default: (.*)"
    location = None

    # WHEN
    text_mode = [] if sys.platform == "win32" else ["--mode", "text"]
    # Since windows doesn't have text mode, it'll pop-up a gui. We use the timeout to ensure it stops
    try:
        help_result = subprocess.run(
            [installer_path, *text_mode, "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert (
            help_result.returncode == 0
        ), f"Installer exited with non-zero code: {help_result.returncode}"
        assert help_result.stdout is not None, "No stdout from --help"
        help_text = help_result.stdout
    except subprocess.TimeoutExpired as e:
        assert e.stdout is not None, "No stdout from --help"
        # mypy/docs say this should be bytes, but was str?
        assert isinstance(e.stdout, str)
        help_text = e.stdout

    # THEN
    help_output = iter(help_text.splitlines())
    while (line := next(help_output, None)) is not None:
        if line.strip().startswith("--prefix"):
            location = re.match(default_pattern, next(help_output, "").strip(), flags=re.IGNORECASE)
            break

    assert (
        location is not None
    ), f"Could not find default install location in help output:\n{help_text}"
    if platform.system() != "Windows":
        assert location.group(1) == default_install_location.as_posix()
    else:
        assert str(Path(location.group(1))) == str(default_install_location)


@pytest.mark.skipif(
    os.getenv("CODEBUILD_SRC_DIR") is None or os.getenv("IS_DEV_BUILD", "false").lower() == "true",
    reason="Only installers built with a license will not be evaluation mode",
)
@pytest.mark.skipif(
    sys.platform == "win32", reason="CLI usage for Windows does not make the eval text available"
)
def test_did_not_build_with_evaluation_mode(installer_path: Path, tmp_path: Path):
    """Tests to see if there's an evaluation version header from installbuilder.

    This is done by launching the installer, exiting before it completes, and checking the output
    does NOT contain a specific line entry. Unfortunately this is makes the test pretty fragile, but
    the behaviour has existed for years. If it exists it's expected to be the second line, but we check
    the top few lines that we're guaranteed to have in case it shifts a tiny bit.

    note: tmp_path is leveraged to ensure that the user's install is not messed with if the test does not
    behave correctly."""
    # GIVEN
    eval_text = r"Created with an evaluation version of InstallBuilder"
    output = []

    # WHEN
    proc = subprocess.Popen(
        [installer_path, "--mode", "text", "--prefix", tmp_path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        # We want to fail the installer fast so that it doesn't proceed to install with the defaults
        proc.wait(0.5)
    except subprocess.TimeoutExpired:
        # expected behaviour due to the above wait happening before the installer finishes
        pass
    finally:
        proc.terminate()
        proc.kill()

    # THEN
    assert proc.stdout is not None
    # Example header from installbuilder
    """----------------------------------------------------------------------------
    Created with an evaluation version of InstallBuilder

    Welcome to the AWS Deadline Cloud for ... Submitter Setup Wizard.

    ----------------------------------------------------------------------------
    """
    output = [proc.stdout.readline().strip() for _ in range(6)]
    assert output

    for line in output:
        assert (
            eval_text not in line
        ), "Installer was detected to have been built with Evaluation mode"


@pytest.mark.skipif(platform.system() != "Darwin", reason="Only run on MacOS")
class TestMacOS:
    def test_user_permissions(self, user_installation: Installation):
        # GIVEN / WHEN / THEN
        self._validate_posix_permissions(user_installation.prefix)
        self._validate_posix_permissions(user_installation.extension)

    def _has_group_other_write(self, mode: int) -> bool:
        return bool(mode & (stat.S_IWGRP | stat.S_IWOTH))

    def _has_user_read_write(self, mode: int) -> bool:
        return bool(mode & (stat.S_IRUSR | stat.S_IWUSR))

    def _has_user_group_other_execute(self, mode: int) -> bool:
        return bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))

    def _validate_posix_permissions(self, installation_path: Path):
        # assists mypy type checking to ignore this on Windows
        assert sys.platform != "win32"
        # pwd is not available on Windows
        import pwd

        # GIVEN
        current_user = pwd.getpwuid(os.getuid())[0]

        # WHEN
        bad_perms: defaultdict[Path, list[str]] = defaultdict(list)
        for entry in [installation_path, *installation_path.rglob("*")]:
            mode = stat.S_IMODE(entry.stat().st_mode)
            if self._has_group_other_write(mode):
                bad_perms[entry].append("should not have group/other write permissions")
            if not self._has_user_read_write(mode):
                bad_perms[entry].append("should have user read/write permissions")
            if entry.is_dir() and not self._has_user_group_other_execute(mode):
                bad_perms[entry].append("is a directory and should have execute permissions")
            if entry.owner() != current_user:
                bad_perms[entry].append(f"is not owned by the '{current_user}'")

        error_message = [f"Found {len(bad_perms)} instance(s) of incorrect permissions"]
        for i, entry in enumerate(bad_perms):
            fmted_reasons = "\n  - ".join(reason for reason in bad_perms[entry])
            error_message.append(
                f"{i+1}. ({stat.S_IMODE(entry.stat().st_mode):o}) '{Path(*entry.parts[len(installation_path.parts):])!s:>3}'\n"
                f"  - {fmted_reasons}"
            )

        # THEN
        assert len(bad_perms) == 0, "\n".join(error_message)


class TestUserInstall:
    def test_install(self, user_installation: Installation):
        # GIVEN / WHEN / THEN
        _validate_files(user_installation)

    @pytest.mark.skipif(
        f"{sys.version_info.major}.{sys.version_info.minor}" not in SUPPORTED_PYTHON_VERSIONS,
        reason="The dependencies are built for the Pythons that ComfyUI runs on",
    )
    def test_dependencies_load(self, user_installation: Installation):
        """The bridge loads the dependencies like this, with ComfyUI's packages left out by -S."""
        # GIVEN
        load = (
            "import site, sys; site.addsitedir(sys.argv[1]); "
            "import deadline.client.api, xxhash, yaml._yaml, awscrt"
        )

        # WHEN
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                load,
                str(user_installation.extension / "dependencies"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        # THEN
        assert result.returncode == 0, result.stderr

    def test_uninstall(self, per_test_user_installation: Installation, uninstaller_path: Path):
        # GIVEN
        installation = per_test_user_installation

        # WHEN
        result = subprocess.run(
            [installation.prefix / uninstaller_path, "--mode", "unattended"],
            check=False,
        )

        # THEN
        assert result.returncode == 0

        # On Windows, the uninstall process will return before the uninstallation is complete.
        # If necessary, wait for up to 1 minute 40 seconds before timing out.
        if platform.system() == "Windows":
            for _ in range(10):
                if not installation.prefix.exists() and not installation.extension.exists():
                    break
                time.sleep(10)
        assert not installation.prefix.exists()
        assert not installation.extension.exists()
        # The rest of ComfyUI, including other extensions, is left alone.
        assert (installation.comfyui / "main.py").is_file()
        assert (installation.comfyui / "custom_nodes" / "other-node" / "__init__.py").is_file()


@pytest.mark.parametrize("problem", ["no main.py", "a clone of the extension"])
def test_invalid_comfyui_folders_are_rejected(installer_path: Path, tmp_path: Path, problem: str):
    # GIVEN
    prefix = tmp_path / "dne"
    comfyui = _make_comfyui(tmp_path)
    extension = comfyui / "custom_nodes" / EXTENSION_NAME
    if problem == "no main.py":
        (comfyui / "main.py").unlink()
    else:
        # Uninstalling removes the extension folder, so the installer mustn't take over a clone.
        extension.mkdir()
        (extension / "__init__.py").write_text("# a clone\n")

    # WHEN
    result = subprocess.run(
        _installer_args(installer_path, prefix, comfyui),
        capture_output=True,
        text=True,
        check=False,
    )

    # THEN
    assert result.returncode != 0
    assert not (prefix / "installer_version.txt").exists()
    if problem == "no main.py":
        assert not extension.exists()
    else:
        assert [f.name for f in extension.iterdir()] == ["__init__.py"]
        assert (extension / "__init__.py").read_text() == "# a clone\n"


@pytest.mark.skipif(
    os.getenv("CODEBUILD_SRC_DIR") is None or os.getenv("IS_DEV_BUILD", "false").lower() == "true",
    reason="Only installers built internally will be signed",
)
class TestVerifySigning:
    @pytest.mark.skipif(platform.system() != "Windows", reason="Only run on Windows")
    def test_windows_signing(self, installer_path: Path):
        r"""Assumes that the Windows SDK is installed so we can find signtool:
            C:/Program Files*/Windows Kits/*/bin/*/x64/signtool.exe
        Example success:

            Index  Algorithm  Timestamp
            ========================================
            0      sha256     Authenticode

            Successfully verified: C:\*.exe

        Example failure:

            Index  Algorithm  Timestamp
            ========================================
            SignTool Error: No signature found.

            Number of errors: 1
        """
        # GIVEN
        # Check PATH, then SDK installation location
        signtool = shutil.which("signtool")
        if not signtool:
            signtool = next(
                glob.iglob("C:/Program Files*/Windows Kits/*/bin/*/x64/signtool.exe"), None
            )
        assert signtool, "signtool not found in expected location"

        # WHEN
        result = subprocess.run(
            [signtool, "verify", "/pa", installer_path],
            capture_output=True,
            text=True,
            check=False,
        )

        # THEN
        assert "SignTool Error:" not in result.stderr, "signtool did not succeed"
        assert result.returncode == 0
        assert "Successfully verified:" in result.stdout

    @pytest.mark.skipif(platform.system() != "Darwin", reason="Only run on MacOS")
    def test_macos_signing(self, installer_path: Path):
        """Tests that the relevant files are signed on MacOS. Assumes codesign and spectl are on the PATH

        Leverages codesign to check signing, and spctl to verify that MacOS does not complain about running it
        """
        # GIVEN
        codesign = shutil.which("codesign")
        spctl = shutil.which("spctl")
        assert codesign, "codesign not found in PATH"
        assert spctl, "spctl not found in PATH"

        # WHEN / THEN
        codesign_result = subprocess.run(
            [codesign, "--verify", "--deep", "--verbose", installer_path],
            capture_output=True,
            text=True,
            check=False,
        )
        assert (
            "code object is not signed at all" not in codesign_result.stdout
        ), "The file is not signed"
        assert codesign_result.returncode == 0

        # WHEN / THEN
        spctl_result = subprocess.run(
            [spctl, "--verbose", "--assess", "--type", "execute", installer_path],
            capture_output=True,
            text=True,
            check=False,
        )
        assert (
            "rejected" not in spctl_result.stderr
        ), "MacOS will not allow this file to be executed"
        assert spctl_result.returncode == 0
        # success message is sent to stderr
        assert "accepted" in spctl_result.stderr, "File was not accepted by MacOS"
