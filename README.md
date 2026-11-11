# AWS Deadline Cloud for ComfyUI

> **Experimental — under development**
>
> This project is in early development and is not ready for production use.
> Features, interfaces, and setup instructions may change.

A ComfyUI extension that submits the open workflow to [AWS Deadline Cloud](https://aws.amazon.com/deadline-cloud/),
from a **Deadline** panel and dialog inside ComfyUI. The job renders the workflow on Deadline Cloud
GPU workers, one task per combination of the workflow's work items, such as seeds, prompts and
images.

## What it adds

- A **Deadline** tab in ComfyUI's sidebar, with:
  - your login status, with **Log in** for Deadline Cloud monitor profiles
  - the farm, queue and storage profile, saved as your Deadline Cloud defaults
  - the [work items](#work-items), with the number of tasks as you change them or the workflow
  - job settings: name, description, priority, initial state, max failed tasks, retries and
    workers, how workers read attachments, and whether every input path has to exist
  - [host requirements](#host-requirements), starting from the job template's
  - the job template's parameters, such as Model Downloads, and the queue's, such as Conda Channels
  - the input files and folders to attach, which you can remove, or add to with a file dialog
    on the ComfyUI machine
  - upload progress, the status of your recent jobs, and **Download** for their outputs, which
    go back into the output directory they were submitted with
  - **Save job bundle**, which creates the job bundle without submitting it, to submit later with
    `deadline bundle submit`
- A **Deadline** button next to **Run**, and **Workflow > Submit to Deadline Cloud…**, which open a
  short dialog: farm, queue, work items and **Submit**.
- Three nodes in the **Deadline Cloud** category, **Deadline String Values**, **Deadline Integer
  Values** and **Deadline Image Values**, that list values to run as separate tasks. See
  [Work items](#work-items).

When you submit, the extension:

1. Converts the open workflow to API format, the same as **Workflow > Export (API)**.
2. Creates a job bundle for the submission in
   `ComfyUI/user/deadline_submissions/<timestamp>_<workflow>/`, with a copy of the
   [job template](#the-job) and the workflow as `<workflow>_api.json`. That folder is a record of
   every workflow you submitted.
3. Submits the job bundle with the Deadline Cloud library, with these parameters filled in. The
   panel shows them, and anything you change there wins:

   | Parameter | Value |
   |---|---|
   | Job name | `ComfyUI - <workflow name>` |
   | Workflow File | The saved API-format workflow |
   | Output Directory | ComfyUI's output directory |
   | Input Directory | ComfyUI's input directory, so load nodes on the worker find the attached input files |
   | Work Items group | One field per list that runs as separate tasks, such as `1-4` |
   | Model Downloads | The download links that ComfyUI templates embed in loader nodes, for the models the workflow loads |

4. Shows the hashing and upload progress, asks you to confirm when Deadline Cloud would (for
   example, to upload files outside the storage profile), and then shows the job ID.

The library runs in the Deadline Cloud CLI's own Python environment, not ComfyUI's, through
`bridge.py`, so ComfyUI doesn't need boto3 or the deadline package.

## Requirements

- **ComfyUI 0.38.0 or later**, with frontend 1.53 or later.
- **Python 3.9 or later** to run the setup script. It can be any Python on the machine; it doesn't
  have to be ComfyUI's.
- **Deadline Cloud credentials and defaults.** Sign in with Deadline Cloud monitor, or use an
  AWS profile, and set a default farm and queue (`deadline config gui`).
- **ComfyUI on your own machine, or your own remote workstation.** The panel submits with the
  Deadline Cloud credentials of the machine that runs the ComfyUI server, so by default it only
  accepts a browser on that machine. Logging in with Deadline Cloud monitor opens on that machine's
  desktop.
- **A queue set up for the job:**
  - the `comfy` conda package in a conda channel that the queue can read
  - a conda queue environment
  - a Linux GPU fleet on driver r580 or newer

  You can build the `comfy` package from the `conda_recipes/comfy` sample in
  [deadline-cloud-samples](https://github.com/aws-deadline/deadline-cloud-samples).

  See [The job](#the-job).

## Installing

Get this repository, then run the setup script with the ComfyUI directory that contains
`custom_nodes`:

```
python install_submitter.py --comfyui /path/to/ComfyUI
```

The script:

1. Copies the extension into `ComfyUI/custom_nodes/deadline-cloud-for-comfyui`. If you cloned
   the repository into `custom_nodes` yourself, it uses that copy where it is.
2. Finds the Deadline Cloud CLI. If `deadline` 0.60.8 or later is on the `PATH`, for example
   from the [Deadline Cloud Submitter installer](https://docs.aws.amazon.com/deadline-cloud/latest/userguide/submitter.html#submitter-installation),
   it uses that. Otherwise it creates a Python environment inside the extension folder, in
   `.deadline-cli`, and installs `deadline[gui,console]` and a new enough `boto3` into it. Either
   way, the CLI stays out of ComfyUI's environment, so its Qt and AWS dependencies can't clash
   with ComfyUI's.
3. Writes `deadline_cloud_submitter.json` in the extension folder, with the CLI to run.
4. Checks the CLI, and tells you to run `deadline config gui` if no default farm and queue are
   set.

Then restart ComfyUI.

| Option | Description |
|---|---|
| `--comfyui PATH` | Required. The ComfyUI directory that contains `custom_nodes`, the one with `main.py`. For Comfy Desktop, it's the `ComfyUI` folder inside the installation, for example `~/ComfyUI-Installs/ComfyUI/ComfyUI`. |
| `--conda-channels "..."` | Optional. By default the submitter leaves **Conda Channels** at the queue's default, the Deadline Cloud channels. Pass channels here only if your studio wants every ComfyUI job to use different ones, for example its own channel first, then `conda-forge`. It's kept when you rerun the script without it; pass `""` to go back to the queue's default. |
| `--deadline-cli PATH` | Use this Deadline Cloud CLI, 0.60.8 or later, instead of looking on the `PATH`. |
| `--private-cli` | Install a private copy of the CLI in the extension folder, even if one is on the `PATH`. |
| `--link` | Symlink this checkout into `custom_nodes` instead of copying it, for development. |
| `--force` | Replace a `custom_nodes/deadline-cloud-for-comfyui` folder that isn't this extension. |

Run the script again to update the extension and the CLI. It doesn't prompt, so it can run from
configuration management such as Ansible. To uninstall, delete the extension folder from
`custom_nodes`.

### Installing manually

These steps do the same as the setup script.

1. **Put the extension in `custom_nodes`.** Clone the repository into ComfyUI's `custom_nodes`
   directory, or copy a downloaded copy there. The folder needs `__init__.py` and `src/` at its
   top level.

   ```
   cd /path/to/ComfyUI/custom_nodes
   git clone https://github.com/aws-deadline/deadline-cloud-for-comfyui.git
   ```

2. **Get the Deadline Cloud CLI, 0.60.8 or later, outside ComfyUI's Python environment.** If you
   installed it with the
   [Deadline Cloud Submitter installer](https://docs.aws.amazon.com/deadline-cloud/latest/userguide/submitter.html#submitter-installation),
   check its version with `deadline --version` and skip to step 3. Otherwise, install it into its
   own Python 3.9 or later environment. The commands below put it in `.deadline-cli` inside the
   extension folder, where the setup script puts it.

   macOS and Linux:

   ```
   cd /path/to/ComfyUI/custom_nodes/deadline-cloud-for-comfyui
   python3 -m venv .deadline-cli
   .deadline-cli/bin/python -m pip install "deadline[gui,console]>=0.60.8,<0.61" "boto3>=1.43.105"
   ```

   Windows (PowerShell):

   ```
   cd C:\path\to\ComfyUI\custom_nodes\deadline-cloud-for-comfyui
   py -m venv .deadline-cli
   .deadline-cli\Scripts\python -m pip install "deadline[gui,console]>=0.60.8,<0.61" "boto3>=1.43.105"
   ```

   Don't install it with ComfyUI's `pip`. Its Qt and AWS dependencies can clash with ComfyUI's.

3. **Tell the extension where the CLI is.** Create `deadline_cloud_submitter.json` in the
   extension folder, next to `__init__.py`:

   ```json
   {
     "deadline_cli": ".deadline-cli/bin/deadline"
   }
   ```

   - `deadline_cli` is the path to the `deadline` executable. A relative path is relative to the
     extension folder. On Windows, it's `.deadline-cli/Scripts/deadline.exe` for a private copy.
     For any other CLI, use the full path from `which deadline` (macOS and Linux) or
     `where.exe deadline` (Windows). Use forward slashes or doubled backslashes, because the file
     is JSON. If you leave `deadline_cli` out, the extension runs the `deadline` on the `PATH`
     that ComfyUI starts with.
   - `conda_channels` is optional. Without it, **Conda Channels** stays at the queue's default,
     the Deadline Cloud channels. Add it only to make every ComfyUI job use different channels,
     as a space-separated list.

   You can set the [environment variables](#configuration) instead of creating the file.

4. **Set up Deadline Cloud**, if you haven't already. Sign in with Deadline Cloud monitor, then
   choose a default farm and queue with `deadline config gui`. Use the CLI from step 2, for
   example `.deadline-cli/bin/deadline config gui`.

5. **Restart ComfyUI.** **Workflow > Submit to Deadline Cloud…** and the **Deadline** button next
   to **Run** appear. If they don't, check the ComfyUI server log for lines that start with
   `Deadline Cloud Submitter:`.

Both the script and the extension check that the CLI is version 0.60.8 or later. The job
also needs botocore 1.43.105 or later in the CLI's Python, which the CLI itself doesn't
require. The script checks it, and the panel stops before uploading if it's too old.

## Configuration

The setup script writes the config file. These environment variables override it. Set them in
the environment that starts ComfyUI.

| Environment variable | Default | Description |
|---|---|---|
| `COMFYUI_DEADLINE_CLI` | `deadline_cli` in the config, then `deadline` on the `PATH` | The Deadline Cloud CLI executable. |
| `COMFYUI_DEADLINE_CONDA_CHANNELS` | `conda_channels` in the config | Optional **Conda Channels** to fill in. When neither is set, the queue's default, the Deadline Cloud channels, is used. |
| `COMFYUI_DEADLINE_CONFIG` | `deadline_cloud_submitter.json` in the extension folder | Another config file to read. |
| `COMFYUI_DEADLINE_JOB_TEMPLATE` | The shipped job template | A job template file, YAML or JSON, to submit instead, for example your studio's version of the shipped template. It needs the same parameters as the shipped template, such as `WorkflowFile`, `InputDir`, `OutputDir` and `ModelDirectory`. Attached local models are mapped through **Model Directory**, so the template has to point ComfyUI at that folder, as the shipped template does. Custom node packs without a registry identity are attached only when the template also defines `CustomNodesDirectory`, and it has to add that folder to ComfyUI's `custom_nodes` paths, as the shipped template does. A template whose task script has an `ITEMS = {}` line gets work items as the shipped job does. |
| `COMFYUI_DEADLINE_ALLOW_REMOTE` | unset | Set it to `1` to accept submit requests from browsers on other machines. |

## The job

The extension ships one job, `comfy_cli_job_template.yaml` in
`src/deadline/comfyui_submitter/`. It starts ComfyUI with `comfy launch` and runs the workflow with
`comfy run`, and it needs the `comfy` conda package.

The job's **Conda Channels** stays at the queue's default unless you change it in the panel
or [configure it](#configuration). The `comfy` package has to be in one of those channels.
For a workflow with work items, the submitter adds their parameters to its copy of the template.
The job handles work items, inputs, outputs, models and custom nodes like this:

- **Work items.** See [Work items](#work-items). To vary seeds, use a **Deadline Integer Values**
  node.
- **Input files.** The submitter lists the files in ComfyUI's input directory that the workflow
  loads, such as Load Image's image, in the job bundle's `asset_references.yaml`, with the whole
  folder that a folder loader or **Deadline Image Values** node reads. They're attached to the
  job, and path mapping points **Input Directory** at them on the worker.
- **Outputs.** Outputs are returned through job attachments. Download them with Deadline Cloud
  monitor or `deadline job download-output`.
- **Models.** Workers don't have your local models. Each model the job needs is downloaded
  through **Model Downloads**, attached from this machine under **Model Directory**, or already
  on the workers in the model cache or a shared folder that **Extra Model Paths Config** points
  at.

  Workflows made from ComfyUI's templates store a download link in each loader node. The
  submitter turns the links for the models the workflow actually loads into **Model Downloads**
  entries, with a `#sha256=` checksum when the template has one. Links with characters that
  aren't safe in the job's shell script are skipped and logged, and so is any `?download=true`
  suffix. A loaded model with no usable link, or whose link names a different file, is taken
  from the local file ComfyUI would load and placed under **Model Directory** in that model's
  folder (`checkpoints/`, `loras/`, and so on). Those files are job attachments. On the worker,
  **Model Directory** is the folder those files are mapped to, and ComfyUI loads each model by
  the name already in the workflow. If the file isn't on this machine, it is omitted from
  **Model Downloads** and from the attachments. The workflow still names the model. ComfyUI
  shows a warning when you submit: there is no usable link, or the link names a different file
  and this machine has no local copy. The job fails unless the workers already have the model.
  Add it to **Model Downloads**. In the panel, **Attachments** lists the local models it will
  attach. Remove one with its **✕** when the workers already have it, and it isn't uploaded.
- **Custom nodes.** ComfyUI saves the Comfy Registry pack and version of each
  custom node in the workflow. The submitter lists the packs of the nodes the workflow runs in
  **Custom Nodes**, as `ID@VERSION` entries, and the job installs them on the worker with their
  Python dependencies. A node with no registry pack, such as one from a git checkout or your
  studio's own, is taken from the pack folder ComfyUI loaded it from and placed under **Custom
  Nodes Directory**, without its `.git`, `__pycache__` or virtual environment folders. Those
  folders are job attachments. On the worker, ComfyUI loads the packs from where they're mapped,
  and the job installs each pack's `requirements.txt`. A single-file custom node is attached the
  same way, as its one `.py` file. When the pack's files can't be found on this machine, ComfyUI
  shows a warning when you submit. Turn off **Attach Custom Nodes** when the workers already
  provide every custom node and its dependencies, for example from a custom nodes conda package.
  The job then installs nothing for custom nodes and ignores **Custom Nodes** and **Custom Nodes Directory**.
  The panel lists the packs it will attach under **Attachments**, and doesn't upload them when
  **Attach Custom Nodes** is off. The Deadline value nodes are never attached, because the job
  replaces them.

## Work items

The job runs the workflow once per combination of its work items. The submitter finds
each of these in the workflow and gives it a task parameter, with a field in the panel's
**Work Items** group:

| In the workflow | One task per | Field by default |
|---|---|---|
| **Deadline String Values** node | Line of text | Every line, such as `1-3` |
| **Deadline Integer Values** node | Number, such as `1-100`, `1-20:2` or `7, 42, 99` | The node's numbers |
| **Deadline Image Values** node | Image in a folder of ComfyUI's input directory, in name order | Every image |
| **Create List** | Item wired into it | Every item |
| **Load Image (from Folder)** | Image in its folder, in name order | Every image |
| A prompt with choices, such as `a {red\|white\|black} fox` | Combination of choices | `0`, which chooses one at random for each task, as ComfyUI does |

Every combination of the fields is a task: a Create List with 3 items and a Deadline Integer
Values node with `1-4` make 12 tasks. The panel shows the count as you change them, such as
"3 × 4 = 12 tasks". Lists can have different numbers of items. Each task adds its
items to the Save nodes' file names, such as `_List64-3_Seed-42`.

In the panel, narrow a field, for example to `2,3`, to run only those items. Set a dynamic
prompt's field to `1-9`, say, to run each of its 9 prompts. A Create List or folder field set to
`0` runs the whole list in one task. A step can have at most 16 task parameters, so the submitter
runs any lists past the first 16 whole, with a warning. The number in a label such as **Create
List 64** is the node's ID, which ComfyUI shows with **Settings > Lite Graph > Node ID badge**. Lists that a workflow builds while it runs, such as Rebatch Images, always run whole in
one task, because Deadline Cloud sets a job's tasks when you submit it.

The value nodes connect to any input of their type: a prompt or LoRA name, a seed or steps, an
image. In ComfyUI each outputs its **preview_item**. The job replaces each with the task's value,
and loads a split folder's image with **Load Image**, so workers don't need this extension. A
value node's field is named after its title when you rename it, such as **Seed**, which also names
its task parameter in Deadline Cloud monitor. An Integer node's field holds its numbers, so
Deadline Cloud monitor shows each task's number; other fields hold item numbers.

To run value nodes' items together rather than in every combination, give them the same
**pair_with**, for example a prompt list and a LoRA list that go together. Paired nodes need the
same number of items: their first items run together, then their second, and so on. Paired nodes
with different numbers of items run every combination, with a warning.

## Host requirements

The panel's **Host requirements** section says which workers can run the job. It starts from the
job template's: the shipped template needs Linux and at least one GPU. Changing a field replaces
the template's requirements with what's in the form, and **Use the template's** goes back to them.
Clearing every field lets any worker run the job.

| Field | Worker capability |
|---|---|
| Operating system | `attr.worker.os.family` |
| CPU architecture | `attr.worker.cpu.arch` |
| vCPUs | `amount.worker.vcpu` |
| Memory (GiB) | `amount.worker.memory`, in MiB |
| GPUs | `amount.worker.gpu` |
| GPU memory (GiB) | `amount.worker.gpu.memory`, in MiB |
| Scratch space (GiB) | `amount.worker.disk.scratch` |
| Add amount | `amount.<name>`, such as a license count |
| Add attribute | `attr.<name>`, with any or all of its values |

If no worker in the queue's fleets meets the requirements, the job's tasks wait until one does.

## Limitations

- The panel can't log out or switch AWS profile. Use `deadline config gui` for those.
- Adding attachments opens a file dialog on the ComfyUI machine's desktop: osascript on macOS,
  PowerShell on Windows and zenity on Linux.
- Deadline Cloud monitor login opens on the ComfyUI server's desktop, not in the browser.
- Custom nodes without a Comfy Registry pack are attached from this machine, so the worker runs
  your local copy and uploads it with each job. A single-file node's own imports still have to
  exist on the worker, since it has no `requirements.txt` to install.
- The workflow must have at least one Save node, such as **SaveImage**.

## Development

See [DEVELOPMENT.md](DEVELOPMENT.md).

- [Contributing](CONTRIBUTING.md): DCO sign-offs and conventional PR titles and commits.
- [Code of conduct](CODE_OF_CONDUCT.md).
- [Security reporting](SECURITY.md).

## License

This project is licensed under the [Apache-2.0 License](LICENSE). See also
[NOTICE](NOTICE).

### External Dependencies

This project integrates with [ComfyUI](https://github.com/Comfy-Org/ComfyUI),
which is licensed under the GPL-3.0. ComfyUI is not bundled with or
distributed by this project. Install ComfyUI separately from its upstream
source.
