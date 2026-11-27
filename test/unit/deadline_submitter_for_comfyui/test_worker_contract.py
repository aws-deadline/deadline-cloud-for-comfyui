# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
What the job template needs from the comfy conda package, which provides ComfyUI on the workers.

The package puts comfy-cli 1.22 or later on the PATH as `comfy`, and sets COMFYUI_LOCATION to a
ComfyUI checkout. It's built for x86_64 Linux only, and it has no ComfyUI-Manager. So a job starts
ComfyUI with `comfy launch --background`, always passes it --base-directory, which needs a
custom_nodes/ folder, and runs the workflow with `comfy run --where local`. test_template_script.py
checks the rest of the launch: its listen address, port and directories, the arguments it refuses,
the stop on exit and the run's timeout.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Sequence

import pytest

from .test_template_script import TEMPLATE, comfy_calls, run_enter, run_task

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="The stand-in comfy is a POSIX script")

# The comfy-cli commands that work with the comfy package, and why some others don't.
ALLOWED_COMMANDS = ("launch", "run", "logs", "stop")
REFUSED_COMMANDS = {
    "install": "it installs another ComfyUI, when the package's is at $COMFYUI_LOCATION",
    "update": "it changes the package's ComfyUI checkout at $COMFYUI_LOCATION",
    "node": "it manages custom nodes with ComfyUI-Manager, which the package doesn't have",
}
# comfy-cli's options before the command that take a value.
OPTIONS_WITH_VALUES = ("--workspace",)


def comfy_command(args: Sequence[str]) -> str | None:
    """comfy-cli's command in its arguments, such as launch."""
    words = iter(args)
    for word in words:
        if word in OPTIONS_WITH_VALUES:
            next(words, None)
        elif not word.startswith("-"):
            return word
    return None


def assert_supported_commands(calls: list[list[str]]) -> None:
    """
    Fails on a comfy command that the comfy package doesn't support. The package provides one
    ComfyUI, without ComfyUI-Manager, so a job that installs or updates ComfyUI or custom nodes
    with comfy-cli fails on the worker, or changes the package's ComfyUI for the jobs after it.
    """
    refused = []
    for args in calls:
        command = comfy_command(args)
        if command not in ALLOWED_COMMANDS:
            why = REFUSED_COMMANDS.get(
                command or "", f"jobs run only {', '.join(ALLOWED_COMMANDS)}"
            )
            refused.append(
                f"`comfy {shlex.join(args)}` runs comfy {command}, which jobs can't: {why}."
            )
    assert not refused, "\n".join(refused)


def test_conda_packages_installs_the_comfy_package():
    """
    The queue environment installs the CondaPackages packages on the worker. Without comfy, the
    job has no ComfyUI or comfy-cli to run.
    """
    parameters = {p["name"]: p for p in TEMPLATE["parameterDefinitions"]}
    assert "CondaPackages" in parameters, "The job template has no CondaPackages parameter."
    parameter = parameters["CondaPackages"]
    assert parameter["type"] == "STRING", f"CondaPackages isn't a STRING parameter: {parameter}"
    assert (
        parameter.get("default") == "comfy"
    ), f"CondaPackages doesn't default to comfy: {parameter}"


def test_no_step_has_host_requirements():
    """
    Customers choose their workers in the panel's Host requirements, which start from the
    template's. The template sets none, so a job can run on any fleet, including a
    customer-managed one, unless the artist asks for more.
    """
    for step in TEMPLATE["steps"]:
        assert (
            "hostRequirements" not in step
        ), f"Step {step['name']} has host requirements: {step['hostRequirements']}"


@POSIX_ONLY
def test_the_launch_starts_the_packages_comfyui_in_the_background(tmp_path):
    """
    comfy launch has to start the package's ComfyUI, at $COMFYUI_LOCATION, without prompting, and
    return once it runs in the background; and ComfyUI doesn't start on a --base-directory without
    a custom_nodes/ folder. Otherwise the job's ComfyUI server never starts on the worker.
    """
    result, args = run_enter(tmp_path, OutputDir=str(tmp_path / "out"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert args, "The Enter script ran no comfy launch."
    cli = args[: args.index("--")] if "--" in args else args
    assert comfy_command(cli) == "launch", cli
    before = cli[: cli.index("launch")]
    assert "--skip-prompt" in before, f"comfy can wait for an answer on the worker: {cli}"
    assert "--workspace" in before, f"comfy launch doesn't name the package's ComfyUI: {cli}"
    assert before[before.index("--workspace") + 1] == str(tmp_path / "ComfyUI"), cli
    assert "--background" in cli, f"comfy launch doesn't return while ComfyUI runs: {cli}"
    # The session's base directory, which test_template_script.py checks the launch passes.
    assert (
        tmp_path / "comfyui" / "custom_nodes"
    ).is_dir(), "The base directory has no custom_nodes/."
    # Every comfy command the Enter script ran, which the stand-in logs.
    calls = (tmp_path / "launch_calls.jsonl").read_text(encoding="utf-8").splitlines()
    assert_supported_commands([json.loads(line) for line in calls])


@POSIX_ONLY
def test_the_run_uses_the_comfyui_the_job_launched(tmp_path):
    """
    Without --where local, comfy run can send the workflow somewhere other than the ComfyUI that
    the job launched on the worker.
    """
    workflow = {"9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "fox"}}}
    code, output, _ = run_task(tmp_path, workflow)
    assert code == 0, output
    calls = comfy_calls(tmp_path)
    assert_supported_commands(calls)
    runs = [args for args in calls if comfy_command(args) == "run"]
    assert runs, "The task script ran no comfy run."
    for args in runs:
        assert "--where" in args and args[args.index("--where") + 1] == "local", args
