# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import sys
import types

import pytest

from deadline.comfyui_submitter.nodes import (
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
    DeadlineImageValues,
    DeadlineIntegerValues,
    DeadlineStringValues,
)
from deadline.comfyui_submitter.submit import IMAGE_NODE, INTEGER_NODE, STRING_NODE


def test_value_nodes_are_registered_under_the_names_the_job_replaces():
    assert NODE_CLASS_MAPPINGS == {
        STRING_NODE: DeadlineStringValues,
        INTEGER_NODE: DeadlineIntegerValues,
        IMAGE_NODE: DeadlineImageValues,
    }
    assert NODE_DISPLAY_NAME_MAPPINGS == {
        STRING_NODE: "Deadline String Values",
        INTEGER_NODE: "Deadline Integer Values",
        IMAGE_NODE: "Deadline Image Values",
    }


def test_string_values_outputs_the_preview_item():
    assert DeadlineStringValues().run("a red fox\n\na white fox\n", 2) == ("a white fox",)


def test_integer_values_outputs_the_preview_item():
    assert DeadlineIntegerValues().run("1-4:2, 42", 3, "pair") == (42,)


@pytest.mark.parametrize(
    ("node", "values", "item", "match"),
    [
        (DeadlineStringValues, "", 1, "Add"),
        (DeadlineStringValues, "a\nb", 3, "has 2 items"),
        (DeadlineIntegerValues, "1-2", 3, "has 2 items"),
        (DeadlineIntegerValues, "one", 1, "isn't a number"),
    ],
)
def test_value_nodes_reject_missing_items(node, values, item, match):
    with pytest.raises(ValueError, match=match):
        node().run(values, item)


def test_string_values_dont_use_dynamic_prompts():
    values = DeadlineStringValues.INPUT_TYPES()["required"]["values"][1]
    # The frontend would otherwise choose one {a|b} choice before the node sees it.
    assert values["dynamicPrompts"] is False


@pytest.mark.parametrize("node", [DeadlineStringValues, DeadlineIntegerValues])
def test_value_nodes_can_be_paired(node):
    assert node.INPUT_TYPES()["optional"]["pair_with"][1]["default"] == ""


@pytest.fixture
def comfy_input(monkeypatch, tmp_path):
    """Stands in for ComfyUI's folder_paths and Load Image node."""
    loaded = []

    class LoadImage:
        def load_image(self, image):
            loaded.append(image)
            return ("image", "mask")

    monkeypatch.setitem(
        sys.modules,
        "folder_paths",
        types.SimpleNamespace(
            get_input_directory=lambda: str(tmp_path),
            get_input_subfolders=lambda: ["cats", "dogs"],
        ),
    )
    monkeypatch.setitem(sys.modules, "nodes", types.SimpleNamespace(LoadImage=LoadImage))
    return loaded


def test_image_values_lists_input_folders(comfy_input):
    assert DeadlineImageValues.INPUT_TYPES()["required"]["folder"][0] == ["cats", "dogs"]


def test_image_values_loads_the_preview_image(comfy_input, tmp_path):
    (tmp_path / "cats").mkdir()
    for name in ("b.png", "a.jpg", "notes.txt"):
        (tmp_path / "cats" / name).write_text("x")
    assert DeadlineImageValues().run("cats", 2) == ("image", "mask")
    assert comfy_input == ["cats/b.png"]


@pytest.mark.parametrize(("folder", "match"), [("../out", "isn't in"), ("empty", "Add")])
def test_image_values_rejects_folders_without_images(comfy_input, tmp_path, folder, match):
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match=match):
        DeadlineImageValues().run(folder, 1)
