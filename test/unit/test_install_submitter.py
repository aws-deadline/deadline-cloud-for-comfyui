# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import importlib.util
import json
import os
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


def fake_cli(path: Path, version: str = "0.60.10") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo "deadline, version {version}"; exit 0; fi\n'
        'if [ "$1" = "config" ]; then echo "farm-123"; exit 0; fi\n'
    )
    path.chmod(0o755)
    return path


def test_find_custom_nodes(comfyui):
    assert install.find_custom_nodes(comfyui) == (comfyui / "custom_nodes").resolve()


def test_find_custom_nodes_rejects_other_dirs(tmp_path):
    with pytest.raises(install.InstallError, match="no custom_nodes"):
        install.find_custom_nodes(tmp_path)


def test_install_extension_copies_runtime_files(comfyui):
    dest = install.install_extension(REPO_ROOT, install.find_custom_nodes(comfyui))
    assert dest == (comfyui / "custom_nodes" / install.EXTENSION_DIR_NAME).resolve()
    assert (dest / "__init__.py").is_file()
    assert (dest / "src/deadline/comfyui_submitter/web/deadline_submit.js").is_file()
    assert (dest / "src/deadline/comfyui_submitter/comfy_cli_job_template.yaml").is_file()
    for skipped in (".git", "test", "build", install.CLI_ENV_DIR_NAME):
        assert not (dest / skipped).exists()


def test_install_extension_update_keeps_cli_env_and_config(comfyui):
    custom_nodes = install.find_custom_nodes(comfyui)
    dest = install.install_extension(REPO_ROOT, custom_nodes)
    (dest / install.CLI_ENV_DIR_NAME / "bin").mkdir(parents=True)
    (dest / install.CONFIG_FILE_NAME).write_text("{}")
    (dest / "stale.txt").write_text("from an old version")

    install.install_extension(REPO_ROOT, custom_nodes)

    assert (dest / install.CLI_ENV_DIR_NAME / "bin").is_dir()
    assert (dest / install.CONFIG_FILE_NAME).read_text() == "{}"
    assert not (dest / "stale.txt").exists()


def test_install_extension_refuses_to_replace_other_directory(comfyui):
    custom_nodes = install.find_custom_nodes(comfyui)
    (custom_nodes / install.EXTENSION_DIR_NAME).mkdir()
    (custom_nodes / install.EXTENSION_DIR_NAME / "mine.txt").write_text("keep")
    with pytest.raises(install.InstallError, match="--force"):
        install.install_extension(REPO_ROOT, custom_nodes)
    assert (custom_nodes / install.EXTENSION_DIR_NAME / "mine.txt").exists()


def test_install_extension_links(comfyui):
    custom_nodes = install.find_custom_nodes(comfyui)
    dest = install.install_extension(REPO_ROOT, custom_nodes, link=True)
    assert dest.is_symlink() and dest.resolve() == REPO_ROOT
    assert install.install_extension(REPO_ROOT, custom_nodes, link=True) == dest
    install.install_extension(REPO_ROOT, custom_nodes)
    assert not dest.is_symlink() and dest.is_dir()


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


def test_main_with_existing_cli(comfyui, tmp_path, capsys):
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
    assert "Restart ComfyUI" in capsys.readouterr().out


def test_main_reports_errors(tmp_path, capsys):
    assert install.main(["--comfyui", str(tmp_path)]) == 1
    assert "Setup failed" in capsys.readouterr().out


def test_installed_extension_reads_config(comfyui, tmp_path, monkeypatch):
    cli = fake_cli(tmp_path / "bin" / "deadline")
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
