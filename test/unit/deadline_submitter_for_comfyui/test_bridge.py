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

import pytest

from deadline.comfyui_submitter import bridge


class Status(enum.Enum):
    AUTHENTICATED = 2


class Source(enum.Enum):
    DEADLINE_CLOUD_MONITOR_LOGIN = 3


class FakeApi:
    def __init__(self):
        self.calls = []

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

    def get_job(self, farmId, queueId, jobId, config):
        if jobId == "job-gone":
            raise RuntimeError("Not found")
        return {
            "name": "fox",
            "lifecycleStatus": "CREATE_COMPLETE",
            "taskRunStatus": "RUNNING",
            "taskRunStatusCounts": {"SUCCEEDED": 3, "RUNNING": 1},
        }

    def create_job_from_job_bundle(self, job_bundle_dir, **kwargs):
        self.calls.append(("create_job_from_job_bundle", job_bundle_dir, kwargs))
        metadata = types.SimpleNamespace(progress=50.0, progressMessage="Uploading 1 of 2")
        kwargs["hashing_progress_callback"](metadata)
        kwargs["upload_progress_callback"](metadata)
        kwargs["print_function_callback"]("Submitting")
        if not kwargs["interactive_confirmation_callback"]("Upload 2 files?", True):
            raise RuntimeError("Canceled")
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
        "storageProfiles": [{"storageProfileId": "sp-1", "displayName": "Mac", "osFamily": None}],
    }


def test_set_defaults_saves_the_farm_queue_and_storage_profile(deadline_api, monkeypatch, capsys):
    request = {"farmId": "farm-2", "queueId": "queue-2", "storageProfileId": "sp-1"}
    code, _ = run(monkeypatch, capsys, "set-defaults", request)
    assert code == 0
    written = deadline_api.config_file.written
    assert written["defaults.farm_id"] == "farm-2"
    assert written["defaults.queue_id"] == "queue-2"
    assert written["settings.storage_profile_id"] == "sp-1"


def test_jobs_reports_each_job(deadline_api, monkeypatch, capsys):
    request = {"farmId": "farm-1", "queueId": "queue-1", "jobIds": ["job-1", "job-gone"]}
    _, events = run(monkeypatch, capsys, "jobs", request)
    assert events[-1]["data"]["jobs"] == [
        {
            "jobId": "job-1",
            "name": "fox",
            "lifecycleStatus": "CREATE_COMPLETE",
            "taskRunStatus": "RUNNING",
            "taskRunStatusCounts": {"SUCCEEDED": 3, "RUNNING": 1},
        },
        {"jobId": "job-gone", "error": "Not found"},
    ]


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
        "result",
    ]
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


@pytest.mark.parametrize("answers", [[{"accept": False}], [], ["not an object"]])
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
