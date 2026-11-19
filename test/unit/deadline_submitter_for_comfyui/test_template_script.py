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


def run_task(
    tmp_path: Path,
    workflow: dict,
    items: dict[str, int] | None = None,
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
    items = items or {}
    sources = [submit.ItemSource(name, name, 1) for name in items]
    script = submit.add_task_lists(TEMPLATE, sources)["steps"][0]["script"]["embeddedFiles"][0]
    for name, index in items.items():
        script["data"] = script["data"].replace(f"{{{{Task.Param.{name}}}}}", str(index))
    script = (
        script["data"]
        .replace("{{Param.OutputDir}}", str(tmp_path / "out"))
        .replace("{{Param.WorkflowFile}}", str(workflow_file))
        .replace("{{Param.WorkflowTimeout}}", str(workflow_timeout))
    )
    (tmp_path / "run.py").write_text(script, encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "COMFYUI_PORT": str(port),
        "PYTHONIOENCODING": "utf-8",
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


def enter_script(
    tmp_path: Path,
    end: str,
    model_directory: Path | None = None,
    extra: str = "",
    custom_nodes_directory: Path | None = None,
    attach_nodes: bool = True,
) -> str:
    """The start of the server environment's Enter script, with the job's values filled in."""
    [environment] = TEMPLATE["jobEnvironments"]
    [enter] = [f for f in environment["script"]["embeddedFiles"] if f["name"] == "Enter"]
    data = enter["data"]
    return (
        data[data.index("model_folders() {") : data.index(end)]
        .replace("{{Session.WorkingDirectory}}", str(tmp_path))
        .replace("{{Param.ModelDirectory}}", str(model_directory or ""))
        .replace("{{Param.ExtraModelPathsConfig}}", extra)
        .replace("{{Param.CustomNodesDirectory}}", str(custom_nodes_directory or ""))
        .replace("{{Param.AttachCustomNodes}}", "true" if attach_nodes else "false")
    )


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
    script = enter_script(
        tmp_path,
        "# ComfyUI can't read an empty",
        model_directory,
        custom_nodes_directory=custom_nodes_directory,
        attach_nodes=attach_nodes,
    )
    result = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + script],
        env={
            "PATH": os.environ["PATH"],
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


def model_path_args(
    tmp_path: Path,
    extra: str = "",
    conda_nodes: Path | None = None,
    model_directory: Path | None = None,
) -> list[str]:
    """The extra model paths arguments the Enter script passes to ComfyUI."""
    script = enter_script(tmp_path, "# Keep ComfyUI's data out", model_directory, extra)
    result = subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail\n"
            + script
            + '\necho ARGS; printf "%s\\n" ${MODEL_PATH_ARGS[@]+"${MODEL_PATH_ARGS[@]}"}',
        ],
        env={
            "PATH": os.environ["PATH"],
            **({"COMFYUI_CUSTOM_NODES_DIR": str(conda_nodes)} if conda_nodes else {}),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.split("ARGS\n", 1)[1].split()


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


def test_custom_node_requirements_are_constrained_to_the_conda_packages(tmp_path):
    """Packages conda installed, such as torch, are pinned too, so a pack can't replace them."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "pip.log"
    # A stand-in python: "-m venv DIR" makes DIR/bin/python, which lists packages as pip list
    # does and records each pip command with the constraints it had.
    venv_python = (
        "#!/bin/sh\n"
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
        "fi\n"
    )
    python.chmod(0o755)
    node = tmp_path / "pack"
    node.mkdir()
    (node / "requirements.txt").write_text("torch>=99\n")
    [environment] = TEMPLATE["jobEnvironments"]
    [enter] = [f for f in environment["script"]["embeddedFiles"] if f["name"] == "Enter"]
    data = enter["data"]
    block = data[data.index("# The attached packs' pip") : data.index("PORT=$(python")]
    script = block.replace("{{Session.WorkingDirectory}}", str(tmp_path))
    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\nATTACHED_NODES=({shlex.quote(str(node))})\n{script}"],
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    constraints = tmp_path / "constraints.txt"
    assert constraints.read_text() == "torch==2.5.1\nnumpy==2.1.0\n"
    assert log.read_text().splitlines() == [
        f"-m pip install -r {node / 'requirements.txt'} constraint={constraints}",
        f"-m pip check constraint={constraints}",
    ]


def _create_list():
    return {
        "1": {
            "class_type": "CreateList",
            "inputs": {f"inputs.input{i}": [str(i + 2), 0] for i in range(3)},
            "_meta": {submit.ITEM_META: "List1"},
        },
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "fox"}},
    }


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


def _string_values(values="a red fox\nlora_b.safetensors\n\nlast", name="Words"):
    # The submitter lists the lines, as plan_task_lists does.
    meta = {
        submit.PREVIEW_META: "lora_b.safetensors",
        submit.VALUES_META: submit.string_values(values),
    }
    if name:
        meta[submit.ITEM_META] = name
    return {
        "5": {"class_type": submit.STRING_NODE, "inputs": {"values": values}, "_meta": meta},
        "7": {"class_type": "LoraLoader", "inputs": {"lora_name": ["5", 0], "model": ["4", 0]}},
    }


@pytest.mark.parametrize(
    ("items", "value"), [({"Words": 0}, "lora_b.safetensors"), ({"Words": 3}, "last")]
)
def test_string_values_are_replaced_by_the_tasks_value(tmp_path, items, value):
    code, output, ran = run_task(tmp_path, _string_values(), items)
    assert code == 0, output
    assert "5" not in ran
    assert ran["7"]["inputs"] == {"lora_name": value, "model": ["4", 0]}


def test_string_values_that_arent_split_output_the_preview(tmp_path):
    code, output, ran = run_task(tmp_path, _string_values(name=None), {})
    assert code == 0, output
    assert ran["7"]["inputs"]["lora_name"] == "lora_b.safetensors"


def test_string_values_past_the_end(tmp_path):
    code, output, _ = run_task(tmp_path, _string_values(), {"Words": 4})
    assert code == 1
    assert "Item 4 of Words doesn't exist. Deadline String Values #5 has 3 items." in output


def _integer_values(name="Seed"):
    meta = {submit.PREVIEW_META: 7}
    if name:
        meta[submit.ITEM_META] = name
    return {
        "15": {"class_type": submit.INTEGER_NODE, "inputs": {"values": "7, 0-2"}, "_meta": meta},
        "16": {"class_type": "KSampler", "inputs": {"seed": ["15", 0], "steps": 20}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "fox"}},
    }


def test_integer_values_are_replaced_by_the_tasks_value(tmp_path):
    code, output, ran = run_task(tmp_path, _integer_values(), {"Seed": 0})
    assert code == 0, output
    assert ran["16"]["inputs"] == {"seed": 0, "steps": 20}
    # A value of 0 still goes in the file names.
    assert ran["9"]["inputs"]["filename_prefix"] == "fox_Seed-0"


def test_integer_values_that_arent_split_output_the_preview(tmp_path):
    _, _, ran = run_task(tmp_path, _integer_values(name=None), {})
    assert ran["16"]["inputs"]["seed"] == 7
    assert ran["9"]["inputs"]["filename_prefix"] == "fox"


def test_value_nodes_need_the_submitter(tmp_path):
    workflow = _integer_values()
    del workflow["15"]["_meta"]
    code, output, _ = run_task(tmp_path, workflow, {})
    assert code == 1
    assert "DeadlineIntegerValues node that the ComfyUI Submitter didn't prepare" in output


def _images(class_type, name="Cats"):
    meta: dict[str, Any] = {submit.FILES_META: ["cats/a.png", "cats/b.png"]}
    if class_type == submit.IMAGE_NODE:
        meta[submit.PREVIEW_META] = "cats/b.png"
    if name:
        meta[submit.ITEM_META] = name
    return {"3": {"class_type": class_type, "inputs": {"folder": "cats"}, "_meta": meta}}


@pytest.mark.parametrize("class_type", [submit.IMAGE_NODE, "LoadImageDataSetFromFolder"])
def test_folders_load_the_tasks_image(tmp_path, class_type):
    _, _, ran = run_task(tmp_path, _images(class_type), {"Cats": 1})
    assert ran["3"]["class_type"] == "LoadImage"
    assert ran["3"]["inputs"] == {"image": "cats/a.png"}


def test_image_values_that_arent_split_load_the_preview(tmp_path):
    for items in ({"Cats": 0}, {}):
        _, _, ran = run_task(tmp_path, _images(submit.IMAGE_NODE, name=None), items)
        assert ran["3"]["inputs"] == {"image": "cats/b.png"}


def test_folder_loaders_that_arent_split_load_the_whole_folder(tmp_path):
    _, _, ran = run_task(tmp_path, _images("LoadImageDataSetFromFolder"), {"Cats": 0})
    assert ran["3"]["class_type"] == "LoadImageDataSetFromFolder"


def test_each_source_selects_its_own_item(tmp_path):
    workflow = {**_create_list(), **_string_values(), **_integer_values()}
    workflow["9"]["inputs"]["filename_prefix"] = "fox"
    items = {"List1": 2, "Words": 1, "Seed": 42}
    code, output, ran = run_task(tmp_path, workflow, items)
    assert code == 0, output
    assert ran["1"]["inputs"] == {"inputs.input0": ["3", 0]}
    assert ran["7"]["inputs"]["lora_name"] == "a red fox"
    assert ran["16"]["inputs"] == {"seed": 42, "steps": 20}
    assert ran["9"]["inputs"]["filename_prefix"] == "fox_List1-2_Words-1_Seed-42"


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


def test_string_values_run_the_lines_the_submitter_counted(tmp_path):
    """The task script takes the submitter's lines, so a U+2028 inside a line stays in it."""
    values = "a red\u2028fox\r\nlora_b.safetensors\n"
    workflow = {
        "5": {"class_type": submit.STRING_NODE, "inputs": {"values": values}},
        "7": {"class_type": "LoraLoader", "inputs": {"lora_name": ["5", 0], "model": ["4", 0]}},
    }
    plan = submit.plan_task_lists(workflow)
    [source] = plan.active
    assert source.count == 2
    for item, line in ((1, "a red\u2028fox"), (2, "lora_b.safetensors")):
        task_dir = tmp_path / str(item)
        task_dir.mkdir()
        code, output, ran = run_task(task_dir, plan.workflow, {source.name: item})
        assert code == 0, output
        assert ran["7"]["inputs"]["lora_name"] == line


def test_string_values_split_at_separator_lines_keep_their_paragraphs(tmp_path):
    values = "a red fox\n---\na cat at dawn.\n\nSoft light.\n"
    workflow = {
        "5": {
            "class_type": submit.STRING_NODE,
            "inputs": {"values": values, "split_by": submit.SPLIT_BY_SEPARATOR},
        },
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": ["5", 0]}},
    }
    plan = submit.plan_task_lists(workflow)
    [source] = plan.active
    code, output, ran = run_task(tmp_path, plan.workflow, {source.name: 2})
    assert code == 0, output
    assert ran["7"]["inputs"]["text"] == "a cat at dawn.\n\nSoft light."


def test_paired_integer_values_select_their_value_by_item_number(tmp_path):
    meta = {
        submit.PREVIEW_META: 1024,
        submit.ITEM_META: "Width",
        submit.VALUES_META: [1024, 1344, 768],
    }
    workflow = {
        "15": {
            "class_type": submit.INTEGER_NODE,
            "inputs": {"values": "1024,1344,768"},
            "_meta": meta,
        },
        "16": {"class_type": "EmptyLatentImage", "inputs": {"width": ["15", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "fox"}},
    }
    code, output, ran = run_task(tmp_path, workflow, {"Width": 2})
    assert code == 0, output
    assert ran["16"]["inputs"]["width"] == 1344
    assert ran["9"]["inputs"]["filename_prefix"] == "fox_Width-2"
    code, output, _ = run_task(tmp_path, workflow, {"Width": 4})
    assert code == 1
    assert "Item 4 of Width doesn't exist" in output
