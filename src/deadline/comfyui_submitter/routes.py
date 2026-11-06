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
import re
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
    stage_local_models,
    stage_local_node_packs,
    write_asset_references,
)
from .submit import (
    ALLOW_REMOTE_ENV_VAR,
    DEFAULT_WORKFLOW_NAME,
    PICKER_KINDS,
    SubmitError,
    WorkItems,
    check_cli_version,
    create_job_bundle,
    custom_nodes,
    host_requirements,
    input_folders,
    job_description,
    job_template_with_items,
    load_config,
    local_node_packs,
    model_downloads,
    models_with_mismatched_links,
    models_without_links,
    picker_command,
    plan_work_items,
    read_node_pack,
    remote_allowed,
    resolve_cli_python,
    resolve_conda_channels,
    resolve_deadline_cli,
    resolve_job_template,
    task_summary,
    template_defines_parameter,
    template_host_requirements,
    template_supports_work_items,
    validate_api_workflow,
)

logger = logging.getLogger(__name__)

SUBMITTED_EVENT = "deadline.submitted"
PROGRESS_EVENT = "deadline.progress"
CONFIRM_EVENT = "deadline.confirm"
DOWNLOAD_EVENT = "deadline.download"
# A farm, queue or job ID, which goes on the CLI's command line.
_RESOURCE_ID = re.compile(r"(farm|queue|job)-[0-9a-f]{32}")

BRIDGE = Path(__file__).resolve().parent / "bridge.py"
# The bridge commands the panel can run directly.
CLOUD_COMMANDS = ("status", "login", "farms", "queues", "queue", "set-defaults", "jobs")
BRIDGE_TIMEOUT = 120
# Logging in waits for the artist to finish in Deadline Cloud monitor.
LOGIN_TIMEOUT = 600
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


# Running submissions, by ID.
_submissions: dict[str, _Submission] = {}


@dataclass
class _Prepared:
    """What a workflow submits with, before the job bundle exists."""

    workflow: dict[str, Any]
    items: WorkItems | None
    # Job parameter values the submitter fills in. WorkflowFile isn't known until the bundle exists.
    values: dict[str, Any] = field(default_factory=dict)
    conda_channels: str | None = None
    warnings: list[str] = field(default_factory=list)
    # Local files for models the job can't download, by the name the workflow loads, and the model
    # folder of each.
    local_models: dict[str, Path] = field(default_factory=dict)
    model_directories: dict[str, str] = field(default_factory=dict)
    model_warnings: list[str] = field(default_factory=list)
    # Custom node packs with no registry identity, by folder or file name.
    local_nodes: dict[str, Path] = field(default_factory=dict)
    unregistered_nodes: list[str] = field(default_factory=list)
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
    # Node type to the registry pack ComfyUI loaded it from.
    packs: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    # Node type to the custom node pack folder, or single .py file, ComfyUI loaded it from.
    dirs: dict[str, Path] = field(default_factory=dict)
    # Node types that come from a custom node module, whether or not its files were found.
    custom: set[str] = field(default_factory=set)


def _installed_node_packs(workflow: dict[str, Any]) -> _InstalledNodes:
    """
    Finds the custom node pack, and its folder, that ComfyUI loaded each workflow node from.
    Both the legacy NODE_CLASS_MAPPINGS packs and V3 extension packs (a comfy_entrypoint that
    returns a ComfyExtension) are found.
    """
    installed = _InstalledNodes()
    try:
        import nodes
    except ImportError:
        return installed
    for class_type in {node["class_type"] for node in workflow.values()}:
        name = _custom_node_pack_name(nodes.NODE_CLASS_MAPPINGS.get(class_type))
        if name is None:
            continue
        installed.custom.add(class_type)
        pack_path = _node_pack_path(nodes, name)
        if pack_path is None:
            continue
        installed.dirs[class_type] = pack_path
        if pack_path.is_dir():
            pack = read_node_pack(pack_path)
            if pack:
                installed.packs[class_type] = pack
    return installed


def _custom_node_pack_name(cls: Any) -> str | None:
    """
    The name of the custom node pack ComfyUI loaded this node class from, or None when the node
    isn't from a custom node pack. ComfyUI tags each class it loads with RELATIVE_PYTHON_MODULE,
    such as "custom_nodes.pack.nodes". V3 extension classes loaded by a ComfyUI release that
    doesn't tag them still have the pack's file path as their __module__, such as
    "/ComfyUI/custom_nodes/pack.nodes", which names the pack the same way.
    """
    module = str(getattr(cls, "RELATIVE_PYTHON_MODULE", "") or "")
    parts = module.split(".")
    if len(parts) >= 2 and parts[0] == "custom_nodes" and parts[1]:
        return parts[1]
    path_parts = re.split(r"[/\\]", str(getattr(cls, "__module__", "") or ""))
    for index, part in enumerate(path_parts[:-1]):
        if part == "custom_nodes":
            pack = path_parts[index + 1].split(".")[0]
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
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=60)
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
    except (OSError, asyncio.TimeoutError) as e:
        return _error(500, f"Couldn't run the Deadline Cloud CLI: {e}")
    return cli


def _is_loopback(remote: str | None) -> bool:
    try:
        return remote is not None and ipaddress.ip_address(remote).is_loopback
    except ValueError:
        return False


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def _read_body(request: web.Request) -> dict[str, Any] | web.Response:
    """Checks that a request comes from an allowed browser and returns its JSON object body."""
    # The routes use this machine's AWS credentials and open windows on its desktop, so by default
    # only a browser on this machine can call them.
    if not _is_loopback(request.remote) and not remote_allowed():
        return _error(
            403,
            "Deadline Cloud submits from the ComfyUI server, with its credentials, so it only "
            f"accepts requests from a browser on that machine. Set {ALLOW_REMOTE_ENV_VAR}=1 to "
            "allow others.",
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


def _prepare(
    body: dict[str, Any], workflow: dict[str, Any], template: Path, config: dict[str, Any]
) -> _Prepared:
    """Plans the work items and fills in the parameters. Raises SubmitError."""
    ui_workflow = body.get("ui_workflow")
    input_dir = Path(folder_paths.get_input_directory())
    items = (
        plan_work_items(workflow, ui_workflow, input_dir)
        if template_supports_work_items(template)
        else None
    )
    prepared = _Prepared(
        workflow=items.workflow if items else workflow,
        items=items,
        conda_channels=resolve_conda_channels(config),
        warnings=list(items.warnings) if items else [],
    )
    values = prepared.values
    values["OutputDir"] = folder_paths.get_output_directory()
    values["InputDir"] = str(input_dir)
    downloads = model_downloads(ui_workflow, workflow)
    if downloads:
        values["ModelDownloads"] = downloads
    installed = _installed_node_packs(workflow)
    if template_defines_parameter(template, "CustomNodes"):
        nodes = custom_nodes(ui_workflow, workflow, installed.packs)
        if nodes:
            values["CustomNodes"] = nodes
    # A template without Model Directory can't map attached models on the worker.
    if template_defines_parameter(template, "ModelDirectory"):
        prepared.local_models, prepared.model_directories = _local_model_paths(
            ui_workflow, workflow
        )
    prepared.model_warnings = _model_warnings(
        ui_workflow, workflow, downloads, prepared.local_models
    )
    prepared.warnings.extend(prepared.model_warnings)
    # A template without Custom Nodes Directory can't load attached packs on the worker.
    if template_defines_parameter(template, "CustomNodesDirectory"):
        prepared.local_nodes, prepared.unregistered_nodes = local_node_packs(
            ui_workflow, workflow, installed.packs, installed.dirs
        )
        prepared.installed_nodes = installed
        prepared.node_warnings = _node_warnings(
            prepared.unregistered_nodes, installed, prepared.local_nodes
        )
        prepared.warnings.extend(prepared.node_warnings)
    return prepared


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


def _stage_prepared(
    prepared: _Prepared,
    body: dict[str, Any],
    workflow: dict[str, Any],
    bundle_dir: Path,
    attach_nodes: bool = True,
) -> _Staged:
    """
    Stages the local models and custom node packs into the bundle, under the directories Deadline
    Cloud maps on the worker. Without attach_nodes, the job installs no custom nodes, so none are
    staged.
    """
    model_dir = bundle_dir / "local_models"
    staged = _stage_local_models(prepared.local_models, prepared.model_directories, model_dir)
    if set(staged) != set(prepared.local_models):
        # The plan counted on attaching these, so warn about the ones that couldn't be.
        prepared.warnings = [w for w in prepared.warnings if w not in prepared.model_warnings]
        prepared.model_warnings = _model_warnings(
            body.get("ui_workflow"), workflow, prepared.values.get("ModelDownloads"), staged
        )
        prepared.warnings.extend(prepared.model_warnings)

    node_dir = bundle_dir / "local_nodes"
    nodes = _stage_local_node_packs(prepared.local_nodes, node_dir) if attach_nodes else {}
    if not attach_nodes or set(nodes) != set(prepared.local_nodes):
        prepared.warnings = [w for w in prepared.warnings if w not in prepared.node_warnings]
        prepared.node_warnings = (
            _node_warnings(prepared.unregistered_nodes, prepared.installed_nodes, nodes)
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


def _stage_local_node_packs(packs: dict[str, Path], node_dir: Path) -> dict[str, Path]:
    """Stages local custom node packs into the bundle. A failure still lets the job submit."""
    if not packs:
        return {}
    try:
        return stage_local_node_packs(packs, node_dir)
    except Exception:
        logger.exception("Couldn't stage local custom node packs")
        return {}


def _node_warnings(
    unregistered: list[str],
    installed: _InstalledNodes,
    staged_nodes: dict[str, Path],
) -> list[str]:
    """A warning for custom nodes that are neither installed from the registry nor attached."""
    missing = [
        class_type
        for class_type in unregistered
        if class_type in installed.custom
        and (
            class_type not in installed.dirs or installed.dirs[class_type].name not in staged_nodes
        )
    ]
    if not missing:
        return []
    return [
        (
            f"The custom nodes {', '.join(missing)} aren't from the Comfy Registry, and their "
            "files couldn't be attached. The job fails unless the workers already have them, "
            "for example in a custom nodes conda package."
        )
    ]


def _model_warnings(
    ui_workflow: Any,
    workflow: dict[str, Any],
    downloads: str | None,
    attached: dict[str, Path],
) -> list[str]:
    """Warnings for loaded models that are neither downloaded nor attached."""
    unlinked = [name for name in models_without_links(workflow, downloads) if name not in attached]
    mismatched = [
        name for name in unlinked if name in models_with_mismatched_links(ui_workflow, workflow)
    ]
    missing = [name for name in unlinked if name not in mismatched]
    warnings = []
    if mismatched:
        warnings.append(
            f"The URL for {', '.join(mismatched)} names a different file, and there is no local "
            "file with that name. Add it to Model Downloads in the panel, or the job fails "
            "unless the workers already have it in the model cache or an extra model path."
        )
    if missing:
        warnings.append(
            f"No download link for {', '.join(missing)}. Add it to Model Downloads in the "
            "panel, or the job fails unless the workers already have it in the model cache "
            "or an extra model path."
        )
    return warnings


def _exclude_local_models(
    prepared: _Prepared,
    excluded: set[str],
    body: dict[str, Any],
    workflow: dict[str, Any],
    downloads: str | None,
) -> None:
    """Leaves out the local models the artist removed, and warns about them like any others."""
    if not excluded.intersection(prepared.local_models):
        return
    prepared.local_models = {
        name: path for name, path in prepared.local_models.items() if name not in excluded
    }
    prepared.warnings = [w for w in prepared.warnings if w not in prepared.model_warnings]
    prepared.model_warnings = _model_warnings(
        body.get("ui_workflow"), workflow, downloads, prepared.local_models
    )
    prepared.warnings.extend(prepared.model_warnings)


def _local_model_paths(
    ui_workflow: Any, api_workflow: dict[str, Any]
) -> tuple[dict[str, Path], dict[str, str]]:
    """Local files for models with no Model Downloads entry. A failed lookup finds none."""
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
    """Saves the workflow's local input files to asset_references.yaml next to the job template."""
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


def _start(coroutine) -> None:
    task = asyncio.create_task(coroutine)
    _watchers.add(task)
    task.add_done_callback(_watchers.discard)


def _plan(body: dict[str, Any], workflow: dict[str, Any], template: Path) -> dict[str, Any]:
    """Works out the panel's plan. It reads files, so it runs in a thread. Raises SubmitError."""
    prepared = _prepare(body, workflow, template, load_config())
    sources = prepared.items.active if prepared.items else []
    job_template = job_template_with_items(template, sources or None)
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
                "parameter": f"{source.name}Items",
                "label": source.label,
                "title": source.title,
                "kind": source.kind,
                # Integer fields hold the values themselves, so they need no preview.
                "values": (
                    []
                    if source.kind == "integer"
                    else [
                        value[:MAX_PREVIEW_LENGTH] for value in source.values[:MAX_PREVIEW_VALUES]
                    ]
                ),
                "count": source.count,
                "range": source.range,
                "pairWith": source.pair_with,
            }
            for source in sources
        ],
        "tasks": task_summary(sources) if sources else None,
        "attachments": {
            "filenames": [str(path) for path in assets.filenames],
            "directories": [str(path) for path in assets.directories],
        },
        # Attached when submitting.
        "localModels": [
            {
                "name": name,
                "directory": prepared.model_directories.get(name),
                "path": str(path),
            }
            for name, path in prepared.local_models.items()
        ],
        # Attached when submitting, unless the artist turns off Attach Custom Nodes.
        "localNodes": [
            {"name": name, "path": str(path)} for name, path in prepared.local_nodes.items()
        ],
        "hostRequirements": template_host_requirements(job_template),
        "warnings": prepared.warnings,
        "nodeWarnings": prepared.node_warnings,
    }


# One plan runs at a time, and one replaced while it waits doesn't run, so slow storage can't pile
# up work.
_plan_lock = asyncio.Lock()
_plan_requests = 0


@PromptServer.instance.routes.post("/deadline/plan")
async def plan_submission(request: web.Request) -> web.Response:
    """Returns what the panel shows for the workflow."""
    global _plan_requests
    checked = await _read_request(request)
    if isinstance(checked, web.Response):
        return checked
    body, workflow, template = checked
    _plan_requests += 1
    this_request = _plan_requests
    async with _plan_lock:
        if this_request != _plan_requests:
            return _error(409, "A newer plan replaced this one.")
        try:
            # Checking files can be slow on network storage, which mustn't hold up ComfyUI's server.
            plan = await asyncio.to_thread(_plan, body, workflow, template)
        except SubmitError as e:
            return _error(400, str(e))
    return web.json_response(plan)


async def _bridge_process(command: str) -> asyncio.subprocess.Process:
    """Starts bridge.py with the Deadline Cloud CLI's Python. Raises SubmitError."""
    python = resolve_cli_python(resolve_deadline_cli(load_config()))
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
    except asyncio.TimeoutError:
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


def _model_names(value: Any) -> set[str]:
    """The local models the artist chose not to attach. Raises SubmitError."""
    if value is None:
        return set()
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise SubmitError("excludedModels must be a list of model names.")
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


async def _build_bundle(
    body: dict[str, Any], workflow: dict[str, Any], template: Path, config: dict[str, Any]
) -> _Bundle:
    """Creates the job bundle from the panel's values. Raises SubmitError."""
    parameters = _string_map(body.get("parameters"), "parameters")
    assets = _attachments(body.get("attachments"))
    excluded_models = _model_names(body.get("excludedModels"))
    requirements = host_requirements(body.get("hostRequirements"))
    description = job_description(body.get("description"))

    prepared = await asyncio.to_thread(_prepare, body, workflow, template, config)
    sources = prepared.items.active if prepared.items else None
    defined = {
        parameter.get("name")
        for parameter in job_template_with_items(template, sources).get("parameterDefinitions", [])
    }
    bundle_dir, workflow_path = create_job_bundle(
        template,
        prepared.workflow,
        _workflow_name(body),
        Path(folder_paths.get_user_directory()) / "deadline_submissions",
        sources=sources,
        requirements=requirements,
        description=description,
    )
    downloads = parameters.get("ModelDownloads", prepared.values.get("ModelDownloads"))
    _exclude_local_models(
        prepared, excluded_models, body, workflow, str(downloads) if downloads else None
    )
    # The job ignores attached packs with Attach Custom Nodes off, so they aren't uploaded.
    attach_nodes = str(parameters.get("AttachCustomNodes", "true")).lower() != "false"
    # Copying models and custom nodes can take a while when the bundle is on another volume.
    staged = await asyncio.to_thread(
        _stage_prepared, prepared, body, workflow, bundle_dir, attach_nodes
    )
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
        bundle = await _build_bundle(body, workflow, template, load_config())
    except SubmitError as e:
        return _error(400, str(e))
    values = [{"name": key, "value": str(value)} for key, value in bundle.values.items()]
    (bundle.bundle_dir / "parameter_values.yaml").write_text(
        yaml.safe_dump({"parameterValues": values}, sort_keys=False), encoding="utf-8"
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
        process = await _bridge_process("submit")
    except SubmitError as e:
        return _error(500, str(e))
    assert process.stdin is not None
    process.stdin.write((json.dumps(submission) + "\n").encode())
    await process.stdin.drain()

    submission_id = uuid.uuid4().hex
    _submissions[submission_id] = _Submission(str(settings["name"]), process)
    logger.info("Submitting %s to Deadline Cloud from %s", name, bundle.bundle_dir)
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
    try:
        async for line in process.stdout:
            event = _parse_event(line)
            if event is None:
                continue
            events.append(event)
            kind = event["event"]
            data = {key: value for key, value in event.items() if key != "event"}
            if kind in ("progress", "message"):
                PromptServer.instance.send_sync(PROGRESS_EVENT, {**base, **data, "kind": kind})
            elif kind == "confirm":
                PromptServer.instance.send_sync(CONFIRM_EVENT, {**base, **data})
            elif kind == "result":
                result = event.get("data")
        stderr = await process.stderr.read()
        await process.wait()
    finally:
        _submissions.pop(submission_id, None)

    if submission.canceled:
        PromptServer.instance.send_sync(SUBMITTED_EVENT, {**base, "status": "CANCELED"})
    elif isinstance(result, dict) and result.get("jobId"):
        logger.info("Submitted %s to Deadline Cloud as %s", submission.name, result["jobId"])
        PromptServer.instance.send_sync(SUBMITTED_EVENT, {**base, **result, "status": "SUBMITTED"})
    else:
        error = _bridge_failure(events, stderr, process.returncode)
        logger.error("Couldn't submit %s to Deadline Cloud: %s", submission.name, error)
        PromptServer.instance.send_sync(
            SUBMITTED_EVENT, {**base, "status": "FAILED", "error": error}
        )


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
    assert submission.process.stdin is not None
    submission.process.stdin.write(
        (json.dumps({"accept": body.get("accept") is True}) + "\n").encode()
    )
    await submission.process.stdin.drain()
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
    submission.process.kill()
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
        )
    except OSError as e:
        return _error(500, f"Couldn't run the Deadline Cloud CLI: {e}")
    _start(_watch_download(process, job_id))
    return web.json_response({"jobId": job_id}, status=202)


async def _watch_download(process: asyncio.subprocess.Process, job_id: str) -> None:
    assert process.stdout is not None and process.stderr is not None
    summary = None
    errors = []
    async for line in process.stdout:
        message = _cli_message(line)
        if message is None:
            continue
        kind = message["messageType"]
        if kind == "progress":
            PromptServer.instance.send_sync(
                DOWNLOAD_EVENT,
                {"jobId": job_id, "status": "DOWNLOADING", "progress": message["value"]},
            )
        elif kind == "summary":
            summary = message
        elif kind == "error":
            errors.append(str(message.get("value")))
    stderr = await process.stderr.read()
    await process.wait()
    if process.returncode == 0 and summary is not None:
        PromptServer.instance.send_sync(
            DOWNLOAD_EVENT,
            {
                "jobId": job_id,
                "status": "DOWNLOADED",
                "message": str(summary.get("value")),
                "fileCount": summary.get("fileCount", 0),
            },
        )
        return
    text = stderr.decode(errors="replace")
    logger.error("Couldn't download the outputs of %s:\n%s", job_id, text)
    last_line = next((line for line in reversed(text.splitlines()) if line.strip()), "")
    PromptServer.instance.send_sync(
        DOWNLOAD_EVENT,
        {"jobId": job_id, "status": "FAILED", "error": (errors or [last_line.strip()])[-1]},
    )
