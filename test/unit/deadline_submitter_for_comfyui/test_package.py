# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import deadline.comfyui_submitter


def test_package_imports() -> None:
    assert deadline.comfyui_submitter.__name__ == "deadline.comfyui_submitter"
