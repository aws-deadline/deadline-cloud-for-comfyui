# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
The Deadline Cloud value nodes, which list values to run as separate Deadline Cloud tasks. In
ComfyUI each outputs its preview item. The comfy-cli job's task script replaces each with the
task's value before running the workflow, so workers don't need this extension installed.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from .submit import (
    IMAGE_NODE,
    INTEGER_NODE,
    SPLIT_BY_CHOICES,
    SPLIT_BY_LINES,
    STRING_NODE,
    image_files,
    input_path,
    integer_values,
    string_values,
)

_DESCRIPTION = (
    " Each task runs with one of them, and every combination of them with other lists is a task, "
    "unless pair_with matches another value node's, which runs their items together. Here, it "
    "outputs the preview item."
)


def _preview_item() -> tuple[str, dict[str, Any]]:
    return (
        "INT",
        {
            "default": 1,
            "min": 1,
            "max": 10000,
            "tooltip": "The item to output when you run the workflow in ComfyUI.",
        },
    )


def _pair_with() -> tuple[str, dict[str, Any]]:
    return (
        "STRING",
        {
            "default": "",
            "tooltip": (
                "A name to pair this node with other value nodes that have the same name and the "
                "same number of items, so their first items run together, then their second and "
                "so on. Leave empty to run every combination."
            ),
        },
    )


def _input_subfolders() -> list[str]:
    """The folders in ComfyUI's input directory, as ComfyUI's own get_input_subfolders lists them."""
    import folder_paths

    # ComfyUI added get_input_subfolders in June 2025.
    if hasattr(folder_paths, "get_input_subfolders"):
        return folder_paths.get_input_subfolders()
    input_dir = folder_paths.get_input_directory()
    folders = []
    for root, _, _ in os.walk(input_dir):
        relative = os.path.relpath(root, input_dir)
        if relative != ".":
            folders.append(relative.replace(os.sep, "/"))
    return sorted(folders)


def _preview(values: list[Any], preview_item: int, what: str) -> Any:
    if not values:
        raise ValueError(f"Add the {what}'s values.")
    if preview_item > len(values):
        raise ValueError(f"The {what} has {len(values)} items, not {preview_item}.")
    return values[preview_item - 1]


class DeadlineStringValues:
    CATEGORY = "Deadline Cloud"
    DESCRIPTION = (
        "Lists text values to run as separate Deadline Cloud tasks: one per line, or separated by "
        "lines of --- so that a value can span several lines. Connect its output to a prompt, a "
        "LoRA name or any other text input." + _DESCRIPTION
    )
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("value",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "values": (
                    "STRING",
                    {
                        "multiline": True,
                        # The frontend would otherwise choose one {a|b} choice first.
                        "dynamicPrompts": False,
                        "default": "",
                        "tooltip": "One value per line, or values separated by lines of ---.",
                    },
                ),
                "preview_item": _preview_item(),
            },
            "optional": {
                "pair_with": _pair_with(),
                "split_by": (
                    list(SPLIT_BY_CHOICES),
                    {
                        "default": SPLIT_BY_LINES,
                        "tooltip": (
                            "lines: one value per line. --- lines: values separated by lines of "
                            "---, so a value such as a prompt can have several lines and "
                            "paragraphs."
                        ),
                    },
                ),
            },
        }

    def run(
        self, values: str, preview_item: int, pair_with: str = "", split_by: str = SPLIT_BY_LINES
    ) -> tuple[str]:
        values_list = string_values(values, split_by)
        return (_preview(values_list, preview_item, "Deadline String Values"),)


class DeadlineIntegerValues:
    CATEGORY = "Deadline Cloud"
    DESCRIPTION = (
        "Lists whole numbers to run as separate Deadline Cloud tasks, such as seeds. Connect its "
        "output to a seed, steps or any other number input." + _DESCRIPTION
    )
    RETURN_TYPES = ("INT",)
    RETURN_NAMES = ("value",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "values": (
                    "STRING",
                    {
                        "default": "1-4",
                        "dynamicPrompts": False,
                        "tooltip": 'Numbers and ranges, such as "1-100", "1-20:2" or "7, 42, 99".',
                    },
                ),
                "preview_item": _preview_item(),
            },
            "optional": {"pair_with": _pair_with()},
        }

    def run(self, values: str, preview_item: int, pair_with: str = "") -> tuple[int]:
        numbers = integer_values(values, allow_repeats=bool(pair_with.strip()))
        return (_preview(numbers, preview_item, "Deadline Integer Values"),)


class DeadlineImageValues:
    CATEGORY = "Deadline Cloud"
    DESCRIPTION = (
        "Lists the images in a folder of ComfyUI's input directory to run as separate Deadline "
        "Cloud tasks, in name order. The folder is attached to the job." + _DESCRIPTION
    )
    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("image", "mask")
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "folder": (
                    _input_subfolders(),
                    {"tooltip": "A folder in ComfyUI's input directory."},
                ),
                "preview_item": _preview_item(),
            },
            "optional": {"pair_with": _pair_with()},
        }

    @classmethod
    def IS_CHANGED(cls, folder: str, preview_item: int, pair_with: str = "") -> str:
        """
        Changes with the preview image's name and contents, as Load Image's does, so ComfyUI loads
        it again after the folder changes.
        """
        try:
            name = _preview_name(folder, preview_item)
            path = _input_folder(folder) / name
            digest = hashlib.sha256(name.encode())
            with open(path, "rb") as file:
                for chunk in iter(lambda: file.read(1 << 20), b""):
                    digest.update(chunk)
        except (OSError, ValueError):
            # run() reports the problem.
            return ""
        return digest.hexdigest()

    def run(self, folder: str, preview_item: int, pair_with: str = "") -> tuple[Any, Any]:
        from nodes import LoadImage

        name = _preview_name(folder, preview_item)
        return LoadImage().load_image(f"{folder}/{name}")


def _input_folder(folder: str) -> Path:
    import folder_paths

    directory = input_path(folder_paths.get_input_directory(), folder)
    if directory is None or not directory.is_dir():
        raise ValueError(f"The folder {folder!r} isn't in ComfyUI's input directory.")
    return directory


def _preview_name(folder: str, preview_item: int) -> str:
    """The name of the image that Deadline Image Values outputs in ComfyUI."""
    return _preview(image_files(_input_folder(folder)), preview_item, "Deadline Image Values")


NODE_CLASS_MAPPINGS = {
    STRING_NODE: DeadlineStringValues,
    INTEGER_NODE: DeadlineIntegerValues,
    IMAGE_NODE: DeadlineImageValues,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    STRING_NODE: "Deadline String Values",
    INTEGER_NODE: "Deadline Integer Values",
    IMAGE_NODE: "Deadline Image Values",
}
