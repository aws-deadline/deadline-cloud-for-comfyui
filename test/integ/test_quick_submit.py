# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Submits a workflow from the quick submit popover with only its job name changed, and checks the
job bundle the server submitted: the repository's job template as it is, with parameter values
that make a valid job for the comfy conda package's workers.

The workflow's PrimitiveString node holds "Deadline Cloud E2E" and its SaveText node writes it to
a text file, without loading any input files or models.
"""

from __future__ import annotations

from bundle_checks import check_bundle, find_submission
from conftest import FARM_ID, QUEUE_ID, WORKFLOWS, ComfyUI, load_workflow, open_popover
from playwright.sync_api import Page, expect

JOB_NAME = "Quick submit test"


def test_quick_submit(page: Page, comfy: ComfyUI) -> None:
    load_workflow(page, WORKFLOWS / "minimal_test_case_workflow_ui.json")
    popover = open_popover(page)
    popover.get_by_label("Job name").fill(JOB_NAME)
    popover.get_by_role("button", name="Submit", exact=True).click()

    # The popover's one row names the job it created, until the next submit.
    row = popover.locator(".dl-pop-status [data-role='submitted']")
    expect(row).to_contain_text(f"Submitted {JOB_NAME} as job-")
    submission = find_submission(comfy.submitted, JOB_NAME)
    expect(row).to_have_text(f"Submitted {JOB_NAME} as {submission.job_id}")
    expect(row.get_by_role("button", name="Copy job ID")).to_be_visible()
    assert submission.request["farmId"] == FARM_ID
    assert submission.request["queueId"] == QUEUE_ID
    check_bundle(submission, comfy.base_directory)
