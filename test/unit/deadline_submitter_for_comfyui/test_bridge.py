# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Tests for bridge.py, which runs in the Deadline Cloud CLI's Python. A stand-in for the deadline
client library records the calls.
"""

import enum
import io
import json
import sys
import types
from pathlib import Path

import pytest

from deadline.comfyui_submitter import bridge


class Status(enum.Enum):
    AUTHENTICATED = 2


class Source(enum.Enum):
    DEADLINE_CLOUD_MONITOR_LOGIN = 3


class FakeEvents:
    """A botocore client's event emitter, which keeps one handler per unique_id."""

    def __init__(self):
        self.handlers: dict[str, list] = {}

    def register(self, event_name, handler, unique_id=None):
        handlers = self.handlers.setdefault(event_name, [])
        if unique_id and any(existing_id == unique_id for existing_id, _ in handlers):
            return
        handlers.append((unique_id, handler))

    def emit(self, event_name, **kwargs):
        for _, handler in self.handlers.get(event_name, []):
            handler(**kwargs)


class FakeApi:
    def __init__(self):
        self.calls = []
        self.client = types.SimpleNamespace(meta=types.SimpleNamespace(events=FakeEvents()))
        # What the library does after CreateJob returns, such as waiting for the job.
        self.after_create_job = None

    def get_boto3_client(self, service_name, config=None):
        return self.client

    def get_credentials_source(self, config):
        return Source.DEADLINE_CLOUD_MONITOR_LOGIN

    def check_authentication_status(self, config):
        return Status.AUTHENTICATED

    def login(self, on_pending_authorization, on_cancellation_check, from_gui):
        on_pending_authorization(credentials_source=Source.DEADLINE_CLOUD_MONITOR_LOGIN)
        return "Logged in."

    def list_farms(self, config):
        return {"farms": [{"farmId": "farm-1", "displayName": "Render"}, {"farmId": "farm-2"}]}

    def list_queues(self, config, farmId):
        self.calls.append(("list_queues", farmId))
        return {"queues": [{"queueId": "queue-1", "displayName": "GPU"}]}

    def get_queue_parameter_definitions(self, farmId, queueId, config):
        return [{"name": "CondaChannels", "type": "STRING", "default": "deadline-cloud"}]

    def list_storage_profiles_for_queue(self, config, farmId, queueId):
        return {"storageProfiles": [{"storageProfileId": "sp-1", "displayName": "Mac"}]}

    def create_job_from_job_bundle(self, job_bundle_dir, **kwargs):
        self.calls.append(("create_job_from_job_bundle", job_bundle_dir, kwargs))
        metadata = types.SimpleNamespace(progress=50.0, progressMessage="Uploading 1 of 2")
        kwargs["hashing_progress_callback"](metadata)
        kwargs["upload_progress_callback"](metadata)
        kwargs["print_function_callback"]("Submitting")
        if not kwargs["interactive_confirmation_callback"]("Upload 2 files?", True):
            raise RuntimeError("Canceled")
        # As the library calls CreateJob, through a client from api.get_boto3_client.
        events = self.get_boto3_client("deadline", config=kwargs["config"]).meta.events
        events.emit("before-call.deadline.CreateJob", params={})
        events.emit("after-call.deadline.CreateJob", parsed={"jobId": "job-1"}, http_response=None)
        if self.after_create_job:
            self.after_create_job()
        return "job-1"


class FakeConfigFile:
    def __init__(self):
        self.settings = {
            "defaults.aws_profile_name": "studio",
            "defaults.farm_id": "farm-1",
            "defaults.queue_id": "queue-1",
            "settings.storage_profile_id": "",
            "defaults.job_attachments_file_system": "COPIED",
        }
        self.written = None

    def read_config(self):
        return dict(self.settings)

    def get_setting(self, name, config):
        return config[name]

    def set_setting(self, name, value, config):
        config[name] = value

    def write_config(self, config):
        self.written = config


@pytest.fixture
def deadline_api(monkeypatch):
    api = FakeApi()
    config_file = FakeConfigFile()
    config = types.SimpleNamespace(config_file=config_file)
    client = types.SimpleNamespace(api=api, config=config)
    monkeypatch.setitem(sys.modules, "deadline.client", client)
    monkeypatch.setitem(sys.modules, "deadline.client.api", api)
    monkeypatch.setitem(sys.modules, "deadline.client.config", config)
    monkeypatch.setitem(sys.modules, "deadline.client.config.config_file", config_file)
    return types.SimpleNamespace(api=api, config_file=config_file)


def run(monkeypatch, capsys, command, request=None, answers=()):
    lines = [json.dumps(request or {}), *map(json.dumps, answers)]
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(lines) + "\n"))
    code = bridge.main(["bridge.py", command])
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    return code, events


def test_status(deadline_api, monkeypatch, capsys):
    code, events = run(monkeypatch, capsys, "status")
    assert code == 0
    assert events == [
        {
            "event": "result",
            "data": {
                "profile": "studio",
                "farmId": "farm-1",
                "queueId": "queue-1",
                "storageProfileId": "",
                "jobAttachmentsFileSystem": "COPIED",
                "credentialsSource": "DEADLINE_CLOUD_MONITOR_LOGIN",
                "authenticationStatus": "AUTHENTICATED",
            },
        }
    ]


def test_login_reports_that_its_waiting(deadline_api, monkeypatch, capsys):
    _, events = run(monkeypatch, capsys, "login")
    assert events[0]["event"] == "message"
    assert events[-1] == {"event": "result", "data": {"message": "Logged in."}}


def test_farms_and_queues(deadline_api, monkeypatch, capsys):
    _, events = run(monkeypatch, capsys, "farms")
    assert events[-1]["data"]["farms"] == [
        {"farmId": "farm-1", "displayName": "Render"},
        {"farmId": "farm-2", "displayName": "farm-2"},
    ]
    _, events = run(monkeypatch, capsys, "queues", {"farmId": "farm-2"})
    assert events[-1]["data"]["queues"] == [{"queueId": "queue-1", "displayName": "GPU"}]
    assert deadline_api.api.calls == [("list_queues", "farm-2")]


def test_queue(deadline_api, monkeypatch, capsys):
    _, events = run(monkeypatch, capsys, "queue", {"farmId": "farm-1", "queueId": "queue-1"})
    assert events[-1]["data"] == {
        "parameters": [{"name": "CondaChannels", "type": "STRING", "default": "deadline-cloud"}],
        "values": {"CondaChannels": "deadline-cloud-v2 deadline-cloud"},
        "storageProfiles": [{"storageProfileId": "sp-1", "displayName": "Mac", "osFamily": None}],
    }


V2 = "deadline-cloud-v2 deadline-cloud"


@pytest.mark.parametrize(
    ("parameter", "channels"),
    [
        # Just before the deadline-cloud channel, wherever it is in the list.
        ({"default": "conda-forge deadline-cloud"}, "conda-forge deadline-cloud-v2 deadline-cloud"),
        (
            {"default": "deadline-cloud  conda-forge"},
            "deadline-cloud-v2 deadline-cloud conda-forge",
        ),
        # Before each one, as the library does.
        (
            {"default": "deadline-cloud conda-forge deadline-cloud"},
            "deadline-cloud-v2 deadline-cloud conda-forge deadline-cloud-v2 deadline-cloud",
        ),
        # Once.
        ({"default": V2}, None),
        ({"default": "deadline-cloud deadline-cloud-v2"}, None),
        # Only for the deadline-cloud channel, not a channel with it in its name or path.
        ({"default": "my-deadline-cloud s3://bucket/deadline-cloud"}, None),
        ({"default": ""}, None),
        ({}, None),
        # Not when the parameter would refuse the value.
        ({"default": "deadline-cloud", "maxLength": len(V2)}, V2),
        ({"default": "deadline-cloud", "maxLength": len(V2) - 1}, None),
        ({"default": "deadline-cloud", "allowedValues": ["deadline-cloud", V2]}, V2),
        ({"default": "deadline-cloud", "allowedValues": ["deadline-cloud"]}, None),
        # Only Conda Channels.
        ({"name": "CondaPackages", "default": "deadline-cloud"}, None),
    ],
)
def test_queue_adds_deadline_cloud_v2_to_the_conda_channels(
    deadline_api, monkeypatch, capsys, parameter, channels
):
    """The job sends it as the value, as the other Deadline Cloud submitters do."""
    definition = {"name": "CondaChannels", "type": "STRING", **parameter}
    monkeypatch.setattr(
        deadline_api.api, "get_queue_parameter_definitions", lambda **kwargs: [definition]
    )
    _, events = run(monkeypatch, capsys, "queue", {"farmId": "farm-1", "queueId": "queue-1"})
    data = events[-1]["data"]
    assert data["values"] == ({definition["name"]: channels} if channels else {})
    # The parameter keeps the queue's own default.
    assert data["parameters"] == [definition]


FARM_ID, QUEUE_ID, PROFILE_ID = "farm-" + "a" * 32, "queue-" + "b" * 32, "sp-" + "c" * 32


def test_set_defaults_saves_the_farm_queue_and_storage_profile(deadline_api, monkeypatch, capsys):
    request = {"farmId": FARM_ID, "queueId": QUEUE_ID, "storageProfileId": PROFILE_ID}
    code, _ = run(monkeypatch, capsys, "set-defaults", request)
    assert code == 0
    written = deadline_api.config_file.written
    assert written["defaults.farm_id"] == FARM_ID
    assert written["defaults.queue_id"] == QUEUE_ID
    assert written["settings.storage_profile_id"] == PROFILE_ID


def test_set_defaults_clears_a_default_set_to_an_empty_string(deadline_api, monkeypatch, capsys):
    request = {"farmId": FARM_ID, "queueId": QUEUE_ID, "storageProfileId": ""}
    code, _ = run(monkeypatch, capsys, "set-defaults", request)
    assert code == 0
    assert deadline_api.config_file.written["settings.storage_profile_id"] == ""


@pytest.mark.parametrize(
    "request_ids",
    [
        {"farmId": "farm-x]\n[other", "queueId": QUEUE_ID},
        {"farmId": FARM_ID, "queueId": "queue-2"},
        {"farmId": FARM_ID, "queueId": QUEUE_ID, "storageProfileId": "sp-1"},
        {"farmId": QUEUE_ID},
        {"farmId": 7},
    ],
)
def test_set_defaults_saves_nothing_when_an_id_is_malformed(
    deadline_api, monkeypatch, capsys, request_ids
):
    code, events = run(monkeypatch, capsys, "set-defaults", request_ids)
    assert code == 1
    assert events[-1]["event"] == "error"
    assert deadline_api.config_file.written is None


SUBMISSION = {
    "bundleDir": "/bundles/fox",
    "farmId": "farm-2",
    "queueId": "queue-2",
    "storageProfileId": "sp-1",
    "parameters": {"OutputDir": "/out", "SeedItems": "1-4"},
    "settings": {
        "name": "ComfyUI - fox",
        "priority": 70,
        "targetTaskRunStatus": "suspended",
        "maxWorkerCount": "",
        "jobAttachmentsFileSystem": "VIRTUAL",
        "requirePathsExist": True,
    },
}


def test_submit_streams_progress_and_asks_to_confirm(deadline_api, monkeypatch, capsys):
    code, events = run(monkeypatch, capsys, "submit", SUBMISSION, answers=[{"accept": True}])
    assert code == 0
    assert [event["event"] for event in events] == [
        "progress",
        "progress",
        "message",
        "confirm",
        "creating",
        "created",
        "result",
    ]
    assert events[5] == {
        "event": "created",
        "jobId": "job-1",
        "farmId": "farm-2",
        "queueId": "queue-2",
    }
    assert events[0] == {
        "event": "progress",
        "stage": "hashing",
        "progress": 50.0,
        "message": "Uploading 1 of 2",
    }
    assert events[3] == {"event": "confirm", "message": "Upload 2 files?", "default": True}
    assert events[-1]["data"] == {"jobId": "job-1", "farmId": "farm-2", "queueId": "queue-2"}

    _, bundle_dir, kwargs = deadline_api.api.calls[-1]
    assert bundle_dir == "/bundles/fox"
    assert kwargs["job_parameters"] == [
        {"name": "OutputDir", "value": "/out"},
        {"name": "SeedItems", "value": "1-4"},
    ]
    assert kwargs["name"] == "ComfyUI - fox"
    assert kwargs["priority"] == 70
    assert kwargs["target_task_run_status"] == "SUSPENDED"
    assert kwargs["job_attachments_file_system"] == "VIRTUAL"
    assert kwargs["require_paths_exist"] is True
    # An empty setting leaves the queue's default.
    assert "max_worker_count" not in kwargs
    assert kwargs["submitter_name"] == "ComfyUI"
    config = kwargs["config"]
    assert (config["defaults.farm_id"], config["defaults.queue_id"]) == ("farm-2", "queue-2")
    assert config["settings.storage_profile_id"] == "sp-1"
    # The farm and queue are for this job only, not saved as the defaults.
    assert deadline_api.config_file.written is None


@pytest.mark.parametrize(
    "answers",
    [
        [{"accept": False}],
        [],
        ["not an object"],
        # Only true is a yes, not other values that are truthy.
        [{"accept": "yes"}],
        [{"accept": 1}],
        [{"accept": "false"}],
    ],
)
def test_submit_stops_unless_confirmed(deadline_api, monkeypatch, capsys, answers):
    code, events = run(monkeypatch, capsys, "submit", SUBMISSION, answers=answers)
    assert code == 1
    assert events[-1] == {"event": "error", "message": "Canceled", "type": "RuntimeError"}


def test_rejects_unknown_commands_and_bad_requests(deadline_api, monkeypatch, capsys):
    code, events = run(monkeypatch, capsys, "delete-farm")
    assert code == 2
    assert events[0]["event"] == "error"
    monkeypatch.setattr(sys, "stdin", io.StringIO("[1]\n"))
    assert bridge.main(["bridge.py", "farms"]) == 1
    assert "isn't a JSON object" in capsys.readouterr().out


def test_submit_keeps_library_output_off_the_events(deadline_api, monkeypatch, capsys):
    create = deadline_api.api.create_job_from_job_bundle

    def noisy(job_bundle_dir, **kwargs):
        print("Submitting to Queue: GPU")
        return create(job_bundle_dir, **kwargs)

    monkeypatch.setattr(deadline_api.api, "create_job_from_job_bundle", noisy)
    lines = [json.dumps(SUBMISSION), json.dumps({"accept": True})]
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(lines) + "\n"))
    assert bridge.main(["bridge.py", "submit"]) == 0
    out, err = capsys.readouterr()
    assert all(json.loads(line)["event"] for line in out.splitlines())
    assert "Submitting to Queue: GPU" in err


def test_progress_events_are_throttled(monkeypatch, capsys):
    clock = iter([0.0, 0.5, 1.2, 1.3])
    monkeypatch.setattr(bridge.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(bridge, "_protocol", sys.stdout)
    report = bridge._progress("uploading")
    for progress in (10, 20, 30, 100):
        assert report(types.SimpleNamespace(progress=progress, progressMessage=""))
    reported = [json.loads(line)["progress"] for line in capsys.readouterr().out.splitlines()]
    # 20 came too soon after 10, but 100 always shows.
    assert reported == [10, 30, 100]


def _bundle(tmp_path, parameter_type):
    (tmp_path / "template.yaml").write_text(
        f"parameterDefinitions:\n- name: Attach\n  type: {parameter_type}\n"
    )
    return str(tmp_path)


def _fake_sdk(monkeypatch, members):
    model = types.SimpleNamespace(
        shape_for=lambda name: types.SimpleNamespace(members=members),
    )
    session = types.SimpleNamespace(get_service_model=lambda name: model)
    sdk_session = types.SimpleNamespace(get_session=lambda: session)
    botocore = types.SimpleNamespace(__version__="1.2.3", session=sdk_session)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.session", sdk_session)


@pytest.mark.parametrize("parameter_type", ["BOOL", "bool"])
def test_submit_stops_before_uploading_if_the_sdk_cant_send_bool(
    deadline_api, monkeypatch, capsys, tmp_path, parameter_type
):
    _fake_sdk(monkeypatch, ["int", "float", "string", "path"])
    request = {**SUBMISSION, "bundleDir": _bundle(tmp_path, parameter_type)}
    code, events = run(monkeypatch, capsys, "submit", request)
    assert code == 1
    assert "botocore 1.2.3" in events[-1]["message"]
    assert "pip install --upgrade boto3" in events[-1]["message"]
    assert deadline_api.api.calls == []


def test_submit_goes_ahead_when_the_sdk_can_send_bool(deadline_api, monkeypatch, capsys, tmp_path):
    _fake_sdk(monkeypatch, ["int", "float", "string", "path", "bool"])
    request = {**SUBMISSION, "bundleDir": _bundle(tmp_path, "BOOL")}
    code, _ = run(monkeypatch, capsys, "submit", request, answers=[{"accept": True}])
    assert code == 0


def test_submit_doesnt_need_a_newer_sdk_without_a_bool_parameter(
    deadline_api, monkeypatch, capsys, tmp_path
):
    _fake_sdk(monkeypatch, ["int", "float", "string", "path"])
    request = {**SUBMISSION, "bundleDir": _bundle(tmp_path, "STRING")}
    code, _ = run(monkeypatch, capsys, "submit", request, answers=[{"accept": True}])
    assert code == 0


def test_submit_reports_a_created_job_before_a_later_failure(deadline_api, monkeypatch, capsys):
    """Waiting for the job or a hook can fail after CreateJob, and the job still runs."""

    def timed_out():
        raise RuntimeError("Timed out waiting for the job to be created")

    deadline_api.api.after_create_job = timed_out
    code, events = run(monkeypatch, capsys, "submit", SUBMISSION, answers=[{"accept": True}])
    assert code == 1
    assert [event["event"] for event in events][-3:] == ["creating", "created", "error"]
    assert events[-2]["jobId"] == "job-1"


def test_submit_registers_its_job_creation_events_once(deadline_api, monkeypatch, capsys):
    """A cached client gets the handlers once, however often the library asks for it."""
    run(monkeypatch, capsys, "submit", SUBMISSION, answers=[{"accept": True}])
    deadline_api.api.get_boto3_client("deadline")
    run(monkeypatch, capsys, "submit", SUBMISSION, answers=[{"accept": True}])
    handlers = deadline_api.api.client.meta.events.handlers
    assert len(handlers["before-call.deadline.CreateJob"]) == 1
    assert len(handlers["after-call.deadline.CreateJob"]) == 1


def _embedded_python_path(tmp_path: Path) -> list[str]:
    """sys.path of ComfyUI portable's embedded Python, after its ._pth file imported site."""
    python = tmp_path / "python_embeded"
    site_packages = python / "Lib" / "site-packages"
    return [
        str(python / "python313.zip"),
        str(python),
        str(tmp_path / "ComfyUI"),
        str(site_packages),
        # pywin32.pth in ComfyUI's site-packages adds folders inside it.
        str(site_packages / "win32" / "lib"),
    ]


def _dependencies(tmp_path: Path) -> Path:
    dependencies = tmp_path / "dependencies"
    (dependencies / "win32").mkdir(parents=True)
    (dependencies / "pywin32.pth").write_text("win32\n")
    return dependencies


def test_dependencies_replace_comfyuis_site_packages_when_site_ran(monkeypatch, tmp_path):
    """On ComfyUI portable, a ._pth file runs site despite -S, so ComfyUI's packages are dropped."""
    path = _embedded_python_path(tmp_path)
    monkeypatch.setattr(sys, "path", list(path))
    dependencies = _dependencies(tmp_path)

    bridge._use_dependencies(str(dependencies), site_ran=True)

    assert sys.path == [*path[:3], str(dependencies), str(dependencies / "win32")]


def test_dependencies_are_added_after_the_path_when_site_didnt_run(monkeypatch, tmp_path):
    """-S kept site from running, so sys.path has no site-packages to drop."""
    path = _embedded_python_path(tmp_path)[:3]
    monkeypatch.setattr(sys, "path", list(path))
    dependencies = _dependencies(tmp_path)

    bridge._use_dependencies(str(dependencies), site_ran=False)

    assert sys.path == [*path, str(dependencies), str(dependencies / "win32")]
