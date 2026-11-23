# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Calls the Deadline Cloud client library for ComfyUI's submitter panel. The native installer uses
ComfyUI's Python and a separate SDK directory. Source installations can use the CLI's Python.
The script reads the request as a JSON object on the first line of
stdin, and writes one JSON object per line to stdout: progress events while it works, then a
"result" or an "error" event. While submitting, it reads each confirmation's answer as a further
line of stdin.

It only imports the standard library and the `deadline` package, never the rest of this extension.
"""

from __future__ import annotations

import contextlib
import json
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
# How many jobs one status request can ask about.
MAX_JOBS = 50
# The fewest seconds between progress events of a stage, so a big upload doesn't flood ComfyUI.
PROGRESS_INTERVAL = 1.0

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


def queue(request: dict[str, Any]) -> dict[str, Any]:
    """The queue's environment parameters and storage profiles."""
    from deadline.client import api

    config = _config({})
    parameters = api.get_queue_parameter_definitions(
        farmId=request["farmId"], queueId=request["queueId"], config=config
    )
    profiles = api.list_storage_profiles_for_queue(
        config=config, farmId=request["farmId"], queueId=request["queueId"]
    )
    return {
        "parameters": [dict(parameter) for parameter in parameters],
        "storageProfiles": [
            {
                "storageProfileId": profile["storageProfileId"],
                "displayName": profile.get("displayName", profile["storageProfileId"]),
                "osFamily": profile.get("osFamily"),
            }
            for profile in profiles.get("storageProfiles", [])
        ],
    }


def set_defaults(request: dict[str, Any]) -> dict[str, Any]:
    """Saves the farm, queue and storage profile as the defaults."""
    from deadline.client.config import config_file

    config_file.write_config(_config(request))
    return {}


# Errors that mean the job is gone, or that these credentials can't see it, rather than a failure
# worth trying again, such as throttling or expired credentials.
JOB_GONE_ERRORS = {"ResourceNotFoundException", "AccessDeniedException"}


def jobs(request: dict[str, Any]) -> dict[str, Any]:
    from deadline.client import api

    config = _config({})
    found = []
    for job_id in list(request.get("jobIds", []))[:MAX_JOBS]:
        try:
            job = api.get_job(
                farmId=request["farmId"], queueId=request["queueId"], jobId=job_id, config=config
            )
        except Exception as e:  # noqa: BLE001 - one missing job mustn't hide the others.
            response = getattr(e, "response", None)
            code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
            status: dict[str, Any] = {"jobId": job_id, "error": str(e)}
            if code in JOB_GONE_ERRORS:
                status["notFound"] = True
            found.append(status)
            continue
        found.append(
            {
                "jobId": job_id,
                "name": job.get("name"),
                "lifecycleStatus": job.get("lifecycleStatus"),
                "taskRunStatus": job.get("taskRunStatus"),
                "taskRunStatusCounts": job.get("taskRunStatusCounts", {}),
            }
        )
    return {"jobs": found}


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
    """Asks the panel, which answers with a line of stdin. No answer counts as no."""
    emit("confirm", message=message, default=default)
    line = sys.stdin.readline()
    try:
        return bool(json.loads(line).get("accept"))
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


def submit(request: dict[str, Any]) -> dict[str, Any]:
    from deadline.client import api

    _check_sdk_has_bool(request["bundleDir"])
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
    "jobs": jobs,
    "submit": submit,
}


def main(argv: list[str]) -> int:
    global _protocol
    _protocol = sys.stdout
    args = argv[1:]
    sdk = None
    if len(args) == 3 and args[0] == "--sdk":
        sdk = Path(args[1])
        args = args[2:]
    if len(args) != 1 or args[0] not in COMMANDS:
        emit("error", message=f"Usage: bridge.py [--sdk DIRECTORY] {{{','.join(COMMANDS)}}}")
        return 2
    try:
        if sdk is not None:
            packages = sdk
            if sys.platform == "win32":
                packages = sdk / f"{sys.version_info.major}.{sys.version_info.minor}"
            if not packages.is_dir():
                raise RuntimeError(
                    f"The Deadline Cloud SDK is missing for Python {sys.version_info.major}."
                    f"{sys.version_info.minor}. Use ComfyUI with Python 3.13 or 3.14 and "
                    "check the submitter installation."
                )
            # Apply only the bundled .pth files, including pywin32's DLL setup on Windows.
            site.addsitedir(str(packages))
            if sys.platform == "win32":
                site.addsitedir(str(sdk / "common"))
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
