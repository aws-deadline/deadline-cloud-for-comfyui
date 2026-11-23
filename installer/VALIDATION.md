<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
# Installer validation

The native suite runs the real installer and uninstaller against disposable folders
with `main.py` and `custom_nodes`. It checks packaged SDK imports, TLS, the Deadline
CLI, bridge configuration, permissions, upgrade and uninstall, preservation of customer files,
and rejection of invalid destinations.

Run the suite with `hatch run test-installer` on Apple silicon macOS or Windows x64.
Set `COMFYUI_INSTALLER_PATH` to select a specific artifact. Build instructions are in
[Development](../DEVELOPMENT.md#building-the-installers).

This revision was built and tested on Apple silicon macOS with InstallBuilder 26.8.0
evaluation, CPython 3.13.18, and Node.js 22.17.0. Windows native testing remains pending
for this revision. The installed bridge also returned a result for an empty job list
using isolated test configuration.

Production builds validate the InstallBuilder license and platform signing. Wizard
navigation and job submission from a running ComfyUI instance require manual testing
before customer release.
