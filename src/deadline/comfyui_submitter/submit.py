# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Helpers for routes.py that don't import ComfyUI, so they can be unit tested."""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import re
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

TEMPLATE_ENV_VAR = "COMFYUI_DEADLINE_JOB_TEMPLATE"
CLI_ENV_VAR = "COMFYUI_DEADLINE_CLI"
# The Python the submitter panel's bridge runs with. By default, the Deadline Cloud CLI's own.
PYTHON_ENV_VAR = "COMFYUI_DEADLINE_PYTHON"
ALLOW_REMOTE_ENV_VAR = "COMFYUI_DEADLINE_ALLOW_REMOTE"
CONDA_CHANNELS_ENV_VAR = "COMFYUI_DEADLINE_CONDA_CHANNELS"
CONFIG_ENV_VAR = "COMFYUI_DEADLINE_CONFIG"

CONFIG_FILE_NAME = "deadline_cloud_submitter.json"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / CONFIG_FILE_NAME

JOB_TEMPLATE = Path(__file__).parent / "comfy_cli_job_template.yaml"
DEFAULT_WORKFLOW_NAME = "ComfyUI workflow"
# ComfyUI's supported_pt_extensions, plus .gguf for the GGUF loader nodes.
MODEL_EXTENSIONS = (".ckpt", ".pt", ".pt2", ".bin", ".pth", ".safetensors", ".pkl", ".sft", ".gguf")

# The oldest CLI the submitter supports. Its library reads the comfy-cli job's BOOL parameter.
MIN_CLI_VERSION = (0, 60, 8)
# The first botocore whose Deadline Cloud API can send that BOOL parameter. The CLI accepts older
# ones, which only fail when they create the job.
MIN_BOTOCORE_VERSION = (1, 43, 105)


logger = logging.getLogger(__name__)

# The job template expands ModelDownloads unquoted in a bash script, so every part of an entry is
# limited to characters that bash treats literally.
_MODEL_DIRECTORY = re.compile(r"[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*")
_MODEL_NAME = re.compile(r"[A-Za-z0-9_+-][A-Za-z0-9._+-]*(/[A-Za-z0-9_+-][A-Za-z0-9._+-]*)*")
_MODEL_URL = re.compile(r"https://[A-Za-z0-9._~:/%+=-]+")
# comfy-cli resolves a CivitAI download from its version ID, so the query string, which bash can't
# take literally, isn't needed.
_CIVITAI_DOWNLOAD_URL = re.compile(r"(https://civitai\.com/api/download/models/[0-9]+)(\?[^#]*)?")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")
# CustomNodes is expanded the same way. Registry node IDs are lowercase with dashes and
# underscores, and versions are semver.
_NODE_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")
_NODE_VERSION = re.compile(r"[0-9]+(\.[0-9A-Za-z-]+)*")
# Nodes that ship with ComfyUI itself, which the registry doesn't serve.
BUILT_IN_NODE_PACKS = ("comfy-core",)


class SubmitError(Exception):
    """An error whose message is shown to the artist in ComfyUI."""


def resolve_job_template() -> Path:
    override = os.environ.get(TEMPLATE_ENV_VAR)
    if override:
        template = Path(override)
        if not template.is_file() or template.suffix.lower() not in (".yaml", ".yml", ".json"):
            raise SubmitError(
                f"{TEMPLATE_ENV_VAR} is set to {template}, which isn't a YAML or JSON job template."
            )
        return template
    return JOB_TEMPLATE


def config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG_PATH)


def load_config() -> dict[str, Any]:
    path = config_path()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise SubmitError(f"Couldn't read the submitter config {path}: {e}")
    if not isinstance(config, dict):
        raise SubmitError(f"The submitter config {path} isn't a JSON object.")
    return config


def resolve_deadline_cli(config: dict[str, Any] | None = None) -> str:
    """A relative deadline_cli in the config is relative to the config file."""
    configured = os.environ.get(CLI_ENV_VAR)
    source = CLI_ENV_VAR
    if not configured and (config or {}).get("deadline_cli"):
        configured = str(config_path().parent / str((config or {})["deadline_cli"]))
        source = f"deadline_cli in {config_path()}"
    if configured:
        if not (os.path.isfile(configured) and os.access(configured, os.X_OK)):
            raise SubmitError(f"{source} is set to {configured}, which isn't an executable file.")
        return configured
    found = shutil.which("deadline")
    if not found:
        raise SubmitError(
            "The Deadline Cloud CLI wasn't found. Run install_submitter.py to set it up, or install "
            f'it into its own Python environment with pip install "deadline[gui]>=0.60.8" and put '
            f"it on the PATH or set {CLI_ENV_VAR} to its `deadline` executable."
        )
    return found


# pip writes this launcher instead of a shebang when the interpreter's path has spaces.
_EXEC_LAUNCHER = re.compile(r"^'''exec' \"?(?P<python>[^\"\n]+?)\"? \"\$0\" \"\$@\"$", re.MULTILINE)


def resolve_cli_python(cli: str) -> str:
    """
    Returns the Python the Deadline Cloud CLI runs with, from its shebang or pip launcher, or
    otherwise the python next to it.
    """
    configured = os.environ.get(PYTHON_ENV_VAR)
    if configured:
        if not (os.path.isfile(configured) and os.access(configured, os.X_OK)):
            raise SubmitError(
                f"{PYTHON_ENV_VAR} is set to {configured}, which isn't an executable file."
            )
        return configured
    candidates = []
    try:
        with open(cli, "rb") as f:
            head = f.read(4096).decode("utf-8", errors="replace")
    except OSError:
        head = ""
    launcher = _EXEC_LAUNCHER.search(head)
    if launcher:
        candidates.append(launcher["python"])
    elif head.startswith("#!"):
        candidates.append(head[2:].splitlines()[0].strip())
    directory = Path(cli).parent
    candidates += [str(directory / name) for name in ("python", "python3", "python.exe")]
    for candidate in candidates:
        if os.path.basename(candidate).lower().startswith("python") and os.path.isfile(candidate):
            return candidate
    raise SubmitError(
        f"Couldn't find the Python that runs the Deadline Cloud CLI {cli}. Set {PYTHON_ENV_VAR} "
        "to it."
    )


PICKER_KINDS = ("files", "folder")
_PICKER_PROMPTS = {
    "files": "Choose files to attach to the Deadline Cloud job",
    "folder": "Choose a folder to attach to the Deadline Cloud job",
}


def picker_command(kind: str, platform: str = sys.platform) -> list[str] | None:
    """
    Returns the command for a native file or folder dialog, which prints one absolute path per line,
    or None when there isn't one. The browser only chooses the kind.
    """
    prompt = _PICKER_PROMPTS[kind]
    if platform == "darwin":
        if kind == "folder":
            return ["osascript", "-e", f'POSIX path of (choose folder with prompt "{prompt}")']
        return [
            "osascript",
            "-e",
            f'set chosen to choose file with prompt "{prompt}" with multiple selections allowed',
            "-e",
            'set output to ""',
            "-e",
            "repeat with item_ in chosen",
            "-e",
            "set output to output & POSIX path of item_ & linefeed",
            "-e",
            "end repeat",
            "-e",
            "return output",
        ]
    if platform == "win32":
        if kind == "folder":
            script = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
                f"$d.Description='{prompt}';"
                "if($d.ShowDialog() -eq 'OK'){[Console]::Out.Write($d.SelectedPath)}"
            )
        else:
            script = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$d=New-Object System.Windows.Forms.OpenFileDialog;"
                f"$d.Title='{prompt}';$d.Multiselect=$true;"
                "if($d.ShowDialog() -eq 'OK'){[Console]::Out.Write(($d.FileNames -join \"`n\"))}"
            )
        return ["powershell", "-NoProfile", "-STA", "-Command", script]
    if shutil.which("zenity"):
        command = ["zenity", "--file-selection", f"--title={prompt}"]
        return command + (["--directory"] if kind == "folder" else ["--multiple", "--separator=\n"])
    return None


def resolve_conda_channels(config: dict[str, Any] | None = None) -> str | None:
    channels = os.environ.get(CONDA_CHANNELS_ENV_VAR) or (config or {}).get("conda_channels")
    if not channels:
        return None
    return " ".join(str(channels).split()) or None


def parse_cli_version(output: str) -> tuple[int, ...] | None:
    """Parses `deadline --version` output, for example "deadline, version 0.60.10"."""
    match = re.search(r"version (\d+)\.(\d+)\.(\d+)", output)
    return tuple(int(part) for part in match.groups()) if match else None


def check_cli_version(cli: str, version_output: str) -> None:
    version = parse_cli_version(version_output)
    # Let an unrecognized version through rather than block submissions on a format change.
    if version is not None and version < MIN_CLI_VERSION:
        minimum = ".".join(map(str, MIN_CLI_VERSION))
        raise SubmitError(
            f"The Deadline Cloud CLI at {cli} is version {'.'.join(map(str, version))}, which can't "
            f"open the submitter with the workflow filled in. Upgrade it to {minimum} or later with "
            f'pip install --upgrade "deadline[gui]>={minimum}".'
        )


def remote_allowed() -> bool:
    return os.environ.get(ALLOW_REMOTE_ENV_VAR, "").lower() in ("1", "true", "yes")


def _describe_missing_node(node_id: str, ui_workflow: Any) -> str:
    # A node inside a subgraph has an API ID like "12:137"; its UI ID is the last part.
    ui_id = node_id.rsplit(":", 1)[-1]
    for node in _workflow_nodes(ui_workflow):
        if str(node.get("id")) != ui_id:
            continue
        description = f"{node_id} ({node.get('type')}"
        properties = node.get("properties")
        pack = properties.get("cnr_id") if isinstance(properties, dict) else None
        if isinstance(pack, str):
            description += f", from the {pack} node pack"
        return description + ")"
    return node_id


def validate_api_workflow(workflow: Any, ui_workflow: Any = None) -> dict[str, Any]:
    if not isinstance(workflow, dict) or not workflow:
        raise SubmitError("The workflow is empty.")
    if "nodes" in workflow and "links" in workflow:
        raise SubmitError("The workflow is in UI format. The submitter needs the API format.")
    # ComfyUI leaves out class_type for a node type it has no definition for, so the node can't
    # be turned into the API format.
    missing = [
        _describe_missing_node(str(node_id), ui_workflow)
        for node_id, node in workflow.items()
        if not isinstance(node, dict) or "class_type" not in node
    ]
    if missing:
        raise SubmitError(
            f"This ComfyUI can't convert node {', '.join(missing)} because its node type isn't "
            "installed. Install the missing node packs and restart ComfyUI. Manager > Install "
            "Missing Custom Nodes finds packs from the Comfy Registry. A node with no pack named "
            "here isn't from the registry, so get its code from whoever made the workflow."
        )
    return workflow


# Work items: the values that the comfy-cli job runs as separate tasks. Each source of them in the
# workflow becomes its own task parameter, so tasks run every combination of their items, unless
# value nodes are paired. The job template's task script selects each task's item, and has its own
# copy of expand_dynamic_prompt and these names, which test_template_script.py keeps in step.
STRING_NODE = "DeadlineStringValues"
INTEGER_NODE = "DeadlineIntegerValues"
IMAGE_NODE = "DeadlineImageValues"
VALUE_NODES = (STRING_NODE, INTEGER_NODE, IMAGE_NODE)
# Core nodes that load every image in a folder of the input directory as a list.
FOLDER_LOADERS = ("LoadImageDataSetFromFolder",)
# Nodes whose folder input names a folder of the input directory, which is sent with the job.
FOLDER_INPUT_NODES = (*FOLDER_LOADERS, "LoadImageTextDataSetFromFolder", IMAGE_NODE)
# The images that LoadImageDataSetFromFolder loads.
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
# The _meta keys that mark the nodes the task script splits, with the task parameter of each.
ITEM_META = "deadline_item"
DYNAMIC_PROMPTS_META = "deadline_dynamic_prompts"
# What a value node outputs in tasks that don't split it: its preview item.
PREVIEW_META = "deadline_preview"
# The images of a folder, as paths in the input directory, in the order its items are numbered.
FILES_META = "deadline_files"
# More combinations than this aren't split into tasks.
MAX_LIST_ITEMS = 1000
MAX_INTEGER_VALUES = 10000
# OpenJD allows 16 task parameters in a step.
MAX_ITEM_SOURCES = 16
# How much of a dynamic prompt's text names it in the panel.
MAX_PROMPT_TITLE = 40
# The line of the task script that create_job_bundle fills in with each task's items. A job
# template without it runs whole lists.
ITEMS_PLACEHOLDER = "ITEMS = {}"
WORK_ITEMS_GROUP = "Work Items"

_LIST_ITEM_INPUT = re.compile(r"(inputs\.)?input\d+")
_ITEMS_PLACEHOLDER_LINE = re.compile(rf"^[ \t]*{re.escape(ITEMS_PLACEHOLDER)}[ \t]*$", re.MULTILINE)
_INTEGER_RANGE = re.compile(r"(-?\d+)(?:-(-?\d+)(?::(\d+))?)?")
_DEFAULT_TITLES = {
    STRING_NODE: "Deadline String Values",
    INTEGER_NODE: "Deadline Integer Values",
    IMAGE_NODE: "Deadline Image Values",
}


def expand_dynamic_prompt(text: str) -> list[str]:
    """
    Lists every prompt that ComfyUI's dynamic prompt syntax, such as "a {red|white} fox", can choose,
    in order. It follows the frontend's processDynamicPrompt, which chooses one at random: // and
    /* */ comments are removed, choices can nest, and \\{, \\| and \\} are literal characters.
    """
    text = re.sub(r"/\*[\s\S]*?\*/|//.*", "", text)
    results = [""]
    i = 0
    while i < len(text):
        char = text[i]
        i += 1
        if char == "\\":
            piece = "\\" + text[i : i + 1]
            i += 1
            results = [result + piece for result in results]
        elif char == "{":
            choices, current, depth = [], "", 0
            while i < len(text):
                char = text[i]
                i += 1
                if char == "\\":
                    current += "\\" + text[i : i + 1]
                    i += 1
                    continue
                if char == "{":
                    depth += 1
                elif char == "}":
                    if not depth:
                        break
                    depth -= 1
                elif char == "|" and not depth:
                    choices.append(current)
                    current = ""
                    continue
                current += char
            choices.append(current)
            expanded = [option for choice in choices for option in expand_dynamic_prompt(choice)]
            results = [result + option for result in results for option in expanded]
            if len(results) > MAX_LIST_ITEMS:
                return results[: MAX_LIST_ITEMS + 1]
        else:
            results = [result + char for result in results]
    return [re.sub(r"\\([{}|])", r"\1", result) for result in results]


def string_values(text: Any) -> list[str]:
    """The values of a Deadline String Values node, one per non-empty line."""
    return [line.strip() for line in str(text).splitlines() if line.strip()]


def _integer_ranges(text: Any) -> list[range]:
    ranges = []
    for part in re.sub(r"\s+", "", str(text)).split(","):
        if not part:
            continue
        match = _INTEGER_RANGE.fullmatch(part)
        if not match:
            raise ValueError(f"{part!r} isn't a number or a range such as 1-10 or 1-10:2.")
        start = int(match[1])
        end = int(match[2]) if match[2] is not None else start
        step = int(match[3] or 1)
        if end < start or step < 1:
            raise ValueError(f"The range {part!r} has to count up.")
        ranges.append(range(start, end + 1, step))
    return ranges


def integer_values(text: Any) -> list[int]:
    """
    The values of a Deadline Integer Values node: numbers and OpenJD ranges, such as "1-4",
    "1-20:2" or "7, 42, 99", which can't repeat a number.
    """
    values: list[int] = []
    for numbers in _integer_ranges(text):
        if len(values) + len(numbers) > MAX_INTEGER_VALUES:
            raise ValueError(f"There are more than {MAX_INTEGER_VALUES} numbers.")
        values.extend(numbers)
    if len(set(values)) < len(values):
        raise ValueError("A number is listed more than once.")
    return values


def _integer_range_expression(text: Any) -> str:
    """The OpenJD range expression for a Deadline Integer Values node's values."""
    parts = []
    for numbers in _integer_ranges(text):
        if len(numbers) == 1:
            parts.append(str(numbers.start))
        else:
            last = numbers[-1]
            parts.append(
                f"{numbers.start}-{last}" + (f":{numbers.step}" if numbers.step > 1 else "")
            )
    return ",".join(parts)


def input_path(input_dir: Path | str, name: Any) -> Path | None:
    """
    The file or folder in ComfyUI's input directory that a node input names, such as "fox.png",
    "fox.png [input]" or "dataset/cats", or None if it names a path outside it.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    if name.endswith(" [input]"):
        name = name[: -len(" [input]")]
    elif re.search(r" \[(output|temp)\]$", name):
        return None
    # ComfyUI checks paths this way too, without resolving symbolic links.
    base = Path(os.path.abspath(input_dir))
    path = Path(os.path.abspath(base / name))
    return path if path != base and path.is_relative_to(base) else None


def image_files(directory: Path) -> list[str]:
    """The images in a folder that LoadImageDataSetFromFolder loads, in name order."""
    try:
        return sorted(
            entry.name
            for entry in directory.iterdir()
            if entry.is_file() and entry.name.lower().endswith(IMAGE_EXTENSIONS)
        )
    except OSError:
        return []


def input_folders(workflow: dict[str, Any], input_dir: Path) -> list[Path]:
    """
    Returns the folders in ComfyUI's input directory that the workflow's folder loaders and Deadline
    Image Values nodes read.
    """
    folders = set()
    for node in workflow.values():
        if node.get("class_type") not in FOLDER_INPUT_NODES:
            continue
        path = input_path(input_dir, node.get("inputs", {}).get("folder"))
        if path is not None and path.is_dir():
            folders.add(path)
    return sorted(folders)


@dataclass
class _UiNodes:
    """The nodes of a UI workflow by ID, so that looking up each API node doesn't scan them all."""

    top_level: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    in_subgraphs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def _index_ui_nodes(ui_workflow: Any) -> _UiNodes:
    index = _UiNodes()
    if not isinstance(ui_workflow, dict):
        return index
    for node in ui_workflow.get("nodes") or []:
        if isinstance(node, dict):
            index.top_level.setdefault(str(node.get("id")), []).append(node)
    definitions = ui_workflow.get("definitions")
    subgraphs = definitions.get("subgraphs") if isinstance(definitions, dict) else None
    for subgraph in subgraphs or []:
        for node in _workflow_nodes(subgraph):
            index.in_subgraphs.setdefault(str(node.get("id")), []).append(node)
    return index


def _dynamic_prompt_templates(ui_nodes: _UiNodes, node_id: str) -> list[str]:
    """
    The text using dynamic prompt syntax in the widgets of an API workflow node's UI node. A node
    inside a subgraph has an API ID like "57:27", and node 57 can also hold the values of widgets it
    promotes.
    """
    parts = node_id.split(":")
    nodes = list(ui_nodes.top_level.get(parts[0], []))
    if len(parts) > 1:
        nodes += ui_nodes.in_subgraphs.get(parts[-1], [])
    templates = []
    for node in nodes:
        for value in node.get("widgets_values") or []:
            if isinstance(value, str) and "{" in value and value not in templates:
                templates.append(value)
    return templates


@dataclass
class ItemSource:
    """A node, or a node's prompt, whose items can run as separate tasks."""

    # The task parameter that selects the item, such as "List12". Its job parameter, "List12Items",
    # holds the range to run.
    name: str
    label: str
    count: int
    # "list", "prompt", "folder", "string", "integer" or "image".
    kind: str = "list"
    # The items, as far as the submitter knows them, for the job parameter's description.
    values: list[str] = field(default_factory=list)
    # Whether it gets a task parameter. A step can have only MAX_ITEM_SOURCES of them.
    enabled: bool = True
    # Value nodes with the same pair_with run their items together rather than in combination.
    pair_with: str = ""
    # The job parameter's default range: item numbers, or an Integer Values node's values. A dynamic
    # prompt's is "0", which chooses at random.
    range: str = ""
    node_id: str = ""
    # The input of a dynamic prompt.
    key: str | None = None
    # What the panel calls it. The label names the node.
    title: str = ""

    def __post_init__(self) -> None:
        self.range = self.range or f"1-{self.count}"
        self.title = self.title or self.label


@dataclass
class WorkItems:
    """What plan_work_items found in a workflow."""

    # The workflow with each split node marked, and dynamic prompts restored.
    workflow: dict[str, Any]
    # Every source found, in workflow order.
    sources: list[ItemSource] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def active(self) -> list[ItemSource]:
        """The sources whose items run as separate tasks."""
        return [source for source in self.sources if source.enabled]


def _identifier(text: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_")
    return ("_" + name if name[:1].isdigit() else name)[:48]


def _source_name(kind: str, node_id: str, node: dict[str, Any], key: str | None) -> str:
    """A value node's title, when it has been renamed, or the kind and node ID."""
    title = (node.get("_meta") or {}).get("title")
    class_type = str(node.get("class_type"))
    if class_type in VALUE_NODES and title and title != _DEFAULT_TITLES[class_type]:
        name = _identifier(str(title))
        if name:
            return name
    # A node inside a subgraph has an API ID like "57:27".
    name = kind.capitalize() + re.sub(r"[^A-Za-z0-9_]", "_", node_id)
    return name + "_" + re.sub(r"[^A-Za-z0-9_]", "_", key) if key else name


def _renamed_title(node: dict[str, Any]) -> str | None:
    """The title an artist gave a value node or Create List node, if they renamed it."""
    title = (node.get("_meta") or {}).get("title")
    default = _DEFAULT_TITLES.get(str(node.get("class_type")), "Create List")
    return str(title).strip() or None if title and title != default else None


def _connection_title(node_id: str, workflow: dict[str, Any]) -> str | None:
    """What a node's output connects to, such as "Image Scale · width" or "Image Scale · width +1"."""
    connections = [
        (other, key)
        for other in workflow.values()
        for key, value in (other.get("inputs") or {}).items()
        if isinstance(value, list)
        and len(value) == 2
        and isinstance(value[1], int)
        and str(value[0]) == str(node_id)
    ]
    if not connections:
        return None
    other, key = connections[0]
    name = (other.get("_meta") or {}).get("title") or other.get("class_type")
    more = f" +{len(connections) - 1}" if len(connections) > 1 else ""
    return f"{name} · {key}{more}"


def _source_title(source: ItemSource, workflow: dict[str, Any]) -> str:
    """
    Names a source for the panel by what it changes: its node's given title, what its output
    connects to or a dynamic prompt's text, and otherwise its label.
    """
    node = workflow[source.node_id]
    if source.kind == "prompt":
        text = str((node.get("inputs") or {}).get(source.key or "", ""))
        shown = text[:MAX_PROMPT_TITLE] + ("…" if len(text) > MAX_PROMPT_TITLE else "")
        return f'{source.label}: "{shown}"' if shown else source.label
    if source.kind == "folder":
        return source.label
    return _renamed_title(node) or _connection_title(source.node_id, workflow) or source.label


def _value_source(
    node_id: str, node: dict[str, Any], input_dir: Path | None
) -> tuple[ItemSource | None, str | None]:
    """
    Marks a value node with its preview item, which the task script outputs when the node isn't
    split, and returns its source when it has more than one item, or an error.
    """
    class_type = node["class_type"]
    inputs = node.get("inputs", {})
    meta = node["_meta"]
    label = f"{_DEFAULT_TITLES[class_type]} {node_id}"
    preview = inputs.get("preview_item", 1)
    preview = preview if isinstance(preview, int) and preview >= 1 else 1
    pair_with = inputs.get("pair_with")
    pair_with = pair_with.strip() if isinstance(pair_with, str) else ""
    if class_type == IMAGE_NODE:
        folder = inputs.get("folder")
        directory = input_path(input_dir, folder) if input_dir else None
        files = image_files(directory) if directory else []
        if not files:
            return None, f"{label} has no images in the folder {folder!r} of the input directory."
        meta[FILES_META] = [f"{folder}/{name}" for name in files]
        values = files
        kind = "image"
    elif not isinstance(inputs.get("values"), str):
        return None, f"{label} needs its values typed in, not connected."
    elif class_type == INTEGER_NODE:
        try:
            numbers = integer_values(inputs["values"])
        except ValueError as e:
            return None, f"{label}: {e}"
        values = [str(number) for number in numbers]
        kind = "integer"
    else:
        values = string_values(inputs["values"])
        kind = "string"
    if not values:
        return None, f"{label} has no values."
    if preview > len(values):
        return None, f"{label} has {len(values)} items, so it can't preview item {preview}."
    if kind == "image":
        meta[PREVIEW_META] = meta[FILES_META][preview - 1]
    else:
        meta[PREVIEW_META] = int(values[preview - 1]) if kind == "integer" else values[preview - 1]
    if len(values) < 2:
        return None, None
    source = ItemSource("", label, len(values), kind, values, pair_with=pair_with, node_id=node_id)
    if kind == "integer":
        source.range = _integer_range_expression(inputs["values"])
        if len(source.range) > 1024:
            return None, f"{label} has too many numbers and ranges. Use fewer, longer ranges."
    return source, None


def plan_work_items(
    api_workflow: dict[str, Any],
    ui_workflow: Any,
    input_dir: Path | None = None,
) -> WorkItems:
    """
    Finds the values in a workflow that the comfy-cli job can run as separate tasks: the items of
    Create List nodes, the images in the folders of folder loaders, the values of the Deadline
    value nodes and the prompts that dynamic prompts can choose. Each is its own source of items,
    and tasks run every combination of them, except value nodes that are paired. The frontend
    chooses each dynamic prompt at random, so the original text, which the UI workflow keeps, is
    restored for the task script to choose from, by default at random. Raises SubmitError for a
    value node the job can't run.
    """
    workflow = copy.deepcopy(api_workflow)
    plan = WorkItems(workflow)
    errors = []
    ui_nodes = _index_ui_nodes(ui_workflow)
    for node_id, node in workflow.items():
        class_type = node.get("class_type")
        inputs = node.get("inputs", {})
        meta = node.setdefault("_meta", {})
        source: ItemSource | None = None
        if class_type == "CreateList":
            count = sum(1 for key in inputs if _LIST_ITEM_INPUT.fullmatch(key))
            if count > 1:
                source = ItemSource("", f"Create List {node_id}", count, node_id=node_id)
        elif class_type in FOLDER_LOADERS:
            folder = inputs.get("folder")
            directory = input_path(input_dir, folder) if input_dir else None
            files = image_files(directory) if directory else []
            if len(files) > 1:
                meta[FILES_META] = [f"{folder}/{name}" for name in files]
                source = ItemSource(
                    "", f"Folder {folder} ({node_id})", len(files), "folder", files, node_id=node_id
                )
        elif class_type in VALUE_NODES:
            source, error = _value_source(node_id, node, input_dir)
            if error:
                errors.append(error)
        if source:
            source.name = _source_name(source.kind, node_id, node, None)
            plan.sources.append(source)
        templates = _dynamic_prompt_templates(ui_nodes, str(node_id))
        expansions = {template: expand_dynamic_prompt(template) for template in templates}
        dynamic: dict[str, list[str]] = {}
        for key, value in inputs.items():
            if not isinstance(value, str):
                continue
            for template in templates:
                # The frontend only chooses for widgets it knows take dynamic prompts.
                if len(expansions[template]) > 1 and (
                    value == template or value in expansions[template]
                ):
                    inputs[key] = template
                    dynamic[key] = expansions[template]
                    break
        if dynamic:
            # Each dynamic prompt maps to its task parameter, or "" to choose at random.
            meta[DYNAMIC_PROMPTS_META] = {key: "" for key in dynamic}
            several = len(dynamic) > 1
            for key, prompts in dynamic.items():
                if len(prompts) > MAX_LIST_ITEMS:
                    plan.warnings.append(
                        f"A dynamic prompt has more than {MAX_LIST_ITEMS} combinations, so each "
                        "task chooses one at random."
                    )
                    continue
                prompt = ItemSource(
                    _source_name("prompt", node_id, node, key if several else None),
                    f"Prompt {node_id}" + (f" ({key})" if several else ""),
                    len(prompts),
                    "prompt",
                    prompts,
                    range="0",
                    node_id=node_id,
                    key=key,
                )
                plan.sources.append(prompt)
    if errors:
        raise SubmitError(" ".join(errors))
    names: set[str] = set()
    for source in plan.sources:
        if source.name in names:
            source.name = f"{source.name}_{re.sub(r'[^A-Za-z0-9_]', '_', source.node_id)}"
        names.add(source.name)
        source.title = _source_title(source, workflow)
    active = plan.active
    for source in active[MAX_ITEM_SOURCES:]:
        source.enabled = False
    if len(active) > MAX_ITEM_SOURCES:
        plan.warnings.append(
            f"Deadline Cloud runs up to {MAX_ITEM_SOURCES} lists as separate tasks, so these run "
            f"whole in each task: {', '.join(s.label for s in active[MAX_ITEM_SOURCES:])}."
        )
    _check_pairs(plan)
    # Mark the nodes of the sources that run as separate tasks. The task script runs others whole.
    for source in plan.active:
        meta = workflow[source.node_id]["_meta"]
        if source.key is None:
            meta[ITEM_META] = source.name
        else:
            meta[DYNAMIC_PROMPTS_META][source.key] = source.name
    for node in workflow.values():
        if not node["_meta"]:
            del node["_meta"]
    return plan


def _check_pairs(plan: WorkItems) -> None:
    """Unpairs value nodes whose pair has different numbers of items, with a warning."""
    pairs: dict[str, list[ItemSource]] = {}
    for source in plan.active:
        if source.pair_with:
            pairs.setdefault(source.pair_with, []).append(source)
    for pair_with, sources in pairs.items():
        if len({source.count for source in sources}) > 1:
            plan.warnings.append(
                f"The value nodes paired as {pair_with!r} have different numbers of items ("
                + ", ".join(f"{source.label}: {source.count}" for source in sources)
                + "), so every combination of them runs."
            )
            for source in sources:
                source.pair_with = ""


def combination(sources: list[ItemSource]) -> str | None:
    """
    The OpenJD combination expression that runs paired sources' items together, such as
    "(Prompt, Lora) * Seed", or None when every combination runs, which is OpenJD's default.
    """
    terms: dict[str, list[str]] = {}
    for source in sources:
        terms.setdefault(source.pair_with or f"\0{source.name}", []).append(source.name)
    if all(len(names) == 1 for names in terms.values()):
        return None
    return " * ".join(
        f"({', '.join(names)})" if len(names) > 1 else names[0] for names in terms.values()
    )


def _default_counts(sources: list[ItemSource]) -> dict[str, tuple[list[str], int]]:
    """The names and number of items of each source, or pair of sources, by default."""
    counts: dict[str, tuple[list[str], int]] = {}
    for source in sources:
        names, _ = counts.setdefault(source.pair_with or f"\0{source.name}", ([], 1))
        names.append(source.name)
        count = 1 if source.range == "0" else source.count
        counts[source.pair_with or f"\0{source.name}"] = (names, count)
    return counts


def task_count(sources: list[ItemSource]) -> int:
    """How many tasks the sources run by default, each pair counting once."""
    return math.prod(count for _, count in _default_counts(sources).values())


def task_summary(sources: list[ItemSource]) -> str:
    """Describes how many tasks the sources run by default, such as "12 tasks: List64 3 × Seed 4"."""
    count = task_count(sources)
    terms = [
        f"{' + '.join(names)} {items}"
        for names, items in _default_counts(sources).values()
        if items > 1
    ]
    return f"{count} task{'' if count == 1 else 's'}" + (f": {' × '.join(terms)}" if terms else "")


def _item_parameters(source: ItemSource) -> dict[str, Any]:
    """The job parameter that holds the range of a source's items to run."""
    shown = []
    for index, value in enumerate(source.values, start=1):
        shown.append(f"{index}: {value[:60]}{'...' if len(value) > 60 else ''}")
        if index == 10 and source.count > 10:
            shown.append(f"and {source.count - 10} more")
            break
    if source.kind == "integer":
        description = (
            f"The values of {source.label} to run as separate tasks, as numbers and ranges such "
            'as "1-100", "1-20:2" or "7,42,99".'
        )
    else:
        description = (
            f"The items of {source.label} to run as separate tasks, numbered from 1, for example "
            f'"1-{source.count}" or "2,3".'
            + (' "0" runs them all in one task.' if source.kind in ("list", "folder") else "")
            + (
                ' "0", the default, chooses one at random for each task, as ComfyUI does.'
                if source.kind == "prompt"
                else ""
            )
        )
    description += " Every combination of the work items is a task"
    if source.pair_with:
        description += (
            f", except that the value nodes paired as {source.pair_with!r} run their items "
            "together, so keep the same number of them"
        )
    description += "."
    if shown and source.kind != "integer":
        description += " Items: " + "; ".join(shown) + "."
    return {
        "name": f"{source.name}Items",
        "type": "STRING",
        "userInterface": {
            "control": "LINE_EDIT",
            "label": source.label[:64],
            "groupLabel": WORK_ITEMS_GROUP,
        },
        "default": source.range,
        "description": description[:2048],
    }


def template_supports_work_items(template: Path) -> bool:
    """Whether a job template's task script has the line that selects each task's items."""
    try:
        return _ITEMS_PLACEHOLDER_LINE.search(template.read_text(encoding="utf-8")) is not None
    except OSError:
        return False


def add_work_items(template: dict[str, Any], sources: list[ItemSource]) -> dict[str, Any]:
    """
    Returns a copy of a job template with a job parameter and a task parameter for each source of
    work items, in the step whose task script has the ITEMS placeholder.
    """
    template = copy.deepcopy(template)
    definitions = template.setdefault("parameterDefinitions", [])
    # Work items follow the workflow parameters.
    at = len(definitions)
    for index, definition in enumerate(definitions):
        if (definition.get("userInterface") or {}).get("groupLabel") == "Workflow Parameters":
            at = index + 1
    definitions[at:at] = [_item_parameters(source) for source in sources]
    items = ", ".join(f'"{source.name}": {{{{Task.Param.{source.name}}}}}' for source in sources)
    for step in template.get("steps", []):
        files = step.get("script", {}).get("embeddedFiles", [])
        if not any(_ITEMS_PLACEHOLDER_LINE.search(file.get("data", "")) for file in files):
            continue
        for file in files:
            file["data"] = _ITEMS_PLACEHOLDER_LINE.sub(
                lambda match: match.group().replace(ITEMS_PLACEHOLDER, f"ITEMS = {{{items}}}"),
                file.get("data", ""),
            )
        space = step.setdefault("parameterSpace", {})
        space.setdefault("taskParameterDefinitions", []).extend(
            {"name": source.name, "type": "INT", "range": f"{{{{Param.{source.name}Items}}}}"}
            for source in sources
        )
        expression = combination(sources)
        if expression:
            space["combination"] = expression
    return template


def _dump_template(job_template: dict[str, Any]) -> str:
    """Writes a job template as YAML, with multiline strings, such as scripts, as literal blocks."""
    import yaml

    class TemplateDumper(yaml.SafeDumper):
        pass

    TemplateDumper.add_representer(
        str,
        lambda dumper, value: dumper.represent_scalar(
            "tag:yaml.org,2002:str", value, style="|" if "\n" in value else None
        ),
    )
    return yaml.dump(job_template, Dumper=TemplateDumper, sort_keys=False, width=100)


def _embedded_models(value: Any):
    """Yields every model entry that ComfyUI stores in a node's properties.models."""
    if isinstance(value, dict):
        models = (
            value.get("properties", {}).get("models")
            if isinstance(value.get("properties"), dict)
            else None
        )
        if isinstance(models, list):
            yield from (model for model in models if isinstance(model, dict))
        for child in value.values():
            yield from _embedded_models(child)
    elif isinstance(value, list):
        for child in value:
            yield from _embedded_models(child)


def _model_entry(model: dict[str, Any]) -> str | None:
    """Formats one embedded model as a FOLDER/FILE=URL[#sha256=HEX] entry, or None if it's unsafe."""
    directory, name, url = model.get("directory"), model.get("name"), model.get("url")
    if not (isinstance(directory, str) and isinstance(name, str) and isinstance(url, str)):
        return None
    url = _normalized_model_url(url)
    if not (
        _MODEL_DIRECTORY.fullmatch(directory)
        and _MODEL_NAME.fullmatch(name)
        and _MODEL_URL.fullmatch(url)
    ):
        logger.warning(
            "Not adding model %r from %r to ModelDownloads: unsupported characters", name, url
        )
        return None
    # A template URL names the original file. A replaced model keeps that URL, so downloading it
    # would fetch the wrong file. Leave it out; the local file is attached instead.
    if _url_names_different_file(name, url):
        logger.warning(
            "Not adding model %r from %r to ModelDownloads: the URL names a different file",
            name,
            url,
        )
        return None
    entry = f"{directory}/{name}={url}"
    digest = model.get("hash")
    if (
        str(model.get("hash_type", "")).upper() == "SHA256"
        and isinstance(digest, str)
        and _SHA256.fullmatch(digest)
    ):
        entry += f"#sha256={digest.lower()}"
    return entry


def _normalized_model_url(url: str) -> str:
    """Removes the query strings a download doesn't need, which the job script can't expand."""
    civitai = _CIVITAI_DOWNLOAD_URL.fullmatch(url)
    return civitai.group(1) if civitai else url.removesuffix("?download=true")


def _url_names_different_file(name: str, url: str) -> bool:
    """The URL's file name differs from the model file the workflow loads."""
    # A CivitAI URL ends in a version ID, so it has no file name to compare.
    if _CIVITAI_DOWNLOAD_URL.fullmatch(url):
        return False
    url_file = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    return url_file != name.rsplit("/", 1)[-1]


def _mismatched_link_name(model: dict[str, Any]) -> str | None:
    """The workflow's model name when its URL names a different file, else None."""
    directory, name, url = model.get("directory"), model.get("name"), model.get("url")
    if not (isinstance(directory, str) and isinstance(name, str) and isinstance(url, str)):
        return None
    url = _normalized_model_url(url)
    if not (
        _MODEL_DIRECTORY.fullmatch(directory)
        and _MODEL_NAME.fullmatch(name)
        and _MODEL_URL.fullmatch(url)
    ):
        return None
    if not _url_names_different_file(name, url):
        return None
    return name.replace("\\", "/")


def models_with_mismatched_links(ui_workflow: Any, api_workflow: dict[str, Any]) -> list[str]:
    """Loaded models whose URL names a different file, so Model Downloads will not fetch them."""
    used = {
        value.replace("\\", "/")
        for node in api_workflow.values()
        for value in node.get("inputs", {}).values()
        if isinstance(value, str)
    }
    return sorted(
        {
            name
            for model in _embedded_models(ui_workflow)
            if (name := _mismatched_link_name(model)) is not None and name in used
        }
    )


def model_downloads(ui_workflow: Any, api_workflow: dict[str, Any]) -> str | None:
    """Only models the API workflow loads are included, so bypassed nodes' models are skipped."""
    used = {
        value
        for node in api_workflow.values()
        for value in node.get("inputs", {}).values()
        if isinstance(value, str)
    }
    entries: dict[str, str] = {}
    for model in _embedded_models(ui_workflow):
        if model.get("name") in used:
            entry = _model_entry(model)
            if entry:
                entries.setdefault(entry.split("=", 1)[0], entry)
    return " ".join(entries.values()) or None


def models_without_links(api_workflow: dict[str, Any], downloads: str | None) -> list[str]:
    """
    Lists the model files the workflow loads that no ModelDownloads entry downloads. The submitter
    attaches a local copy when this machine has the file. Otherwise the job fails unless the
    workers already have it, for example in the model cache or extra model paths.
    """
    linked = set()
    for entry in (downloads or "").split():
        # FOLDER/FILE=URL, where the workflow names FILE, which can include subfolders.
        linked.add(entry.split("=", 1)[0].split("/", 1)[-1])
    models = {
        value.replace("\\", "/")
        for node in api_workflow.values()
        for value in node.get("inputs", {}).values()
        if isinstance(value, str) and value.lower().endswith(MODEL_EXTENSIONS)
    }
    return sorted(models - linked)


def _workflow_nodes(value: Any):
    """Yields every node of a UI-format workflow, including the nodes inside subgraphs."""
    if not isinstance(value, dict):
        return
    for node in value.get("nodes") or []:
        if isinstance(node, dict):
            yield node
    definitions = value.get("definitions")
    if isinstance(definitions, dict):
        for subgraph in definitions.get("subgraphs") or []:
            yield from _workflow_nodes(subgraph)


def _node_pack_entry(node_id: Any, version: Any) -> tuple[str, str] | None:
    """Returns the pack ID and its ID@VERSION entry, or None for a built-in or unsafe ID."""
    if not isinstance(node_id, str) or node_id in BUILT_IN_NODE_PACKS:
        return None
    if not _NODE_ID.fullmatch(node_id):
        logger.warning("Not adding node pack %r to CustomNodes: unsupported characters", node_id)
        return None
    # Without a version, the job installs the latest one.
    if isinstance(version, str) and _NODE_VERSION.fullmatch(version):
        return node_id, f"{node_id}@{version}"
    return node_id, node_id


def read_node_pack(pack_dir: Path) -> tuple[str, str | None] | None:
    """
    Reads the Comfy Registry ID and version of a node pack folder from its pyproject.toml. Returns
    None unless the pack was installed from the registry, which leaves a .tracking file, because a
    pack cloned from git might not be in the registry or might differ from the registry version.
    """
    if not (pack_dir / ".tracking").is_file():
        return None
    try:
        text = (pack_dir / "pyproject.toml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    # tomllib needs Python 3.11, so read the two keys of the [project] table directly.
    project = re.search(r"^\[project\][ \t]*$(.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
    if not project:
        return None
    values = dict(
        re.findall(r"^(name|version)\s*=\s*[\"']([^\"']*)[\"']", project.group(1), re.MULTILINE)
    )
    if "name" not in values:
        return None
    return values["name"], values.get("version")


def _registry_node_packs(
    ui_workflow: Any,
    api_workflow: dict[str, Any],
    installed_packs: Mapping[str, tuple[str, str | None]] | None,
) -> tuple[dict[str, str], set[Any]]:
    """
    The registry packs the workflow uses, as pack ID to ID@VERSION entry, and the node types the
    API workflow runs that have a usable registry identity, including built-in ones.
    """
    used = {node.get("class_type") for node in api_workflow.values()}
    packs: dict[str, str] = {}
    tagged: set[Any] = set()
    identified: set[Any] = set()
    for node in _workflow_nodes(ui_workflow):
        properties = node.get("properties")
        if node.get("type") not in used or not isinstance(properties, dict):
            continue
        if "cnr_id" in properties:
            tagged.add(node.get("type"))
        if properties.get("cnr_id") in BUILT_IN_NODE_PACKS:
            identified.add(node.get("type"))
        pack = _node_pack_entry(properties.get("cnr_id"), properties.get("ver"))
        if pack:
            packs.setdefault(*pack)
            identified.add(node.get("type"))
    for class_type in sorted(used - tagged, key=str):
        pack = _node_pack_entry(*(installed_packs or {}).get(class_type, (None, None)))
        if pack:
            packs.setdefault(*pack)
            identified.add(class_type)
    return packs, identified


def custom_nodes(
    ui_workflow: Any,
    api_workflow: dict[str, Any],
    installed_packs: Mapping[str, tuple[str, str | None]] | None = None,
) -> str | None:
    """
    Lists the Comfy Registry node packs that the workflow uses, as space-separated ID@VERSION
    entries. Only node types the API workflow runs are included, so bypassed nodes' packs are
    skipped. The cnr_id and ver that ComfyUI-Manager saves in each node's properties come first.
    installed_packs maps other node types to the pack this ComfyUI loaded them from, because the
    Manager only saves those properties with its legacy UI.
    """
    packs, _ = _registry_node_packs(ui_workflow, api_workflow, installed_packs)
    return " ".join(packs.values()) or None


def unregistered_node_types(
    ui_workflow: Any,
    api_workflow: dict[str, Any],
    installed_packs: Mapping[str, tuple[str, str | None]] | None = None,
) -> list[str]:
    """
    The node types the API workflow runs with no usable registry identity, except the value nodes,
    which the job replaces.
    """
    _, identified = _registry_node_packs(ui_workflow, api_workflow, installed_packs)
    used = {node.get("class_type") for node in api_workflow.values()} - set(VALUE_NODES)
    return sorted(class_type for class_type in used - identified if isinstance(class_type, str))


def local_node_packs(
    ui_workflow: Any,
    api_workflow: dict[str, Any],
    installed_packs: Mapping[str, tuple[str, str | None]] | None,
    loaded_dirs: Mapping[str, Path],
) -> tuple[dict[str, Path], list[str]]:
    """
    The local packs to attach, by folder or file name, and the node types the workflow runs that
    have no usable registry identity. loaded_dirs maps a node type to the custom node pack folder,
    or single .py file, this ComfyUI loaded it from. Each path is listed once, however many of
    its nodes are used.
    """
    unregistered = unregistered_node_types(ui_workflow, api_workflow, installed_packs)
    packs: dict[str, Path] = {}
    for class_type in unregistered:
        path = loaded_dirs.get(class_type)
        if path is not None:
            packs.setdefault(path.name, path)
    return dict(sorted(packs.items())), unregistered


def template_defines_parameter(template: Path, name: str) -> bool:
    """Whether a YAML or JSON job template defines the job parameter `name`."""
    pattern = (
        rf"""^\s*(-\s*)?name:\s*['"]?{re.escape(name)}['"]?\s*$|"name"\s*:\s*"{re.escape(name)}\""""
    )
    try:
        return re.search(pattern, template.read_text(encoding="utf-8"), re.MULTILINE) is not None
    except OSError:
        return False


def safe_name(name: str) -> str:
    stem = name[: -len(".json")] if name.lower().endswith(".json") else name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    return stem[:100] or "workflow"


def get_safe_path(user_input: str, base_dir: Path | str) -> Path:
    """
    user_input resolved against base_dir. Raises ValueError when the resolved path would fall
    outside base_dir, for example through ".." segments or a symlink. realpath resolves both
    ".." and symlinks, and the boundary check compares against base_dir plus a path separator
    so a sibling such as "base_dir_evil" doesn't pass for a child of base_dir.
    """
    base = os.path.realpath(base_dir)
    target = os.path.realpath(os.path.join(base, user_input))
    if target != base and not target.startswith(base + os.sep):
        raise ValueError("Access Denied: Path injection attempt detected.")
    return Path(target)


def job_template_with_items(template: Path, sources: list[ItemSource] | None) -> dict[str, Any]:
    """Reads a job template, with a parameter for each of the workflow's sources of work items."""
    import yaml

    try:
        text = template.read_text(encoding="utf-8")
        base = json.loads(text) if template.suffix.lower() == ".json" else yaml.safe_load(text)
    except (OSError, ValueError, yaml.YAMLError) as e:
        raise SubmitError(f"Couldn't read the job template {template}: {e}")
    if not isinstance(base, dict):
        raise SubmitError(f"The job template {template} isn't a YAML or JSON object.")
    return add_work_items(base, sources) if sources else base


# The worker capabilities that the panel has their own fields for. Other names are custom ones.
HOST_AMOUNTS = (
    "amount.worker.vcpu",
    "amount.worker.memory",
    "amount.worker.gpu",
    "amount.worker.gpu.memory",
    "amount.worker.disk.scratch",
)
HOST_ATTRIBUTES = ("attr.worker.os.family", "attr.worker.cpu.arch")
# OpenJD saves these first identifiers for its own capabilities and variables.
_RESERVED_CAPABILITY_NAMES = ("worker", "job", "step", "task")
_CAPABILITY_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{0,63}(\.[a-zA-Z_][a-zA-Z0-9_]{0,63})*")
_ATTRIBUTE_VALUE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_-]{0,99}")
MAX_JOB_DESCRIPTION = 2048


def _capability_name(name: Any, prefix: str, standard: tuple[str, ...]) -> str:
    if name in standard:
        return name
    custom = name[len(prefix) :] if isinstance(name, str) and name.startswith(prefix) else ""
    if not _CAPABILITY_NAME.fullmatch(custom) or custom.split(".")[0] in _RESERVED_CAPABILITY_NAMES:
        reserved = ", ".join(_RESERVED_CAPABILITY_NAMES)
        raise SubmitError(
            f"Host requirement {name!r} needs a name of letters, numbers and underscores, "
            f"separated by periods, that doesn't start with {reserved}."
        )
    return name


def _host_amount(amount: Any) -> dict[str, Any]:
    if not isinstance(amount, dict):
        raise SubmitError("Each host requirement amount must be an object.")
    name = _capability_name(amount.get("name"), "amount.", HOST_AMOUNTS)
    result: dict[str, Any] = {"name": name}
    for key in ("min", "max"):
        value = amount.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise SubmitError(f"The {key} of host requirement {name} must be a positive number.")
        result[key] = value
    if len(result) == 1:
        raise SubmitError(f"Host requirement {name} needs a min or a max.")
    if result.get("min", 0) > result.get("max", float("inf")):
        raise SubmitError(f"The min of host requirement {name} is more than its max.")
    return result


def _host_attribute(attribute: Any) -> dict[str, Any]:
    if not isinstance(attribute, dict):
        raise SubmitError("Each host requirement attribute must be an object.")
    name = _capability_name(attribute.get("name"), "attr.", HOST_ATTRIBUTES)
    modes = [mode for mode in ("anyOf", "allOf") if mode in attribute]
    if len(modes) != 1:
        raise SubmitError(f"Host requirement {name} needs either anyOf or allOf.")
    values = attribute[modes[0]]
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(value, str) and _ATTRIBUTE_VALUE.fullmatch(value) for value in values)
    ):
        raise SubmitError(
            f"Host requirement {name} needs values of letters, numbers, underscores and dashes, "
            "each starting with a letter or underscore."
        )
    return {"name": name, modes[0]: values}


def host_requirements(value: Any) -> dict[str, Any] | None:
    """
    Checks the panel's host requirements. None keeps the job template's, and an empty result
    removes them, so the job runs on any worker. Raises SubmitError.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SubmitError("hostRequirements must be an object.")
    result: dict[str, Any] = {}
    for key, check in (("attributes", _host_attribute), ("amounts", _host_amount)):
        listed = value.get(key) or []
        if not isinstance(listed, list):
            raise SubmitError(f"hostRequirements.{key} must be a list.")
        if listed:
            result[key] = [check(item) for item in listed]
    return result


def job_description(value: Any) -> str | None:
    """Checks the panel's job description. Empty keeps the job template's. Raises SubmitError."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise SubmitError("description must be a string.")
    if len(value) > MAX_JOB_DESCRIPTION:
        raise SubmitError(f"The job description can't be over {MAX_JOB_DESCRIPTION} characters.")
    return value


def template_host_requirements(job_template: dict[str, Any]) -> dict[str, Any]:
    """The host requirements of the job template's first step, for the panel to start from."""
    steps = job_template.get("steps")
    first = steps[0] if isinstance(steps, list) and steps else None
    requirements = first.get("hostRequirements") if isinstance(first, dict) else None
    return requirements if isinstance(requirements, dict) else {}


def create_job_bundle(
    template: Path,
    workflow: dict[str, Any],
    name: str,
    dest_dir: Path,
    now: datetime | None = None,
    sources: list[ItemSource] | None = None,
    requirements: dict[str, Any] | None = None,
    description: str | None = None,
) -> tuple[Path, Path]:
    """
    Creates <dest_dir>/<timestamp>_<name>/ with the job template and the API-format workflow, and
    returns the bundle directory and the workflow file. They have to outlive the submitter dialog
    and the job attachments upload. The template is copied as it is, unless the workflow has
    sources of work items to add to it, or the artist changed its host requirements or description.
    """
    suffix = ".json" if template.suffix.lower() == ".json" else ".yaml"
    text = None
    if sources or requirements is not None or description:
        job_template = job_template_with_items(template, sources)
        if description:
            job_template["description"] = description
        if requirements is not None:
            for step in job_template.get("steps") or []:
                if requirements:
                    step["hostRequirements"] = requirements
                else:
                    step.pop("hostRequirements", None)
        text = (
            json.dumps(job_template, indent=2)
            if suffix == ".json"
            else _dump_template(job_template)
        )
    now = now or datetime.now(timezone.utc)
    leaf = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}_{safe_name(name)}"
    # The name comes from the request. safe_name strips traversal, and get_safe_path is the
    # belt-and-suspenders check that the bundle still lands inside dest_dir.
    try:
        bundle_dir = get_safe_path(leaf, dest_dir)
    except ValueError as e:
        raise SubmitError(str(e)) from e
    bundle_dir.mkdir(parents=True)
    if text is None:
        shutil.copyfile(template, bundle_dir / f"template{suffix}")
    else:
        (bundle_dir / f"template{suffix}").write_text(text, encoding="utf-8")
    workflow_path = bundle_dir / f"{safe_name(name)}_api.json"
    workflow_path.write_text(json.dumps(workflow, indent=2), encoding="utf-8")
    return bundle_dir, workflow_path
