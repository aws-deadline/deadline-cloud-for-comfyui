# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
A job bundle test that runs the whole submitter for real.

The test loads test_scripts/minimal_test's workflow into ComfyUI, changes values in the panel's
GUI, and saves the job bundle, then submits the same values. Nothing is stubbed in the browser:
the extension's /deadline routes run for real against the fake Deadline Cloud CLI and bridge that
the session fixture gives the server. A successful submission's bundle is removed once it's
uploaded, but a saved one is kept, to submit later with `deadline bundle submit`, so the bundle
to compare with expected_job_bundle is the saved one, which the same GUI values (the description,
especially) produce.

The workflow's PrimitiveString node carries "Deadline Cloud E2E" and its SaveText node writes
that to a text file, without loading any input files, so the bundle's asset references are empty.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from conftest import FARM_ID, QUEUE_ID, ComfyUI, open_panel
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.skipif(os.name == "nt", reason="The fake CLI is a POSIX shell script")

SCRIPTS = Path(__file__).resolve().parent
WORKFLOW = SCRIPTS / "minimal_test_case_workflow_ui.json"
EXPECTED = SCRIPTS / "expected_job_bundle"
JOB_TITLE = "Minimal Test Case"
DESCRIPTION = (
    "This is a small example workflow with a few of the default job template values changed."
)
INPUT_DIR = "path/to/ComfyUI/input"
OUTPUT_DIR = "path/to/ComfyUI/output"


def test_minimal_job_submission(page: Page, comfy: ComfyUI) -> None:
    # Load the workflow through the topbar's file input.
    page.set_input_files("#comfy-file-input", str(WORKFLOW))

    open_panel(page)
    expect(page.get_by_text("Logged in (default)")).to_be_visible()
    submit = page.get_by_role("button", name="Submit", exact=True)
    expect(submit).to_be_enabled()

    # GUI parameter values: the description will appear in the job template;
    # other parameter values are sent along with the job.
    panel = page.locator(".dl-panel")
    panel.get_by_text("Job settings", exact=True).click()
    panel.get_by_label("Name").fill(JOB_TITLE)
    panel.get_by_label("Description").fill(DESCRIPTION)
    panel.get_by_label("Priority").fill("50")
    panel.get_by_text("Workflow Parameters", exact=True).click()
    panel.get_by_label("Input Directory").fill(INPUT_DIR)
    panel.get_by_label("Output Directory").fill(OUTPUT_DIR)
    panel.get_by_text("Advanced Parameters", exact=True).click()
    panel.get_by_label("Workflow Timeout (seconds)").fill("7200")

    with page.expect_request(lambda request: request.url.endswith("/deadline/bundle")) as saved:
        panel.get_by_role("button", name="Save job bundle").click()
    expect(page.get_by_text("Saved the job bundle to")).to_be_visible()

    # The bundle the panel asked for: the values changed in the GUI, and the server's fills.
    saved_body = json.loads(saved.value.post_data or "{}")
    assert saved_body["description"] == DESCRIPTION
    assert saved_body["settings"]["priority"] == 50

    (bundle,) = list(comfy.submissions.iterdir())
    assert (bundle / "template.yaml").read_bytes() == (EXPECTED / "template.yaml").read_bytes()
    assert (bundle / "asset_references.yaml").read_bytes() == (
        EXPECTED / "asset_references.yaml"
    ).read_bytes()

    # Do not do a bytewise comparison of parameter values because the WorkflowFile parameter
    # depends on the path to this repository.
    expected_params = yaml.safe_load((bundle / "parameter_values.yaml").read_text(encoding="utf-8"))
    saved_params = yaml.safe_load((EXPECTED / "parameter_values.yaml").read_text(encoding="utf-8"))

    assert len(saved_params["parameterValues"]) == len(expected_params["parameterValues"])
    for item in expected_params["parameterValues"]:
        if item["name"] != "WorkflowFile":
            assert {"name": item["name"], "value": item["value"]} in saved_params["parameterValues"]

    # The bundle's workflow is the API-format version of the one loaded above.
    api_workflow = json.loads(next(bundle.glob("*_api.json")).read_text(encoding="utf-8"))
    assert api_workflow["72"]["class_type"] == "PrimitiveString"
    assert api_workflow["72"]["inputs"]["value"] == "Deadline Cloud E2E"
    assert api_workflow["73"]["class_type"] == "SaveText"
    assert api_workflow["73"]["inputs"]["text"] == ["72", 0]
    assert api_workflow["73"]["inputs"]["filename_prefix"] == "ComfyUI"
    assert api_workflow["73"]["inputs"]["format"] == "txt"

    # Then submit the same values, which uploads the bundle: the fake bridge answers job-1.
    with page.expect_request(lambda request: request.url.endswith("/deadline/jobs")) as info:
        submit.click()
    expect(page.get_by_text(f"Submitted {JOB_TITLE} as job-1.")).to_be_visible()
    job = json.loads(info.value.post_data or "{}")
    assert job["farmId"] == FARM_ID
    assert job["queueId"] == QUEUE_ID
