# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Checks of a job bundle that the panel submitted, against what Deadline Cloud and its workers need.

The fake bridge in conftest.py copies each bundle before the server deletes it, with the request
the server sent it: the job parameter values and settings that `deadline` submits with the bundle.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from conftest import QUEUE_PARAMETERS
from openjd.model import create_job, decode_job_template, preprocess_job_parameters

from deadline.comfyui_submitter import submit


@dataclass
class Submission:
    """A submitted job: the copy of its bundle, and the request the server sent the bridge."""

    job_id: str
    bundle: Path
    request: dict[str, Any]

    @property
    def parameters(self) -> dict[str, str]:
        return self.request["parameters"]

    def local(self, path: str) -> Path:
        """Where a path inside the submitted bundle is in the copy. Other paths stay as they are."""
        try:
            return self.bundle / Path(path).relative_to(self.request["bundleDir"])
        except ValueError:
            return Path(path)


def find_submission(submitted: Path, job_name: str) -> Submission:
    """The one job that was submitted with this name."""
    found = []
    for directory in sorted(submitted.iterdir()):
        request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
        if request["settings"].get("name") == job_name:
            found.append(Submission(directory.name, directory / "bundle", request))
    assert len(found) == 1, f"Expected one job named {job_name!r}, found {len(found)}."
    return found[0]


def _yaml(value: Any) -> list[str]:
    return yaml.safe_dump(value, sort_keys=True, width=100).splitlines()


def check_template(
    submission: Submission,
    description: str | None = None,
    host_requirements: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Checks that the bundle's job template is the repository's, with only the description and
    each step's host requirements set, when they're given. The repository's has no host
    requirements, so a job whose form wasn't edited runs on any worker. Returns the template.
    """
    expected = yaml.safe_load(submit.JOB_TEMPLATE.read_text(encoding="utf-8"))
    if description is not None:
        expected["description"] = description
    if host_requirements is not None:
        for step in expected["steps"]:
            step["hostRequirements"] = host_requirements
    template = yaml.safe_load((submission.bundle / "template.yaml").read_text(encoding="utf-8"))
    if template != expected:
        diff = difflib.unified_diff(
            _yaml(expected), _yaml(template), "expected template", "bundle template", lineterm=""
        )
        raise AssertionError(
            "The bundle's job template isn't the expected one:\n" + "\n".join(diff)
        )
    return template


def check_parameters(
    submission: Submission, template: dict[str, Any], expected: dict[str, str]
) -> None:
    """
    Creates the job from the template, with the fake queue's environment parameters, and the
    submitted parameter values with openjd-model, as Deadline Cloud does, so that a value of the
    wrong type, out of range, missing or in neither fails here as it would fail CreateJob. Then
    checks the expected values.
    """
    values = submission.parameters
    definitions = [*template["parameterDefinitions"], *QUEUE_PARAMETERS]
    job_template = decode_job_template(
        template={**template, "parameterDefinitions": definitions}, supported_extensions=["EXPR"]
    )
    job_values = preprocess_job_parameters(
        job_template=job_template,
        job_parameter_values=values,
        job_template_dir=submission.bundle,
        current_working_dir=submission.bundle,
    )
    create_job(job_template=job_template, job_parameter_values=job_values)
    # The queue environment installs the comfy conda package, which provides ComfyUI and
    # comfy-cli on the worker, from CondaPackages, and from the channels in CondaChannels: the
    # queue's, with deadline-cloud-v2 before deadline-cloud, as the other submitters send them.
    expected = {
        "CondaPackages": "comfy",
        "CondaChannels": "deadline-cloud-v2 deadline-cloud",
        **expected,
    }
    wrong = {name: values.get(name) for name in expected if values.get(name) != expected[name]}
    assert not wrong, f"Job parameter values {wrong} aren't the expected {expected}."


def check_workflow(submission: Submission) -> None:
    """
    Checks that WorkflowFile is inside the bundle, so it's uploaded with the job, and that it's
    the API-format version of workflows/minimal_test_case_workflow_ui.json: its PrimitiveString
    node holds "Deadline Cloud E2E" and its SaveText node writes it to a text file.
    """
    workflow_file = submission.parameters["WorkflowFile"]
    assert Path(workflow_file).is_relative_to(submission.request["bundleDir"]), workflow_file
    workflow = json.loads(submission.local(workflow_file).read_text(encoding="utf-8"))
    assert workflow["72"]["class_type"] == "PrimitiveString"
    assert workflow["72"]["inputs"]["value"] == "Deadline Cloud E2E"
    assert workflow["73"]["class_type"] == "SaveText"
    assert workflow["73"]["inputs"]["text"] == ["72", 0]
    assert workflow["73"]["inputs"]["filename_prefix"] == "ComfyUI"
    assert workflow["73"]["inputs"]["format"] == "txt"


def check_asset_references(submission: Submission) -> None:
    """
    Checks that the bundle attaches nothing besides what its PATH parameters name. The workflow
    loads no input files or models, and the template's WorkflowFile and OutputDir parameters
    cover the workflow file and the job's outputs, so anything listed, such as the whole input
    directory, would be uploaded for nothing.
    """
    path = submission.bundle / "asset_references.yaml"
    references = yaml.safe_load(path.read_text(encoding="utf-8"))
    empty = {
        "inputs": {"filenames": [], "directories": []},
        "outputs": {"directories": []},
        "referencedPaths": [],
    }
    assert references == {"assetReferences": empty}, references


def check_bundle(
    submission: Submission,
    base_directory: Path,
    description: str | None = None,
    host_requirements: dict[str, Any] | None = None,
    parameters: dict[str, str] | None = None,
) -> None:
    """
    Every check of a submitted bundle, for a ComfyUI started with this --base-directory. The
    template's replaced fields and the parameter values are the ones the test changed.
    """
    template = check_template(submission, description, host_requirements)
    expected = {
        "InputDir": str(base_directory / "input"),
        "OutputDir": str(base_directory / "output"),
        **(parameters or {}),
    }
    check_parameters(submission, template, expected)
    check_workflow(submission)
    check_asset_references(submission)
