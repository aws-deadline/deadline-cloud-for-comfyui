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
| `install_submitter.py` | The setup script for users. It's tested in `test/unit/test_install_submitter.py`. |
| `src/deadline/comfyui_submitter/submit.py` | Pure helpers for building the submitter command. They don't import ComfyUI, so they're unit tested. |
| `src/deadline/comfyui_submitter/routes.py` | The `/deadline/...` routes on the ComfyUI server: `plan`, `cloud/<command>`, `bundle`, `jobs`, `jobs/confirm`, `jobs/cancel` and `choose`. |
| `src/deadline/comfyui_submitter/bridge.py` | Calls `deadline.client.api` for the routes. It runs in the Deadline Cloud CLI's Python (`python -I bridge.py <command>`), or, in an installer's installation, in ComfyUI's Python with the extension's `dependencies` folder (`python -I -S bridge.py --dependencies <folder> <command>`). It speaks JSON lines, so it only imports the standard library and `deadline`. |
| `frontend/src/extensions/deadline-cloud/` | The frontend extension's TypeScript. `deadline_submit.ts` registers the commands, the action bar button and the sidebar tab; `deadline_ui.ts` is the panel and the quick submit popover, in plain DOM with ComfyUI's theme variables. The folder mirrors where ComfyUI serves the extension. Imports such as `../../scripts/app.js` load ComfyUI's runtime modules; their ambient types are declared in `frontend/src/comfy_app.d.ts` and `frontend/src/comfy_api.d.ts`. |
| `src/deadline/comfyui_submitter/web/` | The compiled JavaScript that ComfyUI serves. It's built by `npm run build` in `frontend/`, which `install_submitter.py` runs, and isn't committed. |
| `src/deadline/comfyui_submitter/comfy_cli_job_template.yaml` | The job template. Each submission copies it into a new job bundle under `ComfyUI/user/deadline_submissions/`, with the workflow, the models and custom nodes it attaches and the list of input files. |
| `installer/` | The InstallBuilder project for the Windows and macOS installer, with its images and icons. |
| `install_builder/` | The InstallBuilder component that installs the extension and its dependencies into the selected ComfyUI folder. |
| `scripts/` | The installer build: `build_installer_cli.py` and `build_installer.py` run it, `deps_bundle.py` builds the dependency bundle, and `find_installbuilder.py` finds InstallBuilder. |
| `pipeline/` | The build and test entry points that the shared installer build runs. |
| `test/unit/` | Unit tests. |
| `test/installer/` | Tests that run a built installer and its uninstaller. |
| `test/integ/` | Playwright tests that submit from the panel and the quick submit popover in a real ComfyUI, with the Deadline Cloud tools faked. See [Integration tests](#integration-tests). |

## Commands

| Command | Description |
|---|---|
| `hatch run test` | Run the unit tests with coverage. |
| `hatch run all:test` | Run the unit tests on Python 3.13 and 3.14. |
| `hatch run lint` | Run ruff, black and mypy. |
| `hatch run fmt` | Format with black, then run ruff. |
| `hatch build` | Build the sdist and the wheel into `dist/`. |
| `hatch version` | Print the version that hatch-vcs derives from git. |
| `hatch run integ:test` | Run the integration tests, which drive the panel in the ComfyUI that `COMFYUI_PATH` names. |
| `npm run build` | In `frontend/`: type-check the TypeScript and compile it into `src/deadline/comfyui_submitter/web/`. |
| `npm run check` | In `frontend/`: type-check only. |

The panel tests in `test/unit` compile the TypeScript into a temporary folder and run it in Node.
They're skipped until `npm ci` has run in `frontend/`.

## Building the installer

The installer is built like the Cinema 4D one, on the operating system it's for, macOS or Windows.
It needs [InstallBuilder](https://installbuilder.com/), Node.js 22 or later with npm, and Hatch:

```
hatch run installer:build-installer --local-dev --install-builder-path /path/to/InstallBuilder --output-dir dist
```

The build:

1. Runs `npm ci` and `npm run build` in `frontend/`, so the installer includes the compiled panel.
2. Builds `dependency_bundle/deadline_cloud_for_comfyui_submitter-deps.zip` from the `installer`
   dependency group in `pyproject.toml`. Packages with compiled modules are installed for Python
   3.13 and 3.14 and flattened into one folder, as in Cinema 4D.
3. Runs InstallBuilder on `installer/DeadlineCloudForComfyUISubmitter.xml`.

Without a license, InstallBuilder builds an evaluation installer, which `--local-dev` allows.
Release builds use `--dev` or `--install-builder-license-path`, with `--install-builder-s3-bucket`
to download InstallBuilder, like Cinema 4D's.

To test the installer, copy it into the repository root and run `hatch run test-installer`, or set
`COMFYUI_INSTALLER_PATH` to it. The tests install into temporary folders and uninstall again.

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
   ComfyUI, open the **Deadline Cloud** sidebar tab, and check that **Job submission settings**
   shows your login status and your default farm and queue, and
   **Advanced settings > Attachments** the models, custom nodes and input files the workflow uses.
   When you aren't logged in, check that **Submit to Deadline Cloud** next to **Run** opens the
   panel with **Job submission settings** open and **Log in**.
4. Click **Submit to Deadline Cloud** next to **Run**, and submit an image-to-image workflow from
   its popover, with its model on this machine. Check the upload progress in the popover, then the
   job's name and ID. Check the job in Deadline Cloud monitor. When it finishes, run
   `deadline job download-output --job-id <job ID>` and check that the image is in ComfyUI's
   output directory.
5. Set a host requirement under **Advanced settings > Host requirements**, such as
   **GPU memory (GiB)**, submit, and check the job's host requirements in Deadline Cloud monitor.
   Then check that **Reset all to defaults** goes back to the template's, which has none.
6. Submit a workflow that uses a custom node installed on this machine, to a worker that doesn't
   have it. Check that the job log lists it under `Attached custom nodes` and that the job
   succeeds. Then remove the pack with its **✕** under **Advanced settings > Attachments**, submit
   again, and check that the job fails on the missing node.
7. Wire three prompts into a **Create List** that feeds a text-to-image workflow's prompt. Check
   that the job runs as one task with all three prompts.

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

## Integration tests

The integration tests in `test/integ` submit `workflows/minimal_test_case_workflow_ui.json` from
the panel and the quick submit popover in a real ComfyUI with
[Playwright](https://playwright.dev/python/), and check the job bundle that Submit makes. The
Deadline Cloud CLI and bridge are faked for the ComfyUI server, so no credentials are needed and
nothing reaches AWS. The fake bridge logs the panel in to a fake farm and queue, runs the real
bridge's `queue` command on a fake Conda queue environment, and copies each submitted bundle
before the server deletes it.

- `test_quick_submit.py` sets the job name in the popover and submits. The popover's row must
  name the job ID that was submitted, and the bundle's job template is the repository's,
  unchanged.
- `test_panel_submit.py` sets the job name, description, priority, the maximum retries, failed
  tasks and workers, Output Directory, Workflow Timeout and the GPU memory host requirement in
  the panel, and submits from its footer. The bundle's job template is the repository's with that
  description and those host requirements. The settings the panel saved in the workflow's
  `extra.deadlineCloud` must hold these values, with no paths but Output Directory and no farm
  or queue IDs, and opening the workflow in a new browser must show them again.

`bundle_checks.py` checks both bundles the same way. It creates the job from the template and the
parameter values with openjd-model, as Deadline Cloud does, so a value of the wrong type, out of
range or missing fails. Conda Packages must be `comfy`, Conda Channels the fake queue's
`deadline-cloud` with `deadline-cloud-v2` before it, the workflow file must be the API-format
workflow inside the bundle, and `asset_references.yaml` must list nothing, since the workflow
loads no input files or models. What the `comfy` conda package needs of the job template on the
workers, such as `comfy launch --background` with a base directory that has `custom_nodes/`, is
checked by `test/unit/deadline_submitter_for_comfyui/test_worker_contract.py`, which
`hatch run test` runs.

The tests need Node.js 22 or later, to build the panel, and a ComfyUI checkout with its
requirements and PyTorch installed into a virtual environment in it. On macOS, PyPI's PyTorch
wheels are the MPS builds, without CUDA, which is fine since the tests start ComfyUI with
`--cpu`:

```
git clone --depth 1 --branch v0.46.0 https://github.com/Comfy-Org/ComfyUI /path/to/ComfyUI
python3.13 -m venv /path/to/ComfyUI/.venv
/path/to/ComfyUI/.venv/bin/python -m pip install torch torchvision torchaudio
/path/to/ComfyUI/.venv/bin/python -m pip install -r /path/to/ComfyUI/requirements.txt
COMFYUI_PATH=/path/to/ComfyUI hatch run integ:test
```

On Linux, add `--index-url https://download.pytorch.org/whl/cpu` to the PyTorch install.

Arguments after `hatch run integ:test` or `hatch run test` replace the default test path, so
name it when you pass any: `hatch run integ:test test/integ -k quick` or
`hatch run test test/unit -k contract`.

The panel is compiled first when needed (`npm ci` and `npm run build` in `frontend/`), and
ComfyUI is started with `--cpu`, this checkout linked in as its only custom node, a fresh base
directory and a free port. Without `COMFYUI_PATH` the tests skip, so `hatch run test` stays
hermetic; `COMFYUI_PYTHON` picks the interpreter that launches ComfyUI when it isn't in the
checkout's `.venv` or `venv`. The integ hatch environment downloads Playwright's Chromium when
it's created. The fakes are shell scripts, so the tests skip on Windows.
