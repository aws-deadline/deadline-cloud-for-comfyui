# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Submits a workflow from the panel with a job name, description, Job Properties' settings, Output
Directory, Workflow Timeout and host requirement changed, and checks the job bundle and settings
the server submitted. Then checks the settings the panel saved in the workflow, and that opening the
workflow again in a new browser shows them.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from bundle_checks import check_bundle, find_submission
from conftest import (
    FARM_ID,
    QUEUE_ID,
    WORKFLOWS,
    ComfyUI,
    load_workflow,
    open_comfyui,
    open_panel,
)
from playwright.sync_api import BrowserContext, Locator, Page, expect

JOB_NAME = "Panel submit test"
DESCRIPTION = "A minimal workflow, with a few of the job template's values changed."
# Job Properties' number settings to set, by the names the panel sends and saves them by, with
# their labels.
SETTINGS = {
    "priority": ("Priority", 75),
    "maxRetriesPerTask": ("Maximum retries per task", 2),
    "maxFailedTasksCount": ("Maximum failed tasks count", 5),
    "maxWorkerCount": ("Maximum worker count", 3),
}
SETTING_VALUES = {key: value for key, (_, value) in SETTINGS.items()}
# Job Properties' fields, in order, with the template's Output Directory last.
PROPERTIES = [
    "Description",
    "Priority",
    "Initial state",
    "Maximum retries per task",
    "Maximum failed tasks count",
    "Maximum worker count",
    "Output Directory",
]
WORKFLOW_TIMEOUT = "7200"
# The GPU memory (GiB) minimum to set. The job asks for it in MiB.
GPU_MEMORY = 16
# The job template has none, so the job's are the GPU memory the test sets.
HOST_REQUIREMENTS = {"amounts": [{"name": "amount.worker.gpu.memory", "min": GPU_MEMORY * 1024}]}
# The panel's drawers, in order, and the ones inside Advanced settings, where the plan adds Timeouts
# and Host requirements. Our job template's parameters have no Job parameters drawer.
DRAWERS = ["Job Properties", "Job submission settings", "Advanced settings"]
INNER_DRAWERS = ["Attachments", "Timeouts", "Host requirements"]


def open_drawers(panel: Locator) -> None:
    """Opens the panel's drawers, once the plan has added Timeouts and Host requirements."""
    titles = "details.dl-section > summary .dl-grow"
    expect(panel.locator(titles)).to_have_text([*DRAWERS, *INNER_DRAWERS])
    expect(panel.locator(f"details.dl-section {titles}")).to_have_text(INNER_DRAWERS)
    panel.locator("details.dl-section").evaluate_all(
        "drawers => drawers.forEach((drawer) => { drawer.open = true; })"
    )


def output_directory(panel: Locator) -> Locator:
    # Not by its label, which its Browse… button's label also has.
    return panel.locator("input[name='parameter:OutputDir']")


def test_panel_submit(
    page: Page,
    new_context: Callable[..., BrowserContext],
    comfy: ComfyUI,
    tmp_path: Path,
) -> None:
    output_dir = str(tmp_path / "renders")
    load_workflow(page, WORKFLOWS / "minimal_test_case_workflow_ui.json")
    panel = open_panel(page)
    open_drawers(panel)
    expect(panel.locator(".dl-slot-properties .dl-label")).to_have_text(PROPERTIES)
    panel.get_by_label("Job name").fill(JOB_NAME)
    panel.get_by_label("Description", exact=True).fill(DESCRIPTION)
    for label, value in SETTINGS.values():
        panel.get_by_label(label, exact=True).fill(str(value))
    output_directory(panel).fill(output_dir)
    panel.get_by_label("Workflow Timeout (seconds)").fill(WORKFLOW_TIMEOUT)
    panel.get_by_label("GPU memory (GiB) minimum").fill(str(GPU_MEMORY))
    panel.get_by_role("button", name="Submit to Deadline Cloud").click()

    # The notification once it's submitted, with the job's ID. ComfyUI also reads it out from a
    # hidden live region, which has the same text.
    submitted = page.get_by_test_id("toast").get_by_text(f"Submitted {JOB_NAME} as job-")
    expect(submitted).to_be_visible()
    submission = find_submission(comfy.submitted, JOB_NAME)
    expect(submitted).to_contain_text(submission.job_id)
    assert submission.request["farmId"] == FARM_ID
    assert submission.request["queueId"] == QUEUE_ID
    # The server always has the library check that the job's input paths exist.
    assert submission.request["settings"] == {
        "name": JOB_NAME,
        "targetTaskRunStatus": "READY",
        "requirePathsExist": True,
        **SETTING_VALUES,
    }
    check_bundle(
        submission,
        comfy.base_directory,
        description=DESCRIPTION,
        host_requirements=HOST_REQUIREMENTS,
        parameters={"OutputDir": output_dir, "WorkflowTimeout": WORKFLOW_TIMEOUT},
    )

    # The settings the panel saved in the workflow, which a shared workflow takes along: no paths
    # but the Output Directory the artist chose, and no farm, queue or storage profile, queue
    # environment values or attachments.
    assert page.evaluate("() => window.app.graph.extra.deadlineCloud") == {
        "version": 1,
        "jobName": JOB_NAME,
        "description": DESCRIPTION,
        "targetTaskRunStatus": "READY",
        **SETTING_VALUES,
        "parameters": {"OutputDir": output_dir, "WorkflowTimeout": WORKFLOW_TIMEOUT},
        "hostRequirements": {"attributes": [], **HOST_REQUIREMENTS},
    }

    # Opening the saved workflow in a new browser, without this one's localStorage, shows them.
    workflow = tmp_path / "saved_workflow.json"
    workflow.write_text(
        page.evaluate("() => JSON.stringify(window.app.graph.serialize())"), encoding="utf-8"
    )
    reopened = new_context().new_page()
    open_comfyui(reopened, comfy)
    load_workflow(reopened, workflow)
    panel = open_panel(reopened)
    # A saved Output Directory other than ComfyUI's output folder opens Job Properties by itself.
    expect(panel.locator(".dl-slot-properties > details")).to_have_js_property("open", True)
    open_drawers(panel)
    expect(panel.get_by_label("Workflow Timeout (seconds)")).to_have_value(WORKFLOW_TIMEOUT)
    expect(output_directory(panel)).to_have_value(output_dir)
    expect(panel.get_by_label("Job name")).to_have_value(JOB_NAME)
    expect(panel.get_by_label("Description", exact=True)).to_have_value(DESCRIPTION)
    for label, value in SETTINGS.values():
        expect(panel.get_by_label(label, exact=True)).to_have_value(str(value))
    expect(panel.get_by_label("GPU memory (GiB) minimum")).to_have_value(str(GPU_MEMORY))
