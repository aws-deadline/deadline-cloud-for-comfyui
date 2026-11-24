# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Finds the local files an API-format ComfyUI workflow reads (input images, videos, audio and so on,
but not custom nodes) and writes them to a job bundle's asset_references.yaml. Only the inputs
that ComfyUI's node definitions mark as files are read, and only files in ComfyUI's input, output
and temp folders are attached. Model files are not part of that scan. A model the workflow loads
is staged into the job's model directory and attached from there. A custom node pack the workflow
uses is staged into the job's custom nodes directory in the same way.
"""

from __future__ import annotations

import fnmatch
import functools
import json
import logging
import os
import re
import shutil
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

from .submit import (
    MODEL_EXTENSIONS,
    input_folders,
    leads_inside,
    real_path,
    workflow_models,
    write_private_text,
)

# Loaders for these file types look in ComfyUI's input directory.
MEDIA_EXTENSIONS = frozenset(
    {
        # Images
        ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".exr", ".hdr", ".avif",
        # Videos
        ".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".flv", ".wmv",
        # Audio
        ".wav", ".mp3", ".flac", ".ogg", ".m4a", ".opus", ".aac",
        # 3D
        ".glb", ".gltf", ".obj", ".ply", ".stl", ".fbx",
    }
)  # fmt: skip

# A value can name its directory with a suffix: "example.png [input]".
_ANNOTATED = re.compile(r"(?P<name>.+) \[(?P<directory>input|output|temp)\]")
_MAX_VALUE_LENGTH = 1024
# A path that starts with two separators: a UNC or Windows device path.
_NETWORK_PATH = re.compile(r"[\\/]{2}")

# The options a ComfyUI node definition sets on an input whose widget uploads a file into
# ComfyUI's input folder, such as LoadImage's image. The input's value names the file to load.
UPLOAD_OPTIONS = frozenset(
    {"image_upload", "animated_image_upload", "video_upload", "audio_upload", "file_upload"}
)
# Input types whose widget saves a capture into ComfyUI's folders and names it, such as
# WebcamCapture's image.
CAPTURE_INPUT_TYPES = frozenset({"WEBCAM", "AUDIO_RECORD"})
# The file inputs of ComfyUI's own loaders. get_local_assets reads these when it isn't given the
# node definitions' own, such as in a test, or when ComfyUI's nodes module can't be imported.
CORE_FILE_INPUTS: dict[str, frozenset[str]] = {
    "LoadImage": frozenset({"image"}),
    "LoadImageMask": frozenset({"image"}),
    "LoadImageOutput": frozenset({"image"}),
    "LoadVideo": frozenset({"file"}),
    "LoadAudio": frozenset({"audio"}),
    "Load3D": frozenset({"model_file"}),
    "WebcamCapture": frozenset({"image"}),
}
# The most files a folder loader's folder attaches, so the plan stays quick to make and to show.
# The artist can add a bigger folder in the panel.
MAX_FOLDER_FILES = 10_000

# folder_paths.folder_names_and_paths maps a folder type to (directories, extensions).
ModelFolders = Mapping[str, Sequence[Any]]

logger = logging.getLogger(__name__)


@dataclass
class LocalAssets:
    filenames: list[Path] = field(default_factory=list)
    directories: list[Path] = field(default_factory=list)
    # What the workflow names but isn't attached, and why, for the panel to show.
    warnings: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.filenames or self.directories)


def workflow_file_inputs(
    workflow: Mapping[str, Any], node_classes: Mapping[str, Any]
) -> dict[str, frozenset[str]]:
    """
    The file inputs of each node type in the workflow, by node type, as its ComfyUI node
    definition marks them. node_classes is ComfyUI's nodes.NODE_CLASS_MAPPINGS. Node types
    without file inputs, or whose definition can't be read, are left out.
    """
    class_types = {
        node.get("class_type") for node in workflow.values() if isinstance(node, Mapping)
    }
    found: dict[str, frozenset[str]] = {}
    for class_type in sorted(name for name in class_types if isinstance(name, str)):
        names = node_file_inputs(node_classes.get(class_type))
        if names:
            found[class_type] = names
    return found


def node_file_inputs(node_class: Any) -> frozenset[str]:
    """
    The inputs that a node class's INPUT_TYPES marks as files: an upload widget, such as
    LoadImage's image, or a capture widget, such as WebcamCapture's. Each input is (type,
    options), in its "required" or "optional" inputs. A definition that can't be read has none.
    """
    if node_class is None:
        return frozenset()
    try:
        definition = node_class.INPUT_TYPES()
        names = set()
        for group in ("required", "optional"):
            inputs = definition.get(group) if isinstance(definition, Mapping) else None
            for name, spec in inputs.items() if isinstance(inputs, Mapping) else ():
                if isinstance(name, str) and _is_file_input(spec):
                    names.add(name)
    except Exception as e:  # noqa: BLE001 - one node's definition mustn't stop the plan.
        # Any node's definition can fail, such as a custom node's that lists a missing folder.
        # Its inputs then aren't attached, which the job's own error makes clear.
        label = getattr(node_class, "__name__", type(node_class).__name__)
        logger.warning("Couldn't read the inputs of node %s: %s", _shown(label), _shown(e))
        return frozenset()
    return frozenset(names)


def _is_file_input(spec: Any) -> bool:
    if not isinstance(spec, (tuple, list)) or not spec:
        return False
    if isinstance(spec[0], str) and spec[0] in CAPTURE_INPUT_TYPES:
        return True
    options = spec[1] if len(spec) > 1 else None
    return isinstance(options, Mapping) and any(options.get(key) is True for key in UPLOAD_OPTIONS)


def get_local_assets(
    workflow: Mapping[str, Any],
    *,
    input_dir: str | os.PathLike[str],
    output_dir: str | os.PathLike[str] | None = None,
    temp_dir: str | os.PathLike[str] | None = None,
    file_inputs: Mapping[str, Collection[str]] | None = None,
) -> LocalAssets:
    """
    Returns the existing local files the workflow loads: the files its file inputs name, such as
    a LoadImage's image, and the files in the folders its folder loaders read. file_inputs is by
    node type, as workflow_file_inputs finds them. Without it, the inputs of ComfyUI's own
    loaders in CORE_FILE_INPUTS are read.

    No other input is read, so a prompt, or any other text in a shared workflow, can't attach a
    file by naming it. A file input names a media file in ComfyUI's input folder, such as
    "fox.png" or "cats/fox.png", or with "[input]", "[output]" or "[temp]", any file in that
    folder. An absolute path isn't attached: the artist can add files from anywhere in the panel.
    Neither is a file or a folder's entry that leads out of its folder through a link. Each of
    those adds a warning. Model files are left out of this scan.
    """
    base = Path(os.path.abspath(input_dir))
    resolver = _Resolver(
        {
            "input": base,
            "output": Path(os.path.abspath(output_dir)) if output_dir else None,
            "temp": Path(os.path.abspath(temp_dir)) if temp_dir else None,
        }
    )
    file_inputs = CORE_FILE_INPUTS if file_inputs is None else file_inputs

    filenames: set[Path] = set()
    warnings: list[str] = []
    for node_id, node in workflow.items():
        inputs = node.get("inputs") if isinstance(node, Mapping) else None
        class_type = node.get("class_type") if isinstance(node, Mapping) else None
        if not isinstance(inputs, Mapping) or not isinstance(class_type, str):
            continue
        for name in sorted(file_inputs.get(class_type, ())):
            value = inputs.get(name)
            # A link to another node's output is a list.
            if not isinstance(value, str):
                continue
            try:
                path, problem = resolver.resolve(value)
            except (OSError, ValueError):
                # An over-long or otherwise invalid path can't be one of the workflow's files.
                continue
            if path is not None:
                filenames.add(path)
            elif problem:
                warnings.append(
                    f"Node {_shown(node_id)} loads {_shown(value)!r}, which {problem}, so it "
                    "isn't attached. Add it with Add files… if the job needs it."
                )

    # Job attachments would upload what every link in a folder points to, so a folder is
    # attached as the files in it that stay in the input folder.
    root = real_path(base)
    for folder in input_folders(workflow, base, warnings):
        shown = _shown(folder.relative_to(base).as_posix())
        files, outside = _folder_files(folder, root)
        if files is None:
            warnings.append(
                f"The folder {shown!r} has more than {MAX_FOLDER_FILES:,} files, so it isn't "
                "attached. Add it with Add folder… if the job needs it."
            )
            continue
        filenames.update(files)
        if outside:
            names = [_shown(path.relative_to(folder).as_posix()) for path in outside]
            more = f", and {len(names) - 5} more" if len(names) > 5 else ""
            warnings.append(
                f"Entries in the folder {shown!r} that link outside ComfyUI's input folder "
                f"aren't attached: {', '.join(names[:5])}{more}."
            )

    return LocalAssets(sorted(filenames), [], list(dict.fromkeys(warnings)))


def _folder_files(folder: Path, root: Path) -> tuple[list[Path] | None, list[Path]]:
    """
    The files in a folder loader's folder and its subfolders, and the entries that lead out of
    root, the real path of ComfyUI's input folder. The files are None when there are more than
    MAX_FOLDER_FILES. Every entry is resolved, not only symbolic links, since Python doesn't
    count a Windows directory junction as one. A link to a folder in the input folder isn't
    followed, so walking can't loop. A link to a file in it is attached under its own name.
    """
    files: list[Path] = []
    outside: list[Path] = []
    pending = [folder]
    while pending:
        directory = pending.pop()
        try:
            real_directory = os.path.realpath(directory)
            with os.scandir(directory) as entries:
                names = sorted(entry.name for entry in entries)
        except (OSError, ValueError):
            continue
        for name in names:
            path = directory / name
            try:
                real = real_path(path)
            except (OSError, ValueError):
                continue
            if not real.is_relative_to(root):
                outside.append(path)
            elif real.is_dir():
                if str(real) == os.path.join(real_directory, name):
                    pending.append(path)
            elif real.is_file():
                files.append(path)
                if len(files) > MAX_FOLDER_FILES:
                    return None, outside
    return sorted(files), sorted(outside)


def write_asset_references(assets: LocalAssets, path: Path) -> Path:
    """
    Writes asset_references.yaml in the job bundle format. Outputs aren't listed because the job
    template's OutputDir path parameter already covers them.
    """

    def block(key: str, paths: Iterable[Path], indent: str) -> list[str]:
        paths = list(paths)
        if not paths:
            return [f"{indent}{key}: []"]
        # A JSON string is also a valid, quoted YAML string.
        return [f"{indent}{key}:"] + [
            f"{indent}- {json.dumps(str(p), ensure_ascii=False)}" for p in paths
        ]

    lines = [
        "assetReferences:",
        "  inputs:",
        *block("filenames", assets.filenames, "    "),
        *block("directories", assets.directories, "    "),
        "  outputs:",
        *block("directories", [], "    "),
        *block("referencedPaths", [], "  "),
    ]
    write_private_text(path, "\n".join(lines) + "\n")
    return path


def find_local_model(folders: ModelFolders, directory: str, name: str) -> Path | None:
    """
    The file ComfyUI would load for this folder type and name, or None. folders is
    folder_paths.folder_names_and_paths: each value is (directories, extensions). The directories
    are searched in order. name may include a subfolder.
    """
    entry = folders.get(directory)
    if not entry:
        return None
    try:
        roots = entry[0]
    except (TypeError, KeyError):
        return None
    if isinstance(roots, (str, bytes)):
        return None
    for root in roots:
        try:
            path = _Resolver._contained(Path(root), name)
        except (OSError, ValueError, TypeError):
            continue
        if path is not None and path.is_file():
            return path
    return None


def model_id(directory: str, name: str) -> str:
    """
    What identifies a local model: its model folder and the name the workflow loads it by, such
    as checkpoints/sd.safetensors. Two models can share a name in different folders.
    """
    return f"{directory}/{name}"


def model_name(key: str) -> str:
    """The name part of a model ID. ComfyUI's model folder names don't contain "/"."""
    return key.split("/", 1)[-1]


def stage_local_models(
    models: Mapping[str, Path], directories: Mapping[str, str], dest: Path
) -> dict[str, Path]:
    """
    Places each model at dest/<directory>/<name> and returns the path of each, by model ID.
    models and directories are by model ID too. The destination matches the model folders in the
    job's extra model paths config, so ComfyUI on the worker loads the original file name from the
    mapped model directory. A hard link is used when the destination is on the same volume;
    otherwise the file is copied. A file that cannot be placed is logged and left out.
    """
    staged: dict[str, Path] = {}
    for key, source in models.items():
        directory = directories.get(key)
        name = model_name(key)
        relative = _model_relative_path(directory, name)
        if relative is None:
            continue
        try:
            target = _Resolver._contained(dest, relative)
        except (OSError, ValueError):
            continue
        if target is None or not source.is_file():
            logger.warning("Not staging model %r from %s", name, source)
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                try:
                    os.link(source, target)
                except OSError as e:
                    # Such as a model on another drive. The copy is deleted with the bundle.
                    logger.warning(
                        "Copying model %r into the job bundle, since it can't be linked from %s: %s",
                        name,
                        source,
                        e,
                    )
                    shutil.copy2(source, target)
        except OSError:
            logger.exception("Couldn't stage model %r from %s", name, source)
            continue
        staged[key] = target
    return dict(sorted(staged.items()))


# Left out of staged node packs: version control, caches and Python virtual environments.
_SKIPPED_NODE_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
    }
)


# Left out of staged node packs: files that usually hold credentials, such as an API-backed
# pack's keys. Job attachments can be read by anyone who can use the queue. Settings files such
# as config.json are kept, since packs need them to run, so a list of names can't catch every
# secret: the panel lets the artist leave a whole pack out.
_SECRET_NODE_FILES = (
    # Environment files, such as .env.local and prod.env.
    ".env",
    ".env.*",
    "*.env",
    ".envrc",
    # Keys and certificates.
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.ppk",
    "*.jks",
    "*.keystore",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    # Tokens and API keys, such as hf_token and api_key.txt. tokenizer.json isn't one.
    "token",
    "token.*",
    "*_token",
    "*_token.*",
    "*-token",
    "*-token.*",
    "api_key",
    "api_key.*",
    "*_api_key",
    "*_api_key.*",
    "apikey",
    "apikey.*",
    # Credentials of other tools.
    ".netrc",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    ".pgpass",
    ".htpasswd",
    ".boto",
    ".s3cfg",
    ".dockercfg",
    "kubeconfig",
    "*.kubeconfig",
    "credentials",
    "credentials.*",
    "service_account*.json",
    "service-account*.json",
    "client_secret*.json",
    "secrets.json",
    "secrets.toml",
    "secrets.yaml",
    "secrets.yml",
)


_NODE_CODE_SUFFIXES = (".py", ".pyi", ".js", ".mjs")


def _is_secret_node_file(name: str) -> bool:
    name = name.lower()
    # A code file is the pack's own, such as a credentials.py that its nodes import, or its web
    # extension's JavaScript.
    if name.endswith(_NODE_CODE_SUFFIXES):
        return False
    return any(fnmatch.fnmatch(name, pattern) for pattern in _SECRET_NODE_FILES)


def _ignore_node_files(root: Path, directory: str, names: list[str], log: bool = True) -> set[str]:
    """
    The entries of a pack's folder to leave out of the job. root is the pack's folder, resolved.
    copytree follows links, so an entry is attached only when it resolves inside the pack, and is
    checked as its target as well as by its own name. A link to a folder in the pack is left out:
    the folder is attached under its own name, and following it could loop. Every entry is
    resolved, not only symbolic links, since Python doesn't count a Windows directory junction as
    one.
    """
    warn = logger.warning if log else _quiet
    ignored = set()
    real_directory = os.path.realpath(directory)
    for name in names:
        path = Path(directory, name)
        if _skipped_node_entry(name, path):
            ignored.add(name)
            continue
        target = path.resolve()
        is_link = str(target) != os.path.join(real_directory, name)
        if not target.is_relative_to(root):
            warn("Leaving %s out of the job: it links outside the pack", path)
            ignored.add(name)
        elif not (path.is_file() or path.is_dir()):
            # Such as a named pipe, which would block the copy, or a link to nothing.
            warn("Leaving %s out of the job: it isn't a file or a folder", path)
            ignored.add(name)
        elif is_link and path.is_dir():
            warn(
                "Leaving %s out of the job: it links to a folder in the pack, which is attached "
                "under its own name",
                path,
            )
            ignored.add(name)
        elif is_link and _in_skipped_node_entry(root, target):
            warn("Leaving %s out of the job: it links to a file that's left out", path)
            ignored.add(name)
        elif path.is_file() and (_is_secret_node_file(name) or _is_secret_node_file(target.name)):
            warn("Leaving %s out of the job: it can hold credentials", path)
            ignored.add(name)
    return ignored


def _quiet(*_args: Any) -> None:
    pass


def _skipped_node_entry(name: str, path: Path) -> bool:
    """Version control, caches, compiled Python and virtual environments."""
    return name in _SKIPPED_NODE_DIRS or name.endswith(".pyc") or (path / "pyvenv.cfg").is_file()


def _in_skipped_node_entry(root: Path, target: Path) -> bool:
    """Whether a link's target in the pack is, or is inside, an entry that's left out."""
    path = root
    for part in target.relative_to(root).parts:
        path = path / part
        if _skipped_node_entry(part, path):
            return True
    return False


def _link_or_copy(source: str, target: str, copied: list[str] | None = None) -> str:
    """
    Hard links source to target, or copies it, such as from another drive, into copied. A link is
    resolved first, so the target holds the file itself on every platform: Windows would otherwise
    hard link the link.
    """
    source = os.path.realpath(source)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)
        if copied is not None:
            copied.append(source)
    return target


# Counting a pack's files stops here. Planning stays quick for a pack with a folder of models.
MAX_COUNTED_NODE_FILES = 100_000


@dataclass
class NodePackSize:
    """What a custom node pack adds to a job, leaving out what staging leaves out."""

    bytes: int = 0
    files: int = 0
    # False when the pack has more than MAX_COUNTED_NODE_FILES files, and counting stopped.
    complete: bool = True


def node_pack_size(source: Path) -> NodePackSize:
    """The size and file count of a custom node pack, as staging would attach it."""
    size = NodePackSize()
    if not source.is_dir():
        size.bytes, size.files = _file_size(source), 1
        return size
    root = source.resolve()
    # Following links is bounded: the ignore rules leave out every link to a folder in the pack.
    for directory, folders, files in os.walk(source, followlinks=True):
        ignored = _ignore_node_files(root, directory, folders + files, log=False)
        folders[:] = [folder for folder in folders if folder not in ignored]
        for name in files:
            if name in ignored:
                continue
            if size.files == MAX_COUNTED_NODE_FILES:
                size.complete = False
                return size
            size.bytes += _file_size(Path(directory, name))
            size.files += 1
    return size


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def stage_local_node_packs(packs: Mapping[str, Path], dest: Path) -> dict[str, Path]:
    """
    Places each custom node pack at dest/<name>: a pack folder is linked file by file, a
    single-file custom node as one file. Files are hard linked when the destination is on the
    same volume, and copied otherwise. Version control, cache and virtual environment folders
    are left out of folders. A pack that cannot be placed is logged and left out.
    """
    staged: dict[str, Path] = {}
    # Packs from two custom node folders, such as MyPack and mypack, would share one folder on a
    # file system that ignores case.
    folded: dict[str, str] = {}
    for name, source in packs.items():
        if not name or name != source.name or PurePath(name).name != name or name in (".", ".."):
            logger.warning("Not staging custom node pack %r from %s", name, source)
            continue
        if name.casefold() in folded:
            logger.warning(
                "Not staging custom node pack %r from %s: its name and %r differ in case alone",
                name,
                source,
                folded[name.casefold()],
            )
            continue
        folded[name.casefold()] = name
        target = dest / name
        copied: list[str] = []
        try:
            if source.is_dir():
                # The pack's own folder can be a link, such as to a checkout elsewhere.
                shutil.copytree(
                    source,
                    target,
                    ignore=functools.partial(_ignore_node_files, source.resolve()),
                    copy_function=functools.partial(_link_or_copy, copied=copied),
                    dirs_exist_ok=True,
                )
            elif source.is_file() and source.suffix == ".py":
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    _link_or_copy(str(source), str(target), copied)
            else:
                logger.warning("Not staging custom node pack %r from %s", name, source)
                continue
        except (OSError, shutil.Error):
            logger.exception("Couldn't stage custom node pack %r from %s", name, source)
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
            continue
        if copied:
            # Copying takes time and space. The copies are deleted with the bundle.
            logger.warning(
                "Copied %d files of custom node pack %r into the job bundle, since they can't be "
                "linked from %s",
                len(copied),
                name,
                source,
            )
        staged[name] = target
    return dict(sorted(staged.items()))


def _model_relative_path(directory: str | None, name: str) -> str | None:
    """`directory/name` with separators normalized. `..` is refused."""
    if not isinstance(directory, str) or not name:
        return None
    parts = (*PurePath(directory).parts, *PurePath(name).parts)
    if ".." in parts:
        return None
    return "/".join(parts) or None


def find_workflow_local_models(
    ui_workflow: Any, api_workflow: Mapping[str, Any], folders: ModelFolders
) -> tuple[dict[str, Path], dict[str, str]]:
    """
    Local files for the models the workflow loads. The names are the same list as
    workflow_models. The folder comes from the loader's properties, or from the subgraph
    definition when the file name is on a subgraph widget. Without a folder, or when the folder
    doesn't have the file, every registered model folder is searched, and a name found in more
    than one folder is attached from each, since the workflow doesn't say which loader reads
    which. Missing files are logged and omitted.

    Returns, by model ID, the local path and folder type of each file to stage as a job attachment.
    """
    if not isinstance(api_workflow, dict):
        return {}, {}
    known = _model_directories(ui_workflow)
    found: dict[str, Path] = {}
    directories: dict[str, str] = {}
    for name in workflow_models(api_workflow):
        located = _search_local_models(folders, name, known.get(name, []))
        if not located:
            logger.warning("No local file for model %r in folders %r", name, known.get(name))
        for path, folder in located:
            found[model_id(folder, name)] = path
            directories[model_id(folder, name)] = folder
    return found, directories


def _model_directories(ui_workflow: Any) -> dict[str, list[tuple[str, bool]]]:
    """
    Maps a model file name written on UI nodes to the ComfyUI folders they load it from, each with
    whether the loader declares it, rather than it being a guess from a subgraph.
    """
    subgraphs = _subgraphs_by_id(ui_workflow)
    # Each subgraph is indexed once, however many nodes are instances of it.
    indexes: dict[str, _SubgraphIndex] = {}
    directories: dict[str, list[tuple[str, bool]]] = {}
    nodes = ui_workflow.get("nodes") if isinstance(ui_workflow, Mapping) else None
    if not isinstance(nodes, list):
        return directories
    for node in nodes:
        # mode 2 and 4 are muted and bypassed. Their models aren't in the API workflow.
        if not isinstance(node, Mapping) or node.get("mode") in (2, 4):
            continue
        node_type = node.get("type")
        index = None
        if isinstance(node_type, str) and node_type in subgraphs:
            index = indexes.get(node_type)
            if index is None:
                index = indexes[node_type] = _SubgraphIndex(subgraphs[node_type])
        widgets = _widget_names_by_value(node) if index else {}
        declared = _node_models(node)
        for filename in _model_filenames(node):
            hint = _model_directory(declared, filename, widgets.get(filename, ()), index)
            # workflow_models normalizes separators, so the lookup key has to match.
            folders = directories.setdefault(filename.replace("\\", "/"), [])
            if hint and hint not in folders:
                folders.append(hint)
    return directories


def _is_model_filename(value: str) -> bool:
    if len(value) > _MAX_VALUE_LENGTH or "://" in value:
        return False
    if any(char in value for char in "\n\r\0"):
        return False
    return PurePath(value).suffix.lower() in MODEL_EXTENSIONS


def _model_filenames(node: Mapping[str, Any]) -> list[str]:
    """Model file names written on a node, ignoring download URLs."""
    found: list[str] = []
    seen: set[str] = set()

    def walk(value: Any, key: str | None = None) -> None:
        if key == "url":
            return
        if isinstance(value, str):
            filename = value.strip()
            if filename not in seen and _is_model_filename(filename):
                seen.add(filename)
                found.append(filename)
        elif isinstance(value, Mapping):
            for child_key, child in value.items():
                walk(child, child_key if isinstance(child_key, str) else None)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return found


def _node_models(node: Mapping[str, Any]) -> list[dict[str, Any]]:
    properties = node.get("properties")
    if not isinstance(properties, Mapping):
        return []
    models = properties.get("models")
    if not isinstance(models, list):
        return []
    return [model for model in models if isinstance(model, dict)]


def _subgraphs(ui_workflow: Any) -> list[Mapping[str, Any]]:
    if not isinstance(ui_workflow, Mapping):
        return []
    definitions = ui_workflow.get("definitions")
    if not isinstance(definitions, Mapping):
        return []
    subgraphs = definitions.get("subgraphs")
    if not isinstance(subgraphs, list):
        return []
    return [subgraph for subgraph in subgraphs if isinstance(subgraph, Mapping)]


def _subgraphs_by_id(ui_workflow: Any) -> dict[str, Mapping[str, Any]]:
    return {
        subgraph_id: subgraph
        for subgraph in _subgraphs(ui_workflow)
        if isinstance(subgraph_id := subgraph.get("id"), str)
    }


def _widget_inputs(node: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    inputs = node.get("inputs")
    if not isinstance(inputs, list):
        return []
    return [
        item
        for item in inputs
        if isinstance(item, Mapping) and isinstance(item.get("widget"), dict)
    ]


def _widget_name(item: Mapping[str, Any]) -> str | None:
    name = item["widget"].get("name")
    if isinstance(name, str):
        return name
    fallback = item.get("name")
    return fallback if isinstance(fallback, str) else None


def _widget_names_by_value(node: Mapping[str, Any]) -> dict[str, list[str]]:
    """Each string widget value on a node, with the names of the widgets that hold it."""
    names: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()

    def add(value: Any, name: Any) -> None:
        if isinstance(value, str) and isinstance(name, str) and (value, name) not in seen:
            seen.add((value, name))
            names.setdefault(value, []).append(name)

    named = node.get("widgets_values_named")
    if isinstance(named, Mapping):
        for key, value in named.items():
            add(value, key)
    widgets = node.get("widgets_values")
    if isinstance(widgets, list):
        for index, item in enumerate(_widget_inputs(node)):
            if index < len(widgets):
                add(widgets[index], _widget_name(item))
    return names


def _directory_from_models(models: Iterable[dict[str, Any]], filename: str) -> str | None:
    for model in models:
        directory = model.get("directory")
        if model.get("name") == filename and isinstance(directory, str):
            return directory
    return None


def _hashable(value: Any) -> bool:
    try:
        hash(value)
    except TypeError:
        return False
    return True


class _SubgraphIndex:
    """
    What a subgraph's widget inputs feed: the links each input has, and, for each link, the first
    inner node that it connects to and that declares a model folder. Built once per subgraph, so
    looking up a file name doesn't read every inner node again.
    """

    def __init__(self, subgraph: Mapping[str, Any]):
        self.input_links: dict[str, list[Any]] = {}
        inputs = subgraph.get("inputs")
        for item in inputs if isinstance(inputs, list) else []:
            ids = item.get("linkIds") if isinstance(item, Mapping) else None
            name = item.get("name") if isinstance(item, Mapping) else None
            if isinstance(name, str) and isinstance(ids, list):
                self.input_links.setdefault(name, []).extend(i for i in ids if _hashable(i))
        # For each link, the position of the first inner node it feeds that declares a folder,
        # and that folder.
        self.first_directory: dict[Any, tuple[int, str]] = {}
        nodes = subgraph.get("nodes")
        for position, inner in enumerate(nodes if isinstance(nodes, list) else []):
            if not isinstance(inner, Mapping) or not isinstance(inner.get("inputs"), list):
                continue
            directory = next(
                (
                    model["directory"]
                    for model in _node_models(inner)
                    if isinstance(model.get("directory"), str)
                ),
                None,
            )
            if directory is None:
                continue
            for item in inner["inputs"]:
                link = item.get("link") if isinstance(item, Mapping) else None
                if link is not None and _hashable(link):
                    self.first_directory.setdefault(link, (position, directory))

    def directory(self, widget_names: Iterable[str]) -> str | None:
        """The folder of the first inner loader that these widget inputs feed, if any."""
        found = [
            self.first_directory[link]
            for name in set(widget_names)
            for link in self.input_links.get(name, ())
            if link in self.first_directory
        ]
        return min(found)[1] if found else None


def _model_directory(
    declared: list[dict[str, Any]],
    filename: str,
    widget_names: Iterable[str],
    subgraph: _SubgraphIndex | None,
) -> tuple[str, bool] | None:
    """
    The folder a node loads a model file from, and whether the node declares it. declared is the
    node's own models, and widget_names the widgets that hold the file name, which a subgraph
    feeds into its inner loaders.
    """
    folder = _directory_from_models(declared, filename)
    if folder:
        return folder, True
    guessed = subgraph.directory(widget_names) if subgraph else None
    return (guessed, False) if guessed else None


# Folder names ComfyUI still accepts from older workflows, as its folder_paths.map_legacy does.
_LEGACY_FOLDERS = {"clip": "text_encoders", "unet": "diffusion_models"}
# Folders ComfyUI registers that don't hold models. A workflow's model name could otherwise
# attach any file from them, such as a custom node pack's cache or settings. Packs are attached
# separately, as a whole.
_NOT_MODEL_FOLDERS = frozenset({"custom_nodes"})


def _search_local_models(
    folders: ModelFolders, name: str, hints: Sequence[tuple[str, bool]]
) -> list[tuple[Path, str]]:
    """
    The local files for a model name, and the folder each was found in. hints are the folders the
    workflow names, each with whether its loader declares it. They're searched first. A loader
    on the worker only reads its declared folder, so when that doesn't have the file, the model
    isn't found. Otherwise, such as for a guess from a subgraph or a folder ComfyUI doesn't
    register, every folder is searched.
    """
    hinted = [
        (_LEGACY_FOLDERS.get(hint, hint) if hint not in folders else hint, declared)
        for hint, declared in hints
    ]
    certain = any(declared and hint in folders for hint, declared in hinted)
    searches = [dict.fromkeys(hint for hint, _ in hinted)]
    if not certain:
        searches.append(dict.fromkeys(folders))
    for candidates in searches:
        located = [
            (path, folder_name)
            for folder_name in candidates
            if folder_name not in _NOT_MODEL_FOLDERS
            and (path := find_local_model(folders, folder_name, name)) is not None
        ]
        if located:
            return located
    return []


def _shown(value: Any, length: int = 60) -> str:
    """
    A value from the workflow for a message, on one line. A long one keeps its end, so a path
    keeps its file name.
    """
    text = re.sub(r"[\s\x00-\x1f\x7f-\x9f]+", " ", str(value)).strip()
    return text if len(text) <= length else "…" + text[-(length - 1) :]


class _Resolver:
    """Finds the file that a file input's value names, in ComfyUI's input, output or temp folder."""

    def __init__(self, roots: Mapping[str, Path | None]):
        self._roots = roots
        self._real_roots: dict[str, Path] = {}

    def _real_root(self, kind: str, root: Path) -> Path:
        # Resolved, so a ComfyUI whose input folder is itself a link, such as to another disk,
        # still attaches the files in it.
        if kind not in self._real_roots:
            self._real_roots[kind] = real_path(root)
        return self._real_roots[kind]

    def resolve(self, value: str) -> tuple[Path | None, str | None]:
        """
        The file a value names, or None, with why it isn't attached when the artist should know.
        """
        if len(value) > _MAX_VALUE_LENGTH or "\n" in value or "\0" in value:
            return None, None

        annotated = _ANNOTATED.fullmatch(value)
        kind, name = (annotated["directory"], annotated["name"]) if annotated else ("input", value)
        name = name.strip()
        # Refused before anything is read from the disk. On Windows, checking a UNC
        # (\\server\share) or device (\\?\, \\.\) path contacts that server or device, which can
        # send the user's credentials to a host that a downloaded workflow names. An absolute
        # path can name any file on the disk: the artist adds those in the panel.
        if _NETWORK_PATH.match(name) or PurePath(name).is_absolute():
            return None, "isn't in ComfyUI's input, output or temp folder"
        # ComfyUI's loaders only list media files in the input folder.
        if not annotated and PurePath(name).suffix.lower() not in MEDIA_EXTENSIONS:
            return None, None

        root = self._roots.get(kind)
        if root is None:
            return None, None
        path = self._contained(root, name)
        if path is None:
            return None, f"isn't in ComfyUI's {kind} folder"
        if not path.is_file():
            return None, None
        if not leads_inside(path, self._real_root(kind, root)):
            return None, f"links outside ComfyUI's {kind} folder"
        return path, None

    @staticmethod
    def _contained(directory: Path, name: str) -> Path | None:
        """Joins name onto directory, refusing names that climb out of it (for example `../x`)."""
        path = Path(os.path.normpath(directory / name))
        base = Path(os.path.normpath(directory))
        return path if base in path.parents else None
