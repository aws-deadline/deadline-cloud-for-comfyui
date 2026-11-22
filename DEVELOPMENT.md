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
| `pipeline/build_installer.sh` | macOS build entrypoint for release automation. |
| `pipeline/test_installer.sh` | Native installer test entrypoint for release automation. |
| `test/installer/` | Tests that execute a built macOS installer in disposable host folders. |
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


## Building the macOS installer

Build on macOS with Node.js 22 or later, npm, Hatch, InstallBuilder, and a **portable CPython
3.13 or 3.14 distribution** for Apple silicon (arm64). A normal virtual environment or
system Python is not a portable runtime. Supply a trusted distribution and retain its
license files. Build/installation paths support spaces; double quotes, dollar signs, backticks,
backslashes, and line breaks are rejected before native command interpolation. The build copies it, checks relocation, installs the pinned Deadline Cloud
GUI/console dependencies from `requirements-installer.txt`, and records package versions and
license notices in the payload. It compiles TypeScript directly into the staged extension,
without adding generated JavaScript to the checkout.

The supported installer target is macOS on Apple silicon. Its Qt runtime requires macOS
13 or later. Native validation uses CPython 3.13 and the InstallBuilder macOS builder;
testing across supported macOS versions remains a release prerequisite. Intel, Windows,
and Linux installers are deferred. See [installer validation](installer/VALIDATION.md)
for commands and the local results.

```
hatch run installer:build-installer \
  --python-runtime /path/to/portable-python \
  --install-builder-path /path/to/installbuilder/bin/builder \
  --version 0.1.0 \
  --local-dev
```

`--local-dev` permits InstallBuilder's watermarked evaluation artifacts for local testing.
A release build requires a licensed builder. The command emits
`dist/installers/DeadlineCloudForComfyUISubmitter-osx-arm64-installer.app`. Payload staging must use a new directory; the default is
`build/installer-payload`. Use `--payload-dir PATH` for separate builds. Use `--stage-only` to
validate the complete offline payload without InstallBuilder, and `--reuse-payload` to build
again from an already staged payload. An optional `--wheelhouse PATH` makes dependency
installation use local wheels with `--no-index`; otherwise the build machine needs package
index access. Customers never download packages as part of installation.

The wizard always shows the standard folder picker, with `~/ComfyUI` as the suggested
location, and an optional base folder for separate active custom nodes. Validation runs
from a temporary, standard-library-only copy of the supplied portable Python, so selecting
a folder requires no system Python. It reads only the selected folder's `main.py`,
`comfyui-source.txt`, and a supported literal Python `launch.sh` command plus the referenced
source/base folders. Launcher scripts are parsed as metadata. Dynamic launchers require
explicit source and base selection. The temporary validator is removed after resolution
and when the wizard exits.

Inspect a selected layout without modifying ComfyUI:

```
/path/to/portable-python/bin/python3 -I installer/install.py resolve \
  --comfyui "/absolute/path/ComfyUI"
```

The JSON response identifies the exact source and active `custom_nodes` folder. Append
`--base-directory "/absolute/path/Active Base"` when the selected source uses a separate
base directory.

The standalone project follows the Maya, Nuke, Houdini, and Cinema 4D InstallBuilder
contract: it allows component selection and preselects the submitter; the public component
starts unselected and owns its host parameters and validation hooks. `individual_install=1`
marks the standalone wizard. Its public CLI options are `--comfyui-directory` and the
optional `--comfyui-base-directory`; `--comfyui-custom-nodes-directory` is available for
unattended wrappers that already know the exact active folder. The per-user `--prefix`
contains the private runtime. A combined installer's application selection page belongs to
its wrapper project, which launches the built standalone installer unattended. Shared build
and signing onboarding remains a separate release task.
The private runtime includes a runnable interpreter because the ComfyUI routes launch
`bridge.py` with it. A standalone frozen Deadline CLI does not replace this runtime. The
installer regenerates the CLI launcher at its final path and validates imports, TLS, Python
and dependency versions before replacing the extension. `deadline_python` and `deadline_cli`
in the extension config select this runtime even when ComfyUI uses another Python.

Run the built-installer tests with the same separate command used by the other Deadline
Cloud integrations:

```
hatch run test-installer
```

The suite reads `dist/installers/DeadlineCloudForComfyUISubmitter-osx-arm64-installer.app`
by default. To test an artifact elsewhere:

```
COMFYUI_INSTALLER_PATH="/absolute/path/installer.app" hatch run test-installer
```

`pipeline/test_installer.sh` runs the same suite for build automation. These tests launch
the real installer and uninstaller with explicit temporary source, custom-node, and runtime
folders; they do not use an existing ComfyUI installation or run ComfyUI. They check CLI
options and defaults, packaged dependencies and permissions, install/upgrade/uninstall,
split layouts, preserved customer settings, and rejected paths. The ordinary `hatch run test`
continues to run only unit tests, and does not require a built installer. Native tests skip
on platforms other than Apple silicon macOS; on a supported Mac, a missing artifact fails
with build instructions. Licensed-build and macOS signing checks run for production
CodeBuild builds (`CODEBUILD_SRC_DIR` set and `IS_DEV_BUILD` other than `true`), matching the
peer installer suites. Local evaluation builds skip those release checks.

For an unattended smoke test, create a disposable host folder containing `main.py` and
`custom_nodes`, then run the native executable inside the `.app` bundle:

```
/path/to/installer.app/Contents/MacOS/installbuilder.sh \
  --mode unattended \
  --prefix "/absolute/path/Deadline Runtime" \
  --comfyui-directory "/absolute/path/ComfyUI"
```

For a split installation, append `--comfyui-base-directory "/absolute/path/Active Base"`.
A launcher folder containing `comfyui-source.txt` and a supported literal Python `launch.sh`
command can be supplied directly with `--comfyui-directory`.

Use absolute real folder paths: the installer rejects a runtime overlapping either source
or active custom-node folders,
nonempty unrelated runtime prefixes, symbolic-link aliases, and unwritable or incomplete
ComfyUI folders before extraction. Quit ComfyUI before upgrading. An installer prefix manages
one ComfyUI installation; uninstall before selecting another, or give that installation its
own prefix. Handled installation errors preserve the previous extension. After a power loss
or forced termination during extraction, a partial runtime prefix may need to be removed
before retrying; do not delete the ComfyUI folder.

The installation hook stages a complete extension and replaces it only after validation.
It preserves existing configuration and extra files. State records the exact source and
active custom-node folder, so uninstall and reinstall also support split layouts. An
installation ID and file hashes
track ownership. Uninstall removes only unchanged packaged files when the installed marker
still agrees with its own state. It removes untouched generated configuration; if settings
were edited, it preserves them and clears references to the removed private runtime. When
customer files remain, it leaves a validated uninstall receipt so reinstall can safely
recognize that folder and restore the integration. It never removes ComfyUI itself or
unrelated custom nodes. Credentials in Deadline Cloud's user configuration are outside this
installer's ownership.

Native tests must cover paths with spaces, the completion-page restart instructions, the real
runtime and bridge, repeated installation, rejected paths, edited settings, and uninstall. Unit tests
cover rollback and ownership edge cases, but do not validate operating-system dialogs or a
Deadline Cloud submission. A real submission still requires your own configured farm, queue,
worker environment, and ComfyUI installation.

Before distributing a customer artifact, use a licensed InstallBuilder build, apply macOS
signing and notarization, and validate on supported Macs. Onboarding to the shared
AWS-managed installer build/signing project and its public reusable release workflows is a
separate release task. This change provides the local project/component and build entrypoints;
it does not deploy build infrastructure or publish an installer. The installer payload
contains only this integration, its compiled panel, and the Deadline runtime dependencies;
ComfyUI and model assets are supplied separately by the customer.
