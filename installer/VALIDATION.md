<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
# Installer validation

The native suite runs the real installer and uninstaller against disposable folders
with `main.py`, `custom_nodes`, and a Python `.venv`. It checks SDK imports and TLS
through the existing Python, bridge startup, permissions, upgrade and uninstall,
preservation of customer files, and rejection of invalid destinations.

Run the suite with `hatch run test-installer` on Apple silicon macOS or Windows x64.
Set `COMFYUI_INSTALLER_PATH` to select a specific artifact. Build instructions are in
[Development](../DEVELOPMENT.md#building-the-installers).

This revision was built on Apple silicon macOS with InstallBuilder 26.8.0 evaluation
and Node.js 22.17.0. The native suite uses CPython 3.13. A separate upgrade from
the previous preview used an external CPython 3.14. It confirmed SDK import,
removal of the old runtime, and preservation of the customer's settings through
uninstall. Windows native testing remains pending.

Production builds validate the InstallBuilder license and platform signing. Wizard
navigation and job submission from a running ComfyUI instance require manual testing
before customer release.
