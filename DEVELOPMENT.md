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
| `src/deadline/comfyui_submitter/nodes.py` | The Deadline value nodes, which list values to run as separate tasks. The job's task script replaces them, so workers don't need this extension. |
| `src/deadline/comfyui_submitter/routes.py` | The `/deadline/...` routes on the ComfyUI server: `plan`, `cloud/<command>`, `jobs`, `jobs/download`, `bundle` and `choose`. |
| `src/deadline/comfyui_submitter/bridge.py` | Calls `deadline.client.api` for the routes. It runs in the Deadline Cloud CLI's Python (`python -I bridge.py <command>`) and speaks JSON lines, so it only imports the standard library and `deadline`. |
| `frontend/src/extensions/deadline-cloud/` | The frontend extension's TypeScript. `deadline_submit.ts` registers the commands, the action bar button and the sidebar tab; `deadline_ui.ts` is the panel and the dialog, in plain DOM with ComfyUI's theme variables. The folder mirrors where ComfyUI serves the extension, so imports such as `../../scripts/app.js` resolve to the type declarations in `frontend/src/scripts/`. |
| `src/deadline/comfyui_submitter/web/` | The compiled JavaScript that ComfyUI serves. It's built by `npm run build` in `frontend/`, which `install_submitter.py` runs, and isn't committed. |
| `src/deadline/comfyui_submitter/comfy_cli_job_template.yaml` | The job template. Each submission copies it into a new job bundle under `ComfyUI/user/deadline_submissions/`, with the workflow, the models and custom nodes it attaches and the list of input files. |
| `test/unit/` | Unit tests. |

## Commands

| Command | Description |
|---|---|
| `hatch run test` | Run the unit tests with coverage. |
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
   default farm and queue, and, under **Attachments**, the models, custom nodes and input files
   the workflow uses.
4. Submit an image-to-image workflow from the **Deadline** button's dialog, with its model on
   this machine and **Extra Model Paths Config** empty. Check the upload progress, the job ID
   notification and the job's status under **Recent jobs**. When the job finishes, click
   **Download** and check that the image is in ComfyUI's output directory.
5. Click **Save job bundle**, and submit the saved bundle with `deadline bundle submit`.
6. Change a host requirement, such as **GPU memory (GiB)**, submit, and check the job's host
   requirements in Deadline Cloud monitor. Then check that **Reset to defaults** goes back to the
   template's.
7. Submit a workflow that uses a custom node installed on this machine, to a worker that doesn't
   have it. Check that the job log lists it under `Attached custom nodes` and that the job
   succeeds.
6. Wire three prompts into a **Create List** that feeds a text-to-image workflow's prompt. Check
   that the job runs as one task with all three prompts. Then choose **Multiple tasks**
   under **Tasks**, and check that it shows 3 tasks, that the job runs 3, and that each output's
   name ends with its item, such as `_List5-2`. Set the field to `2,3` and check that it runs 2.
7. Add a **Deadline Integer Values** node with `1-3` to a text-to-image workflow's seed, and a
   **Deadline String Values** node with two prompts. Check that **Tasks** shows "2 × 3 = 6
   tasks" without a choice of One task or Multiple tasks, that the job runs 6 tasks, and that
   each output's name ends with its items, such as `_String5-2_Integer6-3`. Then give both a
   pair_with such as `look`, with two numbers, and check that the job runs 2 tasks. Add the
   Create List from step 6, and check that **One task** runs it whole in each of those tasks.
8. Set the String Values node's **split_by** to `--- lines`, and give it two prompts of two
   paragraphs each, with a line of `---` between them. Check that **Tasks** previews each prompt
   by its first line, and that each task's prompt keeps both of its paragraphs.

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
