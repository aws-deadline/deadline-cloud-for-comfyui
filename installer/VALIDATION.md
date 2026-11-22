<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
# Local macOS installer validation

Validated on November 22, 2026, after rebasing onto fork and upstream `main` at
`1fe90a296746ddd6f4e661c9e0d861eba3945a1f`. The host was an Apple silicon Mac running
macOS 26.7.3, with InstallBuilder 26.8.0 evaluation, portable CPython 3.13.18,
and Node.js 22.17.0. Python repository checks used Python 3.13.5.

The local artifact is
`dist/installers/DeadlineCloudForComfyUISubmitter-osx-arm64-installer.app`.
It contains Deadline 0.60.11, boto3/botocore 1.44.4, and PySide6-Essentials 6.10.3.
Package inventory and dependency license notices accompany the extension; portable
Python's license files remain in the runtime.

| Check | Result |
|---|---|
| `hatch run lint` | Passed: ruff, black, and mypy. |
| `hatch run test` | 681 passed, 1 Windows-only test skipped. |
| `hatch build` | Source distribution and wheel built successfully. |
| Fresh native installer build | Passed with a newly staged payload and compiled panel. |
| `hatch run test-installer` | 14 passed, 2 production release checks skipped. |
| Hash-verified macOS ARM wheel downloads and dependency closure | Passed for Python 3.13 and 3.14; all 20 dependencies pinned. |
| actionlint and the shared zizmor policy | Passed. |
| XML/JSON parsing, pipeline shell syntax, `git diff --check` | Passed. |

The download regression now passes, including large concurrent stderr output. The reader
drains stdout after terminating the CLI, allowing process cleanup and the failure event
to complete. The unit suite also checks bytecode cleanup across CPython tags and optimization
levels, safe retry after partial cache removal, preserved customer caches and symlinks, and
offline rejection of modified wheels or dependencies missing from the hash lock.

The fresh payload copied the current integration and installation hooks and compiled the
current TypeScript directly into staging. Copied source files matched the checkout byte
for byte. The exact tested app was copied to `dist/installers/` and compared in file
hashes, permissions, and symbolic links. Generated JavaScript and installer binaries
are not committed.

The native suite uses disposable synthetic host folders and isolated configuration. It
checks suggested defaults, component and selected-folder CLI options, offline dependencies,
relocated private Python and Deadline CLI, TLS and bridge imports, ownership and permissions,
clean uninstall after compiling the packaged modules, and split-layout
upgrade/uninstall/reinstall with preserved customer settings, modules, and bytecode. Negative cases reject missing sources, occupied prefixes, source/node overlap,
symbolic links, and unsupported shell-special paths before installation. Test runtimes and
ownership markers were removed; expected customer files and settings remained preserved.

Run the native suite against the default local artifact:

```
hatch run test-installer
```

Or select a specific built artifact:

```
COMFYUI_INSTALLER_PATH="/absolute/path/installer.app" hatch run test-installer
```

`pipeline/test_installer.sh` runs the same suite for build automation. Unit tests cover
rollback, ownership, path validation, safe launcher parsing, selected-folder resolution,
and the reusable component interface. See [development instructions](../DEVELOPMENT.md)
for fresh-payload build commands and release-check gates.

This validation did not run a real ComfyUI or Desktop installation or submit a job.
Operating-system dialogs and GUI Back-button navigation still require manual validation.
The completion page provides restart instructions; it opens no browser or Deadline Cloud
configuration window.

The artifact is an unsigned, watermarked evaluation build for local development.
Customer distribution requires a licensed build, signing/notarization, testing across
supported Apple silicon Macs and macOS versions, and a real panel/job submission against
a configured farm and queue. Production CodeBuild builds require the two release checks
that were skipped locally.

The supported target is Apple silicon on macOS 13 or later. Intel, Windows, and Linux
installers are deferred. ComfyUI, model weights, and sample assets are not bundled.
