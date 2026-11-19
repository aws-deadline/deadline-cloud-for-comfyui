# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""ComfyUI custom-node entry point, which makes src/ importable."""

import logging
import os
import sys

logger = logging.getLogger(__name__)

WEB_DIRECTORY = "./src/deadline/comfyui_submitter/web"
NODE_CLASS_MAPPINGS: dict = {}
NODE_DISPLAY_NAME_MAPPINGS: dict = {}

try:
    import deadline.comfyui_submitter  # noqa: F401
except ImportError:
    sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

try:
    from deadline.comfyui_submitter import routes  # noqa: F401
except Exception:  # Never break ComfyUI startup.
    logger.exception("Deadline Cloud Submitter: failed to register the /deadline routes")

try:
    from deadline.comfyui_submitter import nodes

    NODE_CLASS_MAPPINGS.update(nodes.NODE_CLASS_MAPPINGS)
    NODE_DISPLAY_NAME_MAPPINGS.update(nodes.NODE_DISPLAY_NAME_MAPPINGS)
except Exception:  # Never break ComfyUI startup.
    logger.exception("Deadline Cloud Submitter: failed to register the Deadline value nodes")

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
