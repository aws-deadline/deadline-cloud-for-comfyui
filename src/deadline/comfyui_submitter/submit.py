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
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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

# The oldest CLI the submitter supports. Older libraries don't read BOOL job parameters.
MIN_CLI_VERSION = (0, 60, 8)
# The first botocore whose Deadline Cloud API can send BOOL job parameters. The CLI accepts older
# ones, which only fail when they create the job.
MIN_BOTOCORE_VERSION = (1, 43, 105)


logger = logging.getLogger(__name__)


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
        # PowerShell writes in the console code page unless told otherwise, and the server reads
        # the paths as UTF-8, so paths with accents or dashes would come back garbled.
        utf8 = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        if kind == "folder":
            script = (
                utf8 + "Add-Type -AssemblyName System.Windows.Forms;"
                "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
                f"$d.Description='{prompt}';"
                "if($d.ShowDialog() -eq 'OK'){[Console]::Out.Write($d.SelectedPath)}"
            )
        else:
            script = (
                utf8 + "Add-Type -AssemblyName System.Windows.Forms;"
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


# Job bundles hold the artist's workflow, prompts and file paths, so only the user that runs
# ComfyUI can open them. On Windows, files inherit the ACLs of the user directory instead.
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def make_private_dir(path: Path) -> None:
    """Creates a directory, if it's missing, and limits it to its owner."""
    path.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    # mkdir's mode goes through the umask, and an existing directory keeps its own.
    os.chmod(path, PRIVATE_DIR_MODE)


def _private_opener(path: str, flags: int) -> int:
    """Opens a new file with only its owner's permissions."""
    return os.open(path, flags, PRIVATE_FILE_MODE)


def write_private_text(path: Path, text: str) -> None:
    """Writes a file that only its owner can read, whatever the umask."""
    with open(path, "w", encoding="utf-8", opener=_private_opener) as file:
        file.write(text)
    # An existing file keeps its mode when it's opened.
    os.chmod(path, PRIVATE_FILE_MODE)


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
    # ComfyUI always writes these as a string and an object, so anything else didn't come from it.
    for node_id, node in workflow.items():
        if not isinstance(node["class_type"], str) or not node["class_type"]:
            raise SubmitError(f"Node {_quoted(node_id)} has no node type in the workflow.")
        if not isinstance(node.get("inputs", {}), dict):
            raise SubmitError(f"Node {_quoted(node_id)} has inputs that aren't an object.")
        if node.get("_meta") is not None and not isinstance(node["_meta"], dict):
            raise SubmitError(f"Node {_quoted(node_id)} has a _meta that isn't an object.")
        if node["class_type"] == "CreateList":
            for key in node.get("inputs", {}):
                match = _ANY_LIST_ITEM_INPUT.fullmatch(str(key))
                if match and not _LIST_ITEM_INPUT.fullmatch(str(key)):
                    raise SubmitError(
                        f"Create List {_quoted(node_id)} has an input numbered past "
                        f"{_MAX_LIST_ITEM_DIGITS} digits, which ComfyUI doesn't make."
                    )
    return workflow


def _quoted(value: Any) -> str:
    """A value from a request, shortened and on one line, to quote in an error message."""
    text = re.sub(r"\s+", " ", str(value))
    return text if len(text) <= 40 else text[:39] + "…"


# Task lists: the values that the comfy-cli job runs as separate tasks. Each source of them in the
# workflow becomes its own task parameter, so tasks run every combination of their items, unless
# value nodes are paired. The job template's task script selects each task's item, using these
# names, which test_template_script.py keeps in step.
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
# The _meta key that marks the nodes the task script splits, with the task parameter of each.
ITEM_META = "deadline_item"
# What a value node outputs in tasks that don't split it: its preview item.
PREVIEW_META = "deadline_preview"
# The images of a folder, as paths in the input directory, in the order its items are numbered.
FILES_META = "deadline_files"
# A paired Deadline Integer Values node's numbers, in the order its items are numbered.
VALUES_META = "deadline_values"
# The most numbers a Deadline Integer Values node can list.
MAX_INTEGER_VALUES = 10000
# The most digits a number in Deadline Integer Values can have, as many as a 64-bit seed.
MAX_INTEGER_DIGITS = 20
# OpenJD allows 16 task parameters in a step.
MAX_ITEM_SOURCES = 16
# The line of the task script that create_job_bundle fills in with each task's items. A job
# template without it runs whole lists.
ITEMS_PLACEHOLDER = "ITEMS = {}"
# The group of the job parameters that hold each list's items to run.
TASKS_GROUP = "Tasks"

# A Create List's item inputs: input0, input1 and so on, or inputs.input0 inside a subgraph. The
# number is bounded so it's always safe to read, in the submitter and the task script.
_MAX_LIST_ITEM_DIGITS = 9
_LIST_ITEM_INPUT = re.compile(rf"(inputs\.)?input\d{{1,{_MAX_LIST_ITEM_DIGITS}}}", re.ASCII)
_ANY_LIST_ITEM_INPUT = re.compile(r"(inputs\.)?input\d+", re.ASCII)
# The titles ComfyUI gives the nodes that list tasks. Any other title is one the artist gave.
_DEFAULT_TITLES = {
    "CreateList": "Create List",
    "LoadImageDataSetFromFolder": "Load Image (from Folder)",
    STRING_NODE: "Deadline String Values",
    INTEGER_NODE: "Deadline Integer Values",
    IMAGE_NODE: "Deadline Image Values",
}
_ITEMS_PLACEHOLDER_LINE = re.compile(rf"^[ \t]*{re.escape(ITEMS_PLACEHOLDER)}[ \t]*$", re.MULTILINE)
_INTEGER_RANGE = re.compile(r"(-?\d+)(?:-(-?\d+)(?::(\d+))?)?")


# How a Deadline String Values node splits its text into values: one per line, or at lines of
# "---", so that a value can span several lines, such as a prompt with paragraphs.
SPLIT_BY_LINES = "lines"
SPLIT_BY_SEPARATOR = "--- lines"
SPLIT_BY_CHOICES = (SPLIT_BY_LINES, SPLIT_BY_SEPARATOR)


def string_values(text: Any, split_by: Any = SPLIT_BY_LINES) -> list[str]:
    """
    The values of a Deadline String Values node: one per non-empty line, or the text between lines
    of "---", without the blank lines and spaces around it. Lines end at newlines only:
    splitlines() would also split a value at characters such as U+2028. Raises ValueError for an
    unknown split_by.
    """
    lines = [line.rstrip("\r") for line in str(text).split("\n")]
    if split_by == SPLIT_BY_LINES:
        return [line.strip() for line in lines if line.strip()]
    if split_by != SPLIT_BY_SEPARATOR:
        raise ValueError(f"split_by is {split_by!r}, not one of {', '.join(SPLIT_BY_CHOICES)}.")
    values: list[str] = []
    block: list[str] = []
    for line in [*lines, "---"]:
        if line.strip() == "---":
            value = "\n".join(block).strip()
            if value:
                values.append(value)
            block = []
        else:
            block.append(line)
    return values


def first_line(value: str) -> str:
    """A value as the panel previews it: its first line, with an ellipsis if there's more."""
    line, _, rest = value.partition("\n")
    return f"{line} …" if rest.strip() else line


def _integer_ranges(text: Any) -> list[range]:
    ranges = []
    for part in re.sub(r"\s+", "", str(text)).split(","):
        if not part:
            continue
        match = _INTEGER_RANGE.fullmatch(part)
        if not match:
            raise ValueError(f"{part!r} isn't a number or a range such as 1-10 or 1-10:2.")
        # A 64-bit seed has 20 digits. Python can't read numbers past 4300 digits at all.
        if any(len(number.lstrip("-")) > MAX_INTEGER_DIGITS for number in match.groups() if number):
            raise ValueError(f"A number has more than {MAX_INTEGER_DIGITS} digits.")
        start = int(match[1])
        end = int(match[2]) if match[2] is not None else start
        step = int(match[3] or 1)
        if end < start or step < 1:
            raise ValueError(f"The range {part!r} has to count up.")
        # Counted before building the range, since len() can't count a range this long.
        if (end - start) // step + 1 > MAX_INTEGER_VALUES:
            raise ValueError(f"There are more than {MAX_INTEGER_VALUES} numbers.")
        ranges.append(range(start, end + 1, step))
    return ranges


def integer_values(text: Any, *, allow_repeats: bool = False) -> list[int]:
    """
    The values of a Deadline Integer Values node: numbers and OpenJD ranges, such as "1-4",
    "1-20:2" or "7, 42, 99". Only a paired node can repeat a number, such as a width that two
    frame sizes share.
    """
    values: list[int] = []
    ranges = _integer_ranges(text)
    for numbers in ranges:
        if len(values) + len(numbers) > MAX_INTEGER_VALUES:
            raise ValueError(f"There are more than {MAX_INTEGER_VALUES} numbers.")
        values.extend(numbers)
    if not allow_repeats and len(set(values)) < len(values):
        raise ValueError("A number is listed more than once.")
    # OpenJD also refuses parts that overlap without sharing a number, such as "1-9:2,4".
    spans = sorted((numbers[0], numbers[-1]) for numbers in ranges if numbers)
    if not allow_repeats and any(spans[i][1] >= spans[i + 1][0] for i in range(len(spans) - 1)):
        raise ValueError("Two parts overlap. Write each part's numbers apart, such as 1-3,5-9.")
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
class ItemSource:
    """A node whose items can run as separate tasks."""

    # The task parameter that selects the item, such as "List12". Its job parameter, "List12Items",
    # holds the range to run.
    name: str
    label: str
    count: int
    # "list", "folder", "string", "integer", "paired-integer" or "image".
    kind: str = "list"
    # The items, as far as the submitter knows them, for the job parameter's description.
    values: list[str] = field(default_factory=list)
    # Whether it gets a task parameter. A step can have only MAX_ITEM_SOURCES of them.
    enabled: bool = True
    # Value nodes with the same pair_with run their items together rather than in combination.
    pair_with: str = ""
    # The job parameter's default range: item numbers, or an Integer Values node's values.
    range: str = ""
    node_id: str = ""
    # What the panel calls it. The label names the node.
    title: str = ""

    def __post_init__(self) -> None:
        self.range = self.range or f"1-{self.count}"
        self.title = self.title or self.label

    @property
    def value_node(self) -> bool:
        """
        Whether it's a Deadline value node. They run their items as tasks even when the workflow
        runs as one task: in ComfyUI they output only their preview item.
        """
        return self.kind not in ("list", "folder")

    @property
    def parameter(self) -> str:
        """The job parameter that holds the range of items to run."""
        return f"{self.name}Items"


# OpenJD identifiers are at most 64 characters, and a list's job parameter adds "Items".
MAX_SOURCE_NAME = 64 - len("Items")


# One part of a range of item numbers, with spaces allowed around "-" and ":", as in the panel.
# Item numbers are capped at 9 digits, far past any list, so int() never reaches Python's limit.
_ITEM_RANGE_PART = re.compile(r"(\d{1,9})(?:\s*-\s*(\d{1,9})(?:\s*:\s*(\d{1,9}))?)?", re.ASCII)


# The longest range expression OpenJD takes for a task parameter.
MAX_RANGE_LENGTH = 1024


def check_item_range(source: ItemSource, value: Any) -> int:
    """
    Checks the range of a list's items to run, as OpenJD and the task script take it: "0" on its
    own for a list or folder, or item numbers from 1 to the list's count, in parts that don't
    overlap. An Integer Values field holds its numbers instead. The panel checks it against the
    plan it made, so this catches a list that changed since. Returns the number of tasks it runs.
    Raises SubmitError.
    """
    text = str(value).strip()
    if len(text) > MAX_RANGE_LENGTH:
        raise SubmitError(
            f"{source.title}: the field is longer than {MAX_RANGE_LENGTH} characters. Use fewer, "
            "longer ranges."
        )
    # An Integer Values field holds the numbers themselves, such as seeds.
    if source.kind == "integer":
        try:
            numbers = integer_values(text)
        except ValueError as e:
            raise SubmitError(f"{source.title}: {e}") from None
        if not numbers:
            raise SubmitError(f"{source.title}: add the numbers to run, such as 1-4 or 7,42.")
        return len(numbers)
    if text == "0":
        if not source.value_node:
            return 1
        raise SubmitError(
            f"{source.title}: use item numbers such as 1-{source.count} or 2,3. A value node's "
            "items always run as separate tasks, so it has no 0."
        )
    spans: list[tuple[int, int]] = []
    total = 0
    for part in text.split(","):
        match = _ITEM_RANGE_PART.fullmatch(part.strip())
        if match:
            start, end, step = int(match[1]), int(match[2] or match[1]), int(match[3] or 1)
        if not match or end < start or step < 1:
            raise SubmitError(
                f"{source.title}: use item numbers such as 1-{source.count} or 2,3, not {text!r}."
            )
        spans.append((start, start + (end - start) // step * step))
        total += (end - start) // step + 1
    spans.sort()
    if any(spans[i][1] >= spans[i + 1][0] for i in range(len(spans) - 1)):
        raise SubmitError(f"{source.title}: {text!r} lists an item more than once.")
    if spans[0][0] < 1 or spans[-1][1] > source.count:
        raise SubmitError(
            f"{source.title} has {source.count} items, so it can't run {text!r}. Read the "
            "workflow again, then choose its items."
        )
    return total


@dataclass
class TaskLists:
    """What plan_task_lists found in a workflow."""

    # The workflow with each split node marked.
    workflow: dict[str, Any]
    # Every source found, in workflow order.
    sources: list[ItemSource] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def active(self) -> list[ItemSource]:
        """The sources whose items run as separate tasks."""
        return [source for source in self.sources if source.enabled]


def _unique_name(name: str, suffix: str) -> str:
    """name with suffix added, shortened so that it's still a valid OpenJD identifier."""
    # A very long node ID keeps its end, which tells it apart, and name keeps its first letter.
    suffix = suffix[-(MAX_SOURCE_NAME - 1) :]
    return name[: MAX_SOURCE_NAME - len(suffix)] + suffix


def _identifier(text: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_")
    return ("_" + name if name[:1].isdigit() else name)[:48]


def _renamed_title(node: dict[str, Any]) -> str | None:
    """The title an artist gave a node, if they renamed it."""
    title = (node.get("_meta") or {}).get("title")
    default = _DEFAULT_TITLES.get(str(node.get("class_type")))
    return str(title).strip() or None if title and title != default else None


def _source_name(kind: str, node_id: str, node: dict[str, Any]) -> str:
    """
    The task parameter's name: the node's title, when the artist renamed it, so a list called
    Subject is the Subject column in Deadline Cloud monitor. Otherwise the kind and node ID, such
    as List12.
    """
    title = _renamed_title(node)
    name = _identifier(title) if title else ""
    # A node inside a subgraph has an API ID like "57:27".
    return name or (kind.capitalize() + re.sub(r"[^A-Za-z0-9_]", "_", node_id))[:MAX_SOURCE_NAME]


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


def _list_items(inputs: dict[str, Any], workflow: dict[str, Any]) -> list[str]:
    """
    What a Create List's items are, in the order the task script numbers them, for the panel to
    preview: a typed value, the value of a primitive node wired into it, or the wired node's name.
    """
    keys = sorted(
        (key for key in inputs if _LIST_ITEM_INPUT.fullmatch(key)),
        # "input12" or "inputs.input12".
        key=lambda key: int(key.rsplit("input", 1)[1]),
    )
    items = []
    for key in keys:
        value = inputs[key]
        if isinstance(value, list) and len(value) == 2:
            node = workflow.get(str(value[0]))
            node = node if isinstance(node, dict) else {}
            primitive = (node.get("inputs") or {}).get("value")
            if isinstance(primitive, (str, int, float)) and not isinstance(primitive, bool):
                value = primitive
            else:
                title = (node.get("_meta") or {}).get("title") or node.get("class_type")
                value = f"{title or 'node'} #{value[0]}"
        items.append(str(value))
    return items


def _source_title(source: ItemSource, workflow: dict[str, Any]) -> str:
    """
    Names a source for the panel by what it changes: its node's given title or what its output
    connects to, and otherwise its label. A folder's label names the folder.
    """
    node = workflow[source.node_id]
    renamed = _renamed_title(node)
    if renamed or source.kind == "folder":
        return renamed or source.label
    return _connection_title(source.node_id, workflow) or source.label


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
    label = f"{_DEFAULT_TITLES[class_type]} #{node_id}"
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
            numbers = integer_values(inputs["values"], allow_repeats=bool(pair_with))
        except ValueError as e:
            return None, f"{label}: {e}"
        values = [str(number) for number in numbers]
        kind = "integer"
    else:
        try:
            values = string_values(inputs["values"], inputs.get("split_by", SPLIT_BY_LINES))
        except ValueError as e:
            return None, f"{label}: {e}"
        kind = "string"
        # The task script selects the task's line from these, so it splits them as the panel does.
        meta[VALUES_META] = values
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


def plan_task_lists(
    api_workflow: dict[str, Any], input_dir: Path | None = None, reserved: Collection[str] = ()
) -> TaskLists:
    """
    Finds the values in a workflow that the comfy-cli job can run as separate tasks: the items of
    Create List nodes, the images in the folders of folder loaders and the values of the Deadline
    value nodes. Each is its own source of items, and tasks run every combination of them, except
    value nodes that are paired. reserved lists the job template's parameter names, which the
    lists' job parameters steer clear of. Raises SubmitError for a value node the job can't run.
    """
    workflow = copy.deepcopy(api_workflow)
    plan = TaskLists(workflow)
    errors = []
    for node_id, node in workflow.items():
        class_type = node.get("class_type")
        inputs = node.get("inputs", {})
        meta = node.get("_meta")
        if not isinstance(meta, dict):
            meta = node["_meta"] = {}
        source: ItemSource | None = None
        if class_type == "CreateList":
            values = _list_items(inputs, workflow)
            if len(values) > 1:
                source = ItemSource(
                    "", f"Create List #{node_id}", len(values), values=values, node_id=node_id
                )
        elif class_type in FOLDER_LOADERS:
            folder = inputs.get("folder")
            directory = input_path(input_dir, folder) if input_dir else None
            files = image_files(directory) if directory else []
            if len(files) > 1:
                meta[FILES_META] = [f"{folder}/{name}" for name in files]
                source = ItemSource(
                    "",
                    f"Folder {folder} (#{node_id})",
                    len(files),
                    "folder",
                    files,
                    node_id=node_id,
                )
        elif class_type in VALUE_NODES:
            source, error = _value_source(node_id, node, input_dir)
            if error:
                errors.append(error)
        if source:
            source.name = _source_name(source.kind, node_id, node)
            source.title = _source_title(source, workflow)
            plan.sources.append(source)
    if errors:
        raise SubmitError(" ".join(errors))
    # Two lists the artist gave the same title keep it, with their node IDs, and a number when that's
    # taken too.
    names: set[str] = set()

    def taken(name: str) -> bool:
        # reserved holds the job template's own parameter names, which a list's can't repeat.
        return name in names or f"{name}Items" in reserved

    for source in plan.sources:
        if taken(source.name):
            name, node = source.name, re.sub(r"[^A-Za-z0-9_]", "_", source.node_id)
            source.name, number = _unique_name(name, f"_{node}"), 2
            while taken(source.name):
                source.name, number = _unique_name(name, f"_{node}_{number}"), number + 1
        names.add(source.name)
    # Value nodes take the first places. In ComfyUI they output only their preview item, so one
    # without a task parameter wouldn't run its items. Lists and folders past the limit run whole.
    value_nodes = [source for source in plan.sources if source.value_node]
    if len(value_nodes) > MAX_ITEM_SOURCES:
        raise SubmitError(
            f"Deadline Cloud splits up to {MAX_ITEM_SOURCES} fields into tasks, and the workflow "
            f"has {len(value_nodes)} Deadline value nodes with more than one item. Use at most "
            f"{MAX_ITEM_SOURCES}."
        )
    lists = [source for source in plan.sources if not source.value_node]
    whole = lists[MAX_ITEM_SOURCES - len(value_nodes) :]
    for source in whole:
        source.enabled = False
    if whole:
        plan.warnings.append(
            f"Deadline Cloud splits up to {MAX_ITEM_SOURCES} fields into tasks, so these run "
            f"whole in each task: {', '.join(s.label for s in whole)}."
        )
    _check_pairs(plan)
    # OpenJD sorts an integer range and can't repeat a number, so a paired Integer Values node's
    # numbers wouldn't stay in step with its partners' items. A paired one selects its items by
    # number instead, like the other value nodes, and the task script looks up its value. So does
    # one with repeated numbers whose pair didn't match.
    for source in plan.active:
        if source.kind == "integer" and (
            source.pair_with or len(set(source.values)) < len(source.values)
        ):
            source.kind = "paired-integer"
            source.range = f"1-{source.count}"
            workflow[source.node_id]["_meta"][VALUES_META] = [int(value) for value in source.values]
    # Mark the nodes of the sources that run as separate tasks. The task script runs others whole.
    for source in plan.active:
        workflow[source.node_id]["_meta"][ITEM_META] = source.name
    for node in workflow.values():
        if not node["_meta"]:
            del node["_meta"]
    return plan


def _check_pairs(plan: TaskLists) -> None:
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


def _item_parameters(source: ItemSource) -> dict[str, Any]:
    """The job parameter that holds the range of a source's items to run."""
    shown = []
    for index, value in enumerate(source.values, start=1):
        value = first_line(value)
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
            + (' "0" on its own runs the whole list in each task.' if not source.value_node else "")
        )
    description += " Every combination of the lists is a task"
    if source.pair_with:
        description += (
            f", except that the value nodes paired as {source.pair_with!r} run their items "
            "together, so keep the same number of them"
        )
    description += "."
    if shown and source.kind != "integer":
        description += " Items: " + "; ".join(shown) + "."
    return {
        "name": source.parameter,
        "type": "STRING",
        "userInterface": {
            "control": "LINE_EDIT",
            "label": source.label[:64],
            "groupLabel": TASKS_GROUP,
        },
        "default": source.range,
        "description": description[:2048],
    }


def _task_lists_step(job_template: dict[str, Any]) -> dict[str, Any] | None:
    """The step whose task script has the line that selects each task's items, if any."""
    for step in job_template.get("steps") or []:
        if not isinstance(step, dict):
            continue
        files = (step.get("script") or {}).get("embeddedFiles") or []
        if any(
            isinstance(file, dict) and _ITEMS_PLACEHOLDER_LINE.search(str(file.get("data", "")))
            for file in files
        ):
            return step
    return None


def _has_task_parameters(step: dict[str, Any]) -> bool:
    space = step.get("parameterSpace")
    return isinstance(space, dict) and bool(space.get("taskParameterDefinitions"))


def template_supports_task_lists(template: Path) -> bool:
    """
    Whether the submitter can add the workflow's lists to a job template as task parameters: its
    task script has the line that selects each task's items, and that step has no task parameters
    of its own, which the lists' names, number and combination would have to fit around. The
    template is parsed first, since a JSON template keeps each script on one line, with escaped
    newlines.
    """
    try:
        step = _task_lists_step(read_job_template(template))
    except SubmitError:
        return False
    return step is not None and not _has_task_parameters(step)


def template_has_own_task_parameters(template: Path) -> bool:
    """Whether the step that selects each task's items already has task parameters."""
    try:
        step = _task_lists_step(read_job_template(template))
    except SubmitError:
        return False
    return step is not None and _has_task_parameters(step)


def add_task_lists(template: dict[str, Any], sources: list[ItemSource]) -> dict[str, Any]:
    """
    Returns a copy of a job template with a job parameter and a task parameter for each list, in
    the step whose task script has the ITEMS placeholder.
    """
    template = copy.deepcopy(template)
    definitions = template.setdefault("parameterDefinitions", [])
    # Task lists follow the workflow parameters.
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
            {"name": source.name, "type": "INT", "range": f"{{{{Param.{source.parameter}}}}}"}
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


def _input_strings(api_workflow: dict[str, Any]):
    """
    Yields every string input value of the API workflow. A Deadline String Values node yields
    each of its values, since each task uses one of them, for example one LoRA name per task.
    """
    for node in api_workflow.values():
        string_node = node.get("class_type") == STRING_NODE
        for key, value in node.get("inputs", {}).items():
            if not isinstance(value, str):
                continue
            if string_node and key == "values":
                try:
                    yield from string_values(value, node["inputs"].get("split_by", SPLIT_BY_LINES))
                except ValueError:
                    # Planning reports it.
                    yield from string_values(value)
            else:
                yield value


def workflow_models(api_workflow: dict[str, Any]) -> list[str]:
    """
    Lists the model files the workflow loads: every string input that names a model file, and
    each value of a Deadline String Values node that does. The submitter attaches a local copy
    when this machine has the file. Otherwise the job fails unless the workers already have it,
    for example through Extra Model Paths Config.
    """
    models = {
        value.replace("\\", "/")
        for value in _input_strings(api_workflow)
        if value.lower().endswith(MODEL_EXTENSIONS)
    }
    return sorted(models)


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


def local_node_packs(
    api_workflow: dict[str, Any], loaded_dirs: Mapping[str, Path]
) -> dict[str, Path]:
    """
    The custom node packs to attach, by folder or file name. loaded_dirs maps each node type that
    this ComfyUI loaded from a custom_nodes folder to its pack folder, or single .py file, so
    ComfyUI's own nodes aren't in it. Workers get every pack the workflow uses through job
    attachments, except this extension: the job replaces its value nodes. Each path is listed
    once, however many of its nodes are used.
    """
    packs: dict[str, Path] = {}
    for node in api_workflow.values():
        class_type = node.get("class_type") if isinstance(node, dict) else None
        if class_type in VALUE_NODES:
            continue
        path = loaded_dirs.get(class_type) if isinstance(class_type, str) else None
        if path is not None:
            packs.setdefault(path.name, path)
    return dict(sorted(packs.items()))


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
    outside base_dir, for example through ".." segments or a symlink, or when it is base_dir
    itself. realpath resolves both ".." and symlinks, and the boundary check compares against
    base_dir plus a path separator so a sibling such as "base_dir_evil" doesn't pass for a
    child of base_dir.
    """
    base = os.path.realpath(base_dir)
    target = os.path.realpath(os.path.join(base, user_input))
    if not target.startswith(base + os.sep):
        raise ValueError("Access Denied: Path injection attempt detected.")
    return Path(target)


def read_job_template(template: Path, sources: list[ItemSource] | None = None) -> dict[str, Any]:
    """
    Reads a YAML or JSON job template, with a parameter for each of the workflow's sources of work
    items.
    """
    import yaml

    try:
        text = template.read_text(encoding="utf-8")
        base = json.loads(text) if template.suffix.lower() == ".json" else yaml.safe_load(text)
    except (OSError, ValueError, yaml.YAMLError) as e:
        raise SubmitError(f"Couldn't read the job template {template}: {e}")
    if not isinstance(base, dict):
        raise SubmitError(f"The job template {template} isn't a YAML or JSON object.")
    return add_task_lists(base, sources) if sources else base


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


# The spellings OpenJD takes for a BOOL job parameter, in any case.
_TRUE_VALUES = frozenset({"true", "yes", "on", "1"})
_FALSE_VALUES = frozenset({"false", "no", "off", "0"})
_INT_VALUE = re.compile(r"[+-]?\d+")


def parse_bool(value: Any) -> bool | None:
    """A BOOL job parameter's value as OpenJD reads it, or None when OpenJD would refuse it."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return {0: False, 1: True}.get(value)
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return None


def _number(value: Any, kind: str) -> int | float | None:
    """An INT or FLOAT job parameter's value, or None when OpenJD would refuse it."""
    if isinstance(value, bool):
        return None
    if kind == "INT":
        if isinstance(value, int):
            return value
        text = str(value).strip()
        return int(text) if _INT_VALUE.fullmatch(text) else None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def check_parameter_values(
    definitions: list[dict[str, Any]], parameters: Mapping[str, Any]
) -> None:
    """
    Checks the values of the job template's BOOL, INT, FLOAT and STRING parameters as Deadline
    Cloud would, so a bad one is refused before the bundle is created and files are copied for
    it. Parameters the template doesn't define, such as the queue's, are left to the library.
    Raises SubmitError.
    """
    for definition in definitions:
        if not isinstance(definition, dict) or definition.get("name") not in parameters:
            continue
        name = definition["name"]
        value = parameters[name]
        kind = definition.get("type")
        label = (definition.get("userInterface") or {}).get("label") or name
        checked: Any = value
        if kind == "BOOL":
            if parse_bool(value) is None:
                raise SubmitError(f"{label} must be true or false, not {value!r}.")
            continue
        if kind in ("INT", "FLOAT"):
            checked = _number(value, kind)
            if checked is None:
                what = "a whole number" if kind == "INT" else "a number"
                raise SubmitError(f"{label} must be {what}, not {value!r}.")
            low = _number(definition.get("minValue"), kind)
            high = _number(definition.get("maxValue"), kind)
            if low is not None and checked < low:
                raise SubmitError(f"{label} must be at least {low}, not {value!r}.")
            if high is not None and checked > high:
                raise SubmitError(f"{label} must be at most {high}, not {value!r}.")
        elif kind == "STRING":
            checked = str(value)
            shortest, longest = definition.get("minLength"), definition.get("maxLength")
            if shortest is not None and len(checked) < int(shortest):
                raise SubmitError(f"{label} must be at least {shortest} characters long.")
            if longest is not None and len(checked) > int(longest):
                raise SubmitError(f"{label} must be at most {longest} characters long.")
        else:
            continue
        allowed = definition.get("allowedValues")
        if allowed and checked not in [
            _number(item, kind) if kind != "STRING" else str(item) for item in allowed
        ]:
            choices = ", ".join(str(item) for item in allowed)
            raise SubmitError(f"{label} must be one of {choices}, not {value!r}.")


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
    requirements: dict[str, Any] | None = None,
    sources: list[ItemSource] | None = None,
    description: str | None = None,
) -> tuple[Path, Path]:
    """
    Creates <dest_dir>/<timestamp>_<name>/ with the job template and the API-format workflow, and
    returns the bundle directory and the workflow file. They have to outlive the submitter dialog
    and the job attachments upload. The template is copied as it is, unless the workflow has
    task lists to add to it, or the artist changed its host requirements or description.
    """
    suffix = ".json" if template.suffix.lower() == ".json" else ".yaml"
    text = None
    if sources or requirements is not None or description:
        job_template = read_job_template(template, sources)
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
    now = now or datetime.now(UTC)
    stem = safe_name(name)
    leaf = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}_{stem}"
    # The name comes from the request. safe_name strips traversal, and get_safe_path checks again
    # that the bundle and workflow file land inside dest_dir.
    try:
        bundle_dir = get_safe_path(leaf, dest_dir)
    except ValueError as e:
        raise SubmitError(str(e)) from e
    # Everything in the bundle, such as linked models, is only reachable through these
    # directories, whatever its own permissions.
    make_private_dir(dest_dir)
    bundle_dir.mkdir(mode=PRIVATE_DIR_MODE)
    os.chmod(bundle_dir, PRIVATE_DIR_MODE)
    if text is None:
        text = template.read_text(encoding="utf-8")
    write_private_text(bundle_dir / f"template{suffix}", text)
    try:
        workflow_path = get_safe_path(f"{stem}_api.json", bundle_dir)
    except ValueError as e:
        raise SubmitError(str(e)) from e
    write_private_text(workflow_path, json.dumps(workflow, indent=2))
    return bundle_dir, workflow_path
