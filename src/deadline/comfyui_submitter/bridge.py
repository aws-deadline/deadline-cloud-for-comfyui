# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Calls the Deadline Cloud client library for ComfyUI's submitter panel. ComfyUI runs this script with
the Deadline Cloud CLI's own Python, `python -I bridge.py <command>`, so ComfyUI's environment
doesn't need the library or boto3. When the installer has unpacked the library next to the
extension, ComfyUI's Python runs it instead, `python -I -S bridge.py --dependencies <folder>
<command>`, where -S leaves ComfyUI's packages out. It reads the request as a JSON object on the
first line of stdin, and writes one JSON object per line to stdout: progress events while it works,
then a "result" or an "error" event. While submitting, it reads each confirmation's answer as a
further line of stdin.

It only imports the standard library and the `deadline` package, never the rest of this extension.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import site
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Job settings that create_job_from_job_bundle takes, by the names the panel sends.
JOB_SETTINGS = {
    "priority": "priority",
    "maxFailedTasksCount": "max_failed_tasks_count",
    "maxRetriesPerTask": "max_retries_per_task",
    "maxWorkerCount": "max_worker_count",
    "targetTaskRunStatus": "target_task_run_status",
    "jobAttachmentsFileSystem": "job_attachments_file_system",
    "requirePathsExist": "require_paths_exist",
}
SUBMITTER_NAME = "ComfyUI"
# The fewest seconds between progress events of a stage, so a big upload doesn't flood ComfyUI.
PROGRESS_INTERVAL = 1.0
# The conda channel the comfy package is published to, which the other Deadline Cloud submitters
# add before the deadline-cloud channel (use_deadline_cloud_v2_channel in the library's dialog).
CONDA_CHANNELS_PARAMETER = "CondaChannels"
DEADLINE_CLOUD_CHANNEL = "deadline-cloud"
DEADLINE_CLOUD_V2_CHANNEL = "deadline-cloud-v2"

# Where events go. main() sets it to the real stdout, which the library can't print to while it
# submits.
_protocol = sys.stdout


def emit(event: str, **data: Any) -> None:
    print(json.dumps({"event": event, **data}), file=_protocol, flush=True)


def _config(request: dict[str, Any]):
    """The Deadline Cloud config, with the request's farm, queue and storage profile."""
    from deadline.client.config import config_file

    config = config_file.read_config()
    for key, setting in (
        ("farmId", "defaults.farm_id"),
        ("queueId", "defaults.queue_id"),
        ("storageProfileId", "settings.storage_profile_id"),
    ):
        if key in request:
            config_file.set_setting(setting, str(request[key] or ""), config=config)
    return config


def status(request: dict[str, Any]) -> dict[str, Any]:
    from deadline.client import api
    from deadline.client.config import config_file

    config = config_file.read_config()

    def setting(name: str) -> str:
        return config_file.get_setting(name, config=config)

    return {
        "profile": setting("defaults.aws_profile_name"),
        "farmId": setting("defaults.farm_id"),
        "queueId": setting("defaults.queue_id"),
        "storageProfileId": setting("settings.storage_profile_id"),
        "jobAttachmentsFileSystem": setting("defaults.job_attachments_file_system"),
        "credentialsSource": api.get_credentials_source(config=config).name,
        "authenticationStatus": api.check_authentication_status(config=config).name,
    }


def login(request: dict[str, Any]) -> dict[str, Any]:
    from deadline.client import api

    message = api.login(
        on_pending_authorization=lambda **kwargs: emit(
            "message", message="Finish logging in with Deadline Cloud monitor."
        ),
        on_cancellation_check=lambda: False,
        from_gui=True,
    )
    return {"message": message}


def farms(request: dict[str, Any]) -> dict[str, Any]:
    from deadline.client import api

    response = api.list_farms(config=_config({}))
    return {
        "farms": [
            {"farmId": farm["farmId"], "displayName": farm.get("displayName", farm["farmId"])}
            for farm in response.get("farms", [])
        ]
    }


def queues(request: dict[str, Any]) -> dict[str, Any]:
    from deadline.client import api

    response = api.list_queues(config=_config({}), farmId=request["farmId"])
    return {
        "queues": [
            {"queueId": queue["queueId"], "displayName": queue.get("displayName", queue["queueId"])}
            for queue in response.get("queues", [])
        ]
    }


def deadline_cloud_v2_channels(parameter: dict[str, Any]) -> str | None:
    """
    CondaChannels' default with deadline-cloud-v2 just before each deadline-cloud, as the library's
    dialog makes it, or None when that changes nothing or the parameter wouldn't take it.
    """
    default = parameter.get("default")
    if parameter.get("name") != CONDA_CHANNELS_PARAMETER or not isinstance(default, str):
        return None
    channels = default.split()
    if DEADLINE_CLOUD_CHANNEL not in channels or DEADLINE_CLOUD_V2_CHANNEL in channels:
        return None
    # Before each deadline-cloud, as the library does, so it takes precedence under strict priority.
    with_v2: list[str] = []
    for channel in channels:
        if channel == DEADLINE_CLOUD_CHANNEL:
            with_v2.append(DEADLINE_CLOUD_V2_CHANNEL)
        with_v2.append(channel)
    value = " ".join(with_v2)
    allowed = parameter.get("allowedValues")
    max_length = parameter.get("maxLength")
    if (isinstance(allowed, list) and value not in allowed) or (
        isinstance(max_length, int) and len(value) > max_length
    ):
        return None
    return value


def queue(request: dict[str, Any]) -> dict[str, Any]:
    """
    The queue's environment parameters and storage profiles, and the values the job sends for
    parameters the artist and the config don't set, such as Conda Channels with deadline-cloud-v2.
    """
    from deadline.client import api

    config = _config({})
    parameters = api.get_queue_parameter_definitions(
        farmId=request["farmId"], queueId=request["queueId"], config=config
    )
    profiles = api.list_storage_profiles_for_queue(
        config=config, farmId=request["farmId"], queueId=request["queueId"]
    )
    values = {}
    for parameter in parameters:
        channels = deadline_cloud_v2_channels(dict(parameter))
        if channels is not None:
            values[parameter["name"]] = channels
    return {
        "parameters": [dict(parameter) for parameter in parameters],
        "values": values,
        "storageProfiles": [
            {
                "storageProfileId": profile["storageProfileId"],
                "displayName": profile.get("displayName", profile["storageProfileId"]),
                "osFamily": profile.get("osFamily"),
            }
            for profile in profiles.get("storageProfiles", [])
        ],
    }


# What each saved default's ID looks like. An empty string clears it.
DEFAULT_IDS = {
    "farmId": re.compile(r"farm-[0-9a-f]{32}"),
    "queueId": re.compile(r"queue-[0-9a-f]{32}"),
    "storageProfileId": re.compile(r"sp-[0-9a-f]{32}"),
}


def set_defaults(request: dict[str, Any]) -> dict[str, Any]:
    """
    Saves the farm, queue and storage profile as the defaults. Every ID is checked before
    anything is saved: the config names sections after the farm ID.
    """
    from deadline.client.config import config_file

    for key, pattern in DEFAULT_IDS.items():
        value = request.get(key, "")
        if not isinstance(value, str) or (value and not pattern.fullmatch(value)):
            raise ValueError(f"{key} isn't a Deadline Cloud ID.")
    config_file.write_config(_config({key: request[key] for key in DEFAULT_IDS if key in request}))
    return {}


def _progress(stage: str) -> Callable[[Any], bool]:
    last = [float("-inf")]

    def report(metadata: Any) -> bool:
        progress = getattr(metadata, "progress", None)
        now = time.monotonic()
        if now - last[0] < PROGRESS_INTERVAL and progress != 100:
            return True
        last[0] = now
        emit(
            "progress",
            stage=stage,
            progress=progress,
            message=getattr(metadata, "progressMessage", ""),
        )
        return True

    return report


def _confirm(message: str, default: bool) -> bool:
    """
    Asks the panel, which answers with a line of stdin. Only {"accept": true} is a yes: no answer,
    or anything else, such as "yes" or 1, counts as no.
    """
    emit("confirm", message=message, default=default)
    line = sys.stdin.readline()
    try:
        return json.loads(line).get("accept") is True
    except (ValueError, AttributeError):
        return False


def _check_sdk_has_bool(bundle_dir: str) -> None:
    """
    Stops before uploading if this AWS SDK can't send the job's BOOL parameter. Older ones the
    library accepts only fail after the upload.
    """
    import yaml

    paths = [Path(bundle_dir) / name for name in ("template.yaml", "template.json")]
    path = next((path for path in paths if path.is_file()), None)
    if path is None:
        return
    template = yaml.safe_load(path.read_text(encoding="utf-8"))
    definitions = template.get("parameterDefinitions", [])
    if not any(str(definition.get("type")).upper() == "BOOL" for definition in definitions):
        return

    import botocore
    import botocore.session

    model = botocore.session.get_session().get_service_model("deadline")
    if "bool" not in model.shape_for("JobParameter").members:
        raise RuntimeError(
            f"This job needs a newer AWS SDK than botocore {botocore.__version__}. Update the "
            f"Deadline Cloud CLI's Python with: {sys.executable} -m pip install --upgrade boto3"
        )


def _report_job_creation(request: dict[str, Any]) -> None:
    """
    Emits "creating" just before the library calls CreateJob, and "created" with the job's ID once
    it returns. The library then waits for the job, prints its monitor URL and runs hooks before
    it returns, so without these a failure or Cancel in that time would hide a job that runs.
    """
    from deadline.client import api

    get_client = api.get_boto3_client

    def creating(**kwargs: Any) -> None:
        emit("creating")

    def created(parsed: Any = None, **kwargs: Any) -> None:
        job_id = parsed.get("jobId") if isinstance(parsed, dict) else None
        if isinstance(job_id, str):
            emit(
                "created",
                jobId=job_id,
                farmId=request.get("farmId"),
                queueId=request.get("queueId"),
            )

    def get_boto3_client(service_name: str, *args: Any, **kwargs: Any) -> Any:
        client = get_client(service_name, *args, **kwargs)
        if service_name == "deadline":
            # botocore's per-client events. unique_id keeps a cached client from getting two.
            events = client.meta.events
            events.register(
                "before-call.deadline.CreateJob", creating, unique_id="comfyui-creating"
            )
            events.register("after-call.deadline.CreateJob", created, unique_id="comfyui-created")
        return client

    api.get_boto3_client = get_boto3_client


def submit(request: dict[str, Any]) -> dict[str, Any]:
    from deadline.client import api

    _check_sdk_has_bool(request["bundleDir"])
    _report_job_creation(request)
    settings = request.get("settings", {})
    options = {
        argument: settings[key]
        for key, argument in JOB_SETTINGS.items()
        if settings.get(key) not in (None, "")
    }
    if "target_task_run_status" in options:
        options["target_task_run_status"] = str(options["target_task_run_status"]).upper()
    # Anything the library prints goes to stderr, so it can't break the events on stdout.
    with contextlib.redirect_stdout(sys.stderr):
        job_id = api.create_job_from_job_bundle(
            request["bundleDir"],
            job_parameters=[
                {"name": name, "value": value}
                for name, value in request.get("parameters", {}).items()
            ],
            name=settings.get("name") or None,
            config=_config(request),
            submitter_name=SUBMITTER_NAME,
            from_gui=True,
            print_function_callback=lambda message: emit("message", message=str(message)),
            interactive_confirmation_callback=_confirm,
            hashing_progress_callback=_progress("hashing"),
            upload_progress_callback=_progress("uploading"),
            create_job_result_callback=lambda: True,
            **options,
        )
    return {"jobId": job_id, "farmId": request.get("farmId"), "queueId": request.get("queueId")}


COMMANDS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "status": status,
    "login": login,
    "farms": farms,
    "queues": queues,
    "queue": queue,
    "set-defaults": set_defaults,
    "submit": submit,
}


def _use_dependencies(folder: str, site_ran: bool = not sys.flags.no_site) -> None:
    """
    Adds the installer's dependencies to sys.path. routes.py starts the bridge with -S, so that
    ComfyUI's packages stay out, but the embedded Python of ComfyUI's Windows portable build has a
    ._pth file with `import site`, which turns site processing back on. Then ComfyUI's
    site-packages, and the folders its .pth files added, are dropped from sys.path here instead.
    """
    if site_ran:
        sys.path[:] = [path for path in sys.path if not _in_site_packages(path)]
    # addsitedir also runs the folder's .pth files, as an installed site-packages would.
    site.addsitedir(folder)


def _in_site_packages(path: str) -> bool:
    parts = Path(os.path.normcase(os.path.abspath(path))).parts
    return any(part in ("site-packages", "dist-packages") for part in parts)


def main(argv: list[str]) -> int:
    global _protocol
    _protocol = sys.stdout
    args = argv[1:]
    if len(args) == 3 and args[0] == "--dependencies":
        _use_dependencies(args[1])
        args = args[2:]
    if len(args) != 1 or args[0] not in COMMANDS:
        emit("error", message=f"Usage: bridge.py [--dependencies FOLDER] {{{','.join(COMMANDS)}}}")
        return 2
    try:
        request = json.loads(sys.stdin.readline() or "{}")
        if not isinstance(request, dict):
            raise TypeError("The request isn't a JSON object.")
        emit("result", data=COMMANDS[args[0]](request))
    except Exception as e:  # noqa: BLE001 - every failure goes back to ComfyUI as an event.
        emit("error", message=str(e) or type(e).__name__, type=type(e).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
