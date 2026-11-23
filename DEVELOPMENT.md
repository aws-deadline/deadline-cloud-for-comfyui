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

Build on the target operating system with Node.js 22 or later, npm, Hatch, InstallBuilder,
and a trusted **portable CPython 3.13 or 3.14 distribution**: Apple silicon on macOS, or
x64 on Windows. A virtual environment or framework-dependent system Python is not a
portable runtime. Retain the distribution's license files. Windows embeddable distributions
must be configured to support normal isolated imports and pip; an untouched `._pth`
distribution is unsuitable.

Like Cinema 4D, the build creates a separate dependency archive:
`dependency_bundle/deadline_cloud_for_comfyui_submitter-deps.zip`. Its contents are the SDK
site packages resolved for the selected runtime and platform. The packed interpreter and
early validation bootstrap remain SDK-free. InstallBuilder unpacks the archive into the
installed runtime's site-packages; the helper regenerates the Deadline CLI at its final
location. The builder tests an extracted copy for relocation, TLS, SDK imports, and launchers,
and records dependency versions and license notices. It compiles TypeScript directly into
the staged extension, without adding generated JavaScript to the checkout.

Keeping a runnable Python is necessary for the existing `python -I bridge.py` integration.
Cinema 4D imports its SDK inside the application's embedded Python. ComfyUI deliberately
calls a separate Deadline environment so its Python packages cannot conflict with AWS or
Qt. Dependency ZIP bundling follows Cinema 4D; the additional interpreter preserves this
existing isolation.

For macOS:

```sh
hatch run installer:build-installer \
  --platform MacOS \
  --python-runtime /path/to/portable-python \
  --install-builder-path /path/to/installbuilder \
  --override-installer-version 0.1.0 \
  --local-dev
```

For Windows PowerShell:

```powershell
hatch run installer:build-installer `
  --platform Windows `
  --python-runtime 'C:\Build Tools\Portable Python' `
  --install-builder-path 'C:\Build Tools\InstallBuilder' `
  --override-installer-version 0.1.0 `
  --local-dev
```

`--install-builder-path` accepts an installation directory, as in Cinema 4D, or the builder
executable for compatibility. `--local-dev` permits watermarked evaluation builds locally;
`--dev` is the corresponding build-automation flag. Production builds require a licensed
builder. Artifacts are emitted under `dist/installers/`:

- `DeadlineCloudForComfyUISubmitter-osx-arm64-installer.app`
- `DeadlineCloudForComfyUISubmitter-windows-x64-installer.exe`

Set `COMFYUI_INSTALLER_PYTHON_RUNTIME` instead of `--python-runtime` when a build runner
provisions its runtime. Build automation can use `--install-builder-s3-bucket`, optional
`--install-builder-s3-key`, and `--install-builder-license-path` following Cinema 4D's
private tool provisioning contract. Runtime acquisition belongs to the trusted build
runner; product builds do not fetch arbitrary interpreters or ComfyUI.

Payload staging must use a new directory; the default is `build/installer-payload`.
Use `--payload-dir PATH` for separate builds, `--stage-only` to validate the offline payload
without InstallBuilder, and `--reuse-payload` to build again from verified staging.
An optional `--wheelhouse PATH` resolves dependencies from local wheels with `--no-index`;
otherwise the build machine needs package-index access. Customers never download packages
as part of installation.

### Local Windows validation

On a Windows x64 build/test machine, install the developer prerequisites above and supply
trusted portable CPython plus InstallBuilder. From this checkout, run:

```powershell
$payload = Join-Path $env:TEMP ('comfyui-installer-payload-' + [guid]::NewGuid().ToString('N'))
hatch run installer:build-installer `
  --platform Windows `
  --python-runtime 'C:\Build Tools\Portable Python' `
  --install-builder-path 'C:\Build Tools\InstallBuilder' `
  --payload-dir $payload `
  --local-dev
if ($LASTEXITCODE -ne 0) { throw 'Installer build failed.' }
$env:COMFYUI_INSTALLER_PATH = (Resolve-Path 'dist\installers\DeadlineCloudForComfyUISubmitter-windows-x64-installer.exe').Path
hatch run test-installer -x --tb=short
if ($LASTEXITCODE -ne 0) { throw 'Native installer tests failed.' }
```

The native tests install into disposable synthetic host folders and exercise upgrade and
uninstall. After they pass, quit a real ComfyUI instance and open the built `.exe` to check
the wizard, restart ComfyUI, and verify the panel. These are build/test prerequisites;
customers running a completed installer need none of these developer tools.

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

The wizard always shows the standard folder picker, with `~/ComfyUI` as the suggested
location, and an optional base folder for separate active custom nodes. Validation runs
from a temporary, standard-library-only copy of the supplied portable Python, so selecting
a folder requires no system Python. It reads only the selected folder's `main.py`, literal
`ComfyUI/main.py` child for portable layouts,
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
its wrapper project, which launches the built standalone installer unattended. The shared build/signing workflow
is provided; AWS-managed build-project onboarding remains a release prerequisite.
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

The suite selects the corresponding `.app` or `windows-x64` `.exe` under `dist/installers/`
by default. To test an artifact elsewhere:

```
COMFYUI_INSTALLER_PATH="/absolute/path/installer.app" hatch run test-installer
```

`pipeline/test_installer.sh` and `pipeline/test_installer.ps1` run the same suite for build automation. These tests launch
the real installer and uninstaller with explicit temporary source, custom-node, and runtime
folders; they do not use an existing ComfyUI installation or run ComfyUI. They check CLI
options and defaults, packaged dependencies and permissions, install/upgrade/uninstall,
split layouts, preserved customer settings, and rejected paths. The ordinary `hatch run test`
continues to run only unit tests, and does not require a built installer. Native tests skip
outside Apple silicon macOS and Windows x64; a missing artifact on a supported platform fails
with build instructions. Windows uses unattended execution because its GUI installer has no
text-mode console help. Licensed-build, macOS signing/notarization, and Windows Authenticode
checks run for production
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

Native installer tests run locally on the corresponding operating system; no additional
native-installer GitHub Actions workflow is configured. The existing Python and frontend
CI continues to run for pull requests.
`.github/workflows/installer_xml_check.yml` uses the organization's shared XML change advisory.
`.github/workflows/build_installers.yml` invokes the same shared AWS installer build workflow
as Cinema 4D for MacOS and Windows, with repository-scoped OIDC and protected environments.
`.github/workflows/release_installers.yml` validates an existing version tag and calls the
shared release-installer workflow for both platforms under the protected `release` environment.
It provides the installer release stage, without adding unrelated PyPI or Conda publication.

Before producing a customer release, provision the AWS-managed
`deadline-cloud-for-comfyui-MacOSInstaller` and
`deadline-cloud-for-comfyui-WindowsInstaller` build/signing projects and their protected
GitHub environments/role, the shared `ReleaseInstallerProject`, licensed InstallBuilder,
and trusted portable runtime. Runners must
set `COMFYUI_INSTALLER_PYTHON_RUNTIME` and install Node.js 22+. Release builds require Windows
Authenticode signing and macOS signing/notarization, native validation on supported systems,
and a real panel/job submission against a configured farm and queue. The release gates run
when `CODEBUILD_SRC_DIR` is set and `IS_DEV_BUILD` is not `true`.

The shared workflow invocation and compatible build/test entrypoints are included here;
this PR does not provision cloud infrastructure or publish installers. The payload contains
only this integration, its compiled panel, and Deadline runtime dependencies. ComfyUI and
model assets are supplied separately by the customer.
