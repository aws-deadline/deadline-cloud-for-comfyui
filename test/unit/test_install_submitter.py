# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

spec = importlib.util.spec_from_file_location(
    "install_submitter", REPO_ROOT / "install_submitter.py"
)
assert spec and spec.loader
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX paths and executable bits")


@pytest.fixture
def comfyui(tmp_path) -> Path:
    root = tmp_path / "ComfyUI"
    (root / "custom_nodes").mkdir(parents=True)
    return root


def make_checkout(path: Path) -> Path:
    """
    A checkout of the extension with its runtime files and a compiled panel. The repository only
    has the panel once it's built, and the Python tests run without building it.
    """
    for relative in install.RUNTIME_FILES:
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if relative.parent == install.WEB_DIR:
            target.write_text("// The compiled panel\n")
        else:
            shutil.copy(REPO_ROOT / relative, target)
    return path


@pytest.fixture
def checkout(tmp_path) -> Path:
    return make_checkout(tmp_path / "checkout")


def installed_files(folder: Path) -> set[Path]:
    """Every file and link in folder, including links to folders."""
    return {
        path.relative_to(folder)
        for path in folder.rglob("*")
        if path.is_symlink() or not path.is_dir()
    }


def fake_cli(path: Path, version: str = "0.60.10", with_python: bool = True) -> Path:
    """A CLI, by default with a python next to it, like a pip-installed one has."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo "deadline, version {version}"; exit 0; fi\n'
        'if [ "$1" = "config" ]; then echo "farm-123"; exit 0; fi\n'
    )
    path.chmod(0o755)
    if with_python:
        python = path.parent / "python"
        if not python.exists():
            python.write_text("#!/bin/sh\n")
            python.chmod(0o755)
    return path


def test_find_custom_nodes(comfyui):
    assert install.find_custom_nodes(comfyui) == (comfyui / "custom_nodes").resolve()


def test_find_custom_nodes_rejects_other_dirs(tmp_path):
    with pytest.raises(install.InstallError, match="no custom_nodes"):
        install.find_custom_nodes(tmp_path)


def test_install_extension_copies_runtime_files(comfyui, checkout):
    dest = install.install_extension(checkout, install.find_custom_nodes(comfyui))
    assert dest == (comfyui / "custom_nodes" / install.EXTENSION_DIR_NAME).resolve()
    assert installed_files(dest) == set(install.RUNTIME_FILES)
    for relative in install.RUNTIME_FILES:
        assert (dest / relative).read_bytes() == (checkout / relative).read_bytes()


def test_runtime_files_include_every_tracked_package_file():
    """A module added to the package isn't installed until it's added to RUNTIME_FILES."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git isn't on the PATH")
    result = subprocess.run(
        [git, "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        pytest.skip(f"{REPO_ROOT} isn't a git checkout")
    tracked = {Path(line) for line in result.stdout.splitlines()}
    panel = {install.WEB_DIR / name for name in install.PANEL_FILES}
    runtime = set(install.RUNTIME_FILES) - panel
    assert runtime <= tracked
    assert {path for path in tracked if path.is_relative_to(install.PACKAGE_DIR)} <= runtime
    # src/deadline is a namespace package, so there's no __init__.py there to copy.
    assert Path("src", "deadline", "__init__.py") not in tracked


def test_install_extension_copies_only_the_runtime_files(comfyui, checkout):
    """A checkout's local settings, credentials, logs, old builds and experiments stay out."""
    for name in (".npmrc", ".netrc", ".pypirc", ".env", "credentials.json", "key.pem"):
        (checkout / name).write_text("secret")
    (checkout / "notes.txt").write_text("notes")
    (checkout / "npm-debug.log").write_text("log")
    (checkout / ".aws").mkdir()
    (checkout / ".aws" / "credentials").write_text("[default]")
    (checkout / install.PACKAGE_DIR / "scratch.json").write_text("{}")
    (checkout / install.PACKAGE_DIR / "__pycache__").mkdir()
    (checkout / install.PACKAGE_DIR / "__pycache__" / "submit.cpython-313.pyc").write_bytes(b"")
    (checkout / install.WEB_DIR / "old.js").write_text("// from an older build")
    (checkout / install.CONFIG_FILE_NAME).write_text('{"deadline_cli": "the checkout\'s"}')
    (checkout / install.CLI_ENV_DIR_NAME / "bin").mkdir(parents=True)

    dest = install.install_extension(checkout, install.find_custom_nodes(comfyui))

    assert installed_files(dest) == set(install.RUNTIME_FILES)


@pytest.mark.skipif(sys.platform == "win32", reason="Creating links needs privileges on Windows.")
def test_install_extension_leaves_out_links_outside_the_runtime_files(comfyui, checkout, tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    (private / "id_ed25519").write_text("key")
    (checkout / "keys").symlink_to(private, target_is_directory=True)
    (checkout / install.PACKAGE_DIR / "key.txt").symlink_to(private / "id_ed25519")
    (checkout / install.WEB_DIR / "extra.js").symlink_to(private / "id_ed25519")

    dest = install.install_extension(checkout, install.find_custom_nodes(comfyui))

    assert installed_files(dest) == set(install.RUNTIME_FILES)


def _private_key(tmp_path: Path) -> Path:
    key = tmp_path / "private" / "id_ed25519"
    key.parent.mkdir()
    key.write_text("PRIVATE KEY")
    return key


def _no_file_contains(folder: Path, text: str) -> bool:
    return not any(
        text in path.read_text(errors="replace") for path in folder.rglob("*") if path.is_file()
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Creating links needs privileges on Windows.")
@pytest.mark.parametrize(
    "linked",
    [Path("README.md"), install.PACKAGE_DIR / "routes.py", install.WEB_DIR / "deadline_ui.js"],
)
def test_install_extension_doesnt_copy_a_linked_runtime_file(comfyui, checkout, tmp_path, linked):
    """It fails instead, before the installed copy is replaced, as the copy would be incomplete."""
    custom_nodes = install.find_custom_nodes(comfyui)
    dest = install.install_extension(checkout, custom_nodes)
    (checkout / linked).unlink()
    (checkout / linked).symlink_to(_private_key(tmp_path))

    with pytest.raises(
        install.InstallError, match=f"links in place of .*: {re.escape(linked.as_posix())}\\. "
    ):
        install.install_extension(checkout, custom_nodes)

    assert installed_files(dest) == set(install.RUNTIME_FILES)
    assert not (dest / linked).is_symlink()
    assert _no_file_contains(custom_nodes, "PRIVATE KEY")
    assert os.listdir(custom_nodes) == [install.EXTENSION_DIR_NAME]


@pytest.mark.skipif(sys.platform == "win32", reason="Creating links needs privileges on Windows.")
@pytest.mark.parametrize("linked", [Path("src"), install.PACKAGE_DIR, install.WEB_DIR])
def test_install_extension_doesnt_copy_through_a_linked_folder(comfyui, checkout, tmp_path, linked):
    elsewhere = tmp_path / "elsewhere"
    shutil.move(checkout / linked, elsewhere)
    (checkout / linked).symlink_to(elsewhere, target_is_directory=True)
    custom_nodes = install.find_custom_nodes(comfyui)

    with pytest.raises(
        install.InstallError, match=f"links in place of .*: {re.escape(linked.as_posix())}\\. "
    ):
        install.install_extension(checkout, custom_nodes)

    assert os.listdir(custom_nodes) == []


@pytest.mark.parametrize("junction", [Path("src"), install.WEB_DIR])
def test_install_extension_doesnt_copy_through_a_windows_junction(
    comfyui, checkout, monkeypatch, junction
):
    """
    os.path.islink is False for a junction, so it's checked for separately. Only Windows has
    junctions, so the folder is made to look like one here.
    """
    path = (checkout / junction).resolve()
    isjunction = os.path.isjunction
    monkeypatch.setattr(os.path, "isjunction", lambda p: Path(p) == path or isjunction(p))
    assert not os.path.islink(path)
    custom_nodes = install.find_custom_nodes(comfyui)

    with pytest.raises(
        install.InstallError, match=f"links in place of .*: {re.escape(junction.as_posix())}\\. "
    ):
        install.install_extension(checkout, custom_nodes)

    assert os.listdir(custom_nodes) == []


@pytest.mark.parametrize(
    "missing",
    [
        install.WEB_DIR / "deadline_ui.js",
        install.WEB_DIR / "deadline_submit.js",
        install.PACKAGE_DIR / "routes.py",
        Path("__init__.py"),
    ],
)
def test_install_extension_needs_every_runtime_file(comfyui, checkout, missing):
    (checkout / missing).unlink()
    custom_nodes = install.find_custom_nodes(comfyui)

    with pytest.raises(install.InstallError, match=f"is missing {re.escape(missing.as_posix())},"):
        install.install_extension(checkout, custom_nodes)

    assert os.listdir(custom_nodes) == []


def test_install_extension_update_keeps_cli_env_and_config(comfyui, checkout):
    custom_nodes = install.find_custom_nodes(comfyui)
    dest = install.install_extension(checkout, custom_nodes)
    (dest / install.CLI_ENV_DIR_NAME / "bin").mkdir(parents=True)
    (dest / install.CLI_ENV_DIR_NAME / "bin" / "deadline").write_text("the private CLI")
    (dest / install.CONFIG_FILE_NAME).write_text("{}")
    (dest / "stale.txt").write_text("from an old version")
    (checkout / install.PACKAGE_DIR / "routes.py").write_text("# A new version\n")

    install.install_extension(checkout, custom_nodes)

    assert (dest / install.CLI_ENV_DIR_NAME / "bin" / "deadline").read_text() == "the private CLI"
    assert (dest / install.CONFIG_FILE_NAME).read_text() == "{}"
    assert (dest / install.PACKAGE_DIR / "routes.py").read_text() == "# A new version\n"
    assert not (dest / "stale.txt").exists()
    assert os.listdir(custom_nodes) == [install.EXTENSION_DIR_NAME]


def test_installed_copy_runs_its_script_again(comfyui, checkout, tmp_path):
    """The copy has install_submitter.py, so it can update its CLI without the checkout or Node."""
    dest = install.install_extension(checkout, install.find_custom_nodes(comfyui))
    cli = fake_cli(tmp_path / "bin" / "deadline")
    result = subprocess.run(
        [sys.executable, str(dest / "install_submitter.py")]
        + ["--comfyui", str(comfyui), "--deadline-cli", str(cli)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Using the extension in place" in result.stdout
    config = json.loads((dest / install.CONFIG_FILE_NAME).read_text())
    assert config["deadline_cli"] == str(cli.resolve())


def test_install_extension_refuses_to_replace_other_directory(comfyui, checkout):
    custom_nodes = install.find_custom_nodes(comfyui)
    (custom_nodes / install.EXTENSION_DIR_NAME).mkdir()
    (custom_nodes / install.EXTENSION_DIR_NAME / "mine.txt").write_text("keep")
    with pytest.raises(install.InstallError, match="--force"):
        install.install_extension(checkout, custom_nodes)
    assert (custom_nodes / install.EXTENSION_DIR_NAME / "mine.txt").exists()


@pytest.mark.parametrize("marker", [".git", ".hg", ".svn"])
@pytest.mark.parametrize("link", [False, True])
def test_install_extension_refuses_to_replace_a_checkout(comfyui, checkout, marker, link):
    """A clone in custom_nodes, such as one ComfyUI-Manager made, keeps its history and changes."""
    custom_nodes = install.find_custom_nodes(comfyui)
    clone = custom_nodes / install.EXTENSION_DIR_NAME
    (clone / "src" / "deadline" / "comfyui_submitter").mkdir(parents=True)
    (clone / "__init__.py").write_text("")
    (clone / marker).mkdir()
    (clone / "uncommitted.py").write_text("work in progress")
    with pytest.raises(install.InstallError, match="is a checkout of the extension.*--force"):
        install.install_extension(checkout, custom_nodes, link=link)
    assert (clone / "uncommitted.py").read_text() == "work in progress"
    assert (clone / marker).is_dir()
    # --force still replaces it.
    dest = install.install_extension(checkout, custom_nodes, link=link, force=True)
    assert not (dest / "uncommitted.py").exists()


def test_install_extension_refuses_to_replace_a_git_worktree(comfyui, checkout):
    """A git worktree has a .git file rather than a folder."""
    custom_nodes = install.find_custom_nodes(comfyui)
    clone = custom_nodes / install.EXTENSION_DIR_NAME
    (clone / "src" / "deadline" / "comfyui_submitter").mkdir(parents=True)
    (clone / "__init__.py").write_text("")
    (clone / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
    with pytest.raises(install.InstallError, match="is a checkout"):
        install.install_extension(checkout, custom_nodes)
    assert (clone / ".git").is_file()


def test_install_extension_links(comfyui, checkout):
    custom_nodes = install.find_custom_nodes(comfyui)
    dest = install.install_extension(checkout, custom_nodes, link=True)
    assert dest.is_symlink() and dest.resolve() == checkout.resolve()
    assert install.install_extension(checkout, custom_nodes, link=True) == dest
    install.install_extension(checkout, custom_nodes)
    assert not dest.is_symlink() and dest.is_dir()
    assert installed_files(dest) == set(install.RUNTIME_FILES)


def test_install_extension_links_a_checkout_without_the_compiled_panel(comfyui, checkout):
    """A link uses the checkout as it is, so the development flow doesn't need every file."""
    for name in install.PANEL_FILES:
        (checkout / install.WEB_DIR / name).unlink()
    dest = install.install_extension(checkout, install.find_custom_nodes(comfyui), link=True)
    assert dest.is_symlink() and dest.resolve() == checkout.resolve()


def test_install_extension_in_place(tmp_path):
    custom_nodes = tmp_path / "custom_nodes"
    checkout = custom_nodes / "some-clone-name"
    checkout.mkdir(parents=True)
    assert install.install_extension(checkout, custom_nodes.resolve()) == checkout.resolve()
    assert os.listdir(custom_nodes) == ["some-clone-name"]


def test_check_cli(tmp_path):
    assert install.check_cli(fake_cli(tmp_path / "deadline")) == "0.60.10"


def test_check_cli_rejects_old_version(tmp_path):
    with pytest.raises(install.InstallError, match="0.60.8 or later"):
        install.check_cli(fake_cli(tmp_path / "deadline", version="0.60.3"))


def _fake_cli_with_botocore(tmp_path, botocore_version):
    cli = fake_cli(tmp_path / "env" / "deadline")
    python = cli.parent / "python"
    python.write_text(f"#!/bin/sh\necho {botocore_version}\n")
    python.chmod(0o755)
    return cli


def test_check_cli_accepts_a_new_enough_sdk(tmp_path):
    assert install.check_cli(_fake_cli_with_botocore(tmp_path, "1.43.105")) == "0.60.10"


def test_check_cli_rejects_an_old_sdk(tmp_path):
    with pytest.raises(install.InstallError, match=r"botocore 1\.43\.104.*1\.43\.105"):
        install.check_cli(_fake_cli_with_botocore(tmp_path, "1.43.104"))


def test_check_cli_leaves_an_unreadable_sdk_to_submitting(tmp_path):
    assert install.check_cli(_fake_cli_with_botocore(tmp_path, "unknown")) == "0.60.10"


def test_check_cli_rejects_a_cli_without_its_own_python(monkeypatch, tmp_path):
    """A standalone build has no Python, and the panel runs the CLI's Python."""
    monkeypatch.delenv("COMFYUI_DEADLINE_PYTHON", raising=False)
    with pytest.raises(install.InstallError, match="Couldn't find the Python.*private copy"):
        install.check_cli(fake_cli(tmp_path / "standalone" / "deadline", with_python=False))


def test_find_cli_installs_private_copy_when_the_path_cli_has_no_python(monkeypatch, tmp_path):
    monkeypatch.delenv("COMFYUI_DEADLINE_PYTHON", raising=False)
    cli = fake_cli(tmp_path / "standalone" / "deadline", with_python=False)
    monkeypatch.setenv("PATH", str(cli.parent))
    private = tmp_path / "private" / "deadline"
    monkeypatch.setattr(install, "install_cli", lambda extension_dir: private)
    assert install.find_cli(tmp_path, None, False) == private


def test_install_extension_link_keeps_the_copys_config(comfyui, checkout):
    custom_nodes = install.find_custom_nodes(comfyui)
    copy = install.install_extension(checkout, custom_nodes)
    (copy / install.CLI_ENV_DIR_NAME / "bin").mkdir(parents=True)
    (copy / install.CONFIG_FILE_NAME).write_text('{"conda_channels": "s3://bucket/Conda"}')

    dest = install.install_extension(checkout, custom_nodes, link=True)

    assert dest.is_symlink() and dest.resolve() == checkout.resolve()
    config = json.loads((checkout / install.CONFIG_FILE_NAME).read_text())
    assert config == {"conda_channels": "s3://bucket/Conda"}


def test_install_extension_link_keeps_the_checkouts_own_config(comfyui, checkout):
    custom_nodes = install.find_custom_nodes(comfyui)
    copy = install.install_extension(checkout, custom_nodes)
    (copy / install.CONFIG_FILE_NAME).write_text('{"conda_channels": "from the copy"}')
    (checkout / install.CONFIG_FILE_NAME).write_text('{"conda_channels": "the checkout\'s"}')

    install.install_extension(checkout, custom_nodes, link=True)

    config = json.loads((checkout / install.CONFIG_FILE_NAME).read_text())
    assert config == {"conda_channels": "the checkout's"}


def test_find_cli_installs_private_copy_when_the_path_cli_has_an_old_sdk(monkeypatch, tmp_path):
    cli = _fake_cli_with_botocore(tmp_path, "1.43.31")
    monkeypatch.setenv("PATH", str(cli.parent))
    private = tmp_path / "private" / "deadline"
    monkeypatch.setattr(install, "install_cli", lambda extension_dir: private)
    assert install.find_cli(tmp_path, None, False) == private


def test_script_runs_with_only_the_standard_library(tmp_path):
    """It runs before anything is installed, so it can't import PyYAML, even indirectly."""
    code = (
        "import runpy, sys\n"
        "sys.modules['yaml'] = None\n"
        "sys.argv = ['install_submitter.py', '--help']\n"
        f"runpy.run_path({str(REPO_ROOT / 'install_submitter.py')!r}, run_name='__main__')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "--comfyui" in result.stdout


def test_check_cli_rejects_missing(tmp_path):
    with pytest.raises(install.InstallError, match="isn't an executable"):
        install.check_cli(tmp_path / "deadline")


def test_write_config_stores_private_cli_relative(tmp_path):
    cli = tmp_path / install.CLI_ENV_DIR_NAME / "bin" / "deadline"
    path = install.write_config(tmp_path, cli, "s3://b/Conda/Default\n conda-forge")
    assert json.loads(path.read_text()) == {
        "deadline_cli": ".deadline-cli/bin/deadline",
        "conda_channels": "s3://b/Conda/Default conda-forge",
    }


def test_write_config_is_only_readable_by_its_owner(tmp_path):
    previous = os.umask(0o022)
    try:
        (tmp_path / install.CONFIG_FILE_NAME).write_text("{}")
        path = install.write_config(tmp_path, tmp_path / "a", "s3://b conda-forge")
    finally:
        os.umask(previous)
    assert path.stat().st_mode & 0o777 == 0o600


def test_write_config_keeps_channels_when_not_passed(tmp_path):
    install.write_config(tmp_path, tmp_path / "a", "s3://b conda-forge")
    path = install.write_config(tmp_path, Path("/opt/deadline/bin/deadline"), None)
    assert json.loads(path.read_text()) == {
        "deadline_cli": "/opt/deadline/bin/deadline",
        "conda_channels": "s3://b conda-forge",
    }


def test_write_config_clears_channels_with_empty_string(tmp_path):
    install.write_config(tmp_path, tmp_path / "a", "s3://b conda-forge")
    path = install.write_config(tmp_path, tmp_path / "a", "")
    assert json.loads(path.read_text())["conda_channels"] == ""


def test_install_cli_creates_env_and_installs_deadline(monkeypatch, tmp_path):
    created, commands = [], []
    monkeypatch.setattr(
        install.venv, "create", lambda path, **kwargs: created.append((path, kwargs["symlinks"]))
    )
    monkeypatch.setattr(install.subprocess, "run", lambda cmd, check: commands.append(cmd))

    cli = install.install_cli(tmp_path)

    env_dir = tmp_path / install.CLI_ENV_DIR_NAME
    assert created == [(env_dir, True)]
    assert commands[0][0] == str(env_dir / "bin" / "python")
    assert commands[0][-2:] == ["deadline[gui,console]>=0.60.8,<0.61", "boto3>=1.43.105"]
    assert cli == env_dir / "bin" / "deadline"


def test_find_cli_prefers_explicit_cli(monkeypatch, tmp_path):
    cli = fake_cli(tmp_path / "explicit" / "deadline")
    monkeypatch.setenv("PATH", str(fake_cli(tmp_path / "path" / "deadline").parent))
    assert install.find_cli(tmp_path, cli, private=False) == cli.resolve()


def test_find_cli_uses_new_enough_cli_on_path(monkeypatch, tmp_path):
    cli = fake_cli(tmp_path / "path" / "deadline")
    monkeypatch.setenv("PATH", str(cli.parent))
    monkeypatch.setattr(install, "install_cli", lambda _: pytest.fail("installed a private CLI"))
    assert install.find_cli(tmp_path, None, private=False) == cli


@pytest.mark.parametrize("path_cli_version", [None, "0.60.3"])
def test_find_cli_installs_private_copy_without_usable_cli_on_path(
    monkeypatch, tmp_path, path_cli_version
):
    path_dir = tmp_path / "path"
    path_dir.mkdir()
    if path_cli_version:
        fake_cli(path_dir / "deadline", version=path_cli_version)
    monkeypatch.setenv("PATH", str(path_dir))
    private = tmp_path / "ext" / ".deadline-cli" / "bin" / "deadline"
    monkeypatch.setattr(install, "install_cli", lambda extension_dir: private)
    assert install.find_cli(tmp_path / "ext", None, private=False) == private


def test_find_cli_private_ignores_path(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(fake_cli(tmp_path / "path" / "deadline").parent))
    private = tmp_path / "private" / "deadline"
    monkeypatch.setattr(install, "install_cli", lambda extension_dir: private)
    assert install.find_cli(tmp_path, None, private=True) == private


def test_main_with_existing_cli(comfyui, checkout, tmp_path, capsys, monkeypatch):
    built: list[Path] = []
    monkeypatch.setattr(install, "build_panel", built.append)
    monkeypatch.setattr(install, "REPO_ROOT", checkout)
    cli = fake_cli(tmp_path / "bin" / "deadline")
    channels = "s3://bucket/DeadlineCloud/Conda/Default conda-forge"

    assert (
        install.main(
            ["--comfyui", str(comfyui), "--deadline-cli", str(cli), "--conda-channels", channels]
        )
        == 0
    )

    dest = comfyui / "custom_nodes" / install.EXTENSION_DIR_NAME
    config = json.loads((dest / install.CONFIG_FILE_NAME).read_text())
    assert config == {"deadline_cli": str(cli.resolve()), "conda_channels": channels}
    assert built == [checkout]
    assert "Restart ComfyUI" in capsys.readouterr().out


def test_main_reports_errors(tmp_path, capsys):
    assert install.main(["--comfyui", str(tmp_path)]) == 1
    assert "Setup failed" in capsys.readouterr().out


def test_installed_extension_reads_config(comfyui, checkout, tmp_path, monkeypatch):
    monkeypatch.setattr(install, "build_panel", lambda source: None)
    monkeypatch.setattr(install, "REPO_ROOT", checkout)
    cli = fake_cli(tmp_path / "bin" / "deadline")
    assert (
        install.main(
            [
                "--comfyui",
                str(comfyui),
                "--deadline-cli",
                str(cli),
                "--conda-channels",
                "s3://b conda-forge",
            ]
        )
        == 0
    )
    dest = comfyui / "custom_nodes" / install.EXTENSION_DIR_NAME

    spec = importlib.util.spec_from_file_location(
        "installed_submit", dest / "src/deadline/comfyui_submitter/submit.py"
    )
    assert spec and spec.loader
    installed = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "installed_submit", installed)
    spec.loader.exec_module(installed)
    monkeypatch.delenv(installed.CONFIG_ENV_VAR, raising=False)
    monkeypatch.delenv(installed.CLI_ENV_VAR, raising=False)
    monkeypatch.delenv(installed.CONDA_CHANNELS_ENV_VAR, raising=False)

    config = installed.load_config()
    assert installed.resolve_deadline_cli(config) == str(cli.resolve())
    assert installed.resolve_conda_channels(config) == "s3://b conda-forge"


def frontend_checkout(tmp_path: Path) -> Path:
    source = tmp_path / "checkout"
    (source / "frontend").mkdir(parents=True)
    (source / "frontend" / "package.json").write_text("{}")
    return source


def fake_node_tools(
    tmp_path: Path, monkeypatch, node_version: str = "v22.11.0", build_fails: bool = False
) -> Path:
    """A node that prints its version, and an npm that logs its arguments and builds the panel."""
    bin_dir = tmp_path / "node-bin"
    bin_dir.mkdir()
    log = tmp_path / "npm.log"
    (bin_dir / "node").write_text(f"#!/bin/sh\necho {node_version}\n")
    web = "../src/deadline/comfyui_submitter/web"
    build = (
        'echo "error TS2304: Cannot find name" >&2; exit 2'
        if build_fails
        else f"mkdir -p {web} && touch {web}/deadline_submit.js {web}/deadline_ui.js"
    )
    (bin_dir / "npm").write_text(
        f'#!/bin/sh\necho "$PWD $*" >> {log}\n'
        f'if [ "$1" = run ] && [ "$2" = build ]; then {build}; fi\n'
    )
    for tool in ("node", "npm"):
        (bin_dir / tool).chmod(0o755)
    # The fake npm still needs the system tools, such as mkdir.
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    return log


def test_build_panel_installs_the_build_tools_and_compiles_the_typescript(tmp_path, monkeypatch):
    source = frontend_checkout(tmp_path)
    log = fake_node_tools(tmp_path, monkeypatch)
    install.build_panel(source)
    frontend = (source / "frontend").resolve()
    assert log.read_text().splitlines() == [
        f"{frontend} ci --no-audit --no-fund",
        f"{frontend} run build",
    ]
    for name in install.PANEL_FILES:
        assert (source / install.WEB_DIR / name).is_file()


def test_build_panel_needs_node(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(install.InstallError, match="needs Node.js 22 or later, with npm"):
        install.build_panel(frontend_checkout(tmp_path))


def test_build_panel_rejects_an_old_node(tmp_path, monkeypatch):
    fake_node_tools(tmp_path, monkeypatch, node_version="v18.20.4")
    with pytest.raises(install.InstallError, match="is 18.20.4. Building the panel needs"):
        install.build_panel(frontend_checkout(tmp_path))


def test_build_panel_reports_a_failed_build(tmp_path, monkeypatch):
    fake_node_tools(tmp_path, monkeypatch, build_fails=True)
    with pytest.raises(install.InstallError, match="npm run build failed(.|\n)*TS2304"):
        install.build_panel(frontend_checkout(tmp_path))


def test_build_panel_uses_the_compiled_panel_of_a_copy_without_the_typescript(
    tmp_path, monkeypatch
):
    """Running the installed copy's script again doesn't need Node."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    source = tmp_path / "installed"
    (source / install.WEB_DIR).mkdir(parents=True)
    for name in install.PANEL_FILES:
        (source / install.WEB_DIR / name).write_text("")
    install.build_panel(source)


def test_build_panel_needs_the_typescript_or_the_compiled_panel(tmp_path):
    with pytest.raises(install.InstallError, match="neither the panel's TypeScript"):
        install.build_panel(tmp_path)
