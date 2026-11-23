# Development

This project uses [Hatch](https://hatch.pypa.io/), set up like the `aws-deadline/deadline-cloud-for-*`
repositories. The version comes from git tags through `hatch-vcs`.

```
pip install -r requirements-development.txt
```

The panel is written in TypeScript, in `frontend/`. Changing it needs Node.js 22 or later:

```
cd frontend
npm ci
```

## Layout

| Path | Contents |
|---|---|
| `__init__.py` | The ComfyUI custom-node entry point. ComfyUI imports it from `custom_nodes/`. |
| `install_submitter.py` | The setup script for source installations. It's tested in `test/unit/test_install_submitter.py`. |
| `src/deadline/comfyui_submitter/submit.py` | Pure helpers for building the submitter command. They don't import ComfyUI, so they're unit tested. |
| `src/deadline/comfyui_submitter/routes.py` | The `/deadline/...` routes on the ComfyUI server: `plan`, `cloud/<command>`, `jobs`, `jobs/download`, `bundle` and `choose`. |
| `src/deadline/comfyui_submitter/bridge.py` | Calls `deadline.client.api` for the routes. It runs in the Deadline Cloud CLI's Python (`python -I bridge.py <command>`) and speaks JSON lines, so it only imports the standard library and `deadline`. |
| `frontend/src/extensions/deadline-cloud/` | The frontend extension's TypeScript. `deadline_submit.ts` registers the commands, the action bar button and the sidebar tab; `deadline_ui.ts` is the panel and the dialog, in plain DOM with ComfyUI's theme variables. The folder mirrors where ComfyUI serves the extension. Imports such as `../../scripts/app.js` load ComfyUI's runtime modules; their ambient types are declared in `frontend/src/comfy_app.d.ts` and `frontend/src/comfy_api.d.ts`. |
| `src/deadline/comfyui_submitter/web/` | The compiled JavaScript that ComfyUI serves. It's built by `npm run build` in `frontend/`, which `install_submitter.py` runs, and isn't committed. |
| `src/deadline/comfyui_submitter/comfy_cli_job_template.yaml` | The job template. Each submission copies it into a new job bundle under `ComfyUI/user/deadline_submissions/`, with the workflow, the models it attaches and the list of input files. |
| `installer/` | Standalone InstallBuilder project, offline installation hooks, and installer text. |
| `install_builder/` | Reusable InstallBuilder component definition. |
| `scripts/build_installer.py` | Allowlisted payload staging and native installer build. |
| `scripts/build_installer_cli.py` | Build CLI following the other Deadline integrations' options. |
| `scripts/deps_bundle.py` | Hash-locked SDK dependency ZIP for the selected Python/platform. |
| `pipeline/build_installer.sh`, `pipeline/build_installer.ps1` | macOS and Windows build entrypoints for release automation. |
| `pipeline/test_installer.sh`, `pipeline/test_installer.ps1` | Native installer test entrypoints for release automation. |
| `test/installer/` | Tests that execute built macOS and Windows installers in disposable host folders. |
| `test/unit/` | Unit tests, including installer ownership, rollback, relocation, and safe removal. |

## Commands

| Command | Description |
|---|---|
| `hatch run test` | Run the unit tests with coverage. |
| `hatch run test-installer` | Test the built Apple silicon installer without coverage. |
| `hatch run all:test` | Run the unit tests on Python 3.13 and 3.14. |
| `hatch run lint` | Run ruff, black and mypy. |
| `hatch run fmt` | Format with black, then run ruff. |
| `hatch build` | Build the sdist and the wheel into `dist/`. |
| `hatch version` | Print the version that hatch-vcs derives from git. |
| `npm run build` | In `frontend/`: type-check the TypeScript and compile it into `src/deadline/comfyui_submitter/web/`. |
| `npm run check` | In `frontend/`: type-check only. |

The panel tests in `test/unit` compile the TypeScript into a temporary folder and run it in Node.
They're skipped until `npm ci` has run in `frontend/`.

## Testing in ComfyUI

The route tests in `test/unit` stub ComfyUI's `server` and `folder_paths` modules, and use a fake
`deadline` CLI and a fake CLI Python (`COMFYUI_DEADLINE_PYTHON`) in place of the bridge. The bridge
tests use a stand-in for `deadline.client.api`. The frontend extension and the real library need
a running ComfyUI, so test them by hand:

1. Install ComfyUI 0.38.0 or later into a virtual environment.
2. Link this checkout into it and set up a private CLI:
   `python install_submitter.py --comfyui /path/to/ComfyUI --link --conda-channels "..."`.
   Or run `npm ci` and `npm run build` in `frontend/`, link the checkout by hand, install the
   Deadline Cloud CLI into a **separate** environment, and start ComfyUI with
   `COMFYUI_DEADLINE_CLI` pointing at it:

   ```
   COMFYUI_DEADLINE_CLI=~/.venvs/deadline/bin/deadline python main.py --cpu
   ```
3. Check the server log for errors that start with `Deadline Cloud Submitter:`. Then open
   ComfyUI, open the **Deadline** sidebar tab, and check that it shows your login status, your
   default farm and queue, and, under **Attachments**, the models and input files the workflow
   loads.
4. Submit an image-to-image workflow from the **Deadline** button's dialog, with its model on
   this machine and **Extra Model Paths Config** empty. Check the upload progress, the job ID
   notification and the job's status under **Recent jobs**. When the job finishes, click
   **Download** and check that the image is in ComfyUI's output directory.
5. Click **Save job bundle**, and submit the saved bundle with `deadline bundle submit`.
6. Change a host requirement, such as **GPU memory (GiB)**, submit, and check the job's host
   requirements in Deadline Cloud monitor. Then check that **Reset to defaults** goes back to the
   template's.

To exercise the routes without the frontend:

```
curl -s -X POST -H 'Content-Type: application/json' http://127.0.0.1:8188/deadline/cloud/status --data '{}'
curl -s -X POST -H 'Content-Type: application/json' http://127.0.0.1:8188/deadline/plan \
  --data '{"name": "curl test", "workflow": {"1": {"class_type": "SaveImage", "inputs": {}}}}'
```

To run a bridge command by hand, with the CLI's Python:

```
echo '{}' | /path/to/deadline-cli-python -I src/deadline/comfyui_submitter/bridge.py status
```


## Building the installers

Build on the target operating system with Node.js 22 or later, npm, Hatch,
InstallBuilder, and CPython 3.13 for the Hatch build environment. Use Apple silicon
for macOS and x64 for Windows.

The build compiles the panel into the installer payload and bundles the locked SDK
wheels in `dependency_bundle/deadline_cloud_for_comfyui_submitter-deps.zip`, like
Cinema 4D. The installer uses ComfyUI's Python to validate the selected folder and
installation prefix. InstallBuilder then extracts the SDK, and the helper checks
the corresponding native wheels before replacing the extension. The panel loads the
SDK in a subprocess of ComfyUI's Python and calls the installed CLI for downloads.

For macOS:

```sh
hatch run installer:build-installer \
  --platform MacOS \
  --install-builder-path /path/to/installbuilder \
  --local-dev
```

For Windows PowerShell:

```powershell
$payload = Join-Path $env:TEMP ('comfyui-installer-payload-' + [guid]::NewGuid().ToString('N'))
hatch run installer:build-installer `
  --platform Windows `
  --install-builder-path 'C:\Build Tools\InstallBuilder' `
  --payload-dir $payload `
  --local-dev
```

Artifacts go to `dist/installers/`. Build automation accepts the same InstallBuilder location,
private S3 archive, license, development, and version-override options as Cinema 4D.
Local builds can use a fresh `--payload-dir`, validate it with `--stage-only`, rebuild
with `--reuse-payload`, or resolve SDK wheels from a local `--wheelhouse`.

### Testing an installer

Run `hatch run test-installer` on the target operating system. The suite picks the
installer under `dist/installers/`. Set `COMFYUI_INSTALLER_PATH` to use another artifact.
It runs the native installer and uninstaller against disposable folders with a Python
`.venv`, `main.py` and `custom_nodes`. It checks packaged dependencies, permissions, upgrade,
uninstall, customer file preservation, and invalid destinations.

For unattended installation, pass `--comfyui-directory` with the absolute path to
the ComfyUI folder and `--prefix` with the separate Deadline installation directory.
Use `--comfyui-python` with the absolute path to the Python executable if your ComfyUI
environment lives outside its `.venv`.
Use the same prefix and ComfyUI folder for upgrades. `hatch run test` runs unit tests
without an installer.

### Updating the installer dependency lock

`requirements-installer.in` contains the direct SDK/runtime versions. The generated
`requirements-installer.txt` pins transitive dependencies and artifact SHA256 hashes, with
platform markers for Windows-only dependencies. The builder uses `pip --target` and requires
hashes for both package-index and local-wheelhouse builds. Source distributions are excluded.
The universal lock is generated with the pinned resolver below; update direct versions in
the input file first. The constraints preserve existing versions while adding platform
coverage; remove or adjust them when deliberately upgrading dependencies.

```sh
python -m pip install uv==0.9.26
uv pip compile --universal --python-version 3.13 --generate-hashes --no-header \
  --default-index https://pypi.org/simple --constraints requirements-installer.txt \
  --output-file /tmp/comfyui-installer-requirements.txt requirements-installer.in
python -c 'from pathlib import Path; p=Path("requirements-installer.txt"); p.write_text("# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.\n" + Path("/tmp/comfyui-installer-requirements.txt").read_text())'
```

Review every changed version and hash. Check wheel availability and dependency closure for
Python 3.13 and 3.14 on both `macosx_13_0_arm64` and `win_amd64`, with separate destinations:
`pip download --require-hashes --only-binary=:all: --implementation cp --platform PLATFORM
--python-version VERSION -r requirements-installer.txt --dest PATH`.
The offline dependency tests check platform markers and wheel selection. Rebuild the
installer and run its native tests before accepting a lock update.

### Shared build and release

The repository calls the shared installer build, release, and XML-change workflows.
Customer releases require the managed macOS and Windows build/signing projects, the
shared release project, protected GitHub environments and role, and licensed InstallBuilder.
Verify Windows Authenticode, macOS signing and notarization,
and a real panel submission for the release. See [Installer validation](installer/VALIDATION.md).
