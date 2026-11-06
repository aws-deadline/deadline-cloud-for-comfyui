# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
The Deadline Cloud value nodes, which list values to run as separate Deadline Cloud tasks. In
ComfyUI each outputs its preview item. The comfy-cli job's task script replaces each with the
task's value before running the workflow, so workers don't need this extension installed.
"""

from __future__ import annotations

from typing import Any

from .submit import (
    IMAGE_NODE,
    INTEGER_NODE,
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


def _preview(values: list[Any], preview_item: int, what: str) -> Any:
    if not values:
        raise ValueError(f"Add the {what}'s values.")
    if preview_item > len(values):
        raise ValueError(f"The {what} has {len(values)} items, not {preview_item}.")
    return values[preview_item - 1]


class DeadlineStringValues:
    CATEGORY = "Deadline Cloud"
    DESCRIPTION = (
        "Lists text values, one per line, to run as separate Deadline Cloud tasks. Connect its "
        "output to a prompt, a LoRA name or any other text input." + _DESCRIPTION
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
                        "tooltip": "One value per line.",
                    },
                ),
                "preview_item": _preview_item(),
            },
            "optional": {"pair_with": _pair_with()},
        }

    def run(self, values: str, preview_item: int, pair_with: str = "") -> tuple[str]:
        return (_preview(string_values(values), preview_item, "Deadline String Values"),)


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
        return (_preview(integer_values(values), preview_item, "Deadline Integer Values"),)


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
        import folder_paths

        return {
            "required": {
                "folder": (
                    folder_paths.get_input_subfolders(),
                    {"tooltip": "A folder in ComfyUI's input directory."},
                ),
                "preview_item": _preview_item(),
            },
            "optional": {"pair_with": _pair_with()},
        }

    def run(self, folder: str, preview_item: int, pair_with: str = "") -> tuple[Any, Any]:
        import folder_paths
        from nodes import LoadImage

        directory = input_path(folder_paths.get_input_directory(), folder)
        if directory is None or not directory.is_dir():
            raise ValueError(f"The folder {folder!r} isn't in ComfyUI's input directory.")
        name = _preview(image_files(directory), preview_item, "Deadline Image Values")
        return LoadImage().load_image(f"{folder}/{name}")


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
