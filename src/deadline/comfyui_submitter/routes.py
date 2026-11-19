# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
The Deadline Cloud routes on the ComfyUI server. ComfyUI's submitter panel plans a submission with
/deadline/plan, calls the Deadline Cloud client library through /deadline/cloud/<command>, and
submits with /deadline/jobs. The library runs in the Deadline Cloud CLI's own Python, through
bridge.py. Background tasks report progress and results over the websocket.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import folder_paths
import yaml
from aiohttp import web
from server import PromptServer

from .get_local_assets import (
    LocalAssets,
    find_workflow_local_models,
    get_local_assets,
    model_name,
    stage_local_models,
    stage_local_node_packs,
    write_asset_references,
)
from .submit import (
    ALLOW_REMOTE_ENV_VAR,
    DEFAULT_WORKFLOW_NAME,
    FOLDER_LOADERS,
    PICKER_KINDS,
    VALUE_NODES,
    ItemSource,
    SubmitError,
    TaskLists,
    check_cli_version,
    check_item_range,
    check_parameter_values,
    create_job_bundle,
    first_line,
    host_requirements,
    input_folders,
    job_description,
    load_config,
    local_node_packs,
    parse_bool,
    picker_command,
    plan_task_lists,
    read_job_template,
    remote_allowed,
    resolve_cli_python,
    resolve_conda_channels,
    resolve_deadline_cli,
    resolve_job_template,
    template_defines_parameter,
    template_has_own_task_parameters,
    template_host_requirements,
    template_supports_task_lists,
    validate_api_workflow,
    workflow_models,
    write_private_text,
)

logger = logging.getLogger(__name__)

SUBMITTED_EVENT = "deadline.submitted"
PROGRESS_EVENT = "deadline.progress"
CONFIRM_EVENT = "deadline.confirm"
# The job template's switch for loading the attached custom node packs on the worker.
ATTACH_NODES = "AttachCustomNodes"
DOWNLOAD_EVENT = "deadline.download"
# A farm, queue or job ID, which goes on the CLI's command line.
_RESOURCE_ID = re.compile(r"(farm|queue|job)-[0-9a-f]{32}")
# download-output's JSON summary is one line that lists every downloaded file, so it can be far
# longer than asyncio's default 64 KiB line limit.
DOWNLOAD_LINE_LIMIT = 64 * 1024 * 1024
# The job settings that a saved bundle keeps as deadline:<name> parameter values, which `deadline
# bundle submit` reads.
SAVED_JOB_SETTINGS = (
    "priority",
    "targetTaskRunStatus",
    "maxFailedTasksCount",
    "maxRetriesPerTask",
    "maxWorkerCount",
)
_SUBMISSION_ID = re.compile(r"[0-9a-f]{32}")

BRIDGE = Path(__file__).resolve().parent / "bridge.py"
# The bridge commands the panel can run directly.
CLOUD_COMMANDS = ("status", "login", "farms", "queues", "queue", "set-defaults", "jobs")
BRIDGE_TIMEOUT = 120
# A first run of deadline --version can be slow, but it shouldn't hang.
CLI_VERSION_TIMEOUT = 60
# Logging in waits for the artist to finish in Deadline Cloud monitor.
LOGIN_TIMEOUT = 600
# A confirmation nobody answers, for example after its tab closed, is declined after this long, so
# the submission ends instead of waiting forever.
CONFIRM_TIMEOUT = 600
# A bundle made for a submission holds this file. The bundle is deleted when its submission is
# done, or if that didn't happen, such as when ComfyUI stopped, when ComfyUI next starts after a
# day. Bundles saved to keep don't have it.
SUBMISSION_MARKER = ".deadline-submission"
OLD_SUBMISSION_AGE = 24 * 60 * 60
# The queue environment parameter that the config's conda_channels fills in.
CONDA_CHANNELS_PARAMETER = "CondaChannels"
# How much of each source's items the plan sends for previews.
MAX_PREVIEW_VALUES = 100
MAX_PREVIEW_LENGTH = 120

# Keeps a reference to each background task so it isn't garbage collected while it runs.
_watchers: set[asyncio.Task] = set()

_cli_versions: dict[str, str] = {}


@dataclass
class _Submission:
    name: str
    process: asyncio.subprocess.Process
    canceled: bool = False
    # The browser tab that submitted it, which alone gets its events.
    client_id: str | None = None
    confirms: int = 0
    answered: int = 0
    # Timers that decline unanswered confirmations, canceled when the submission ends.
    timers: list[asyncio.Task] = field(default_factory=list)
    # The job bundle, deleted once the job is created or the submission is canceled.
    bundle_dir: Path | None = None
    # Where the job's outputs download to, shown with the job in the panel.
    output_dir: str | None = None


_submissions: dict[str, _Submission] = {}


@dataclass
class _Prepared:
    """What a workflow submits with, before the job bundle exists."""

    # The workflow to submit: with its lists marked, when the template runs them as tasks.
    workflow: dict[str, Any]
    items: TaskLists | None = None
    # Job parameter values the submitter fills in. WorkflowFile isn't known until the bundle exists.
    values: dict[str, Any] = field(default_factory=dict)
    conda_channels: str | None = None
    warnings: list[str] = field(default_factory=list)
    # Local files for the models the workflow loads, and the model folder of each, by model ID:
    # the folder and the name the workflow loads it by.
    local_models: dict[str, Path] = field(default_factory=dict)
    model_directories: dict[str, str] = field(default_factory=dict)
    model_warnings: list[str] = field(default_factory=list)
    # The custom node packs the workflow uses, by folder or file name.
    local_nodes: dict[str, Path] = field(default_factory=dict)
    installed_nodes: _InstalledNodes | None = None
    node_warnings: list[str] = field(default_factory=list)


@dataclass
class _Staged:
    """What was placed in the job bundle to attach, and the folders the job maps them through."""

    model_dir: Path | None = None
    node_dir: Path | None = None
    filenames: list[Path] = field(default_factory=list)
    directories: list[Path] = field(default_factory=list)


@dataclass
class _InstalledNodes:
    # Node type to the custom node pack folder, or single .py file, ComfyUI loaded it from.
    dirs: dict[str, Path] = field(default_factory=dict)
    # Node types that come from a custom node module, whether or not its files were found.
    custom: set[str] = field(default_factory=set)


def _installed_node_packs(workflow: dict[str, Any]) -> _InstalledNodes:
    """
    Finds the custom node pack, and its folder, that ComfyUI loaded each workflow node from.
    Both the legacy NODE_CLASS_MAPPINGS packs and V3 extension packs (a comfy_entrypoint that
    returns a ComfyExtension) are found. The value nodes are left out: the job replaces them, so
    workers don't need this extension.
    """
    installed = _InstalledNodes()
    try:
        import nodes
    except ImportError:
        return installed
    for class_type in {node["class_type"] for node in workflow.values()} - set(VALUE_NODES):
        name = _custom_node_pack_name(nodes.NODE_CLASS_MAPPINGS.get(class_type))
        if name is None:
            continue
        installed.custom.add(class_type)
        pack_path = _node_pack_path(nodes, name)
        if pack_path is None:
            continue
        installed.dirs[class_type] = pack_path
    return installed


def _custom_node_pack_name(cls: Any) -> str | None:
    """
    The name of the custom node pack ComfyUI loaded this node class from, or None when the node
    isn't from a custom node pack. ComfyUI tags each class it loads with RELATIVE_PYTHON_MODULE,
    "custom_nodes." and the pack's name, which can contain dots, such as "custom_nodes.Pack.v2".
    V3 extension classes loaded by a ComfyUI release that doesn't tag them still have the pack's
    path as their __module__, such as "/ComfyUI/custom_nodes/Pack_x_v2.nodes". ComfyUI writes
    the dots in a pack folder's name as "_x_" there.
    """
    module = str(getattr(cls, "RELATIVE_PYTHON_MODULE", "") or "")
    prefix = "custom_nodes."
    if module.startswith(prefix) and len(module) > len(prefix):
        return module[len(prefix) :]
    path_parts = re.split(r"[/\\]", str(getattr(cls, "__module__", "") or ""))
    for index, part in enumerate(path_parts[:-1]):
        if part == "custom_nodes":
            pack = path_parts[index + 1].split(".")[0].replace("_x_", ".")
            return pack or None
    return None


def _node_pack_path(nodes: Any, name: str) -> Path | None:
    """
    The custom node pack ComfyUI loaded as `name`: its folder, or its .py file for a single-file
    custom node. A single-file node records the custom_nodes folder itself in LOADED_MODULE_DIRS,
    so the folder check refuses it and the file check finds <name>.py beside it.
    """
    roots = []
    loaded = getattr(nodes, "LOADED_MODULE_DIRS", {}).get(name)
    if loaded:
        candidate = Path(loaded)
        # A single-file custom node's entry is the custom_nodes folder, not the pack name.
        if candidate.name == name and candidate.is_dir():
            return candidate
        roots.append(candidate)
    entry = getattr(folder_paths, "folder_names_and_paths", {}).get("custom_nodes")
    roots.extend(Path(root) for root in (entry[0] if entry else []))
    for root in roots:
        candidate = root / name
        if candidate.is_dir():
            return candidate
    for root in roots:
        candidate = root / f"{name}.py"
        if candidate.is_file():
            return candidate
    return None


async def _cli_version_output(cli: str) -> str:
    if cli not in _cli_versions:
        process = await asyncio.create_subprocess_exec(
            cli,
            "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), CLI_VERSION_TIMEOUT)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        _cli_versions[cli] = stdout.decode(errors="replace")
    return _cli_versions[cli]


async def _checked_cli(config: dict[str, Any]) -> str | web.Response:
    """Returns the Deadline Cloud CLI, once it's a version the submitter supports."""
    try:
        cli = resolve_deadline_cli(config)
    except SubmitError as e:
        return _error(400, str(e))
    try:
        check_cli_version(cli, await _cli_version_output(cli))
    except SubmitError as e:
        # Check again next time, so upgrading the CLI doesn't need a ComfyUI restart.
        _cli_versions.pop(cli, None)
        return _error(400, str(e))
    except (TimeoutError, OSError) as e:
        return _error(500, f"Couldn't run the Deadline Cloud CLI: {e}")
    return cli


def _is_loopback(remote: str | None) -> bool:
    try:
        return remote is not None and ipaddress.ip_address(remote).is_loopback
    except ValueError:
        return False


def _is_local_host(host: str | None) -> bool:
    """Whether a Host header names this machine by localhost or a loopback address."""
    if host is None:
        return False
    host = host.strip("[]").rstrip(".").lower()
    return host == "localhost" or host.endswith(".localhost") or _is_loopback(host)


_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _client_id(body: dict[str, Any]) -> str | None:
    """The websocket client ID of the browser tab that sent a request, if it sent one."""
    client_id = body.get("clientId")
    return client_id if isinstance(client_id, str) and _CLIENT_ID.fullmatch(client_id) else None


def _send(event: str, data: dict[str, Any], client_id: str | None) -> None:
    """
    Sends an event to the browser tab that started the work. Without its ID, as from an older
    panel, every connected tab gets it.
    """
    PromptServer.instance.send_sync(event, data, client_id)


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _log_value(value: object) -> str:
    """A value from a request, with its line breaks escaped so it can't forge log entries."""
    return str(value).replace("\r\n", "\\r\\n").replace("\n", "\\n").replace("\r", "\\r")


def _log_block(text: str) -> str:
    """Multi-line output for a log entry, indented so its lines can't pass for log entries."""
    return text.rstrip().replace("\r\n", "\n").replace("\n", "\n    ").replace("\r", "\\r")


# Headers that a reverse proxy adds. A proxy on this machine connects from a loopback address, so
# without them the browsers it forwards would pass for local ones.
_FORWARDING_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Real-IP")


async def _read_body(request: web.Request) -> dict[str, Any] | web.Response:
    """Checks that a request comes from an allowed browser and returns its JSON object body."""
    # The routes act with this machine's AWS credentials, so they accept only ComfyUI's own page
    # or a request no page sent. --enable-cors-header turns off ComfyUI's origin check, and
    # "same-site" includes other ports on this machine.
    if request.headers.get("Sec-Fetch-Site", "none") not in ("same-origin", "none"):
        return _error(403, "Deadline Cloud doesn't accept requests from other websites.")
    forwarded = any(header in request.headers for header in _FORWARDING_HEADERS)
    if (forwarded or not _is_loopback(request.remote)) and not remote_allowed():
        return _error(
            403,
            "Deadline Cloud submits from the ComfyUI server, with its credentials, so it only "
            "accepts requests from a browser on that machine, not through a proxy. Set "
            f"{ALLOW_REMOTE_ENV_VAR}=1 to allow others.",
        )
    # A website can point its own name at 127.0.0.1, so that its page counts as same-origin with
    # ComfyUI. Its name is still in the Host header.
    if not _is_local_host(request.url.host) and not remote_allowed():
        return _error(
            403,
            "Deadline Cloud only accepts requests to localhost or a loopback address. Set "
            f"{ALLOW_REMOTE_ENV_VAR}=1 to allow other host names.",
        )
    if request.content_type != "application/json":
        return _error(415, "Expected a JSON request.")
    try:
        body = await request.json()
    except ValueError:
        return _error(400, "The request body isn't valid JSON.")
    if not isinstance(body, dict):
        return _error(400, "The request body isn't a JSON object.")
    return body


async def _read_request(
    request: web.Request,
) -> tuple[dict[str, Any], dict[str, Any], Path] | web.Response:
    """Checks a request and returns its body, its API-format workflow and the job template."""
    body = await _read_body(request)
    if isinstance(body, web.Response):
        return body
    try:
        workflow = validate_api_workflow(body.get("workflow"), body.get("ui_workflow"))
        template = resolve_job_template()
    except SubmitError as e:
        return _error(400, str(e))
    return body, workflow, template


def _workflow_name(body: dict[str, Any]) -> str:
    name = str(body.get("name") or DEFAULT_WORKFLOW_NAME)
    return name[: -len(".json")] if name.lower().endswith(".json") else name


def _has_lists(workflow: dict[str, Any]) -> bool:
    """Whether the workflow has nodes whose items could run as separate tasks."""
    return any(
        node.get("class_type") in ("CreateList", *FOLDER_LOADERS) for node in workflow.values()
    )


def _prepare(
    body: dict[str, Any], workflow: dict[str, Any], template: Path, config: dict[str, Any]
) -> _Prepared:
    """
    Plans the task lists, finds what the workflow needs attached and fills in the parameters.
    Raises SubmitError.
    """
    input_dir = Path(folder_paths.get_input_directory())
    # A template without the ITEMS line in its task script, or whose step has task parameters of
    # its own, runs the workflow as one task.
    items = None
    if template_supports_task_lists(template):
        reserved = {
            str(parameter.get("name"))
            for parameter in read_job_template(template).get("parameterDefinitions") or []
            if isinstance(parameter, dict)
        }
        items = plan_task_lists(workflow, input_dir, reserved)
    # The task script replaces value nodes, and the extension that defines them isn't attached.
    if items is None and any(
        isinstance(node, dict) and node.get("class_type") in VALUE_NODES
        for node in workflow.values()
    ):
        reason = (
            "its step already has task parameters"
            if template_has_own_task_parameters(template)
            else "its task script doesn't select each task's items"
        )
        raise SubmitError(
            f"The job template {template.name} can't run Deadline value nodes: {reason}. Use the "
            "submitter's job template, or remove the value nodes."
        )
    prepared = _Prepared(
        workflow=items.workflow if items else workflow,
        items=items,
        conda_channels=resolve_conda_channels(config),
    )
    if items is None and template_has_own_task_parameters(template) and _has_lists(workflow):
        prepared.warnings.append(
            f"The job template {template.name} already has task parameters, so the workflow's "
            "lists run whole in one task."
        )
    prepared.values["OutputDir"] = folder_paths.get_output_directory()
    prepared.values["InputDir"] = str(input_dir)
    # A template without Model Directory can't map attached models on the worker.
    if template_defines_parameter(template, "ModelDirectory"):
        prepared.local_models, prepared.model_directories = _local_model_paths(
            body.get("ui_workflow"), workflow
        )
    prepared.model_warnings = _model_warnings(workflow, prepared.local_models)
    prepared.warnings.extend(prepared.model_warnings)
    # A template without Custom Nodes Directory can't load attached packs on the worker.
    if template_defines_parameter(template, "CustomNodesDirectory"):
        installed = _installed_node_packs(workflow)
        prepared.local_nodes = local_node_packs(workflow, installed.dirs)
        prepared.installed_nodes = installed
        prepared.node_warnings = _node_warnings(installed, prepared.local_nodes)
        prepared.warnings.extend(prepared.node_warnings)
    return prepared


def _stage_prepared(
    prepared: _Prepared, workflow: dict[str, Any], bundle_dir: Path, attach_nodes: bool = True
) -> _Staged:
    """
    Stages the local models and custom node packs into the bundle, under the directories Deadline
    Cloud maps on the worker. Without attach_nodes, the job loads no attached custom nodes, so
    none are staged.
    """
    model_dir = bundle_dir / "local_models"
    staged = _stage_local_models(prepared.local_models, prepared.model_directories, model_dir)
    if set(staged) != set(prepared.local_models):
        # The plan counted on attaching these, so warn about the ones that couldn't be.
        prepared.warnings = [w for w in prepared.warnings if w not in prepared.model_warnings]
        prepared.model_warnings = _model_warnings(workflow, staged)
        prepared.warnings.extend(prepared.model_warnings)

    node_dir = bundle_dir / "local_nodes"
    nodes = _stage_local_node_packs(prepared.local_nodes, node_dir) if attach_nodes else {}
    if not attach_nodes or set(nodes) != set(prepared.local_nodes):
        prepared.warnings = [w for w in prepared.warnings if w not in prepared.node_warnings]
        prepared.node_warnings = (
            _node_warnings(prepared.installed_nodes, nodes)
            if attach_nodes and prepared.installed_nodes
            else []
        )
        prepared.warnings.extend(prepared.node_warnings)

    return _Staged(
        model_dir=model_dir if staged else None,
        node_dir=node_dir if nodes else None,
        filenames=[*staged.values(), *(path for path in nodes.values() if path.is_file())],
        directories=[path for path in nodes.values() if path.is_dir()],
    )


def _stage_local_models(
    models: dict[str, Path], directories: dict[str, str], model_dir: Path
) -> dict[str, Path]:
    """Stages local models into the bundle. A failure still lets the submission go ahead."""
    if not models:
        return {}
    try:
        return stage_local_models(models, directories, model_dir)
    except Exception:
        logger.exception("Couldn't stage local models")
        return {}


def _stage_local_node_packs(packs: dict[str, Path], node_dir: Path) -> dict[str, Path]:
    """Stages local custom node packs into the bundle. A failure still lets the job submit."""
    if not packs:
        return {}
    try:
        return stage_local_node_packs(packs, node_dir)
    except Exception:
        logger.exception("Couldn't stage local custom node packs")
        return {}


def _node_warnings(installed: _InstalledNodes, staged_nodes: dict[str, Path]) -> list[str]:
    """A warning for custom nodes whose files couldn't be attached."""
    missing = sorted(
        class_type
        for class_type in installed.custom
        if class_type not in installed.dirs or installed.dirs[class_type].name not in staged_nodes
    )
    if not missing:
        return []
    return [
        (
            f"The files of the custom nodes {', '.join(missing)} couldn't be attached. The job "
            "fails unless the workers already have them, for example in a custom nodes conda "
            "package."
        )
    ]


def _model_warnings(workflow: dict[str, Any], attached: dict[str, Path]) -> list[str]:
    """A warning for loaded models that aren't attached. attached is by model ID."""
    names = {model_name(key) for key in attached}
    missing = [name for name in workflow_models(workflow) if name not in names]
    if not missing:
        return []
    return [
        (
            f"{', '.join(missing)} won't be attached. The job fails unless the workers already "
            "have it, for example through Extra Model Paths Config."
        )
    ]


def _exclude_local_models(
    prepared: _Prepared,
    excluded: set[str],
    workflow: dict[str, Any],
) -> None:
    """
    Leaves out the local models the artist removed, by model ID, and warns about them like any
    others. A name on its own, from a panel older than model IDs, leaves out every model of that
    name.
    """
    # Only an entry that matches no model ID is a name on its own.
    names = excluded - set(prepared.local_models)
    kept = {
        key: path
        for key, path in prepared.local_models.items()
        if key not in excluded and model_name(key) not in names
    }
    if kept == prepared.local_models:
        return
    prepared.local_models = kept
    prepared.warnings = [w for w in prepared.warnings if w not in prepared.model_warnings]
    prepared.model_warnings = _model_warnings(workflow, prepared.local_models)
    prepared.warnings.extend(prepared.model_warnings)


def _local_model_paths(
    ui_workflow: Any, api_workflow: dict[str, Any]
) -> tuple[dict[str, Path], dict[str, str]]:
    """Local files for the models the workflow loads. A failed lookup finds none."""
    try:
        return find_workflow_local_models(
            ui_workflow, api_workflow, folder_paths.folder_names_and_paths
        )
    except Exception:
        logger.exception("Couldn't resolve local models")
        return {}, {}


def _local_assets(workflow: dict[str, Any]) -> LocalAssets:
    """The input files the workflow loads, with the folders its folder loaders read."""
    input_dir = folder_paths.get_input_directory()
    assets = get_local_assets(
        workflow,
        input_dir=input_dir,
        output_dir=folder_paths.get_output_directory(),
        temp_dir=folder_paths.get_temp_directory(),
    )
    assets.directories = sorted({*assets.directories, *input_folders(workflow, Path(input_dir))})
    return assets


def _save_asset_references(
    workflow: dict[str, Any],
    bundle_dir: Path,
    assets: LocalAssets | None = None,
    extra_filenames: Iterable[Path] | None = None,
    extra_directories: Iterable[Path] | None = None,
) -> None:
    """
    Saves the workflow's local input files, and the staged models and custom node packs in
    extra_filenames and extra_directories, to asset_references.yaml next to the job template.
    """
    try:
        chosen = assets if assets is not None else _local_assets(workflow)
        if extra_filenames:
            chosen.filenames = sorted(set(chosen.filenames).union(extra_filenames))
        if extra_directories:
            chosen.directories = sorted(set(chosen.directories).union(extra_directories))
        write_asset_references(chosen, bundle_dir / "asset_references.yaml")
    except Exception:
        # The submitter still works without the references. The artist can add files in its dialog.
        logger.exception("Couldn't save asset_references.yaml")


def _submissions_dir() -> Path:
    return Path(folder_paths.get_user_directory()) / "deadline_submissions"


def _remove_bundle(bundle_dir: Path | None) -> None:
    """
    Deletes a submission's job bundle, with its copies of models. Deadline Cloud has the job, and
    job attachments has its files, so the bundle isn't needed.
    """
    if bundle_dir is not None and (bundle_dir / SUBMISSION_MARKER).is_file():
        _delete_bundle(bundle_dir)


def _delete_bundle(bundle_dir: Path) -> None:
    """
    Deletes a marked bundle. What can't be deleted, such as a file another program has open, is
    marked again, so a later sweep tries again.
    """
    shutil.rmtree(bundle_dir, ignore_errors=True)
    if bundle_dir.exists():
        logger.warning("Couldn't delete all of the job bundle %s", _log_value(bundle_dir))
        try:
            write_private_text(bundle_dir / SUBMISSION_MARKER, "")
        except OSError:
            logger.warning("Couldn't mark %s to delete later", _log_value(bundle_dir))


def _remove_old_submissions(submissions_dir: Path, age: float = OLD_SUBMISSION_AGE) -> None:
    """Deletes submissions' bundles older than age seconds, such as from a failed submission."""
    cutoff = time.time() - age
    try:
        bundles = list(submissions_dir.iterdir())
    except OSError:
        return
    for bundle_dir in bundles:
        try:
            marker = bundle_dir / SUBMISSION_MARKER
            if marker.is_file() and marker.stat().st_mtime < cutoff:
                _delete_bundle(bundle_dir)
        except OSError:
            logger.warning("Couldn't delete the old job bundle %s", _log_value(bundle_dir))


def _start(coroutine) -> None:
    task = asyncio.create_task(coroutine)
    _watchers.add(task)
    task.add_done_callback(_watchers.discard)


def _plan(body: dict[str, Any], workflow: dict[str, Any], template: Path) -> dict[str, Any]:
    """Works out the panel's plan. It reads files, so it runs in a thread. Raises SubmitError."""
    prepared = _prepare(body, workflow, template, load_config())
    sources = prepared.items.active if prepared.items else []
    job_template = read_job_template(template, sources)
    assets = _local_assets(workflow)
    return {
        "name": _workflow_name(body),
        "template": template.name,
        "parameters": job_template.get("parameterDefinitions", []),
        "values": prepared.values,
        "queueValues": (
            {CONDA_CHANNELS_PARAMETER: prepared.conda_channels} if prepared.conda_channels else {}
        ),
        "sources": [
            {
                "name": source.name,
                "parameter": source.parameter,
                "label": source.label,
                "title": source.title,
                "kind": source.kind,
                # Integer fields hold the values themselves, so they need no preview.
                "values": (
                    []
                    if source.kind == "integer"
                    else [
                        first_line(value)[:MAX_PREVIEW_LENGTH]
                        for value in source.values[:MAX_PREVIEW_VALUES]
                    ]
                ),
                "count": source.count,
                "range": source.range,
                "pairWith": source.pair_with,
            }
            for source in sources
        ],
        "attachments": {
            "filenames": [str(path) for path in assets.filenames],
            "directories": [str(path) for path in assets.directories],
        },
        # Attached when submitting.
        "localModels": [
            {
                "id": key,
                "name": model_name(key),
                "directory": prepared.model_directories.get(key),
                "path": str(path),
            }
            for key, path in prepared.local_models.items()
        ],
        "hostRequirements": template_host_requirements(job_template),
        # Attached when submitting, unless the artist turns off Attach Custom Nodes.
        "localNodes": [
            {"name": name, "path": str(path)} for name, path in prepared.local_nodes.items()
        ],
        "warnings": prepared.warnings,
        # About splitting the lists into tasks, so only for Multiple tasks.
        "taskWarnings": prepared.items.warnings if prepared.items else [],
        "nodeWarnings": prepared.node_warnings,
    }


# One plan runs at a time, and one replaced by a newer plan from the same tab while it waits
# doesn't run, so slow storage can't pile up work.
_plan_lock = asyncio.Lock()
# The latest plan request of each tab, by client ID.
_plan_requests: dict[str | None, int] = {}


@PromptServer.instance.routes.post("/deadline/plan")
async def plan_submission(request: web.Request) -> web.Response:
    """Returns what the panel shows for the workflow."""
    checked = await _read_request(request)
    if isinstance(checked, web.Response):
        return checked
    body, workflow, template = checked
    client_id = _client_id(body)
    this_request = _plan_requests.get(client_id, 0) + 1
    _plan_requests[client_id] = this_request
    async with _plan_lock:
        if _plan_requests.get(client_id) != this_request:
            return _error(409, "A newer plan replaced this one.")
        try:
            # Checking files can be slow on network storage, which mustn't hold up ComfyUI's server.
            plan = await asyncio.to_thread(_plan, body, workflow, template)
        except SubmitError as e:
            return _error(400, str(e))
        finally:
            if _plan_requests.get(client_id) == this_request:
                del _plan_requests[client_id]
    return web.json_response(plan)


async def _bridge_process(command: str, cli: str | None = None) -> asyncio.subprocess.Process:
    """
    Starts bridge.py with the Python of the Deadline Cloud CLI, or of the configured one when cli
    isn't given. Raises SubmitError.
    """
    python = resolve_cli_python(cli or resolve_deadline_cli(load_config()))
    try:
        return await asyncio.create_subprocess_exec(
            # -I keeps this extension's modules and PYTHONPATH out of the CLI's environment.
            python,
            "-I",
            str(BRIDGE),
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        raise SubmitError(f"Couldn't run the Deadline Cloud CLI's Python {python}: {e}")


def _parse_event(line: bytes) -> dict[str, Any] | None:
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) and isinstance(event.get("event"), str) else None


def _bridge_failure(events: list[dict[str, Any]], stderr: bytes, returncode: int | None) -> str:
    for event in events:
        if event["event"] == "error":
            return str(event.get("message"))
    errors = stderr.decode(errors="replace").strip()
    logger.error("The Deadline Cloud bridge exited with code %s:\n%s", returncode, errors)
    last_line = next((line for line in reversed(errors.splitlines()) if line.strip()), "")
    return f"The Deadline Cloud library failed. {last_line.strip()}".strip()


async def _run_bridge(command: str, request: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Runs one bridge command and returns its result. Raises SubmitError."""
    process = await _bridge_process(command)
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate((json.dumps(request) + "\n").encode()), timeout
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        raise SubmitError(f"The Deadline Cloud library took longer than {timeout:g} seconds.")
    events = [event for event in map(_parse_event, stdout.splitlines()) if event]
    for event in events:
        if event["event"] == "result" and isinstance(event.get("data"), dict):
            return event["data"]
    raise SubmitError(_bridge_failure(events, stderr, process.returncode))


@PromptServer.instance.routes.post("/deadline/cloud/{command}")
async def call_deadline_cloud(request: web.Request) -> web.Response:
    """Runs a Deadline Cloud library call for the panel."""
    command = request.match_info["command"]
    if command not in CLOUD_COMMANDS:
        return _error(404, f"There's no Deadline Cloud command {command!r}.")
    body = await _read_body(request)
    if isinstance(body, web.Response):
        return body
    try:
        result = await _run_bridge(
            command, body, LOGIN_TIMEOUT if command == "login" else BRIDGE_TIMEOUT
        )
    except SubmitError as e:
        return _error(502, str(e))
    return web.json_response(result)


def _model_ids(value: Any) -> set[str]:
    """The local models the artist chose not to attach. Raises SubmitError."""
    if value is None:
        return set()
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise SubmitError("excludedModels must be a list of model IDs.")
    return set(value)


def _string_map(value: Any, what: str) -> dict[str, Any]:
    """Checks a {name: value} object of parameter values or job settings. Raises SubmitError."""
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(item, (str, int, float, bool)) or item is None for item in value.values()
    ):
        raise SubmitError(f"{what} must be an object of strings and numbers.")
    return {str(key): item for key, item in value.items() if item is not None}


def _attachments(value: Any) -> LocalAssets | None:
    """The input files and folders the artist chose to attach. Raises SubmitError."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SubmitError("attachments must be an object.")
    paths = {}
    for key in ("filenames", "directories"):
        listed = value.get(key, [])
        if not isinstance(listed, list) or not all(isinstance(path, str) for path in listed):
            raise SubmitError(f"attachments.{key} must be a list of paths.")
        paths[key] = sorted({Path(path) for path in listed if path.strip()})
    return LocalAssets(paths["filenames"], paths["directories"])


@dataclass
class _Bundle:
    """A job bundle that's ready to submit, with its job parameter values."""

    bundle_dir: Path
    workflow_path: Path
    values: dict[str, Any]
    warnings: list[str]


def _attaches_nodes(definitions: list[Any], parameters: dict[str, Any]) -> bool:
    """
    Whether the job loads the attached custom node packs: the panel's Attach Custom Nodes, or the
    job template's default, read as OpenJD reads a BOOL. A template without it attaches them.
    """
    definition = next(
        (d for d in definitions if isinstance(d, dict) and d.get("name") == ATTACH_NODES),
        {},
    )
    value = parameters.get(ATTACH_NODES, definition.get("default", True))
    return parse_bool(value) is not False


def _check_paired_counts(sources: list[ItemSource], counts: dict[str, int]) -> None:
    """
    Checks that paired fields run the same number of items, which OpenJD needs to run them
    together. The panel checks this too, so this catches a request from elsewhere. Raises
    SubmitError.
    """
    pairs: dict[str, list[ItemSource]] = {}
    for source in sources:
        if source.pair_with:
            pairs.setdefault(source.pair_with, []).append(source)
    for paired in pairs.values():
        if len({counts[source.name] for source in paired}) > 1:
            items = ", ".join(f"{source.title} has {counts[source.name]}" for source in paired)
            raise SubmitError(f"Paired fields need the same number of items: {items}.")


async def _build_bundle(
    body: dict[str, Any],
    workflow: dict[str, Any],
    template: Path,
    config: dict[str, Any],
    submission: bool = True,
) -> _Bundle:
    """
    Creates the job bundle from the panel's values. A bundle for a submission is marked, to delete
    when the submission is done. Raises SubmitError.
    """
    parameters = _string_map(body.get("parameters"), "parameters")
    assets = _attachments(body.get("attachments"))
    excluded_models = _model_ids(body.get("excludedModels"))
    requirements = host_requirements(body.get("hostRequirements"))
    description = job_description(body.get("description"))
    definitions = read_job_template(template).get("parameterDefinitions") or []
    # Refused before anything is prepared, staged or copied for the job.
    check_parameter_values(definitions, parameters)

    # The panel splits the workflow's lists and folders into tasks only when the artist chooses to.
    # Otherwise they run whole. Value nodes run their items as tasks either way, since in ComfyUI
    # they output only their preview item.
    split = body.get("splitTasks", False)
    if not isinstance(split, bool):
        raise SubmitError("splitTasks must be true or false.")

    prepared = await asyncio.to_thread(_prepare, body, workflow, template, config)
    items = prepared.items
    sources = [s for s in items.active if split or s.value_node] if items else []
    # Lists that run whole have no parameters, such as ones from a panel loaded before One task was
    # the default.
    for source in items.sources if items and not split else []:
        if not source.value_node:
            parameters.pop(source.parameter, None)
    if items and sources:
        prepared.warnings = [*items.warnings, *prepared.warnings]
    counts = {
        source.name: check_item_range(source, parameters.get(source.parameter, source.range))
        for source in sources
    }
    _check_paired_counts(sources, counts)
    defined = {
        parameter.get("name")
        for parameter in read_job_template(template, sources).get("parameterDefinitions", [])
    }
    # A list's field from an older plan, such as before the list was renamed, before anything is
    # staged. Queue parameters aren't in the template, but don't end in "Items".
    stale = sorted(name for name in parameters if name.endswith("Items") and name not in defined)
    if sources and stale:
        raise SubmitError(
            f"The workflow's lists changed since it was read ({', '.join(stale)}). Read the "
            "workflow again, then choose their items."
        )
    bundle_dir, workflow_path = create_job_bundle(
        template,
        prepared.workflow,
        _workflow_name(body),
        _submissions_dir(),
        requirements=requirements,
        sources=sources,
        description=description,
    )
    # Marked before the models are staged, so a bundle left by a crash while copying them is
    # still deleted.
    if submission:
        try:
            write_private_text(bundle_dir / SUBMISSION_MARKER, "")
        except OSError:
            logger.warning("Couldn't mark %s to delete after submitting", _log_value(bundle_dir))
    _exclude_local_models(prepared, excluded_models, workflow)
    # The job ignores attached packs with Attach Custom Nodes off, so they aren't uploaded.
    attach_nodes = _attaches_nodes(definitions, parameters)
    # Copying models and custom nodes can take a while when the bundle is on another volume.
    staged = await asyncio.to_thread(_stage_prepared, prepared, workflow, bundle_dir, attach_nodes)
    await asyncio.to_thread(
        _save_asset_references, workflow, bundle_dir, assets, staged.filenames, staged.directories
    )

    # The artist's values win over the ones the submitter fills in, except the workflow file. The
    # library rejects parameters that neither the job template nor the queue defines.
    filled = {key: value for key, value in prepared.values.items() if key in defined}
    values = {**filled, **parameters, "WorkflowFile": str(workflow_path)}
    # Attached files are only mapped through these directories, so they win over the panel's values.
    if staged.model_dir is not None:
        values["ModelDirectory"] = str(staged.model_dir)
    if staged.node_dir is not None:
        values["CustomNodesDirectory"] = str(staged.node_dir)
    return _Bundle(bundle_dir, workflow_path, values, prepared.warnings)


@PromptServer.instance.routes.post("/deadline/bundle")
async def save_bundle(request: web.Request) -> web.Response:
    """
    Creates the job bundle and saves its parameter values, to submit later with `deadline bundle
    submit`.
    """
    checked = await _read_request(request)
    if isinstance(checked, web.Response):
        return checked
    body, workflow, template = checked
    try:
        settings = _string_map(body.get("settings"), "settings")
        # A saved bundle is kept: it's submitted later, from this folder.
        bundle = await _build_bundle(body, workflow, template, load_config(), submission=False)
    except SubmitError as e:
        return _error(400, str(e))
    values: list[dict[str, Any]] = [
        {"name": key, "value": str(value)} for key, value in bundle.values.items()
    ]
    # The library passes these to CreateJob as they are, so the numbers stay numbers.
    values.extend(
        {"name": f"deadline:{key}", "value": settings[key]}
        for key in SAVED_JOB_SETTINGS
        if settings.get(key) not in (None, "")
    )
    write_private_text(
        bundle.bundle_dir / "parameter_values.yaml",
        yaml.safe_dump({"parameterValues": values}, sort_keys=False),
    )
    return web.json_response({"job_bundle": str(bundle.bundle_dir), "warnings": bundle.warnings})


@PromptServer.instance.routes.post("/deadline/jobs")
async def submit_job(request: web.Request) -> web.Response:
    """
    Creates the job bundle and submits it in the background. The panel follows its progress over the
    websocket.
    """
    checked = await _read_request(request)
    if isinstance(checked, web.Response):
        return checked
    body, workflow, template = checked
    name = _workflow_name(body)
    farm_id, queue_id = body.get("farmId"), body.get("queueId")
    if not (isinstance(farm_id, str) and farm_id and isinstance(queue_id, str) and queue_id):
        return _error(400, "Choose a farm and a queue.")
    try:
        config = load_config()
        settings = _string_map(body.get("settings"), "settings")
    except SubmitError as e:
        return _error(400, str(e))
    cli = await _checked_cli(config)
    if isinstance(cli, web.Response):
        return cli
    try:
        bundle = await _build_bundle(body, workflow, template, config)
    except SubmitError as e:
        return _error(400, str(e))
    # A server that runs for weeks also deletes old bundles, not only when ComfyUI starts.
    _start(asyncio.to_thread(_remove_old_submissions, _submissions_dir()))

    settings.setdefault("name", f"ComfyUI - {name}")
    submission = {
        "bundleDir": str(bundle.bundle_dir),
        "farmId": farm_id,
        "queueId": queue_id,
        "parameters": {key: str(value) for key, value in bundle.values.items()},
        "settings": settings,
    }
    storage_profile = body.get("storageProfileId")
    if isinstance(storage_profile, str):
        submission["storageProfileId"] = storage_profile
    try:
        process = await _bridge_process("submit", cli)
    except SubmitError as e:
        return _error(500, str(e))
    # A bridge that already exited is reported by _watch_submission, like any other failure.
    await _write_line(process, submission)

    # The panel sends its own ID, so it can match events sent before this response reaches it.
    requested_id = body.get("submissionId")
    if (
        isinstance(requested_id, str)
        and _SUBMISSION_ID.fullmatch(requested_id)
        and requested_id not in _submissions
    ):
        submission_id = requested_id
    else:
        submission_id = uuid.uuid4().hex
    _submissions[submission_id] = _Submission(
        str(settings["name"]),
        process,
        client_id=_client_id(body),
        bundle_dir=bundle.bundle_dir,
        output_dir=_output_dir(bundle.values),
    )
    logger.info(
        "Submitting %s to Deadline Cloud from %s",
        _log_value(name),
        _log_value(bundle.bundle_dir),
    )
    _start(_watch_submission(submission_id))
    return web.json_response(
        {
            "submissionId": submission_id,
            "job_bundle": str(bundle.bundle_dir),
            "workflow_file": str(bundle.workflow_path),
            "warnings": bundle.warnings,
        },
        status=202,
    )


async def _watch_submission(submission_id: str) -> None:
    """Relays a submission's progress to the panel and reports how it ended."""
    submission = _submissions[submission_id]
    process = submission.process
    assert process.stdout is not None and process.stderr is not None
    base = {"submissionId": submission_id, "name": submission.name}
    events = []
    result = None
    # Read stderr while reading stdout. A child that fills the stderr pipe blocks before it closes
    # stdout, so reading stderr only after stdout ends would wait forever.
    stderr_task = asyncio.create_task(process.stderr.read())
    try:
        async for line in process.stdout:
            event = _parse_event(line)
            if event is None:
                continue
            events.append(event)
            kind = event["event"]
            data = {key: value for key, value in event.items() if key != "event"}
            if kind in ("progress", "message"):
                _send(PROGRESS_EVENT, {**base, **data, "kind": kind}, submission.client_id)
            elif kind == "confirm":
                submission.confirms += 1
                _send(CONFIRM_EVENT, {**base, **data}, submission.client_id)
                submission.timers.append(
                    asyncio.create_task(
                        _decline_unanswered(submission_id, submission, submission.confirms)
                    )
                )
            elif kind == "result":
                result = event.get("data")
        stderr = await stderr_task
        await process.wait()
    finally:
        stderr_task.cancel()
        for timer in submission.timers:
            timer.cancel()
        _submissions.pop(submission_id, None)

    # A job the bridge created is reported even when Cancel came too late to stop it.
    if isinstance(result, dict) and result.get("jobId"):
        logger.info(
            "Submitted %s to Deadline Cloud as %s",
            _log_value(submission.name),
            _log_value(result["jobId"]),
        )
        submitted = {**base, **result, "status": "SUBMITTED"}
        if submission.output_dir:
            submitted["outputDir"] = submission.output_dir
        _send(SUBMITTED_EVENT, submitted, submission.client_id)
        await asyncio.to_thread(_remove_bundle, submission.bundle_dir)
    elif submission.canceled:
        _send(SUBMITTED_EVENT, {**base, "status": "CANCELED"}, submission.client_id)
        await asyncio.to_thread(_remove_bundle, submission.bundle_dir)
    else:
        # A failed submission's bundle is kept for a day, to look into or submit again.
        error = _bridge_failure(events, stderr, process.returncode)
        logger.error(
            "Couldn't submit %s to Deadline Cloud: %s",
            _log_value(submission.name),
            _log_block(str(error)),
        )
        _send(SUBMITTED_EVENT, {**base, "status": "FAILED", "error": error}, submission.client_id)


def _output_dir(values: dict[str, Any]) -> str | None:
    """The OutputDir a job is submitted with. The CLI downloads the job's outputs there."""
    value = values.get("OutputDir")
    return str(value) if value else None


def _download_directory(files: Any) -> str | None:
    """The folder that holds every file the CLI downloaded, from its summary's list of files."""
    if not isinstance(files, list) or not files or not all(isinstance(f, str) for f in files):
        return None
    try:
        return os.path.commonpath([os.path.dirname(f) for f in files])
    except ValueError:
        # Files on different Windows drives have no folder in common.
        return None


async def _write_line(process: asyncio.subprocess.Process, data: dict[str, Any]) -> bool:
    """Sends the bridge a JSON line. Returns False when it has already exited."""
    assert process.stdin is not None
    try:
        process.stdin.write((json.dumps(data) + "\n").encode())
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        return False
    return True


async def _decline_unanswered(submission_id: str, submission: _Submission, number: int) -> None:
    """Declines the bridge's confirmation `number` if it's still unanswered after the timeout."""
    await asyncio.sleep(CONFIRM_TIMEOUT)
    if _submissions.get(submission_id) is not submission or submission.answered >= number:
        return
    logger.warning(
        "Nobody answered the confirmation of %s within %g seconds, so it isn't submitted.",
        _log_value(submission.name),
        CONFIRM_TIMEOUT,
    )
    submission.answered = number
    await _write_line(submission.process, {"accept": False})


def _running_submission(body: dict[str, Any]) -> _Submission | web.Response:
    submission = _submissions.get(str(body.get("submissionId")))
    if submission is None:
        return _error(404, "That submission isn't running.")
    return submission


@PromptServer.instance.routes.post("/deadline/jobs/confirm")
async def confirm_submission(request: web.Request) -> web.Response:
    """Answers a submission's confirmation."""
    body = await _read_body(request)
    if isinstance(body, web.Response):
        return body
    submission = _running_submission(body)
    if isinstance(submission, web.Response):
        return submission
    submission.answered = submission.confirms
    if not await _write_line(submission.process, {"accept": body.get("accept") is True}):
        return _error(404, "That submission isn't running.")
    return web.json_response({})


@PromptServer.instance.routes.post("/deadline/jobs/cancel")
async def cancel_submission(request: web.Request) -> web.Response:
    """Stops a submission. A job it already created stays."""
    body = await _read_body(request)
    if isinstance(body, web.Response):
        return body
    submission = _running_submission(body)
    if isinstance(submission, web.Response):
        return submission
    submission.canceled = True
    try:
        submission.process.kill()
    except ProcessLookupError:
        # It already exited. _watch_submission reports how it ended.
        pass
    return web.json_response({})


_picker_lock = asyncio.Lock()


@PromptServer.instance.routes.post("/deadline/choose")
async def choose_paths(request: web.Request) -> web.Response:
    """Opens a native file or folder dialog on the ComfyUI machine and returns the chosen paths."""
    body = await _read_body(request)
    if isinstance(body, web.Response):
        return body
    kind = body.get("kind")
    if kind not in PICKER_KINDS:
        return _error(400, f"kind must be one of {', '.join(PICKER_KINDS)}.")
    command = picker_command(kind)
    if command is None:
        return _error(
            501,
            "There's no file dialog on the ComfyUI machine. On Linux, install zenity.",
        )
    if _picker_lock.locked():
        return _error(409, "A file dialog is already open on the ComfyUI machine.")
    async with _picker_lock:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await process.communicate()
        except OSError as e:
            return _error(500, f"Couldn't open the file dialog: {e}")
    # Canceling the dialog exits with an error and prints nothing.
    paths = [line.strip() for line in stdout.decode(errors="replace").splitlines()]
    if process.returncode != 0:
        paths = []
    return web.json_response({"paths": [str(Path(path)) for path in paths if path]})


def _cli_message(line: bytes) -> dict[str, Any] | None:
    """A JSON line from the CLI's --output json."""
    try:
        message = json.loads(line)
    except ValueError:
        return None
    return message if isinstance(message, dict) and "messageType" in message else None


@PromptServer.instance.routes.post("/deadline/jobs/download")
async def download_outputs(request: web.Request) -> web.Response:
    """Downloads a job's outputs in the background, to the paths the job was submitted with."""
    body = await _read_body(request)
    if isinstance(body, web.Response):
        return body
    ids = [str(body.get(key)) for key in ("farmId", "queueId", "jobId")]
    if not all(_RESOURCE_ID.fullmatch(value) for value in ids):
        return _error(400, "farmId, queueId and jobId must be Deadline Cloud IDs.")
    farm_id, queue_id, job_id = ids
    try:
        config = load_config()
    except SubmitError as e:
        return _error(400, str(e))
    cli = await _checked_cli(config)
    if isinstance(cli, web.Response):
        return cli
    command = [
        cli,
        "job",
        "download-output",
        "--farm-id",
        farm_id,
        "--queue-id",
        queue_id,
        "--job-id",
        job_id,
        "--conflict-resolution",
        "create_copy",
        "--output",
        "json",
        "--yes",
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=DOWNLOAD_LINE_LIMIT,
        )
    except OSError as e:
        return _error(500, f"Couldn't run the Deadline Cloud CLI: {e}")
    _start(_watch_download(process, job_id, _client_id(body)))
    return web.json_response({"jobId": job_id}, status=202)


async def _watch_download(
    process: asyncio.subprocess.Process, job_id: str, client_id: str | None = None
) -> None:
    assert process.stdout is not None and process.stderr is not None
    summary = None
    errors = []
    # Read stderr while reading stdout, as _watch_submission does.
    stderr_task = asyncio.create_task(process.stderr.read())
    try:
        async for line in process.stdout:
            message = _cli_message(line)
            if message is None:
                continue
            kind = message["messageType"]
            if kind == "progress":
                _send(
                    DOWNLOAD_EVENT,
                    {"jobId": job_id, "status": "DOWNLOADING", "progress": message["value"]},
                    client_id,
                )
            elif kind == "summary":
                summary = message
            elif kind == "error":
                errors.append(str(message.get("value")))
        stderr = await stderr_task
        await process.wait()
    except Exception as e:
        # Any failure, such as a line over the limit, still ends the panel's download.
        logger.exception("Couldn't read the download of %s", _log_value(job_id))
        if process.returncode is None:
            process.kill()
        await process.wait()
        _send(
            DOWNLOAD_EVENT,
            {"jobId": job_id, "status": "FAILED", "error": f"Couldn't read the CLI's output: {e}"},
            client_id,
        )
        return
    finally:
        stderr_task.cancel()
    if process.returncode == 0 and summary is not None:
        downloaded = {
            "jobId": job_id,
            "status": "DOWNLOADED",
            "message": str(summary.get("value")),
            "fileCount": summary.get("fileCount", 0),
        }
        directory = _download_directory(summary.get("files"))
        if directory:
            downloaded["directory"] = directory
        _send(DOWNLOAD_EVENT, downloaded, client_id)
        return
    text = stderr.decode(errors="replace")
    logger.error(
        "Couldn't download the outputs of %s:\n    %s", _log_value(job_id), _log_block(text)
    )
    last_line = next((line for line in reversed(text.splitlines()) if line.strip()), "")
    _send(
        DOWNLOAD_EVENT,
        {"jobId": job_id, "status": "FAILED", "error": (errors or [last_line.strip()])[-1]},
        client_id,
    )


# Deletes old bundles in the background, so ComfyUI's start doesn't wait on slow storage.
threading.Thread(
    target=lambda: _remove_old_submissions(_submissions_dir()),
    name="deadline-old-submissions",
    daemon=True,
).start()
