<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
# Installer validation

The supported installer targets are Windows x64 and Apple silicon macOS 13 or later.
ComfyUI, model weights, workflows, and sample assets are not bundled.

The November 23, 2026 local checks used an Apple silicon Mac, InstallBuilder 26.8.0
evaluation, portable CPython 3.13.18, and Node.js 22.17.0. Python repository checks used
Python 3.13.5. The default artifacts are:

- `dist/installers/DeadlineCloudForComfyUISubmitter-osx-arm64-installer.app`
- `dist/installers/DeadlineCloudForComfyUISubmitter-windows-x64-installer.exe`

| Check | Result |
|---|---|
| `hatch run lint` | Passed: Ruff, Black, and mypy. |
| `hatch run test` | 732 passed, 3 Windows-only tests skipped on the local Mac. |
| `hatch build` | Source distribution and wheel built successfully. |
| Fresh native macOS installer build | Passed with a new payload and freshly compiled TypeScript. |
| `hatch run test-installer` on macOS | 15 passed; 3 production/platform checks skipped. |
| Windows executable build | Passed in the initial run; native installation tests reported errors before cancellation. Local Windows reproduction is required. |
| SDK wheel hashes and dependency closure | Passed: Python 3.13/3.14, macOS ARM (20 wheels each) and Windows x64 (22 wheels each). |
| Native tool downloads | Actual Python and InstallBuilder release bytes verified against pinned hashes for both platforms. |
| actionlint and the shared zizmor policy | Passed for the three shared installer workflows. |

The SDK is packaged in a separate dependency ZIP, following Cinema 4D's bundling layout.
The packed portable runtime contains only build tooling; its early validation bootstrap is
SDK-free. The archive records ordinary files with 0644/0755 modes and explicit parent
directories with 0755 modes. A disposable extracted runtime is tested for TLS, SDK/Qt imports,
relocation, and regenerated Deadline CLI launchers before the installer is built.

The native suite is run locally against an installer built on the corresponding operating
system. Windows native installation validation remains incomplete: the initial canceled run showed
installation errors. Reproduce the first failure locally with `hatch run test-installer -x
--tb=short` on Windows x64; Windows is not emulated on the local Mac. No additional native-installer CI workflow is configured.
The suite checks source and portable-parent selection, separate active custom nodes,
spaced paths, packaged dependencies, permissions, install/upgrade/uninstall/reinstall,
preserved settings and additions, and rejection of invalid or linked destinations.
Uninstall must leave no managed product files; an entirely empty private installation
folder is also accepted. Windows directory junctions are treated as links.

Run the native suite against the default local artifact:

```sh
hatch run test-installer
```

Or select a specific built artifact (PowerShell uses `$env:COMFYUI_INSTALLER_PATH`):

```sh
COMFYUI_INSTALLER_PATH="/absolute/path/installer.app" hatch run test-installer
```

`pipeline/test_installer.sh` and `pipeline/test_installer.ps1` run the same suite. Unit tests
cover transaction rollback, ownership, safe launcher parsing, selected-folder resolution,
platform build contracts, dependency-lock enforcement, and safe SDK/tool extraction.
See [development instructions](../DEVELOPMENT.md#building-the-installers) for build commands,
platform requirements, and release gates.

These tests use disposable synthetic hosts. They do not install into a real ComfyUI,
start ComfyUI, submit jobs, or use customer models. Operating-system dialogs and wizard
Back-button navigation still require manual validation. The finish page provides restart
instructions and opens no browser or Deadline configuration window.

Evaluation artifacts are unsigned development builds. Customer distribution requires licensed
InstallBuilder, Windows Authenticode signing, macOS signing/notarization, testing on supported
systems, and a real panel/job submission against a configured farm and queue. Production
CodeBuild runs enforce release checks when `CODEBUILD_SRC_DIR` is present and `IS_DEV_BUILD`
is not `true`. The shared AWS build/signing projects and protected environments must be
configured before release workflows can publish installers.

The shared installer-XML check is an advisory requiring coordination of XML changes with
release infrastructure. It intentionally flags changed installer definitions; it is separate
from functional native-test results.
