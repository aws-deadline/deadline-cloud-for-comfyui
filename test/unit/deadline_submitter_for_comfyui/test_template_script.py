# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Runs the comfy-cli job template's task script with a stand-in `comfy` command. The script runs
comfy-cli twice: once to print the workflow in API format, and once to run it, which the stand-in
saves for the test to read. Also runs the part of the server environment's Enter script that
writes extra_model_paths.yaml.
"""

import argparse
import http.server
import io
import json
import os
import shlex
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml
from openjd.model import SymbolTable, decode_job_template

from deadline.comfyui_submitter import submit

pytestmark = pytest.mark.skipif(os.name == "nt", reason="The stand-in comfy is a POSIX script")

TEMPLATE = yaml.safe_load(submit.JOB_TEMPLATE.read_text())
PARSED_TEMPLATE = decode_job_template(template=TEMPLATE, supported_extensions=["EXPR"])


def environment_values(
    tmp_path: Path, raw_parameters: dict[str, str] | None = None, /, **parameters: str
) -> dict[str, str]:
    """Resolve the job environment's variables as OpenJD does, without editing script source."""
    values = {p["name"]: str(p.get("default", "")) for p in TEMPLATE["parameterDefinitions"]}
    values.update(parameters)
    symbols = SymbolTable(source={"Session.WorkingDirectory": str(tmp_path)})
    for parameter, value in values.items():
        symbols["Param." + parameter] = value
        symbols["RawParam." + parameter] = (raw_parameters or {}).get(parameter, value)
    assert PARSED_TEMPLATE.jobEnvironments is not None
    [environment] = PARSED_TEMPLATE.jobEnvironments
    assert environment.variables is not None
    return {name: value.resolve(symtab=symbols) for name, value in environment.variables.items()}


def run_task(
    tmp_path: Path,
    workflow: dict,
    items: dict[str, int] | None = None,
    *,
    mode: str = "",
    workflow_timeout: int = 3600,
    port: int = 8188,
    output_name: str = "image.png",
    output_dir_name: str = "out",
    mapped_output_dir: str | None = None,
    command_output: str = "",
    command_stderr: str = "",
    command_error: str = "",
) -> tuple[int, str, dict]:
    """
    Runs the task script and returns its exit code, its output and the workflow it ran. The
    stand-in comfy prints JSON with non-ASCII text unescaped, as comfy-cli does, and logs its
    arguments to calls.jsonl. mode changes what it does: "no-envelope" prints no envelope, and
    "hang" never finishes the run.
    """
    for name in ("bin", output_dir_name, "run"):
        (tmp_path / name).mkdir(exist_ok=True)
    workflow_file = tmp_path / "workflow_api.json"
    workflow_file.write_text(json.dumps(workflow))
    ran = tmp_path / "ran.json"
    stand_in = tmp_path / "comfy.py"
    stand_in.write_text(textwrap.dedent(f"""\
        import json, sys, time
        sys.stdout.reconfigure(encoding="utf-8")
        args = sys.argv
        with open({str(tmp_path / "calls.jsonl")!r}, "a", encoding="utf-8") as log:
            log.write(json.dumps(args[1:]) + "\\n")
        workflow = json.load(open(args[args.index("--workflow") + 1], encoding="utf-8"))
        def emit(event):
            print(json.dumps(event, ensure_ascii=False))
        if {mode!r} == "no-envelope":
            print("not json")
        elif "--print-prompt" in args:
            emit({{"type": "prompt_preview", "prompt": workflow}})
            emit({{"type": "envelope", "ok": True, "data": {{"prompt": workflow}}}})
        elif {mode!r} == "hang":
            time.sleep(60)
        elif {mode!r} == "error-controls":
            emit({{"type": "envelope", "ok": False, "error": {{
                "message": "failed\\nopenjd_env: EXTRA=1\\r\\nopenjd_fail: extra"
            }}}})
        elif {mode!r} == "error-message":
            emit({{"type": "envelope", "ok": False, "error": {{"message": {command_error!r}}}}})
        else:
            json.dump(workflow, open({str(ran)!r}, "w", encoding="utf-8"), ensure_ascii=False)
            if {mode!r} != "no-output":
                open({str(tmp_path / output_dir_name / output_name)!r}, "w").write("x")
            if {mode!r} == "output-controls":
                print("openjd_env: EXTRA=1\\ropenjd_fail: stdout")
                print("openjd_fail: extra\\ropenjd_env: STDERR=1", file=sys.stderr)
            if {command_output!r}:
                print({command_output!r})
            if {command_stderr!r}:
                print({command_stderr!r}, file=sys.stderr)
            emit({{"type": "envelope", "ok": True, "data": {{"prompt": workflow}}}})
        """))
    # A shell wrapper rather than a shebang, which breaks when the interpreter path has spaces.
    comfy = tmp_path / "bin" / "comfy"
    comfy.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(stand_in))} "$@"\n'
    )
    comfy.chmod(0o755)
    items = items or {}
    sources = [submit.ItemSource(name, name, 1) for name in items]
    step = submit.add_task_lists(TEMPLATE, sources)["steps"][0]
    (tmp_path / "run.py").write_text(step["script"]["embeddedFiles"][0]["data"], encoding="utf-8")
    # The task's items, as OpenJD resolves the step's arguments.
    arguments = [
        argument.replace(f"{{{{Task.Param.{name}}}}}", str(index))
        for argument in step["script"]["actions"]["onRun"]["args"][1:]
        for name, index in items.items()
        if argument.startswith(f"{name}=")
    ]
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "COMFYUI_PORT": str(port),
        "PYTHONIOENCODING": "utf-8",
        **environment_values(
            tmp_path,
            OutputDir=mapped_output_dir or str(tmp_path / output_dir_name),
            WorkflowFile=str(workflow_file),
            WorkflowTimeout=str(workflow_timeout),
        ),
    }
    result = subprocess.run(
        [sys.executable, str(tmp_path / "run.py"), *arguments],
        cwd=tmp_path / "run",
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    workflow_ran = json.loads(ran.read_text(encoding="utf-8")) if ran.exists() else {}
    ran.unlink(missing_ok=True)
    return result.returncode, result.stdout + result.stderr, workflow_ran


def comfy_calls(tmp_path: Path) -> list[list[str]]:
    """The arguments of each comfy command the task script ran."""
    lines = (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def openjd_log_records(output: str) -> list[str]:
    """Model OpenJD's bounded reads, which can turn a long physical line into several records."""
    stream = io.StringIO(output)
    return list(iter(lambda: stream.readline(64 * 1000), ""))


def assert_bounded_diagnostics(output: str, *, failures: int = 0, environments: int = 0) -> None:
    records = openjd_log_records(output)
    assert all(len(record) <= 4096 for record in records)
    assert len([record for record in records if record.startswith("openjd_fail:")]) == failures
    assert len([record for record in records if record.startswith("openjd_env:")]) == environments


def enter_script(end: str | None = None) -> str:
    """The server environment's Enter script, or its prefix, without source substitutions."""
    [environment] = TEMPLATE["jobEnvironments"]
    [enter] = [f for f in environment["script"]["embeddedFiles"] if f["name"] == "Enter"]
    data = enter["data"]
    return data if end is None else data[: data.index(end)]


def write_model_paths(
    tmp_path: Path,
    model_directory: Path | None = None,
    conda_nodes: Path | None = None,
    custom_nodes_directory: Path | None = None,
    attach_nodes: bool = True,
) -> Any:
    """
    Runs the part of the Enter script that writes extra_model_paths.yaml, and returns the config
    it wrote.
    """
    script = enter_script("# ComfyUI can't read an empty")
    result = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + script],
        env={
            "PATH": os.environ["PATH"],
            **environment_values(
                tmp_path,
                ModelDirectory=str(model_directory or ""),
                CustomNodesDirectory=str(custom_nodes_directory or ""),
                AttachCustomNodes="true" if attach_nodes else "false",
            ),
            **({"COMFYUI_CUSTOM_NODES_DIR": str(conda_nodes)} if conda_nodes else {}),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return yaml.safe_load((tmp_path / "extra_model_paths.yaml").read_text())


def test_model_paths_map_every_attached_model_folder(tmp_path):
    """Folders outside the common ones, such as style_models, are mapped too."""
    attached = tmp_path / "attached models"
    for folder in (attached / "loras", attached / "style_models"):
        folder.mkdir(parents=True)
    # Not a model folder type name, so it's left out rather than written as a YAML key.
    (attached / "bad: name").mkdir()
    assert write_model_paths(tmp_path, attached) == {
        "model_directory": {
            "base_path": str(attached),
            "loras": "loras/",
            "style_models": "style_models/",
        },
    }


def test_model_paths_are_empty_without_models_or_conda_custom_nodes(tmp_path):
    """The file stays empty, and the Enter script then doesn't pass it to ComfyUI."""
    assert write_model_paths(tmp_path) is None
    assert write_model_paths(tmp_path, tmp_path / "missing") is None


def test_model_paths_add_the_conda_environments_custom_nodes(tmp_path):
    conda_nodes = tmp_path / "conda custom_nodes"
    conda_nodes.mkdir()
    assert write_model_paths(tmp_path, conda_nodes=conda_nodes) == {
        "conda_custom_nodes": {"custom_nodes": str(conda_nodes)}
    }


def test_model_paths_add_the_attached_custom_nodes(tmp_path):
    attached = tmp_path / "attached nodes"
    (attached / "studio-nodes").mkdir(parents=True)
    assert write_model_paths(tmp_path, custom_nodes_directory=attached) == {
        "attached_custom_nodes": {"custom_nodes": str(attached)}
    }


def test_model_paths_leave_out_attached_custom_nodes_when_attaching_is_off(tmp_path):
    """The workers then provide every custom node, for example from a conda package."""
    attached = tmp_path / "attached nodes"
    (attached / "studio-nodes").mkdir(parents=True)
    assert write_model_paths(tmp_path, custom_nodes_directory=attached, attach_nodes=False) is None


def test_the_job_log_lists_the_attached_custom_nodes(tmp_path):
    """The names come from the bundle, so they're written as diagnostics, not OpenJD commands."""
    attached = tmp_path / "attached nodes"
    (attached / "studio-nodes").mkdir(parents=True)
    (attached / "openjd_fail: x").mkdir()
    result = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + enter_script("# ComfyUI can't read an empty")],
        env={
            "PATH": os.environ["PATH"],
            **environment_values(tmp_path, CustomNodesDirectory=str(attached)),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "comfy: Attached custom nodes: 'openjd_fail: x', 'studio-nodes'"
    ]


def model_path_args(
    tmp_path: Path,
    extra: str = "",
    conda_nodes: Path | None = None,
    model_directory: Path | None = None,
) -> list[str]:
    """The extra model paths arguments the Enter script passes to ComfyUI."""
    script = enter_script("# Keep ComfyUI's data out")
    result = subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail\n"
            + script
            + "\npython -c 'import json, sys; print(json.dumps(sys.argv[1:]))'"
            + ' ${MODEL_PATH_ARGS[@]+"${MODEL_PATH_ARGS[@]}"}',
        ],
        env={
            "PATH": os.environ["PATH"],
            **environment_values(
                tmp_path, ModelDirectory=str(model_directory or ""), ExtraModelPathsConfig=extra
            ),
            **({"COMFYUI_CUSTOM_NODES_DIR": str(conda_nodes)} if conda_nodes else {}),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_an_empty_extra_model_paths_file_isnt_passed_to_comfyui(tmp_path):
    """ComfyUI can't read an empty extra_model_paths.yaml."""
    assert model_path_args(tmp_path) == []
    studio = "/mnt/studio/extra_model_paths.yaml"
    assert model_path_args(tmp_path, studio) == ["--extra-model-paths-config", studio]


def test_conda_custom_nodes_and_the_studio_config_are_both_passed(tmp_path):
    conda_nodes = tmp_path / "conda_nodes"
    conda_nodes.mkdir()
    studio = "/mnt/studio/extra_model_paths.yaml"
    assert model_path_args(tmp_path, studio, conda_nodes) == [
        "--extra-model-paths-config",
        str(tmp_path / "extra_model_paths.yaml"),
        studio,
    ]


def test_attached_models_and_the_studio_config_are_both_passed(tmp_path):
    attached = tmp_path / "models"
    (attached / "loras").mkdir(parents=True)
    studio = "/mnt/studio/extra_model_paths.yaml"
    assert model_path_args(tmp_path, studio, model_directory=attached) == [
        "--extra-model-paths-config",
        str(tmp_path / "extra_model_paths.yaml"),
        studio,
    ]


def test_model_paths_preserve_quotes_and_line_breaks(tmp_path):
    attached = tmp_path / 'artist\'s "models"\n$folder'
    (attached / "checkpoints").mkdir(parents=True)
    conda_nodes = tmp_path / 'artist\'s "custom nodes"\n$folder'
    conda_nodes.mkdir()
    assert write_model_paths(tmp_path, attached, conda_nodes) == {
        "model_directory": {"base_path": str(attached), "checkpoints": "checkpoints/"},
        "conda_custom_nodes": {"custom_nodes": str(conda_nodes)},
    }
    studio = str(tmp_path / 'artist\'s "config"; $(touch injected).yaml')
    assert model_path_args(tmp_path, studio, conda_nodes, attached) == [
        "--extra-model-paths-config",
        str(tmp_path / "extra_model_paths.yaml"),
        studio,
    ]
    assert not (tmp_path / "injected").exists()


def run_enter(
    tmp_path: Path,
    *,
    launch_output: str = "",
    log_output: str = "",
    stop_output: str = "",
    stats_output: str = "{}",
    **parameters: str,
) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run the whole Enter script with stand-in comfy and curl commands, without a listener."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "launch_calls.jsonl"
    stand_in = tmp_path / "comfy.py"
    stand_in.write_text(textwrap.dedent(f"""\
        import json, sys
        with open({str(calls)!r}, "a") as f:
            f.write(json.dumps(sys.argv[1:]) + "\\n")
        if "logs" in sys.argv:
            print(json.dumps({{"data": {{"lines": [{log_output!r}]}}}}))
        elif "launch" in sys.argv:
            print({launch_output!r})
        elif "stop" in sys.argv:
            print({stop_output!r})
            print({stop_output!r}, file=sys.stderr)
        """))
    comfy = bin_dir / "comfy"
    comfy.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(stand_in))} "$@"\n'
    )
    comfy.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(stats_output)}\n")
    curl.chmod(0o755)
    (bin_dir / "python").symlink_to(sys.executable)
    result = subprocess.run(
        ["bash", "-c", enter_script()],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "COMFYUI_LOCATION": str(tmp_path / "ComfyUI"),
            **environment_values(tmp_path, **parameters),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    launches = []
    if calls.exists():
        for line in calls.read_text().splitlines():
            args = json.loads(line)
            if "launch" in args:
                launches.append(args)
    return result, launches[0] if launches else []


def test_enter_passes_paths_and_extra_arguments_as_literal_argv(tmp_path):
    session = tmp_path / 'artist\'s "session"; $value'
    session.mkdir()
    output = str(session / "out'\"; $(touch injected)")
    inputs = str(session / "input'\"; `touch injected`")
    config = str(session / "extra'\" model paths.yaml")
    extra_args = "--lowvram --name \"two words\" --literal '$(touch injected)' ; touch injected"
    result, args = run_enter(
        session,
        InputDir=inputs,
        OutputDir=output,
        ExtraModelPathsConfig=config,
        ComfyUIArgs=extra_args,
    )
    assert result.returncode == 0, result.stderr
    assert args[args.index("--input-directory") + 1] == inputs
    assert args[args.index("--output-directory") + 1] == output
    assert args[args.index("--extra-model-paths-config") + 1] == config
    start = args.index("--") + 1
    assert args[start : start + 8] == [
        "--lowvram",
        "--name",
        "two words",
        "--literal",
        "$(touch injected)",
        ";",
        "touch",
        "injected",
    ]
    assert not (session / "injected").exists()
    assert Path(output).is_dir()


def test_enter_rejects_unclosed_argument_quotes_before_launching(tmp_path):
    result, args = run_enter(tmp_path, ComfyUIArgs='--name "unfinished')
    assert result.returncode != 0
    assert "openjd_fail: Invalid Extra ComfyUI Arguments" in result.stdout
    assert args == []


def test_job_environment_uses_mapped_paths_without_reinterpreting_their_contents(tmp_path):
    mapped = {
        name: str(tmp_path / f"{name}'\"{{{{Param.InputDir}}}}")
        for name in ("WorkflowFile", "InputDir", "OutputDir", "ModelDirectory")
    }
    raw = {name: f"/submitter/{name}" for name in mapped}
    environment = environment_values(tmp_path, raw, **mapped)
    assert environment["COMFYUI_WORKFLOW_FILE"] == mapped["WorkflowFile"]
    assert environment["COMFYUI_INPUT_DIR"] == mapped["InputDir"]
    assert environment["COMFYUI_OUTPUT_DIR"] == mapped["OutputDir"]
    assert environment["COMFYUI_MODEL_DIRECTORY"] == mapped["ModelDirectory"]


@pytest.mark.parametrize(
    "extra_args",
    [
        "--listen",
        "--listen 0.0.0.0 --port 1234 --output-directory /other",
        "--lis=0.0.0.0 --po=1234 --output-dir=/other",
    ],
)
def test_enter_keeps_the_required_server_arguments_authoritative(tmp_path, extra_args):
    output = str(tmp_path / "out")
    result, args = run_enter(tmp_path, ComfyUIArgs=extra_args, OutputDir=output)
    assert result.returncode == 0, result.stderr
    # ComfyUI uses argparse, including abbreviation and the last value of repeated options.
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", nargs="?", const="0.0.0.0")
    parser.add_argument("--port", type=int)
    parser.add_argument("--base-directory")
    parser.add_argument("--input-directory")
    parser.add_argument("--output-directory")
    parsed = parser.parse_args(args[args.index("--") + 1 :])
    assert parsed.listen == "127.0.0.1"
    assert parsed.port != 1234
    assert parsed.output_directory == output
    assert parsed.base_directory == str(tmp_path / "comfyui")
    assert parsed.input_directory == str(tmp_path / "comfyui" / "input")


def test_enter_rejects_an_argument_terminator_before_launching(tmp_path):
    result, args = run_enter(tmp_path, ComfyUIArgs="-- --listen 0.0.0.0")
    assert result.returncode != 0
    assert "openjd_fail: Invalid Extra ComfyUI Arguments" in result.stdout
    assert args == []


def test_enter_reports_paths_and_child_output_without_control_lines(tmp_path):
    result, _ = run_enter(
        tmp_path,
        InputDir=str(tmp_path / "input\r\nopenjd_env: EXTRA=1\r"),
        OutputDir=str(tmp_path / "out"),
        launch_output="openjd_env: EXTRA=2\ropenjd_fail: extra\n",
        stats_output="openjd_env: EXTRA=3\ropenjd_fail: stats\n",
    )
    assert result.returncode == 0, result.stderr
    controls = [line for line in result.stdout.splitlines() if line.startswith("openjd_")]
    assert len([line for line in controls if line.startswith("openjd_env:")]) == 1
    assert not any(line.startswith("openjd_fail:") for line in controls)
    assert "comfy: openjd_env: EXTRA=2" in result.stdout


def test_enter_reports_import_failure_logs_and_stop_output_as_diagnostics(tmp_path):
    result, _ = run_enter(
        tmp_path,
        OutputDir=str(tmp_path / "out"),
        log_output="(IMPORT FAILED)\r\nTraceback\nopenjd_env: LOG=1\ropenjd_fail: log\n",
        stop_output="openjd_env: STOP=1\ropenjd_fail: stop\n",
    )
    assert result.returncode == 1
    controls = [line for line in result.stdout.splitlines() if line.startswith("openjd_")]
    assert len([line for line in controls if line.startswith("openjd_env:")]) == 1
    assert [line for line in controls if line.startswith("openjd_fail:")] == [
        "openjd_fail: A custom node failed to import"
    ]
    assert "comfy: openjd_env: LOG=1" in result.stdout
    assert "comfy: openjd_env: STOP=1" in result.stdout


@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize("long_output", [False, True])
def test_exit_prefixes_stop_output_and_preserves_its_exit_code(tmp_path, exit_code, long_output):
    comfy = tmp_path / "comfy"
    output = (
        "x" * (64 * 1000 - len("comfy: ")) + "openjd_env: STOP=1"
        if long_output
        else "openjd_env: STOP=1\ropenjd_fail: stop"
    )
    comfy.write_text(
        "#!/bin/sh\n"
        + f"printf '%s\\n' {shlex.quote(output)}\n"
        + f"printf '%s\\n' {shlex.quote('openjd_fail: stderr')} >&2\n"
        + f"exit {exit_code}\n"
    )
    comfy.chmod(0o755)
    [environment] = TEMPLATE["jobEnvironments"]
    [exit_file] = [
        file for file in environment["script"]["embeddedFiles"] if file["name"] == "Exit"
    ]
    result = subprocess.run(
        ["bash", "-c", exit_file["data"]],
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "COMFYUI_PORT": "8188",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == exit_code
    assert not result.stderr
    assert_bounded_diagnostics(result.stdout)
    if not long_output:
        assert result.stdout.splitlines() == [
            "comfy: openjd_env: STOP=1\\ropenjd_fail: stop",
            "comfy: openjd_fail: stderr",
        ]


@pytest.mark.parametrize("stream", ["launch", "stats", "logs", "stop"])
def test_enter_bounds_each_forwarded_diagnostic_record(tmp_path, stream):
    # With one prefix for the whole line, the next OpenJD reader chunk starts with a marker.
    text = "x" * (64 * 1000 - len("comfy: ")) + "openjd_env: EXTRA=1"
    options = {"OutputDir": str(tmp_path / "out")}
    if stream == "launch":
        options["launch_output"] = text
    elif stream == "stats":
        options["stats_output"] = text
    else:
        options["log_output"] = "(IMPORT FAILED)\n" + (text if stream == "logs" else "")
        options["stop_output"] = text if stream == "stop" else ""
    result, _ = run_enter(tmp_path, **options)
    assert result.returncode == (1 if stream in ("logs", "stop") else 0), result.stderr
    assert "openjd_env: EXTRA=1" in result.stdout
    assert_bounded_diagnostics(
        result.stdout + result.stderr,
        failures=int(stream in ("logs", "stop")),
        environments=1,
    )


def test_enter_bounds_a_long_invalid_input_path_and_its_directory_error(tmp_path):
    path = str(tmp_path / ("x" * (64 * 1000) + "openjd_env: EXTRA=1"))
    result, args = run_enter(tmp_path, InputDir=path, OutputDir=str(tmp_path / "out"))
    assert result.returncode != 0
    assert args == []
    assert "Input directory:" in result.stdout
    assert_bounded_diagnostics(result.stdout + result.stderr)


# Line and paragraph separators that str.splitlines() also splits on, and other non-ASCII text.
UNUSUAL_TEXT = "final fade.   \x85\x0b\x0c\x1c — café, naïve 🦊"


def _prompt(text=UNUSUAL_TEXT, prefix="fox — café 🦊"):
    return {
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": text}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": prefix}},
    }


def test_prompts_with_line_separators_and_other_unicode_run(tmp_path):
    """comfy-cli prints them unescaped, inside one JSON line that splitlines() would break up."""
    code, output, ran = run_task(tmp_path, _prompt())
    assert code == 0, output
    assert ran["6"]["inputs"]["text"] == UNUSUAL_TEXT
    assert ran["9"]["inputs"]["filename_prefix"] == "fox — café 🦊"
    [preview, run] = comfy_calls(tmp_path)
    assert "--print-prompt" in preview and "--wait" in run


def test_task_paths_with_quotes_and_shell_characters_run(tmp_path):
    session = tmp_path / 'artist\'s "session"; $(touch injected)'
    session.mkdir()
    code, output, ran = run_task(session, _prompt("fox"))
    assert code == 0, output
    assert ran == _prompt("fox")
    [preview, _] = comfy_calls(session)
    assert preview[preview.index("--workflow") + 1] == str(session / "workflow_api.json")
    assert not (session / "injected").exists()


def test_task_reports_output_names_and_command_output_without_control_lines(tmp_path):
    code, output, ran = run_task(
        tmp_path,
        _prompt("fox"),
        mode="output-controls",
        output_name="image\nopenjd_env: EXTRA=2\n",
        output_dir_name="out\nopenjd_env: EXTRA=3\n",
    )
    assert code == 0, output
    assert ran == _prompt("fox")
    assert not any(line.startswith(("openjd_env:", "openjd_fail:")) for line in output.splitlines())
    assert "comfy: openjd_env: EXTRA=1" in output


def test_task_reports_a_multiline_command_error_as_one_failure(tmp_path):
    code, output, _ = run_task(tmp_path, _prompt("fox"), mode="error-controls")
    assert code == 1
    assert len([line for line in output.splitlines() if line.startswith("openjd_fail:")]) == 1
    assert not any(line.startswith("openjd_env:") for line in output.splitlines())


def test_task_bounds_large_command_output_without_changing_the_json_workflow(tmp_path):
    text = "x" * (64 * 1000 - len("comfy: ")) + "openjd_env: EXTRA=1"
    workflow = _prompt(text)
    code, output, ran = run_task(tmp_path, workflow, command_output=text, command_stderr=text)
    assert code == 0, output
    assert ran == workflow
    assert "openjd_env: EXTRA=1" in output
    assert_bounded_diagnostics(output)


def test_task_bounds_a_large_failure_and_keeps_one_failure_record(tmp_path):
    text = "x" * (64 * 1000 - len("openjd_fail: comfy run failed: 0 ")) + "openjd_env: EXTRA=1"
    code, output, _ = run_task(tmp_path, _prompt("fox"), mode="error-message", command_error=text)
    assert code == 1
    assert "openjd_env: EXTRA=1" in output
    assert_bounded_diagnostics(output, failures=1)


def test_task_bounds_a_long_output_path_in_its_summary(tmp_path):
    path = str(tmp_path / ("x" * (64 * 1000) + "openjd_env: EXTRA=1"))
    code, output, _ = run_task(tmp_path, _prompt("fox"), mode="no-output", mapped_output_dir=path)
    assert code == 1
    assert "Finished the workflow. Outputs in" in output
    assert_bounded_diagnostics(output, failures=1)


def test_task_bounds_a_long_interrupt_error(tmp_path):
    script = TEMPLATE["steps"][0]["script"]["embeddedFiles"][0]["data"]
    script = script[: script.index("# Convert the workflow")]
    script += """
def unavailable(*args, **kwargs):
    raise OSError(os.environ["INTERRUPT_ERROR"])

urllib.request.urlopen = unavailable
interrupt()
"""
    worker = tmp_path / "interrupt.py"
    worker.write_text(script)
    result = subprocess.run(
        [sys.executable, str(worker)],
        env={
            **os.environ,
            **environment_values(tmp_path),
            "COMFYUI_PORT": "8188",
            "INTERRUPT_ERROR": "x" * (64 * 1000) + "openjd_env: EXTRA=1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Couldn't interrupt the workflow" in result.stdout
    assert_bounded_diagnostics(result.stdout + result.stderr)


def test_task_without_saved_outputs_fails(tmp_path):
    code, output, _ = run_task(tmp_path, _prompt("fox"), mode="no-output")
    assert code == 1
    assert "openjd_fail: The workflow produced no output files." in output


def test_output_without_an_envelope_says_so(tmp_path):
    code, output, _ = run_task(tmp_path, _prompt("fox"), mode="no-envelope")
    assert code == 1
    assert "openjd_fail: comfy run printed no result (exit code 0)" in output


def test_the_run_waits_as_long_as_the_workflow_timeout(tmp_path):
    """comfy run's default 120 second wait for progress is shorter than loading a large model."""
    code, output, _ = run_task(tmp_path, _prompt("fox"), workflow_timeout=5400)
    assert code == 0, output
    [preview, run] = comfy_calls(tmp_path)
    assert "--timeout" not in preview
    assert run[run.index("--timeout") + 1] == "5400"


def test_a_workflow_past_its_timeout_is_interrupted(tmp_path):
    interrupts = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            interrupts.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        code, output, _ = run_task(
            tmp_path, _prompt("fox"), mode="hang", workflow_timeout=2, port=server.server_port
        )
    finally:
        server.shutdown()
    assert code == 1
    assert "didn't finish within the Workflow Timeout of 2 seconds" in output
    assert interrupts == ["/interrupt"]


def _environment_file(name: str) -> str:
    [environment] = TEMPLATE["jobEnvironments"]
    [file] = [f for f in environment["script"]["embeddedFiles"] if f["name"] == name]
    return file["data"]


def _run_node_requirements(
    tmp_path: Path, requirements: str
) -> tuple[subprocess.CompletedProcess, list[str]]:
    """
    Runs the Enter script's custom node requirements block for one pack, and returns its result
    and the pip commands it ran.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "pip.log"
    log.touch()
    # A stand-in python: "-m venv DIR" makes DIR/bin/python, which lists packages as pip list
    # does and records each pip command with the constraints it had. Both run the real Python
    # for anything else, such as the script's diagnostics and the requirements check.
    real_python = shlex.quote(sys.executable)
    venv_python = (
        "#!/bin/sh\n"
        f'case "$1" in -c|*.py) exec {real_python} "$@";; esac\n'
        'if [ "$3" = list ]; then printf "torch==2.5.1\\nnumpy==2.1.0\\n"; exit 0; fi\n'
        f'echo "$* constraint=$PIP_CONSTRAINT" >> {shlex.quote(str(log))}\n'
    )
    python = bin_dir / "python"
    python.write_text(
        "#!/bin/sh\n"
        'if [ "$2" = venv ]; then\n'
        '  mkdir -p "$4/bin"\n'
        f'  printf %s {shlex.quote(venv_python)} > "$4/bin/python"\n'
        '  chmod +x "$4/bin/python"\n'
        f'else exec {real_python} "$@"; fi\n'
    )
    python.chmod(0o755)
    node = tmp_path / "pack"
    node.mkdir()
    (node / "requirements.txt").write_text(requirements)
    check = tmp_path / "check-requirements.py"
    check.write_text(_environment_file("CheckRequirements"))
    data = _environment_file("Enter")
    block = data[data.index("# The attached packs' pip") : data.index("PORT=$(python")]
    diagnostics = data[data.index("diagnostics() {") : data.index("MODEL_PATHS=")]
    nodes = f"ATTACHED_NODES=({shlex.quote(str(node))})"
    result = subprocess.run(
        # The Enter script gets the requirements check as its first argument.
        ["bash", "-c", f"set -euo pipefail\n{diagnostics}\n{nodes}\n{block}", "enter", str(check)],
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "COMFYUI_SESSION_DIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, log.read_text().splitlines()


def test_custom_node_requirements_are_constrained_to_the_conda_packages(tmp_path):
    """Packages conda installed, such as torch, are pinned too, so a pack can't replace them."""
    result, pip = _run_node_requirements(tmp_path, "torch>=99\n")
    assert result.returncode == 0, result.stdout + result.stderr
    constraints = tmp_path / "constraints.txt"
    assert constraints.read_text() == "torch==2.5.1\nnumpy==2.1.0\n"
    requirements = tmp_path / "pack" / "requirements.txt"
    assert pip == [
        f"-m pip install -r {requirements} constraint={constraints}",
        f"-m pip check constraint={constraints}",
    ]


def test_a_pack_cant_point_pip_at_another_package_index(tmp_path):
    """The worker runs pip with the queue role's credentials."""
    result, pip = _run_node_requirements(
        tmp_path, "numpy>=1.26\n--extra-index-url https://packages.example.com/simple\n"
    )
    assert result.returncode == 1
    assert pip == []
    lines = result.stdout.splitlines()
    assert (
        "comfy: Line 2, '--extra-index-url https://packages.example.com/simple': it's a pip option"
        in lines
    )
    assert lines[-1] == (
        "openjd_fail: The requirements of pack use pip options, paths or URLs that jobs don't "
        "install"
    )


@pytest.mark.parametrize(
    "line",
    [
        "numpy>=1.26  # pinned for the sampler",
        "# a comment",
        'opencv-python-headless; sys_platform == "linux"',
        "segment-anything @ git+https://github.com/facebookresearch/segment-anything.git",
        "torchvision==0.20.1 --hash=sha256:0123abcd",
        "https://files.example.com/pkg-1.0-py3-none-any.whl",
    ],
)
def test_requirements_check_accepts_packages_and_https_urls(tmp_path, line):
    (tmp_path / "requirements.txt").write_text(line + "\n")
    (tmp_path / "check.py").write_text(_environment_file("CheckRequirements"))
    result = subprocess.run(
        [sys.executable, str(tmp_path / "check.py"), str(tmp_path / "requirements.txt")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode, result.stdout) == (0, "")


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ("--extra-index-url https://packages.example.com/simple", "it's a pip option"),
        ("-i https://packages.example.com/simple", "it's a pip option"),
        ("--index-url=https://packages.example.com/simple", "it's a pip option"),
        ("--trusted-host packages.example.com", "it's a pip option"),
        ("--find-links https://packages.example.com/", "it's a pip option"),
        ("-r other.txt", "it's a pip option"),
        ("-c constraints.txt", "it's a pip option"),
        ("-e .", "it's a pip option"),
        ("numpy --index-url https://packages.example.com/simple", "it has a pip option"),
        ("numpy \\\n  --extra-index-url https://packages.example.com/", "it has a pip option"),
        ("pkg @ https://${AWS_SECRET_ACCESS_KEY}@packages.example.com/x", "environment variable"),
        ("pkg @ http://packages.example.com/pkg.whl", "only https URLs are installed"),
        ("git+ssh://git@example.com/pack.git", "only https URLs are installed"),
        ("pkg @ file:///tmp/pkg", "only https URLs are installed"),
        ("./vendor/pkg", "it's a local path"),
        ("~/pkg", "it's a local path"),
        ("C:\\pkgs\\pkg.whl", "it's a local path"),
    ],
)
def test_requirements_check_refuses_what_could_change_where_pip_installs_from(
    tmp_path, line, reason
):
    (tmp_path / "requirements.txt").write_text("numpy\n" + line + "\n")
    (tmp_path / "check.py").write_text(_environment_file("CheckRequirements"))
    result = subprocess.run(
        [sys.executable, str(tmp_path / "check.py"), str(tmp_path / "requirements.txt")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    [message] = result.stdout.splitlines()
    assert message.startswith("Line 2, ")
    assert reason in message


def _create_list():
    return {
        "1": {
            "class_type": "CreateList",
            "inputs": {f"inputs.input{i}": [str(i + 2), 0] for i in range(3)},
            "_meta": {submit.ITEM_META: "List1"},
        },
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "fox"}},
    }


@pytest.mark.parametrize(
    "argument", ["List1=2 or 1", "List1=", "List1=١", "=2", "List 1=2", "List1"]
)
def test_the_task_refuses_an_argument_that_isnt_a_list_and_an_item_number(tmp_path, argument):
    """The task's items come in as arguments, which the script reads as data, never as code."""
    script = tmp_path / "run.py"
    script.write_text(TEMPLATE["steps"][0]["script"]["embeddedFiles"][0]["data"], encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script), argument],
        env={
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            **environment_values(tmp_path, OutputDir=str(tmp_path)),
            "COMFYUI_PORT": "1",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout.startswith("openjd_fail: The task argument ")
    assert result.stdout.rstrip("\n").endswith("isn't a list name and an item number.")


def test_runs_whole_lists_for_item_0(tmp_path):
    code, output, ran = run_task(tmp_path, _create_list(), {"List1": 0})
    assert code == 0, output
    assert ran["1"]["inputs"] == _create_list()["1"]["inputs"]
    assert ran["9"]["inputs"]["filename_prefix"] == "fox"
    assert "Running the workflow with the workflow" in output


def test_create_list_keeps_the_tasks_item(tmp_path):
    code, output, ran = run_task(tmp_path, _create_list(), {"List1": 3})
    assert code == 0, output
    assert ran["1"]["inputs"] == {"inputs.input0": ["4", 0]}
    assert ran["9"]["inputs"]["filename_prefix"] == "fox_List1-3"
    assert "Running the workflow with List1 3" in output


def test_create_list_counts_inputs_numbered_with_ascii_digits_alone(tmp_path):
    """The panel counts them that way, so an input numbered with other digits isn't an item."""
    workflow = _create_list()
    workflow["1"]["inputs"]["inputs.input\u0660"] = ["hidden", 0]
    code, output, ran = run_task(tmp_path, workflow, {"List1": 3})
    assert code == 0, output
    assert ran["1"]["inputs"] == {"inputs.input0": ["4", 0], "inputs.input\u0660": ["hidden", 0]}


def test_lists_without_a_task_parameter_run_whole(tmp_path):
    # The submitter leaves out the task parameters of sources that are turned off.
    code, output, ran = run_task(tmp_path, _create_list(), {})
    assert code == 0, output
    assert len(ran["1"]["inputs"]) == 3


def test_fails_for_an_item_past_the_end(tmp_path):
    code, output, _ = run_task(tmp_path, _create_list(), {"List1": 4})
    assert code == 1
    assert "openjd_fail: Item 4 of List1 doesn't exist. A Create List has 3 items." in output


def test_fails_for_a_negative_item(tmp_path):
    """Python would otherwise count back from the end of the list."""
    code, output, ran = run_task(tmp_path, _create_list(), {"List1": -1})
    assert code == 1
    assert "openjd_fail: Item -1 of List1 doesn't exist. A Create List has 3 items." in output
    assert ran == {}


def test_fails_when_a_tasks_list_has_no_node(tmp_path):
    """Without the node's marker, every task would run the whole list."""
    workflow = _create_list()
    del workflow["1"]["_meta"]
    code, output, ran = run_task(tmp_path, workflow, {"List1": 2})
    assert code == 1
    assert "openjd_fail: The workflow has no node for List1" in output
    assert ran == {}


def _images(name="Cats"):
    meta: dict[str, Any] = {submit.FILES_META: ["cats/a.png", "cats/b.png"]}
    if name:
        meta[submit.ITEM_META] = name
    return {
        "3": {
            "class_type": "LoadImageDataSetFromFolder",
            "inputs": {"folder": "cats"},
            "_meta": meta,
        }
    }


def test_folders_load_the_tasks_image(tmp_path):
    _, _, ran = run_task(tmp_path, _images(), {"Cats": 1})
    assert ran["3"]["class_type"] == "LoadImage"
    assert ran["3"]["inputs"] == {"image": "cats/a.png"}


def test_folder_loaders_that_arent_split_load_the_whole_folder(tmp_path):
    _, _, ran = run_task(tmp_path, _images(), {"Cats": 0})
    assert ran["3"]["class_type"] == "LoadImageDataSetFromFolder"


def test_each_source_selects_its_own_item(tmp_path):
    workflow = {**_create_list(), **_images()}
    code, output, ran = run_task(tmp_path, workflow, {"List1": 2, "Cats": 1})
    assert code == 0, output
    assert ran["1"]["inputs"] == {"inputs.input0": ["3", 0]}
    assert ran["3"]["inputs"] == {"image": "cats/a.png"}
    assert ran["9"]["inputs"]["filename_prefix"] == "fox_List1-2_Cats-1"


def test_a_wired_prefix_gets_the_tasks_items_through_concatenate_text(tmp_path):
    """The task can't add its items to a prefix wired from another node, so ComfyUI adds them."""
    workflow = _create_list()
    workflow["9"]["inputs"]["filename_prefix"] = ["20", 0]
    workflow["20"] = {"class_type": "PrimitiveString", "inputs": {"value": "fox"}}
    code, output, prompt = run_task(tmp_path, workflow, {"List1": 2})
    assert code == 0, output
    assert prompt["9"]["inputs"]["filename_prefix"] == ["deadline_prefix_9", 0]
    assert prompt["deadline_prefix_9"] == {
        "class_type": "StringConcatenate",
        "inputs": {"string_a": ["20", 0], "string_b": "_List1-2", "delimiter": ""},
    }


def test_a_wired_prefix_needs_concatenate_text_on_the_worker(tmp_path):
    """A worker whose ComfyUI is older than Concatenate Text fails the task with a clear message."""
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    workflow = _create_list()
    workflow["9"]["inputs"]["filename_prefix"] = ["20", 0]
    workflow["20"] = {"class_type": "PrimitiveString", "inputs": {"value": "fox"}}
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        code, output, _ = run_task(tmp_path, workflow, {"List1": 2}, port=server.server_port)
    finally:
        server.shutdown()
    assert code == 1
    assert requests == ["/object_info/StringConcatenate"]
    assert "needs ComfyUI 0.3.35 or later on the worker" in output


def test_long_list_names_give_way_to_item_numbers_in_file_names(tmp_path):
    """Five long list names would make output file names too long for the file system."""
    names = [f"List{i}_" + "x" * 40 for i in range(5)]
    workflow = {"9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "fox"}}}
    for i, name in enumerate(names):
        workflow[str(i + 1)] = {
            "class_type": "CreateList",
            "inputs": {"inputs.input0": "a", "inputs.input1": "b"},
            "_meta": {submit.ITEM_META: name},
        }
    code, output, ran = run_task(tmp_path, workflow, {name: 2 for name in names})
    assert code == 0, output
    assert ran["9"]["inputs"]["filename_prefix"] == "fox_items-2-2-2-2-2"
    assert f"Output files end in _items-2-2-2-2-2, for {', '.join(names)}." in output
