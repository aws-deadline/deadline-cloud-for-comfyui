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
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TEMPLATE_ENV_VAR = "COMFYUI_DEADLINE_JOB_TEMPLATE"
CLI_ENV_VAR = "COMFYUI_DEADLINE_CLI"
# The Python the submitter panel's bridge runs with. By default, the Deadline Cloud CLI's own.
PYTHON_ENV_VAR = "COMFYUI_DEADLINE_PYTHON"
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


PICKER_KINDS = ("files", "folder", "file")
# The panel opens these for attachments and for the job's path parameters, such as its output
# directory, which isn't attached.
_PICKER_PROMPTS = {
    "files": "Choose files for the Deadline Cloud job",
    "folder": "Choose a folder for the Deadline Cloud job",
    "file": "Choose a file for the Deadline Cloud job",
}


def picker_command(kind: str, platform: str = sys.platform) -> list[str] | None:
    """
    Returns the command for a native file or folder dialog, which prints one absolute path per line,
    or None when there isn't one. The browser only chooses the kind: files, a folder, or one file.
    """
    prompt = _PICKER_PROMPTS[kind]
    if platform == "darwin":
        if kind == "folder":
            return ["osascript", "-e", f'POSIX path of (choose folder with prompt "{prompt}")']
        if kind == "file":
            return ["osascript", "-e", f'POSIX path of (choose file with prompt "{prompt}")']
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
            multiple = "$true" if kind == "files" else "$false"
            script = (
                utf8 + "Add-Type -AssemblyName System.Windows.Forms;"
                "$d=New-Object System.Windows.Forms.OpenFileDialog;"
                f"$d.Title='{prompt}';$d.Multiselect={multiple};"
                "if($d.ShowDialog() -eq 'OK'){[Console]::Out.Write(($d.FileNames -join \"`n\"))}"
            )
        return ["powershell", "-NoProfile", "-STA", "-Command", script]
    if shutil.which("zenity"):
        command = ["zenity", "--file-selection", f"--title={prompt}"]
        if kind == "folder":
            return [*command, "--directory"]
        return [*command, "--multiple", "--separator=\n"] if kind == "files" else command
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


class _UiNodes:
    """
    A UI-format workflow's nodes, indexed once: the top-level nodes and each subgraph's nodes, by
    ID. A node inside a subgraph has an API ID like "12:137": node 137 of the subgraph that
    top-level node 12 is an instance of.
    """

    def __init__(self, ui_workflow: Any):
        self.top: dict[str, dict[str, Any]] = {}
        self.subgraphs: dict[str, dict[str, dict[str, Any]]] = {}
        # Any subgraph's node, by its own ID, for an API ID whose path can't be followed.
        self.anywhere: dict[str, dict[str, Any]] = {}
        if isinstance(ui_workflow, dict):
            self.top = self._index(ui_workflow.get("nodes"))
            self._add_subgraphs(ui_workflow, depth=0)

    @staticmethod
    def _index(nodes: Any) -> dict[str, dict[str, Any]]:
        index: dict[str, dict[str, Any]] = {}
        for node in nodes if isinstance(nodes, list) else []:
            if isinstance(node, dict):
                index.setdefault(str(node.get("id")), node)
        return index

    def _add_subgraphs(self, value: dict[str, Any], depth: int) -> None:
        definitions = value.get("definitions")
        subgraphs = definitions.get("subgraphs") if isinstance(definitions, dict) else None
        # ComfyUI keeps every subgraph's definition at the top. The depth limit is for nesting.
        if not isinstance(subgraphs, list) or depth > 32:
            return
        for subgraph in subgraphs:
            if not isinstance(subgraph, dict):
                continue
            nodes = self._index(subgraph.get("nodes"))
            self.subgraphs.setdefault(str(subgraph.get("id")), nodes)
            for node_id, node in nodes.items():
                self.anywhere.setdefault(node_id, node)
            self._add_subgraphs(subgraph, depth + 1)

    def find(self, api_id: str) -> dict[str, Any] | None:
        *path, last = api_id.split(":")
        scope: dict[str, dict[str, Any]] | None = self.top
        for part in path:
            instance = scope.get(part) if scope is not None else None
            scope = self.subgraphs.get(str(instance.get("type"))) if instance else None
        node = scope.get(last) if scope is not None else None
        return node or (self.anywhere.get(last) if path else None)


def _describe_missing_node(node_id: str, ui_nodes: _UiNodes) -> str:
    node = ui_nodes.find(node_id)
    if node is None:
        return node_id
    description = f"{node_id} ({node.get('type')}"
    properties = node.get("properties")
    pack = properties.get("cnr_id") if isinstance(properties, dict) else None
    if isinstance(pack, str):
        description += f", from the {pack} node pack"
    return description + ")"


# The most missing nodes an error describes. A workflow can have thousands.
_MAX_DESCRIBED_NODES = 20


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
    missing_ids = [
        str(node_id)
        for node_id, node in workflow.items()
        if not isinstance(node, dict) or "class_type" not in node
    ]
    if missing_ids:
        ui_nodes = _UiNodes(ui_workflow)
        missing = [
            _describe_missing_node(node_id, ui_nodes)
            for node_id in missing_ids[:_MAX_DESCRIBED_NODES]
        ]
        if len(missing_ids) > _MAX_DESCRIBED_NODES:
            missing.append(f"and {len(missing_ids) - _MAX_DESCRIBED_NODES} more")
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
                ascii_digits = _ANY_LIST_ITEM_INPUT.fullmatch(str(key))
                if _UNICODE_LIST_ITEM_INPUT.fullmatch(str(key)) and not ascii_digits:
                    raise SubmitError(
                        f"Create List {_quoted(node_id)} has an input named {_quoted(key)}, "
                        "numbered with digits that ComfyUI doesn't use."
                    )
                if ascii_digits and not _LIST_ITEM_INPUT.fullmatch(str(key)):
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
# workflow becomes its own task parameter, so tasks run every combination of their items. The job
# template's task script selects each task's item, using these names, which
# test_template_script.py keeps in step.
# Core nodes that load every image in a folder of the input directory as a list.
FOLDER_LOADERS = ("LoadImageDataSetFromFolder",)
# Nodes whose folder input names a folder of the input directory, which is sent with the job.
FOLDER_INPUT_NODES = (
    *FOLDER_LOADERS,
    "LoadImageTextDataSetFromFolder",
    "LoadVideoDataSetFromFolder",
    "LoadVideoTextDataSetFromFolder",
)
# The images that LoadImageDataSetFromFolder loads.
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
# The most files a folder loader's folder attaches, so the plan stays quick to make and to show.
# The artist can add a bigger folder in the panel.
MAX_FOLDER_FILES = 10_000
# The most entries read in all the folders of one workflow's folder loaders: files, folders, and
# links that lead out. A folder of empty folders or of links has no files to count.
MAX_FOLDER_ENTRIES = 50_000
# The _meta key that marks the nodes the task script splits, with the task parameter of each.
ITEM_META = "deadline_item"
# The images of a folder, as paths in the input directory, in the order its items are numbered.
FILES_META = "deadline_files"
# OpenJD allows 16 task parameters in a step.
MAX_ITEM_SOURCES = 16
# The most lists and folders a workflow's plan considers. Later ones run whole in each task. It
# keeps planning a workflow with thousands of list nodes quick.
MAX_PLANNED_SOURCES = 256
# The most tasks a job can run: every combination of its lists' items. The panel has the same
# limit.
MAX_TASKS = 10_000
# The longest range of a list's items, which is also the job parameter's maxLength.
MAX_ITEM_RANGE_LENGTH = 1024
# The line that marks the task script that selects each task's items, which it reads from its
# NAME=NUMBER arguments. A job template without it runs whole lists.
ITEMS_PLACEHOLDER = "ITEMS = {}"
# The group of the job parameters that hold each list's items to run.
TASKS_GROUP = "Tasks"

# A Create List's item inputs: input0, input1 and so on, or inputs.input0 inside a subgraph. The
# number is bounded so it's always safe to read, in the submitter and the task script.
_MAX_LIST_ITEM_DIGITS = 9
_LIST_ITEM_INPUT = re.compile(rf"(inputs\.)?input\d{{1,{_MAX_LIST_ITEM_DIGITS}}}", re.ASCII)
_ANY_LIST_ITEM_INPUT = re.compile(r"(inputs\.)?input\d+", re.ASCII)
# Digits in any script, such as Arabic-Indic ones, which Python's \d matches without re.ASCII.
_UNICODE_LIST_ITEM_INPUT = re.compile(r"(inputs\.)?input\d+")
# The _meta keys the submitter writes for the task script. A workflow's own are dropped.
_SUBMITTER_META_PREFIX = "deadline_"
# The titles ComfyUI gives the nodes that list tasks. Any other title is one the artist gave.
_DEFAULT_TITLES = {
    "CreateList": "Create List",
    "LoadImageDataSetFromFolder": "Load Image (from Folder)",
}
_ITEMS_PLACEHOLDER_LINE = re.compile(rf"^[ \t]*{re.escape(ITEMS_PLACEHOLDER)}[ \t]*$", re.MULTILINE)


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
    # ComfyUI checks paths this way too, without resolving symbolic links. The submitter checks
    # links as well, with leads_inside, before it attaches anything.
    base = Path(os.path.abspath(input_dir))
    path = Path(os.path.abspath(base / name))
    return path if path != base and path.is_relative_to(base) else None


def without_long_path_prefix(path: str) -> str:
    r"""
    path without the \\?\ prefix that Windows' realpath adds to a path that's too long to have
    none, so it compares with a path that doesn't have it. \\?\UNC\server\share becomes
    \\server\share. Comparing is all it's for: Windows may need the prefix to open a long path.
    """
    if path[:8].upper() == "\\\\?\\UNC\\":
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def real_path(path: Path | str) -> Path:
    """
    path with every link resolved, so a folder that's a link can still be a root, for comparing
    with another real path.
    """
    return Path(without_long_path_prefix(os.fspath(os.path.realpath(path))))


def leads_inside(path: Path | str, root: Path) -> bool:
    """
    Whether path, with every link resolved, is inside root, whose links are resolved already.
    Job attachments uploads what a link points to, so a link in ComfyUI's input folder could
    otherwise attach any file on the disk. Every path is resolved, not only symbolic links, since
    Python doesn't count a Windows directory junction as one.
    """
    try:
        real = real_path(path)
    except (OSError, ValueError):
        return False
    return real != root and real.is_relative_to(root)


def input_folders(
    workflow: Mapping[str, Any], input_dir: Path, warnings: list[str] | None = None
) -> list[Path]:
    """
    Returns the folders in ComfyUI's input directory that the workflow's folder loaders read. A
    folder that leads out of the input directory through a link is left out, with a warning in
    warnings. The folders aren't attached whole: get_local_assets attaches the files in them.
    """
    root = real_path(input_dir)
    folders = set()
    for node_id, node in workflow.items():
        if not isinstance(node, Mapping) or node.get("class_type") not in FOLDER_INPUT_NODES:
            continue
        inputs = node.get("inputs")
        folder = inputs.get("folder") if isinstance(inputs, Mapping) else None
        path = input_path(input_dir, folder)
        if path is None or not path.is_dir():
            continue
        if leads_inside(path, root):
            folders.add(path)
        elif warnings is not None:
            warnings.append(
                f"Node {_quoted(node_id)} loads the folder {_quoted(folder)!r}, which links "
                "outside ComfyUI's input folder, so it isn't attached. Add it with Add folder… "
                "if the job needs it."
            )
    return sorted(folders)


@dataclass
class FolderFiles:
    """What list_input_folders found in a folder loader's folder."""

    # The files that are attached, sorted.
    files: list[Path] = field(default_factory=list)
    # The entries that lead out of ComfyUI's input folder, which aren't attached, sorted.
    outside: list[Path] = field(default_factory=list)
    # Why nothing in the folder is attached when it's too big to list, to follow "The folder
    # 'cats' ", such as "has more than 10,000 files". None when it's listed in full.
    too_big: str | None = None


class _Budget:
    """The entries left to read in one workflow's folders."""

    def __init__(self, entries: int):
        self.left = entries


def list_input_folders(
    workflow: Mapping[str, Any], input_dir: Path, warnings: list[str] | None = None
) -> dict[Path, FolderFiles]:
    """
    The files in each folder that input_folders finds, by folder, as attachments and task lists
    both see them, so a folder whose files aren't attached is never split into tasks. The folders
    share MAX_FOLDER_ENTRIES, and each attaches at most MAX_FOLDER_FILES files. A folder that
    goes past either attaches nothing.
    """
    root = real_path(input_dir)
    budget = _Budget(MAX_FOLDER_ENTRIES)
    return {
        folder: _list_folder(folder, root, budget)
        for folder in input_folders(workflow, input_dir, warnings)
    }


def _list_folder(folder: Path, root: Path, budget: _Budget) -> FolderFiles:
    """
    The files in a folder and its subfolders, and the entries that lead out of root, the real
    path of ComfyUI's input folder. Every entry is resolved, not only symbolic links, since Python
    doesn't count a Windows directory junction as one. A link to a folder in the input folder
    isn't followed, so walking can't loop. A link to a file in it is attached under its own name.
    """
    files: list[Path] = []
    outside: list[Path] = []
    pending = [folder]
    while pending:
        directory = pending.pop()
        names = []
        try:
            real_directory = str(real_path(directory))
            with os.scandir(directory) as entries:
                for entry in entries:
                    budget.left -= 1
                    if budget.left < 0:
                        return FolderFiles(
                            too_big=f"takes the workflow's folders past {MAX_FOLDER_ENTRIES:,} "
                            "entries"
                        )
                    names.append(entry.name)
        except (OSError, ValueError):
            continue
        for name in sorted(names):
            path = directory / name
            try:
                real = real_path(path)
            except (OSError, ValueError):
                continue
            if not real.is_relative_to(root):
                outside.append(path)
            elif path.is_dir():
                if str(real) == os.path.join(real_directory, name):
                    pending.append(path)
            elif path.is_file():
                files.append(path)
                if len(files) > MAX_FOLDER_FILES:
                    return FolderFiles(too_big=f"has more than {MAX_FOLDER_FILES:,} files")
    return FolderFiles(sorted(files), sorted(outside))


def folder_images(folder: Path, files: Iterable[Path]) -> list[str]:
    """
    The names of the images in folder itself, of its attached files, that
    LoadImageDataSetFromFolder loads, in name order. Only attached images can be a task's item.
    """
    return sorted(
        path.name
        for path in files
        if path.parent == folder and path.name.lower().endswith(IMAGE_EXTENSIONS)
    )


@dataclass
class ItemSource:
    """A node whose items can run as separate tasks."""

    # The task parameter that selects the item, such as "List12". Its job parameter, "List12Items",
    # holds the range to run.
    name: str
    label: str
    count: int
    # "list" or "folder".
    kind: str = "list"
    # The items, as far as the submitter knows them, for the job parameter's description.
    values: list[str] = field(default_factory=list)
    # Whether it gets a task parameter. A step can have only MAX_ITEM_SOURCES of them.
    enabled: bool = True
    # The job parameter's default range of item numbers.
    range: str = ""
    node_id: str = ""
    # What the panel calls it. The label names the node.
    title: str = ""

    def __post_init__(self) -> None:
        self.range = self.range or f"1-{self.count}"
        self.title = self.title or self.label

    @property
    def parameter(self) -> str:
        """The job parameter that holds the range of items to run."""
        return f"{self.name}Items"


# OpenJD identifiers are at most 64 characters, and a list's job parameter adds "Items".
MAX_SOURCE_NAME = 64 - len("Items")


# One part of a range of item numbers, with spaces allowed around "-" and ":", as in the panel.
# Item numbers are capped at 9 digits, far past any list, so int() never reaches Python's limit.
_ITEM_RANGE_PART = re.compile(r"(\d{1,9})(?:\s*-\s*(\d{1,9})(?:\s*:\s*(\d{1,9}))?)?", re.ASCII)


def check_item_range(source: ItemSource, value: Any) -> tuple[str, int]:
    """
    Checks the range of a list's items to run, as OpenJD and the task script take it: "0" on its
    own, or item numbers from 1 to the list's count, in parts that don't overlap. The panel checks
    it against the plan it made, so this catches a list that changed since. Returns the range as
    it's submitted, without spaces, and how many items it runs, which is 1 for "0". Raises
    SubmitError.
    """
    text = str(value).strip()
    if text == "0":
        return "0", 1
    if len(text) > MAX_ITEM_RANGE_LENGTH:
        raise SubmitError(
            f"{source.title}: a range of items can be at most {MAX_ITEM_RANGE_LENGTH} characters."
        )
    spans: list[tuple[int, int]] = []
    parts: list[str] = []
    count = 0
    for part in text.split(","):
        match = _ITEM_RANGE_PART.fullmatch(part.strip())
        if match:
            start, end, step = int(match[1]), int(match[2] or match[1]), int(match[3] or 1)
        if not match or end < start or step < 1:
            raise SubmitError(
                f"{source.title}: use item numbers such as 1-{source.count} or 2,3, not {text!r}."
            )
        spans.append((start, start + (end - start) // step * step))
        count += (end - start) // step + 1
        parts.append(
            str(start) if end == start else f"{start}-{end}" + (f":{step}" if step > 1 else "")
        )
    spans.sort()
    if any(spans[i][1] >= spans[i + 1][0] for i in range(len(spans) - 1)):
        raise SubmitError(f"{source.title}: {text!r} lists an item more than once.")
    if spans[0][0] < 1 or spans[-1][1] > source.count:
        raise SubmitError(
            f"{source.title} has {source.count} items, so it can't run {text!r}. Read the "
            "workflow again, then choose its items."
        )
    return ",".join(parts), count


def check_task_count(counts: Iterable[int]) -> int:
    """
    The number of tasks that the lists' item counts make, since tasks run every combination of
    them. Raises SubmitError when it's over MAX_TASKS.
    """
    total = math.prod(counts)
    if total > MAX_TASKS:
        raise SubmitError(
            f"The job would run {total:,} tasks, every combination of its lists' items, which is "
            f"over the limit of {MAX_TASKS:,}. Choose fewer items, or run a list whole with 0."
        )
    return total


def without_submitter_meta(workflow: dict[str, Any]) -> dict[str, Any]:
    """
    A copy of an API workflow without the _meta keys that the submitter writes for the task
    script. A shared workflow's own could make the task script follow a list, or load files, that
    the panel never showed.
    """
    workflow = copy.deepcopy(workflow)
    for node in workflow.values():
        meta = node.get("_meta") if isinstance(node, dict) else None
        if isinstance(meta, dict):
            for key in [key for key in meta if str(key).startswith(_SUBMITTER_META_PREFIX)]:
                del meta[key]
    return workflow


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


def _consumers(workflow: dict[str, Any]) -> dict[str, list[tuple[dict[str, Any], str]]]:
    """
    Each node's consumers: the (node, input name) pairs that its outputs connect to. It's built
    once for the workflow, so naming every list doesn't read the whole workflow again for each.
    """
    consumers: dict[str, list[tuple[dict[str, Any], str]]] = {}
    for other in workflow.values():
        for key, value in (other.get("inputs") or {}).items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[1], int):
                consumers.setdefault(str(value[0]), []).append((other, key))
    return consumers


def _connection_title(
    node_id: str, consumers: Mapping[str, list[tuple[dict[str, Any], str]]]
) -> str | None:
    """What a node's output connects to, such as "Image Scale · width" or "Image Scale · width +1"."""
    connections = consumers.get(str(node_id), [])
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


def _source_title(
    source: ItemSource,
    workflow: dict[str, Any],
    consumers: Mapping[str, list[tuple[dict[str, Any], str]]],
) -> str:
    """
    Names a source for the panel by what it changes: its node's given title or what its output
    connects to, and otherwise its label. A folder's label names the folder.
    """
    node = workflow[source.node_id]
    renamed = _renamed_title(node)
    if renamed or source.kind == "folder":
        return renamed or source.label
    return _connection_title(source.node_id, consumers) or source.label


def _some(labels: list[str], shown: int = 5) -> str:
    """The first few labels, and how many more there are."""
    more = f", and {len(labels) - shown} more" if len(labels) > shown else ""
    return ", ".join(labels[:shown]) + more


def plan_task_lists(
    api_workflow: dict[str, Any], input_dir: Path | None = None, reserved: Collection[str] = ()
) -> TaskLists:
    """
    Finds the values in a workflow that the comfy-cli job can run as separate tasks: the items of
    Create List nodes and the images in the folders of folder loaders. Each is its own source of
    items, and tasks run every combination of them. reserved lists the job template's parameter
    names, which the lists' job parameters steer clear of.
    """
    # The task script follows the submitter's _meta keys, so a shared workflow's own could make a
    # node follow another list's items, or load files the panel never showed.
    workflow = without_submitter_meta(api_workflow)
    plan = TaskLists(workflow)
    consumers = _consumers(workflow)
    base = Path(os.path.abspath(input_dir)) if input_dir else None
    # Each folder's items are its attached images, as get_local_assets lists them. An image that
    # links outside the input directory, or is in a folder that does or that's too big to attach,
    # isn't attached, so it can't be one of a task's items. Folders are only listed when a folder
    # loader could be split.
    listed: dict[Path, FolderFiles] | None = None
    too_big: list[tuple[str, str]] = []
    unplanned = 0
    for node_id, node in workflow.items():
        class_type = node.get("class_type")
        inputs = node.get("inputs", {})
        meta = node.get("_meta")
        if not isinstance(meta, dict):
            meta = node["_meta"] = {}
        source: ItemSource | None = None
        if class_type in ("CreateList", *FOLDER_LOADERS) and (
            len(plan.sources) >= MAX_PLANNED_SOURCES
        ):
            unplanned += 1
        elif class_type == "CreateList":
            values = _list_items(inputs, workflow)
            if len(values) > 1:
                source = ItemSource(
                    "", f"Create List #{node_id}", len(values), values=values, node_id=node_id
                )
        elif class_type in FOLDER_LOADERS:
            folder = inputs.get("folder")
            directory = input_path(base, folder) if base else None
            if directory and base and listed is None:
                listed = list_input_folders(workflow, base)
            listing = listed.get(directory) if directory and listed else None
            files: list[str] = []
            if listing and listing.too_big:
                too_big.append((f"Folder {_quoted(folder)} (#{_quoted(node_id)})", listing.too_big))
            elif directory and listing:
                files = folder_images(directory, listing.files)
            if directory and base and len(files) > 1:
                # Relative to the input directory, which the job maps, whatever the workflow's
                # folder says, such as an absolute path on this machine or one with "..".
                relative = directory.relative_to(base).as_posix()
                meta[FILES_META] = [f"{relative}/{name}" for name in files]
                source = ItemSource(
                    "",
                    f"Folder {folder} (#{node_id})",
                    len(files),
                    "folder",
                    files,
                    node_id=node_id,
                )
        if source:
            source.name = _source_name(source.kind, node_id, node)
            source.title = _source_title(source, workflow, consumers)
            plan.sources.append(source)
    if unplanned:
        plan.warnings.append(
            f"Deadline Cloud reads up to {MAX_PLANNED_SOURCES} lists and folders in a workflow, so "
            f"{unplanned} more run whole in each task."
        )
    if len(too_big) == 1:
        label, reason = too_big[0]
        plan.warnings.append(f"{label} {reason}, so it isn't attached or split into tasks.")
    elif too_big:
        plan.warnings.append(
            "These folders are too big to attach, so they aren't split into tasks: "
            f"{_some([label for label, _ in too_big])}."
        )
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
    active = plan.active
    for source in active[MAX_ITEM_SOURCES:]:
        source.enabled = False
    if len(active) > MAX_ITEM_SOURCES:
        plan.warnings.append(
            f"Deadline Cloud splits up to {MAX_ITEM_SOURCES} fields into tasks, so these run "
            f"whole in each task: {_some([s.label for s in active[MAX_ITEM_SOURCES:]])}."
        )
    # Mark the nodes of the sources that run as separate tasks. The task script runs others whole.
    for source in plan.active:
        workflow[source.node_id]["_meta"][ITEM_META] = source.name
    for node in workflow.values():
        if not node["_meta"]:
            del node["_meta"]
    return plan


# OpenJD refuses Unicode's control characters in a label, and all but line breaks and tabs in a
# description. A list item, a folder name or a file name can hold one.
_LABEL_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_DESCRIPTION_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _item_parameters(source: ItemSource) -> dict[str, Any]:
    """The job parameter that holds the range of a source's items to run."""
    shown = []
    for index, value in enumerate(source.values, start=1):
        shown.append(f"{index}: {value[:60]}{'...' if len(value) > 60 else ''}")
        if index == 10 and source.count > 10:
            shown.append(f"and {source.count - 10} more")
            break
    description = (
        f"The items of {source.label} to run as separate tasks, numbered from 1, for example "
        f'"1-{source.count}" or "2,3". "0" on its own runs the whole list in each task. Every '
        "combination of the lists is a task."
    )
    if shown:
        description += " Items: " + "; ".join(shown) + "."
    return {
        "name": source.parameter,
        "type": "STRING",
        "maxLength": MAX_ITEM_RANGE_LENGTH,
        "userInterface": {
            "control": "LINE_EDIT",
            "label": _LABEL_CONTROL_CHARACTERS.sub(" ", source.label)[:64],
            "groupLabel": TASKS_GROUP,
        },
        "default": source.range,
        "description": _DESCRIPTION_CONTROL_CHARACTERS.sub(" ", description)[:2048],
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
    the step whose task script has the ITEMS placeholder. The task script gets each task's items
    as NAME=NUMBER arguments, so no value is written into its code. A studio template's other
    steps are left as they are.
    """
    template = copy.deepcopy(template)
    definitions = template.setdefault("parameterDefinitions", [])
    # Task lists follow the workflow parameters.
    at = len(definitions)
    for index, definition in enumerate(definitions):
        if (definition.get("userInterface") or {}).get("groupLabel") == "Workflow Parameters":
            at = index + 1
    definitions[at:at] = [_item_parameters(source) for source in sources]
    step = _task_lists_step(template)
    if step is None:
        return template
    on_run = step.setdefault("script", {}).setdefault("actions", {}).setdefault("onRun", {})
    on_run.setdefault("args", []).extend(
        f"{source.name}={{{{Task.Param.{source.name}}}}}" for source in sources
    )
    space = step.setdefault("parameterSpace", {})
    space.setdefault("taskParameterDefinitions", []).extend(
        {"name": source.name, "type": "INT", "range": f"{{{{Param.{source.parameter}}}}}"}
        for source in sources
    )
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


def workflow_models(api_workflow: dict[str, Any]) -> list[str]:
    """
    Lists the model files the workflow loads: every string input that names a model file. The
    submitter attaches a local copy when this machine has the file. Otherwise the job fails unless
    the workers already have it, for example through Extra Model Paths Config.
    """
    models = {
        value.replace("\\", "/")
        for node in api_workflow.values()
        for value in node.get("inputs", {}).values()
        if isinstance(value, str) and value.lower().endswith(MODEL_EXTENSIONS)
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
    attachments. Each path is listed once, however many of its nodes are used.
    """
    packs: dict[str, Path] = {}
    for node in api_workflow.values():
        class_type = node.get("class_type") if isinstance(node, dict) else None
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
MAX_JOB_NAME = 128
# Template expressions, which OpenJD would read in a job name, and control characters. A
# description can have tabs and line breaks.
_NAME_FORBIDDEN = re.compile(r"\{\{|\}\}|[\x00-\x1f\x7f-\x9f]")
_DESCRIPTION_FORBIDDEN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
# Deadline Cloud's limits for the job settings that are whole numbers.
_SETTING_RANGES = {
    "priority": (0, 100),
    "maxFailedTasksCount": (0, 2147483647),
    "maxRetriesPerTask": (0, 2147483647),
    "maxWorkerCount": (1, 2147483647),
}


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
        # JSON from the panel can hold NaN and Infinity, which OpenJD doesn't accept.
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
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
    # OpenJD refuses a value with spaces around it, such as " true".
    text = str(value).lower()
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
        if not _INT_VALUE.fullmatch(text):
            return None
        try:
            return int(text)
        except ValueError:
            # Python refuses a number of over 4300 digits, as OpenJD does.
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def check_parameter_values(
    definitions: list[dict[str, Any]], parameters: Mapping[str, Any]
) -> None:
    """
    Checks the values of the job template's BOOL, INT, FLOAT, STRING and PATH parameters as Deadline
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
        elif kind in ("STRING", "PATH"):
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
            str(item) if kind in ("STRING", "PATH") else _number(item, kind) for item in allowed
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
    if _DESCRIPTION_FORBIDDEN.search(value):
        raise SubmitError(
            "The job description can't have control characters other than tabs and line breaks."
        )
    return value


def check_job_settings(settings: dict[str, Any]) -> None:
    """Checks the panel's job settings against Deadline Cloud's limits. Raises SubmitError."""
    name = settings.get("name")
    if name is not None and (
        not isinstance(name, str)
        or not 1 <= len(name) <= MAX_JOB_NAME
        or _NAME_FORBIDDEN.search(name)
    ):
        raise SubmitError(
            f"The job name must be 1 to {MAX_JOB_NAME} characters, without {{{{, }}}} or "
            "control characters."
        )
    for key, (low, high) in _SETTING_RANGES.items():
        value = settings.get(key)
        # Empty leaves the queue's default.
        if value is None or value == "":
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise SubmitError(f"{key} must be a whole number from {low} to {high}.")
    status = settings.get("targetTaskRunStatus")
    if status not in (None, "") and str(status).upper() not in ("READY", "SUSPENDED"):
        raise SubmitError("targetTaskRunStatus must be READY or SUSPENDED.")


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
