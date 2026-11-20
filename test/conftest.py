# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""
Silences the one log record the repository root's __init__.py makes under pytest.

Any directory with an __init__.py is a package to pytest, and the repository root has one,
because it's ComfyUI's custom-node entry point. Collecting the root as a package imports it,
and its /deadline route registration then fails (there's no ComfyUI in a test process) and
logs "failed to register the /deadline routes" into the captured logs of every failing test.
Under ComfyUI itself the record is real, so it's only dropped here, by name and message.
"""

from __future__ import annotations

import logging


class _NotUnderComfyUI(logging.Filter):
    """Drops the root __init__.py's route-registration error, which only pytest triggers."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "failed to register the /deadline routes" not in record.getMessage()


logging.getLogger("__init__").addFilter(_NotUnderComfyUI())
