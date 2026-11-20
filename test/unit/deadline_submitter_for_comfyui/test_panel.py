# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Compiles the panel's TypeScript and runs deadline_ui.js in Node with ComfyUI's app and api modules
stubbed out, to check how submitting orders itself against planning and the server's events. It
needs the build tools that `npm ci` in frontend/ installs, and is skipped without them.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
FRONTEND = Path(__file__).resolve().parents[3] / "frontend"
TSC = FRONTEND / "node_modules" / ".bin" / ("tsc.cmd" if os.name == "nt" else "tsc")
pytestmark = pytest.mark.skipif(
    NODE is None or not TSC.exists(), reason="Needs Node.js, and npm ci run in frontend/"
)

PANEL = Path(__file__).parent / "panel"


@pytest.fixture(scope="module")
def ui_source(tmp_path_factory) -> str:
    """The compiled deadline_ui.js. It's built into a temporary folder, not the extension's."""
    out = tmp_path_factory.mktemp("panel-build")
    result = subprocess.run(
        [str(TSC), "-p", str(FRONTEND), "--outDir", str(out)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return (out / "deadline_ui.js").read_text(encoding="utf-8")


@pytest.fixture
def run_scenario(tmp_path, ui_source):
    return lambda scenario: _run_scenario(tmp_path, ui_source, scenario)


def _run_scenario(tmp_path: Path, ui_source: str, scenario: str) -> dict:
    # deadline_ui.js imports ../../scripts/app.js and ../../scripts/api.js.
    (tmp_path / "scripts").mkdir()
    for name in ("app.js", "api.js"):
        shutil.copy(PANEL / name, tmp_path / "scripts" / name)
    shutil.copy(PANEL / "harness.mjs", tmp_path / "harness.mjs")
    web = tmp_path / "extension" / "web"
    web.mkdir(parents=True)
    # The harness reads the panel's state, and what a queue field shows, through these exports,
    # which only the copy has. Exporting the section helpers also makes the module fail to load,
    # and every scenario fail, if one of them is missing.
    exports = (
        "\nexport { state as __state, queueParameterValue as __queueParameterValue,"
        " parameterControl as __parameterControl, RENDERERS as __renderers,"
        " pollJobs as __pollJobs, formFromRequirements as __formFromRequirements,"
        " requirementsFromForm as __requirementsFromForm,"
        " createView as __createView,"
        " answerConfirmation as __answerConfirmation };\n"
    )
    (web / "deadline_ui.js").write_text(ui_source + exports, encoding="utf-8")
    (tmp_path / "package.json").write_text('{"type": "module"}')
    completed = subprocess.run(
        [str(NODE), "--no-warnings", str(tmp_path / "harness.mjs"), scenario],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


NEW = {"image": "new.png", "attachments": ["/input/new.png"]}


def test_submitting_an_edit_before_replanning_waits_for_the_new_plan(run_scenario):
    result = run_scenario("edited-before-replanning")
    assert result["jobsAfterEdit"] == []
    assert result["replanned"]
    # Only a newer plan from the same tab replaces a waiting one.
    assert result["planClientIds"] == ["tab-1", "tab-1"]
    [job] = result["jobs"]
    assert {key: job[key] for key in NEW} == NEW
    # The server sends the submission's events to this tab only.
    assert job["clientId"] == "tab-1"


def test_submitting_while_planning_waits_for_the_plan(run_scenario):
    result = run_scenario("edited-while-planning")
    assert result["canSubmitWhilePlanning"] is False
    assert result["jobsWhilePlanning"] == []
    [job] = result["jobs"]
    assert {key: job[key] for key in NEW} == NEW


def test_a_confirmation_before_the_response_is_shown(run_scenario):
    result = run_scenario("early-confirm")
    assert result["submission"]["confirm"] == {"message": "Upload 5 GB?", "default": True}
    assert result["submission"]["starting"] is False


def test_confirmation_answers_include_the_submitting_client(run_scenario):
    result = run_scenario("answer-confirmation")
    assert result == {
        "answers": [{"submissionId": "a" * 32, "clientId": "tab-1", "accept": True}],
        "pending": None,
    }


def test_a_submission_finished_before_the_response_is_cleared(run_scenario):
    result = run_scenario("early-submitted")
    assert result["submission"] is None
    assert result["canSubmit"] is True
    assert "success" in result["toasts"]


def test_a_cleared_queue_parameter_goes_back_to_the_queue_default(run_scenario):
    """An empty value would replace the queue's default, such as its conda channels."""
    result = run_scenario("queue-default")
    assert result["edited"] == "my-channel"
    assert result["cleared"] is None


def test_a_cleared_queue_parameter_goes_back_to_the_configured_value(run_scenario):
    result = run_scenario("queue-configured")
    assert result["edited"] == "my-channel"
    assert result["cleared"] == "s3://bucket/Conda/Default conda-forge"


def test_a_queue_parameter_the_template_defines_defaults_to_the_templates_value(run_scenario):
    """Conda Packages goes back to the job's comfy, not the queue's empty default."""
    result = run_scenario("queue-template-value")
    assert result == {
        "shown": "comfy",
        "unedited": "comfy",
        "edited": "comfy my-nodes",
        "cleared": "comfy",
        "shownAfterClearing": "comfy",
    }


def test_every_attachment_is_listed_and_can_be_removed_or_restored(run_scenario):
    result = run_scenario("all-attachments")
    planned = [f"/input/image-{index}.png" for index in range(105)]
    external = "/outside/review-me.png"
    assert result["listed"] == [
        *planned,
        external,
        "/outside/frames",
        "/manual/clip.mov",
        "/manual/frames",
    ]
    assert result["removedRowClass"] == "dl-removed"
    assert result["sentAfterRemoving"] == {
        "filenames": [*planned, "/manual/clip.mov"],
        "directories": ["/outside/frames", "/manual/frames"],
    }
    assert result["listedAfterRemovingManual"] == [*planned, external, "/outside/frames"]
    assert result["sentAfterRestoring"] == {
        "filenames": [*planned, external],
        "directories": ["/outside/frames"],
    }


def test_attachment_lists_keep_independent_scroll_positions_when_updated(run_scenario):
    result = run_scenario("all-attachments")
    assert result["scrollAfterRemoving"] == 640
    assert result["scrollAfterRestoring"] == 640
    assert result["inputScrollAfterAddingModels"] == 640
    assert result["newModelScroll"] == 0
    assert result["scrollAfterRemovingModel"] == {"inputs": 640, "models": 320}
    assert result["scrollAfterRestoringModel"] == {"inputs": 640, "models": 320}
    assert result["inputScrollAfterModelsDisappear"] == 640
    assert result["attachmentsStillOpen"] is True


def test_a_model_is_removed_by_its_folder_and_name(run_scenario):
    """Two models with the same file name, in different folders, are removed one at a time."""
    result = run_scenario("same-name-models")
    assert result["excludedModels"] == ["loras/shared.safetensors"]


def test_overlapping_job_polls_run_one_at_a_time(run_scenario):
    """A poll asked for during another runs once after it, rather than starting a second loop."""
    result = run_scenario("poll-overlapping")
    assert result["mostAtOnce"] == 1
    assert result["polls"] == 2


def test_a_job_whose_status_cant_be_read_isnt_polled_again(run_scenario):
    """A deleted job, or one from another account, would otherwise cost a bridge call each poll."""
    assert run_scenario("poll-error")["polls"] == 1


def test_a_job_whose_status_failed_for_a_moment_keeps_its_status_and_is_polled_again(run_scenario):
    """Throttling or expired credentials don't take a job out of Recent jobs' polling."""
    result = run_scenario("poll-transient")
    assert result["statusAfterError"] == "RUNNING"
    assert result["polls"] == 3


def test_jobs_are_polled_once_the_account_loads(run_scenario):
    assert run_scenario("poll-after-account")["polls"] == 1


def test_a_running_job_is_polled_again(run_scenario):
    assert run_scenario("poll-running")["polls"] == 2


def test_a_download_with_no_outputs_says_so(run_scenario):
    [toast] = run_scenario("download-nothing")["toasts"]
    assert toast == {
        "severity": "info",
        "detail": "fox: There are no output files available for download at this moment.",
    }


def test_host_requirement_values_without_a_checkbox_are_kept(run_scenario):
    """aarch64 has no checkbox, so CPU architecture stays a custom attribute with both values."""
    assert run_scenario("host-round-trip")["requirements"] == {
        "attributes": [
            {"name": "attr.worker.os.family", "anyOf": ["linux"]},
            {"name": "attr.worker.cpu.arch", "anyOf": ["x86_64", "aarch64"]},
        ],
        "amounts": [{"name": "amount.worker.gpu", "min": 1}],
    }


def test_a_submitted_job_remembers_where_its_outputs_download_to(run_scenario):
    assert run_scenario("submitted-output-dir")["outputDir"] == "/comfy/output"


@pytest.mark.parametrize(
    ("scenario", "destination"),
    [
        ("downloaded-to-cli-folder", "to /comfy/output/fox"),
        ("downloaded-to-submitted-folder", "to /comfy/output"),
        # A job saved by an older panel has no folder.
        ("downloaded-to-unknown", "into the output directory it was submitted with"),
    ],
)
def test_a_finished_download_says_where_the_files_are(run_scenario, scenario, destination):
    assert run_scenario(scenario)["toasts"] == [f"Downloaded 2 files from fox {destination}."]
