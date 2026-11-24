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
MISSING_TOOLS = NODE is None or not TSC.exists()
# CI sets this so that the frontend job fails, rather than passing with no panel tests run.
if MISSING_TOOLS and os.environ.get("COMFYUI_DEADLINE_REQUIRE_PANEL_TESTS") == "1":
    raise RuntimeError("The panel tests need Node.js, and npm ci run in frontend/")
pytestmark = pytest.mark.skipif(MISSING_TOOLS, reason="Needs Node.js, and npm ci run in frontend/")

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
    shutil.copy(PANEL / "item_ranges.json", tmp_path / "item_ranges.json")
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
        " createView as __createView, itemEditBase as __itemEditBase,"
        " DIALOG_SECTIONS as __dialogSections, PANEL_SECTIONS as __panelSections,"
        " loadQueue as __loadQueue, loadQueues as __loadQueues, loadFarms as __loadFarms,"
        " attachmentEdits as __attachmentEdits,"
        " answerConfirmation as __answerConfirmation, cancelSubmission as __cancelSubmission };\n"
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
    assert result["submission"]["confirm"] == {
        "message": "Upload 5 GB?",
        "default": True,
        "promptId": "prompt-1",
    }
    assert result["submission"]["starting"] is False


def answer(prompt_id: str, accept: bool) -> dict:
    return {"submissionId": "a" * 32, "clientId": "tab-1", "promptId": prompt_id, "accept": accept}


def test_confirmation_answers_name_their_prompt_and_the_submitting_client(run_scenario):
    """The server only takes an answer that names the prompt pending now."""
    result = run_scenario("answer-confirmation")
    assert result == {"answers": [answer("prompt-1", True)], "pending": None}


def test_a_new_confirmation_survives_an_earlier_answer_finishing(run_scenario):
    result = run_scenario("answer-while-next-prompt-arrives")
    assert result["answersWhileFirstPending"] == [answer("prompt-1", True)]
    assert result["nextPrompt"] == {
        "message": "Run hooks?",
        "default": False,
        "promptId": "prompt-2",
    }
    # Each answer names the prompt it was shown with, so the first can't answer the second.
    assert result["answers"] == [answer("prompt-1", True), answer("prompt-2", False)]
    assert result["pending"] is None


def test_cancel_requests_include_the_submitting_client(run_scenario):
    assert run_scenario("cancel-submission")["cancels"] == [
        {"submissionId": "a" * 32, "clientId": "tab-1"}
    ]


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


def test_a_custom_node_pack_can_be_removed_and_added_back(run_scenario):
    """A pack can hold keys in its settings, which anyone who can use the queue could read."""
    result = run_scenario("all-attachments")
    assert result["packRow"].startswith("studio-nodes (3.2 GB in 12,500 files)")
    # A job uploads a large pack until the artist removes it.
    assert (
        "The custom node pack studio-nodes is 3.2 GB in 12,500 files." in result["largePackStatus"]
    )
    assert "The custom node pack" not in result["packStatus"]
    assert result["packRowRemoved"] == "dl-removed"
    assert "Not attaching the custom nodes in studio-nodes." in result["packStatus"]
    assert result["excludedNodes"] == ["studio-nodes"]
    assert "Not attaching the custom nodes" not in result["packStatusAfterRestoring"]
    assert result["excludedNodesAfterRestoring"] == []


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


def test_bool_parameters_are_read_as_openjd_reads_them(run_scenario):
    """So the Attach Custom Nodes checkbox and the attachments agree with what the job does."""
    assert run_scenario("parse-bool")["values"] == [True] * 6 + [False] * 6 + [None] * 5


def test_an_earlier_queues_details_dont_replace_the_chosen_queues(run_scenario):
    """The queue parameters and storage profiles sent are the chosen queue's."""
    assert run_scenario("late-queue-details") == {"queueParameters": ["Queue2Parameter"]}


def test_a_farm_or_queue_the_panel_doesnt_list_isnt_submitted_to(run_scenario):
    assert run_scenario("target-not-listed") == {
        "queueId": "",
        # Not chosen for the next queue, rather than None, so the account's default can fill it.
        "storageProfileId": None,
        "canSubmitUnlisted": False,
        "canSubmitWithoutFarms": False,
    }


def test_switching_queues_forgets_the_previous_queues_details(run_scenario):
    """Nothing from queue 1, its parameters or its storage profile, is shown or sent to queue 2."""
    result = run_scenario("switch-queue")
    assert result["queue1Sent"] == {
        "parameters": {"Queue1Parameter": "for queue 1", "Shared": "edited for queue 1"},
        "storageProfileId": "sp-1",
    }
    assert result["canSubmitBeforeSwitching"] is True
    assert result["queueWhileLoading"] is None
    assert result["queueParametersWhileLoading"] is None
    assert result["canSubmitWhileLoading"] is False
    assert result["blockerWhileLoading"] == "Loading the queue's details…"
    assert result["sentWhileLoading"] is None
    assert result["canSubmitLoaded"] is True
    # Shared has the same name on both queues, but its edit was for queue 1.
    assert result["queue2Sent"] == {
        "queueId": "queue-2",
        "parameters": {"Queue2Parameter": "for queue 2"},
        "storageProfileId": "",
    }


def test_submit_waits_for_the_latest_queue_details(run_scenario):
    result = run_scenario("pending-queue-details")
    assert result["canSubmitWhilePending"] is False
    assert result["blockerWhilePending"] == "Loading the queue's details…"
    assert result["sentWhilePending"] is None
    # An earlier answer for the same queue came last, and was ignored.
    assert result["parameters"] == ["Newer"]
    assert result["profiles"] == ["sp-new"]
    assert result["canSubmitLoaded"] is True
    assert result["blockerLoaded"] is None
    # Details that fail to load keep Submit off, and Submit says why.
    assert result["queueFailed"] is None
    assert result["canSubmitFailed"] is False
    assert result["blockerFailed"].startswith("Couldn't load the queue's details:")
    assert "ThrottlingException" in result["blockerFailed"]


def test_a_storage_profile_of_none_survives_reloading_the_account(run_scenario):
    """None is a choice, not a missing value that the account's default fills in again."""
    result = run_scenario("storage-profile-none")
    # The default isn't dropped while the queue's details load.
    assert result["whileLoading"] == {"storageProfileId": "sp-1", "canSubmit": False}
    assert result["defaultSent"] == "sp-1"
    assert result["afterReload"] == ""
    assert result["noneSent"] == ""
    assert result["savedDefaults"] == [""]


def test_a_default_storage_profile_the_queue_doesnt_list_needs_a_choice(run_scenario):
    """Submitting without it would upload files that are on shared storage, without saying so."""
    result = run_scenario("storage-profile-cleared")
    assert result["storageProfileId"] is None
    assert result["canSubmit"] is False
    assert "doesn't list the storage profile sp-gone" in result["blocker"]
    assert result["sentBeforeChoosing"] is None
    assert result["canSubmitAfterNone"] is True
    assert result["blockerAfterNone"] is None
    assert result["noneSent"] == ""


def test_attachment_changes_stay_with_their_workflow(run_scenario):
    """A file added to workflow A, or removed from it, doesn't change workflow B's job."""
    result = run_scenario("workflow-attachments")
    in_a = {
        "attachments": {"filenames": ["/manual/clip.mov"], "directories": []},
        "excludedModels": ["checkpoints/sd.safetensors"],
    }
    assert result["fromA"] == in_a
    assert result["fromB"] == {
        "attachments": {"filenames": ["/input/old.png"], "directories": []},
        "excludedModels": [],
    }
    # Back in A, its changes are as the artist left them.
    assert result["backInA"] == in_a


def test_a_file_dialog_that_closes_after_switching_workflows_adds_nothing(run_scenario):
    result = run_scenario("late-file-dialog")
    assert result["fromB"] == ["/input/old.png"]
    assert result["fromA"] == ["/input/old.png"]
    assert "warn" in result["toasts"]


def test_both_views_show_what_the_job_attaches_next_to_submit(run_scenario):
    result = run_scenario("attachment-summary")
    assert result["planned"] == "Attaches 2 files, 2 models and 1 custom node pack."
    summary = (
        "Attaches 2 files, 1 folder, 1 model and 1 custom node pack. "
        "You added /outside/clip.mov, /outside/frames."
    )
    assert result["summary"] == summary
    assert result["sidebar"] == summary
    assert result["dialog"] == summary
    # The summary is what's sent.
    assert result["sent"] == {
        "attachments": {
            "filenames": ["/input/old.png", "/outside/clip.mov"],
            "directories": ["/outside/frames"],
        },
        "excludedModels": ["checkpoints/b.safetensors"],
        "excludedNodes": [],
    }


FOLDER = "/input/cats"
FOLDER_FILES = [f"{FOLDER}/cat-{index}.png" for index in range(5000)]
LOOSE_FILES = ["/input/fox.png", "/input/mask.png", "/input/owl.png"]


def test_a_folder_loaders_folder_is_one_row_until_it_is_opened(run_scenario):
    """A dataset folder of thousands of files doesn't render a row for each until it's opened."""
    result = run_scenario("attachment-folders")
    collapsed = result["collapsed"]
    assert collapsed["listed"] == [*LOOSE_FILES, FOLDER]
    assert collapsed["folder"] == f"▸ {FOLDER} (5,000 files)✕"
    assert collapsed["expanded"] == "false"
    assert collapsed["title"] == "Attachments (4)"
    opened = result["opened"]
    assert opened["rows"] == 4 + 5000
    assert opened["folder"].startswith(f"▾ {FOLDER} (5,000 files)")
    assert opened["expanded"] == "true"
    # Each file is listed by its path in the folder.
    assert (opened["first"], opened["firstClass"]) == ("cat-0.png✕", "dl-nested")
    assert result["closed"]["rows"] == 4
    assert result["closed"]["expanded"] == "false"


def test_one_click_removes_a_folder_and_add_back_restores_every_file(run_scenario):
    result = run_scenario("attachment-folders")
    removed = result["folderRemoved"]
    assert removed["sent"] == {"filenames": LOOSE_FILES, "directories": []}
    assert removed["folder"] == f"▸ {FOLDER} (5,000 files)Add back"
    assert removed["folderClass"] == "dl-folder dl-removed"
    assert removed["title"] == "Attachments (3)"
    assert removed["rows"] == 4
    restored = result["folderRestored"]
    assert restored["sent"] == {"filenames": [*LOOSE_FILES, *FOLDER_FILES], "directories": []}
    assert restored["folderClass"] == "dl-folder"
    assert restored["title"] == "Attachments (4)"


def test_a_file_removed_from_an_open_folder_leaves_the_rest_attached(run_scenario):
    result = run_scenario("attachment-folders")
    removed = result["fileRemoved"]
    kept = [path for path in FOLDER_FILES if path != f"{FOLDER}/cat-42.png"]
    assert removed["sent"] == {"filenames": [*LOOSE_FILES, *kept], "directories": []}
    assert removed["folder"] == f"▾ {FOLDER} (4,999 of 5,000 files)✕"
    assert removed["fileRow"] == "cat-42.pngAdd back"
    assert removed["fileClass"] == "dl-nested dl-removed"
    # The list stays where the artist scrolled it.
    assert removed["scroll"] == 900
    # The folder's ✕ removes the rest, and Add back restores every file, with the one removed on
    # its own.
    assert result["removedWhileOpen"]["sent"] == {"filenames": LOOSE_FILES, "directories": []}
    assert result["removedWhileOpen"]["scroll"] == 900
    assert result["restoredWhileOpen"]["sent"] == {
        "filenames": [*LOOSE_FILES, *FOLDER_FILES],
        "directories": [],
    }


def test_the_summary_counts_a_folder_loaders_folder_as_one_folder(run_scenario):
    result = run_scenario("attachment-folders")
    summary = "Attaches 3 files and 1 folder (5,000 files)."
    assert result["collapsed"]["summary"] == summary
    assert result["shownSummary"] == summary
    assert result["fileRemoved"]["summary"] == "Attaches 3 files and 1 folder (4,999 files)."
    assert result["folderRemoved"]["summary"] == "Attaches 3 files."
    assert result["summaryWithAddedFolder"] == (
        "Attaches 3 files and 2 folders (5,000 files in 1 of them). You added /outside/frames."
    )


def test_the_sidebar_lists_the_attachments_before_submit(run_scenario):
    sections = run_scenario("panel-sections")["sections"]
    assert sections.index("attachments") < sections.index("submit")
    assert sections[-1] == "submit"


def test_the_panel_has_the_submitters_task_limit(run_scenario):
    from deadline.comfyui_submitter import submit

    result = run_scenario("task-limit")
    assert result == {
        "maxTasks": submit.MAX_TASKS,
        # As one task, the lists run whole, however many items they have.
        "canSubmitOneTask": True,
        # 100 × 101 tasks is over the limit, and 100 × 100 is the limit.
        "canSubmitOverLimit": False,
        "canSubmitAtLimit": True,
    }


def test_the_dialog_lists_the_attachments_before_submit(run_scenario):
    # The Deadline button opens the dialog, which must show every file the job uploads.
    sections = run_scenario("dialog-sections")["sections"]
    assert "attachments" in sections
    assert sections.index("attachments") < sections.index("submit")


def test_task_lists_count_tasks_and_send_their_ranges(run_scenario):
    result = run_scenario("task-lists")
    # OpenJD ranges, as the job's task parameters take them. "0" runs a list whole, as one item.
    assert result["counts"] == [4, 10, 3, None, None, 1, None]
    # Every combination: 3 list items × 5 images.
    assert result["tasks"] == 15
    # Previews follow the order the tasks run in.
    assert result["previews"] == [
        "b.png, c.png, d.png, …",
        "All of them, in each task",
        "",
        "a.png, c.png",
    ]
    # A 3-item list: numbers past the end or below 1 can't be submitted. 0 runs it whole.
    assert result["listCounts"] == [3, 2, 1, None, None, None, 2, None]
    assert result["openjdCounts"] == [None, None, None, None, None, 2, 4]
    assert result["splitByDefault"] is False
    assert result["splitRequest"] is True
    # One task: no list parameters, and the edited ranges come back when splitting again.
    assert result["wholeTasks"] == 1
    assert result["wholeParameters"] == {}
    assert result["wholeRequest"] is False
    assert result["splitAgain"] == 6
    assert result["editKept"] == "2"
    assert result["editAfterChange"] is None
    assert result["splitWhileLoading"] is False
    assert result["splitAfterSwitch"] is True
    assert result["splitOtherWorkflow"] is False
    assert result["canSubmitBadRange"] is False
    assert result["canSubmit"] is True
    assert result["parameters"] == {"List1Items": "1-3", "Folder2Items": "1-2"}


def test_list_fields_take_the_same_ranges_as_the_submitter(run_scenario):
    """item_ranges.json is shared with test_submit.py, so the panel and the submitter agree."""
    cases = json.loads((PANEL / "item_ranges.json").read_text())
    assert run_scenario("item-range-contract")["counts"] == [case["count"] for case in cases]
