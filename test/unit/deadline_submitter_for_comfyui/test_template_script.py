# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Runs the comfy-cli job template's task script with a stand-in `comfy` command, to check how each
task selects its work items. The script runs comfy-cli twice: once to print the workflow it would
run, and once to run the task's version of it, which the stand-in saves for the test to read. Each
task's items are filled in as create_job_bundle fills them in, from the task's parameters.
"""

import json
import os
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

from deadline.comfyui_submitter import submit

pytestmark = pytest.mark.skipif(os.name == "nt", reason="The stand-in comfy is a POSIX script")

TEMPLATE = yaml.safe_load(submit.JOB_TEMPLATE.read_text())


def run_task(tmp_path: Path, workflow: dict, items: dict[str, int]) -> tuple[int, str, dict]:
    """Runs the task script and returns its exit code, its output and the workflow it ran."""
    for name in ("bin", "out", "run"):
        (tmp_path / name).mkdir(exist_ok=True)
    workflow_file = tmp_path / "workflow_api.json"
    workflow_file.write_text(json.dumps(workflow))
    ran = tmp_path / "ran.json"
    stand_in = tmp_path / "comfy.py"
    stand_in.write_text(textwrap.dedent(f"""\
        import json, sys
        args = sys.argv
        workflow = json.load(open(args[args.index("--workflow") + 1]))
        if "--print-prompt" in args:
            print(json.dumps({{"type": "envelope", "ok": True, "data": {{"prompt": workflow}}}}))
        else:
            json.dump(workflow, open({str(ran)!r}, "w"))
            open({str(tmp_path / "out" / "image.png")!r}, "w").write("x")
            print(json.dumps({{"type": "envelope", "ok": True}}))
        """))
    # A shell wrapper rather than a shebang, which breaks when the interpreter path has spaces.
    comfy = tmp_path / "bin" / "comfy"
    comfy.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(stand_in))} "$@"\n'
    )
    comfy.chmod(0o755)
    sources = [submit.ItemSource(name, name, 1) for name in items]
    script = submit.add_work_items(TEMPLATE, sources)["steps"][0]["script"]["embeddedFiles"][0]
    for name, index in items.items():
        script["data"] = script["data"].replace(f"{{{{Task.Param.{name}}}}}", str(index))
    script = (
        script["data"]
        .replace("{{Param.OutputDir}}", str(tmp_path / "out"))
        .replace("{{Param.WorkflowFile}}", str(workflow_file))
    )
    (tmp_path / "run.py").write_text(script)
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "COMFYUI_PORT": "8188",
    }
    result = subprocess.run(
        [sys.executable, str(tmp_path / "run.py")],
        cwd=tmp_path / "run",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    workflow_ran = json.loads(ran.read_text()) if ran.exists() else {}
    ran.unlink(missing_ok=True)
    return result.returncode, result.stdout + result.stderr, workflow_ran


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


def test_lists_the_submitter_didnt_mark_run_whole(tmp_path):
    workflow = _create_list()
    del workflow["1"]["_meta"]
    _, _, ran = run_task(tmp_path, workflow, {"List1": 2})
    assert len(ran["1"]["inputs"]) == 3


def test_lists_without_a_task_parameter_run_whole(tmp_path):
    # The submitter leaves out the task parameters of sources that are turned off.
    code, output, ran = run_task(tmp_path, _create_list(), {})
    assert code == 0, output
    assert len(ran["1"]["inputs"]) == 3


def test_fails_for_an_item_past_the_end(tmp_path):
    code, output, _ = run_task(tmp_path, _create_list(), {"List1": 4})
    assert code == 1
    assert "openjd_fail: Item 4 of List1 doesn't exist. A Create List has 3 items." in output


def _string_values(values="a red fox\nlora_b.safetensors\n\nlast", name="Words"):
    meta = {submit.PREVIEW_META: "lora_b.safetensors"}
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
    assert "Item 4 of Words doesn't exist. Deadline String Values 5 has 3 items." in output


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


def _dynamic(text="a {red|white|black} fox", name="Prompt6"):
    return {
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": text, "clip": ["4", 1]},
            "_meta": {submit.DYNAMIC_PROMPTS_META: {"text": name}},
        }
    }


def test_dynamic_prompt_uses_the_tasks_choice(tmp_path):
    _, _, ran = run_task(tmp_path, _dynamic(), {"Prompt6": 2})
    assert ran["6"]["inputs"]["text"] == "a white fox"


def test_dynamic_prompts_that_arent_split_choose_the_same_each_run(tmp_path):
    chosen = {
        run_task(tmp_path, _dynamic(name=""), {"List1": 2})[2]["6"]["inputs"]["text"]
        for _ in range(2)
    }
    assert len(chosen) == 1
    assert chosen <= {"a red fox", "a white fox", "a black fox"}


def test_dynamic_prompts_expand_as_the_submitter_counts(tmp_path):
    # The task script has its own copy of expand_dynamic_prompt, which must stay in step.
    text = "{a|b /* c */} {x|{y|z}} \\{lit\\} // {p|q}"
    expected = submit.expand_dynamic_prompt(text)
    chosen = [
        run_task(tmp_path, _dynamic(text), {"Prompt6": i})[2]["6"]["inputs"]["text"]
        for i in (1, 2, 6)
    ]
    assert chosen == [expected[0], expected[1], expected[5]]


def test_each_source_selects_its_own_item(tmp_path):
    workflow = {**_create_list(), **_string_values(), **_dynamic(), **_integer_values()}
    workflow["9"]["inputs"]["filename_prefix"] = "fox"
    items = {"List1": 2, "Words": 1, "Prompt6": 3, "Seed": 42}
    code, output, ran = run_task(tmp_path, workflow, items)
    assert code == 0, output
    assert ran["1"]["inputs"] == {"inputs.input0": ["3", 0]}
    assert ran["7"]["inputs"]["lora_name"] == "a red fox"
    assert ran["6"]["inputs"]["text"] == "a black fox"
    assert ran["16"]["inputs"] == {"seed": 42, "steps": 20}
    assert ran["9"]["inputs"]["filename_prefix"] == "fox_List1-2_Words-1_Prompt6-3_Seed-42"
