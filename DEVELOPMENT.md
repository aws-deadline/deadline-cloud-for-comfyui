# Development

This project uses [Hatch](https://hatch.pypa.io/), set up like the `aws-deadline/deadline-cloud-for-*`
repositories. The version comes from git tags through `hatch-vcs`.

```
pip install -r requirements-development.txt
```

## Layout

| Path | Contents |
|---|---|
| `__init__.py` | The ComfyUI custom-node entry point. ComfyUI imports it from `custom_nodes/`. |
| `install_submitter.py` | The setup script for users. It's tested in `test/unit/test_install_submitter.py`. |
| `src/deadline/comfyui_submitter/submit.py` | Pure helpers for building the submitter command. They don't import ComfyUI, so they're unit tested. |
| `src/deadline/comfyui_submitter/routes.py` | The `/deadline/...` routes on the ComfyUI server: `plan`, `cloud/<command>`, `jobs` and `bundle`. |
| `src/deadline/comfyui_submitter/bridge.py` | Calls `deadline.client.api` for the routes. It runs in the Deadline Cloud CLI's Python (`python -I bridge.py <command>`) and speaks JSON lines, so it only imports the standard library and `deadline`. |
| `src/deadline/comfyui_submitter/nodes.py` | The Deadline String, Integer and Image Values nodes. The job's task script replaces them, so workers don't need them. |
| `src/deadline/comfyui_submitter/web/` | The frontend extension. `deadline_submit.js` registers the commands, the action bar button and the sidebar tab; `deadline_ui.js` is the panel and the dialog, in plain DOM with ComfyUI's theme variables. |
| `src/deadline/comfyui_submitter/comfy_cli_job_template.yaml` | The job template. Each submission copies it into a new job bundle under `ComfyUI/user/deadline_submissions/`, with a task parameter and a job parameter added for each source of work items when the template's task script has the `ITEMS = {}` line. |
| `test/unit/` | Unit tests. |

## Commands

| Command | Description |
|---|---|
| `hatch run test` | Run the unit tests with coverage. |
| `hatch run all:test` | Run the unit tests on Python 3.10–3.13. |
| `hatch run lint` | Run ruff, black and mypy. |
| `hatch run fmt` | Format with black, then run ruff. |
| `hatch build` | Build the sdist and the wheel into `dist/`. |
| `hatch version` | Print the version that hatch-vcs derives from git. |

## Testing in ComfyUI

The route tests in `test/unit` stub ComfyUI's `server` and `folder_paths` modules, and use a fake
`deadline` CLI and a fake CLI Python (`COMFYUI_DEADLINE_PYTHON`) in place of the bridge. The bridge
tests use a stand-in for `deadline.client.api`. The frontend extension and the real library need
a running ComfyUI, so test them by hand:

1. Install ComfyUI 0.38.0 or later into a virtual environment.
2. Link this checkout into it and set up a private CLI:
   `python install_submitter.py --comfyui /path/to/ComfyUI --link --conda-channels "..."`.
   Or link it by hand, install the Deadline Cloud CLI into a **separate** environment, and
   start ComfyUI with `COMFYUI_DEADLINE_CLI` pointing at it:

   ```
   COMFYUI_DEADLINE_CLI=~/.venvs/deadline/bin/deadline python main.py --cpu
   ```
3. Check the server log for errors that start with `Deadline Cloud Submitter:`. Then open
   ComfyUI, open the **Deadline** sidebar tab, and check that it shows your login status, your
   default farm and queue, and the workflow's work items and task count. Change a Deadline
   Integer Values node and check that the task count follows.
4. Submit a workflow with a Deadline Integer Values node set to `1-2` from the **Deadline** button's
   dialog. Check the upload progress, the job ID notification and the job's status under
   **Recent jobs**. Then run `deadline job download-output` and check that it returns two images.

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
