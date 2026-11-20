# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Runs the comfy-cli job template's task script with a stand-in `comfy` command. The script runs
comfy-cli twice: once to print the workflow in API format, and once to run it, which the stand-in
saves for the test to read. Also runs the part of the server environment's Enter script that
writes extra_model_paths.yaml.
"""

import http.server
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

from deadline.comfyui_submitter import submit

pytestmark = pytest.mark.skipif(os.name == "nt", reason="The stand-in comfy is a POSIX script")

TEMPLATE = yaml.safe_load(submit.JOB_TEMPLATE.read_text())


def environment_values(tmp_path: Path, **parameters: str) -> dict[str, str]:
    """Resolve the job environment's variables as OpenJD does, without editing script source."""
    values = {p["name"]: str(p.get("default", "")) for p in TEMPLATE["parameterDefinitions"]}
    values.update(parameters)
    [environment] = TEMPLATE["jobEnvironments"]
    result = {}
    for name, value in environment["variables"].items():
        value = value.replace("{{Session.WorkingDirectory}}", str(tmp_path))
        for parameter, parameter_value in values.items():
            value = value.replace("{{Param." + parameter + "}}", parameter_value)
        assert "{{" not in value
        result[name] = value
    return result


def run_task(
    tmp_path: Path,
    workflow: dict,
    *,
    mode: str = "",
    workflow_timeout: int = 3600,
    port: int = 8188,
) -> tuple[int, str, dict]:
    """
    Runs the task script and returns its exit code, its output and the workflow it ran. The
    stand-in comfy prints JSON with non-ASCII text unescaped, as comfy-cli does, and logs its
    arguments to calls.jsonl. mode changes what it does: "no-envelope" prints no envelope, and
    "hang" never finishes the run.
    """
    for name in ("bin", "out", "run"):
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
        else:
            json.dump(workflow, open({str(ran)!r}, "w", encoding="utf-8"), ensure_ascii=False)
            open({str(tmp_path / "out" / "image.png")!r}, "w").write("x")
            emit({{"type": "envelope", "ok": True, "data": {{"prompt": workflow}}}})
        """))
    # A shell wrapper rather than a shebang, which breaks when the interpreter path has spaces.
    comfy = tmp_path / "bin" / "comfy"
    comfy.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(stand_in))} "$@"\n'
    )
    comfy.chmod(0o755)
    script = TEMPLATE["steps"][0]["script"]["embeddedFiles"][0]["data"]
    (tmp_path / "run.py").write_text(script, encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "COMFYUI_PORT": str(port),
        "PYTHONIOENCODING": "utf-8",
        **environment_values(
            tmp_path,
            OutputDir=str(tmp_path / "out"),
            WorkflowFile=str(workflow_file),
            WorkflowTimeout=str(workflow_timeout),
        ),
    }
    result = subprocess.run(
        [sys.executable, str(tmp_path / "run.py")],
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


def enter_script(end: str | None = None) -> str:
    """The server environment's Enter script, or its prefix, without source substitutions."""
    [environment] = TEMPLATE["jobEnvironments"]
    [enter] = [f for f in environment["script"]["embeddedFiles"] if f["name"] == "Enter"]
    data = enter["data"]
    return data if end is None else data[: data.index(end)]


def write_model_paths(
    tmp_path: Path, model_directory: Path | None = None, conda_nodes: Path | None = None
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
            **environment_values(tmp_path, ModelDirectory=str(model_directory or "")),
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


def run_enter(tmp_path: Path, **parameters: str) -> tuple[subprocess.CompletedProcess, list[str]]:
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
            print(json.dumps({{"data": {{"lines": []}}}}))
        """))
    comfy = bin_dir / "comfy"
    comfy.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(stand_in))} "$@"\n'
    )
    comfy.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text('#!/bin/sh\nprintf "{}\\n"\n')
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
    assert args[-8:] == [
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
