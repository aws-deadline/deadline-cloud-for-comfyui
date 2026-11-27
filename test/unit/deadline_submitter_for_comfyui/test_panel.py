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
import yaml

from deadline.comfyui_submitter import submit

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
    web = tmp_path / "extension" / "web"
    web.mkdir(parents=True)
    # The harness reads the panel's state, and what a queue field shows, through these exports,
    # which only the copy has. Exporting the section helpers also makes the module fail to load,
    # and every scenario fail, if one of them is missing.
    exports = (
        "\nexport { state as __state, queueParameterValue as __queueParameterValue,"
        " RENDERERS as __renderers, formFromRequirements as __formFromRequirements,"
        " requirementsFromForm as __requirementsFromForm, editHostForm as __editHostForm,"
        " createView as __createView, renderQueueParameters as __renderQueueParameters,"
        " PANEL_SECTIONS as __panelSections, POPOVER_SECTIONS as __popoverSections,"
        " loadQueue as __loadQueue, loadQueues as __loadQueues, loadFarms as __loadFarms,"
        " attachmentEdits as __attachmentEdits, workflowEdits as __workflowEdits,"
        " answerConfirmation as __answerConfirmation, cancelSubmission as __cancelSubmission };\n"
    )
    (web / "deadline_ui.js").write_text(ui_source + exports, encoding="utf-8")
    (tmp_path / "package.json").write_text('{"type": "module"}')
    # Our job template's parameters, for the scenarios that lay out the panel as it shows them.
    template = yaml.safe_load(submit.JOB_TEMPLATE.read_text(encoding="utf-8"))
    (tmp_path / "template_parameters.json").write_text(json.dumps(template["parameterDefinitions"]))
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


@pytest.mark.parametrize(
    ("scenario", "default"),
    [
        # The queue's Conda Channels with deadline-cloud-v2, which the job sends.
        ("queue-default", "deadline-cloud-v2 deadline-cloud"),
        # The submitter's configured channels win over the queue's.
        ("queue-configured", "s3://bucket/Conda/Default conda-forge"),
    ],
)
def test_a_queue_parameter_sends_its_default_until_edited_and_again_when_cleared(
    run_scenario, scenario, default
):
    """An empty value would replace the queue's default, such as its conda channels."""
    assert run_scenario(scenario) == {
        "unedited": default,
        "edited": "my-channel",
        "cleared": default,
    }


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


def test_host_requirement_values_without_a_checkbox_are_kept(run_scenario):
    """aarch64 has no checkbox, so CPU architecture stays a custom attribute with both values."""
    assert run_scenario("host-round-trip")["requirements"] == {
        "attributes": [
            {"name": "attr.worker.os.family", "anyOf": ["linux"]},
            {"name": "attr.worker.cpu.arch", "anyOf": ["x86_64", "aarch64"]},
        ],
        "amounts": [{"name": "amount.worker.gpu", "min": 1}],
    }


def test_bool_parameters_are_read_as_openjd_reads_them(run_scenario):
    """So the attachments agree with what the job does with Attach Custom Nodes."""
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
    assert result["queueParametersWhileLoading"] == []
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
    assert removed["rows"] == 4
    restored = result["folderRestored"]
    assert restored["sent"] == {"filenames": [*LOOSE_FILES, *FOLDER_FILES], "directories": []}
    assert restored["folderClass"] == "dl-folder"


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


BUTTONS = "Additional settingsSubmit"
CHECKING = "Checking your Deadline Cloud credentials…"


def test_the_popover_shows_one_submission_and_then_the_job_it_created(run_scenario):
    result = run_scenario("popover")
    assert result["name"] == "Job name"
    assert result["uploading"] == [BUTTONS, "foxUploading input files · 42%Cancel"]
    # Until the next submit, and each time the popover opens. There's no list of jobs.
    assert result["submitted"] == [BUTTONS, "Submitted fox as job-1"]
    assert result["copied"] == ["job-1"]
    assert result["reopened"] == [BUTTONS, "Submitted fox as job-1"]
    assert result["next"] == [BUTTONS, "Submitted owl as job-2"]
    # A submit that fails or is canceled shows its toast, and leaves no row for the job before.
    assert result["afterFailure"] == [BUTTONS]
    assert result["afterCancel"] == [BUTTONS]
    assert result["partsWithoutTarget"] == [
        "Choose a farm and queue in Additional settings first.",
        BUTTONS,
    ]
    assert result["partsWhileChecking"] == [CHECKING, BUTTONS]


def test_the_toolbar_button_opens_the_panel_unless_logged_in(run_scenario):
    """Logging in, or fixing the credentials, happens in the panel's Job submission settings."""
    result = run_scenario("toolbar-button")
    assert result["loggedIn"] == {"popover": BUTTONS, "panelOpened": 0}
    assert result["toggledAgain"] == {"popover": None, "panelOpened": 0}
    assert result["notLoggedIn"] == {"popover": None, "panelOpened": 1}
    assert result["whileChecking"] == {"popover": CHECKING, "panelOpened": 1}
    assert result["afterChecking"] == {"popover": None, "panelOpened": 2}
    assert result["panelDrawers"] == ["Job submission settings"]
    assert result["loggedInAgain"] == {"popover": BUTTONS, "panelOpened": 2}
    assert result["afterFailedCheck"] == {"popover": None, "panelOpened": 3}


CUSTOM_REQUIREMENTS_TIP = (
    "Capabilities your farm defines on its workers, such as a license count or installed software."
)


def test_the_panel_has_three_closed_drawers_with_the_shared_dialogs_labels(run_scenario):
    """The names and labels of the Deadline Cloud submitter dialog that other DCCs share."""
    result = run_scenario("drawers")
    inner = [["Attachments", []], ["Timeouts", []], ["Host requirements", []]]
    assert result["tree"] == [
        ["Job Properties", []],
        ["Job submission settings", []],
        ["Advanced settings", inner],
    ]
    assert result["open"] == []
    # A chevron and the title, with no icon or summary.
    assert result["summaries"] == [["chevron", "title"]] * 6
    assert result["labels"] == {
        "Job Properties": [
            "Description",
            "Priority",
            "Initial state",
            "Maximum retries per task",
            "Maximum failed tasks count",
            "Maximum worker count",
            "Output Directory",
        ],
        "Job submission settings": [
            "Farm",
            "Queue",
            "Default storage profile",
            "Conda Packages",
            "Conda Channels",
            "Rez Packages",
        ],
        "Attachments": ["Job attachments"],
        "Timeouts": ["Server Start Timeout (seconds)", "Workflow Timeout (seconds)"],
        "Host requirements": [
            "Operating system",
            "CPU architecture",
            "vCPUs",
            "Memory (GiB)",
            "GPUs",
            "GPU memory (GiB)",
            "Scratch space (GiB)",
        ],
    }
    # Each queue environment's parameters under its group label, as the library writes it.
    assert result["subheadings"] == {
        "Job submission settings": [
            ["Queue Environment: Conda", None],
            ["Queue Environment: Rez", None],
        ],
        "Host requirements": [["Custom host requirements", CUSTOM_REQUIREMENTS_TIP]],
    }
    assert result["placeholders"] == ["Default", "Default", "No limit"]
    assert result["account"] == "Logged in (default)"
    assert result["queueReset"] == ["Use the default"] * 3
    # A custom template's other visible parameters get a drawer of their own.
    assert result["customTree"] == [*inner, ["Job parameters", []]]
    assert result["customJobParameters"] == ["Sampler", "Seed"]


LOGGED_IN = {"account": "Logged in (default)", "dot": "dl-dot dl-ok"}
NOT_LOGGED_IN = {"blocker": "Log in to Deadline Cloud to submit.", "dot": "dl-dot dl-warn"}
CANT_LOG_IN = {"blocker": "Check your Deadline Cloud credentials to submit.", "dot": None}
SUBMISSION = ["Job submission settings"]
TIMEOUTS = ["Advanced settings", "Timeouts"]


def shown(opened: list[str], **fields: str | bool | None) -> dict:
    """What the panel shows: the drawers open, the account line, and why Submit is off."""
    blocker = fields.get("blocker")
    return {"open": opened, "blocker": None, "canSubmit": blocker is None, **fields}


def test_drawers_open_themselves_for_the_account_and_for_values_submit_needs(run_scenario):
    result = run_scenario("auto-open")
    # Not while the account is checked, and Submit gives no reason for the account then.
    assert result["checking"] == shown([], account=CHECKING, dot=None, canSubmit=False)
    assert result["notLoggedIn"] == shown(
        SUBMISSION, account="Not logged in (default)Log in", **NOT_LOGGED_IN
    )
    assert result["loggingIn"] == shown(
        SUBMISSION, account="Finish logging in with Deadline Cloud monitor…", **NOT_LOGGED_IN
    )
    bad = (
        "The Deadline Cloud credentials (default) don't work (CONFIGURATION_ERROR). Check the AWS "
        "profile in Deadline Cloud monitor or the Deadline Cloud configuration.Check again"
    )
    assert result["badCredentials"] == shown(SUBMISSION, account=bad, **CANT_LOG_IN)
    failed = "Couldn't reach the ComfyUI server: Couldn't reach Deadline Cloud.Try again"
    assert result["failedCheck"] == shown(SUBMISSION, account=failed, **CANT_LOG_IN)
    no_target = "Choose a farm and a queue to submit to."
    assert result["noTarget"] == shown(SUBMISSION, blocker=no_target, **LOGGED_IN)
    # The Farm select it needs has a warning border.
    assert result["farmNeeded"] == ["select"]
    assert result["loggedIn"] == shown([], **LOGGED_IN)
    timeout = "Workflow Timeout (seconds) must be a whole number from 60 to 82800."
    assert result["badTimeout"] == shown(TIMEOUTS, blocker=timeout, **LOGGED_IN)
    # A drawer never closes itself.
    assert result["fixedTimeout"] == shown(TIMEOUTS, **LOGGED_IN)
    priority = "Priority must be a whole number from 0 to 100."
    assert result["badPriority"] == shown(["Job Properties"], blocker=priority, **LOGGED_IN)
    # A check that fails after a successful one forgets the account, so Submit is off, and the
    # drawer shows only the notice.
    assert result["failedRecheck"] == shown(SUBMISSION, account=failed, **CANT_LOG_IN)
    assert result["failedRecheckLabels"] == []


def test_a_saved_output_directory_other_than_comfyuis_opens_job_properties(run_scenario):
    """So the artist sees where the job's outputs go."""
    assert run_scenario("saved-output-dir") == {
        "other": ["Job Properties"],
        "same": [],
        # The field wouldn't take it, so it isn't applied.
        "relative": [],
        "unsaved": [],
    }


def test_drawers_stay_open_in_another_workflow(run_scenario):
    """Timeouts isn't there while the other workflow's plan loads, and comes back open."""
    assert run_scenario("drawers-across-workflows") == {
        "timeoutsWhilePlanning": False,
        "open": ["Advanced settings", "Timeouts"],
    }


def test_the_host_requirements_form_is_sent_only_once_edited(run_scenario):
    """Our template has none, so the form starts empty and the job runs on any worker."""
    result = run_scenario("host-form")
    assert result["empty"] == {
        "checked": 0,
        "values": [],
        "resetHidden": True,
        "labels": [
            "Operating system",
            "CPU architecture",
            "vCPUs",
            "Memory (GiB)",
            "GPUs",
            "GPU memory (GiB)",
            "Scratch space (GiB)",
        ],
        "options": ["linux", "macos", "windows", "x86_64", "arm64"],
        "buttons": ["Add amount", "Add attribute", "Reset all to defaults"],
        # The job template's host requirements apply.
        "sent": None,
    }
    gpu_memory = {"attributes": [], "amounts": [{"name": "amount.worker.gpu.memory", "min": 16384}]}
    assert result["edited"] == {"resetHidden": False, "sent": gpu_memory}
    assert result["reset"] == {"checked": 0, "values": [], "resetHidden": True, "sent": None}


def test_list_parameters_are_not_sent(run_scenario):
    """The job runs as one task, so it has no task parameters for the lists' ranges."""
    assert run_scenario("list-parameters") == {"parameters": {"Seed": 5}, "splitTasks": False}


def test_values_a_field_cant_submit_keep_submit_off_and_say_why(run_scenario):
    result = run_scenario("invalid-values")
    assert result["blockers"] == [
        None,
        "Priority must be a whole number from 0 to 100.",
        "Priority must be a whole number from 0 to 100.",
        "Priority must be a whole number from 0 to 100.",
        "Maximum worker count must be a whole number from 1 to 2147483647.",
        "Maximum retries per task must be a whole number from 0 to 2147483647.",
        "Steps must be a whole number from 1 to 100.",
        "OutputDir must be an absolute path.",
        "Enter a job name.",
        "The job name can be at most 128 characters.",
        "The job name can't have {{, }} or control characters.",
        "The description can't have control characters other than tabs and line breaks.",
        "Choose a value for Mode.",
        "Upscale must be true or false.",
        "Prompt can have at most 20 characters.",
        "The min and max of GPUs must be 0 or more.",
        (
            "Name worker.licenses with letters, numbers and underscores, separated by periods, not "
            "starting with worker, job, step or task."
        ),
        "Set a min or a max for license.nuke.",
        (
            "The values of software need letters, numbers, underscores and dashes, each starting "
            "with a letter or underscore."
        ),
        "Enter a number.",
    ]
    assert result["keptError"] == {"shown": ["Enter a number."], "blocker": "Enter a number."}
    # The first blocker is None with Output Directory's relative default, which the artist didn't set.
    assert result["paths"] == [None, None, None, "OutputDir can't be empty."]
    # An empty Input Directory, say, runs the job with an empty one.
    assert result["sentPaths"] == {"OutputDir": "/out", "Frames": ""}


def test_parameters_get_the_controls_openjd_gives_them(run_scenario):
    result = run_scenario("field-controls")
    assert result["controls"] == {
        "Upscale": {"tag": "input", "type": "checkbox", "browse": None},
        "Frames": {"tag": "input", "type": None, "browse": "Browse…"},
        # The file dialog only picks files that exist.
        "Movie": {"tag": "input", "type": None, "browse": None},
        "Mask": {"tag": "input", "type": None, "browse": "Browse…"},
        "Sampler": {"tag": "select", "type": None, "browse": None},
        "Denoise": {"tag": "input", "type": "checkbox", "browse": None},
        "InputDir": {"tag": "input", "type": None, "browse": "Browse…"},
    }
    # A default its choices don't have shows as Choose….
    assert result["samplerOptions"] == ["Choose…", "euler", "dpmpp"]
    assert result["denoise"] == "yes"
    assert result["chooseKind"] == "file"


def test_parameters_can_have_the_names_objects_have(run_scenario):
    assert run_scenario("prototype-names")["parameters"] == {
        "constructor": "a",
        "toString": 3,
        "__proto__": "q",
    }


def test_a_workflow_keeps_its_edits_when_it_is_saved_or_renamed(run_scenario):
    assert run_scenario("renamed-workflow") == {"afterSaving": "Mine", "atOldPath": None}


def test_a_re_render_keeps_the_focus_and_caret_in_the_job_name(run_scenario):
    assert run_scenario("focus")["focus"] == {
        "replaced": True,
        "focused": True,
        "selection": [2, 5],
    }


def test_a_path_chosen_after_another_workflow_opened_isnt_set(run_scenario):
    result = run_scenario("browse-after-switching")
    assert result["afterSwitching"] is None
    assert "warn" in result["toasts"]
    assert result["chosen"] == "/chosen/out"


SAVED = {
    "version": 1,
    "jobName": "My job",
    "description": "For the studio",
    "priority": 70,
    "targetTaskRunStatus": "READY",
    # Numbers, checkboxes, choices and Output Directory, as the absolute path the artist chose.
    # Not other paths, text or hidden parameters.
    "parameters": {"Steps": "30", "Upscale": "false", "OutputDir": "/Users/me/out"},
    "hostRequirements": {"attributes": [], "amounts": [{"name": "amount.worker.gpu", "min": 1}]},
}


def test_a_submission_saves_its_settings_in_the_workflow_and_saves_it_when_unchanged(run_scenario):
    """
    Workflows are shared, so text, paths other than Output Directory, the farm and queue, queue
    environment values and attachments aren't saved in them.
    ComfyUI saves the workflow only when it's in a file and had no other changes.
    """
    result = run_scenario("saved-settings-written")
    assert result["saved"] == {"saved": SAVED, "commands": ["Comfy.SaveWorkflow"]}
    # The settings are in the workflow for the artist's next save.
    for case in ("modified", "temporary", "olderFrontend"):
        assert result[case] == {"saved": SAVED, "commands": []}
    # A submission the server refused saves nothing.
    assert result["failed"] == {"saved": None, "commands": []}
    assert result["sameLayoutKey"] is True


UNSAVED: dict[str, object] = {
    "jobName": None,
    "description": "",
    "settings": {},
    "edits": {},
    "hostRequirements": None,
}


def test_a_workflows_saved_settings_load_without_what_no_longer_applies(run_scenario):
    result = run_scenario("saved-settings-loaded")
    assert result["loaded"] == {
        "jobName": "Saved name",
        "description": "Saved description",
        # A maximum worker count of 0 and a string for maximum retries per task are dropped.
        "settings": {"priority": 80, "targetTaskRunStatus": "SUSPENDED"},
        # Steps is over its max, Prompt is text, WorkflowFile is a path, the panel hides Attach
        # Custom Nodes now, and the template has no Gone.
        "edits": {"Mode": "b", "Upscale": "false", "OutputDir": "/tmp/out"},
        # Its min is more than its max.
        "hostRequirements": None,
    }
    assert result["sent"]["priority"] == 80
    assert result["sent"]["status"] == "SUSPENDED"
    assert result["sent"]["parameters"]["Mode"] == "b"
    assert result["sent"]["parameters"]["Steps"] == 20
    assert result["sent"]["parameters"]["OutputDir"] == "/tmp/out"
    # The job attaches the custom nodes, as the template's default says.
    assert result["sent"]["parameters"]["AttachCustomNodes"] == "true"
    # The artist couldn't set a relative one.
    assert result["relativeOutputDir"] == {}
    assert result["globalPriority"] == 50
    assert result["hosts"]["hostRequirements"] == {
        "attributes": [{"name": "attr.worker.os.family", "anyOf": ["linux"]}],
        "amounts": [{"name": "amount.worker.gpu", "min": 1}],
    }
    # More than 50 host requirements aren't loaded. Keys the form doesn't have are left out.
    assert result["tooMany"] is None
    assert result["otherShape"] == {
        "attributes": [],
        "amounts": [{"name": "amount.worker.gpu", "min": 1}],
    }
    for case in ("templateText", "unknownVersion", "malformed", "unsaved"):
        assert result[case] == UNSAVED
    # A tab keeps what the artist set, rather than what its file saved, when it's opened again.
    assert result["backAgain"] == "Edited"
    # A workflow closed and opened again starts afresh.
    assert result["reopened"] == "From the file"
    # A name typed while the first plan loads stays.
    assert result["typed"] == {"jobName": "Typed", "settings": {"priority": 90}}
