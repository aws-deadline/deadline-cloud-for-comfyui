# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

# The dependency group in pyproject.toml that the installer bundles. They aren't the project's
# dependencies, which ComfyUI installs into its own Python environment.
INSTALLER_DEPENDENCY_GROUP = "installer"


def get_project_dict(project_path: Path | None = None) -> dict[str, Any]:
    with open(str((project_path or get_git_root()) / "pyproject.toml"), "rb") as pyproject_toml:
        return tomllib.load(pyproject_toml)


class Dependency:
    pip_requirement: str
    name: str

    def __init__(self, dep: str):
        self.pip_requirement = dep.strip().split(";", maxsplit=1)[0].replace(" ", "")
        self.name = dep.strip().split(" ", maxsplit=1)[0]

    def for_pip(self) -> str:
        return self.pip_requirement

    def __repr__(self) -> str:
        return self.for_pip()


def get_dependencies(pyproject_dict: dict[str, Any]) -> list[Dependency]:
    groups = pyproject_dict.get("dependency-groups", {})
    if INSTALLER_DEPENDENCY_GROUP not in groups:
        raise ValueError(
            f"pyproject.toml is missing the {INSTALLER_DEPENDENCY_GROUP} dependency group"
        )
    return [Dependency(dep_str) for dep_str in groups[INSTALLER_DEPENDENCY_GROUP]]


def get_git_root() -> Path:
    return Path(__file__).parents[1].resolve()
