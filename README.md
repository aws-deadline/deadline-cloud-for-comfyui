# AWS Deadline Cloud for ComfyUI

> **Experimental — under development**
>
> This project is in early development and is not ready for production use.
> Features, interfaces, and setup instructions may change.

A ComfyUI extension that submits the open workflow to [AWS Deadline Cloud](https://aws.amazon.com/deadline-cloud/),
from a **Deadline** panel and dialog inside ComfyUI. The job runs the workflow on Deadline Cloud
GPU workers, with the models and input files it uses attached to the job.

## What it adds

- A **Deadline** tab in ComfyUI's sidebar, with:
  - your login status, with **Log in** for Deadline Cloud monitor profiles
  - the farm, queue and storage profile, saved as your Deadline Cloud defaults
  - job settings: name, description, priority, initial state, max failed tasks, retries and
    workers, how workers read attachments, and whether every input path has to exist
  - [host requirements](#host-requirements), starting from the job template's
  - the job template's parameters, such as Workflow Timeout, and the queue's, such as Conda Channels
  - the input files and folders, and the models, to attach, which you can remove, or add to with a
    file dialog on the ComfyUI machine
  - upload progress, the status of your recent jobs, and **Download** for their outputs, which
    go back into the output directory they were submitted with. Each job shows that directory.
  - **Save job bundle**, which creates the job bundle without submitting it, to submit later with
    `deadline bundle submit`. The bundle keeps the parameters, host requirements, priority,
    initial state, and the failed task, retry and worker limits. Pass the job name with `--name`.
- A **Deadline** button next to **Run**, and **Workflow > Submit to Deadline Cloud…**, which open a
  short dialog: farm, queue and **Submit**.

When you submit, the extension:

1. Converts the open workflow to API format, the same as **Workflow > Export (API)**.
2. Creates a job bundle for the submission in
   `ComfyUI/user/deadline_submissions/<timestamp>_<workflow>/`, with a copy of the
   [job template](#the-job) and the workflow as `<workflow>_api.json`. The bundle is deleted once
   the job is created, or when you cancel. A failed submission's bundle is kept for a day, to look
   into or submit again.
3. Submits the job bundle with the Deadline Cloud library, with these parameters filled in. The
   panel shows them, and anything you change there wins:

   | Parameter | Value |
   |---|---|
   | Job name | `ComfyUI - <workflow name>` |
   | Workflow File | The saved API-format workflow |
   | Output Directory | ComfyUI's output directory |
   | Input Directory | ComfyUI's input directory, so load nodes on the worker find the attached input files |
   | Model Directory | The bundle's folder of attached models, so ComfyUI on the worker finds them |

4. Shows the hashing and upload progress, asks you to confirm when Deadline Cloud would (for
   example, to upload files outside the storage profile), and then shows the job ID.

The library runs in the Deadline Cloud CLI's own Python environment, not ComfyUI's, through
`bridge.py`, so ComfyUI doesn't need boto3 or the deadline package.

## Requirements

- **ComfyUI 0.38.0 or later**, running on Python 3.13 or later, with frontend 1.53 or later.
- **Python 3.13 or later** for installation from source, to run the setup script. It can be any Python on the machine; it doesn't
  have to be ComfyUI's.
- **Node.js 22 or later**, with npm, for installation from source to build the panel. ComfyUI doesn't need
  it once the extension is installed.
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
  [deadline-cloud-samples](https://github.com/aws-deadline/deadline-cloud-samples). The job runs
  ComfyUI and its scripts with the Python in the `comfy` package's conda environment, not the
  fleet's own Python. The workers' Python version doesn't need to match anything here.

  See [The job](#the-job).

## Installing

### Windows and macOS installers

When your studio supplies an installer built from this repository, quit ComfyUI and open
`DeadlineCloudForComfyUISubmitter-windows-x64-installer.exe` on 64-bit Windows, or the
`osx-arm64` installer on an Apple silicon Mac with macOS 13 or later.
Select the ComfyUI submitter, then choose the folder containing both `main.py` and
`custom_nodes`. The folder picker suggests `ComfyUI` in your home folder. The installer
supports standard ComfyUI layouts and requires a writable `custom_nodes` directory.

The installer includes the compiled panel, Deadline Cloud SDK and CLI, and the Python
runtime used by the submitter. Restart ComfyUI after installation to load the extension.
Keep the `DeadlineCloudForComfyUISubmitter` folder in your home directory while the
extension is installed. Set up Deadline Cloud monitor credentials or an AWS profile
for your account.

Run the newer installer with the same ComfyUI folder to upgrade. It preserves existing
submitter settings and unrelated custom nodes. To remove an installer installation, quit
ComfyUI and run `uninstall.exe` on Windows or `uninstall.app` on macOS from the Deadline
installation folder. Start ComfyUI again after uninstalling. The uninstaller removes
unchanged integration files and its own runtime, and keeps customer additions and edited
settings. ComfyUI, models, workflows, and Deadline Cloud credentials stay in place.

The shared installer build workflow uses the AWS-managed build/signing projects, which
must be provisioned for this integration before producing a customer release. Local and
pull-request builds use evaluation InstallBuilder and are not signed customer releases.
See [Development](DEVELOPMENT.md#building-the-installers) for build and release prerequisites.

### Installing from source

Get this repository, then run the setup script with the ComfyUI directory that contains
`custom_nodes`:

```
python install_submitter.py --comfyui /path/to/ComfyUI
```

The script:

1. Builds the panel. It runs `npm ci` and `npm run build` in `frontend/`, which compiles the
   panel's TypeScript into the JavaScript that ComfyUI serves.
2. Copies the extension into `ComfyUI/custom_nodes/deadline-cloud-for-comfyui`. If you cloned
   the repository into `custom_nodes` yourself, it uses that copy where it is.
3. Finds the Deadline Cloud CLI. If `deadline` 0.60.8 or later is on the `PATH`, and it runs on a
   Python the extension can find, it uses that. The panel runs the CLI's own Python, so a
   standalone build of the CLI without one isn't used. Otherwise it creates a Python environment
   inside the extension folder, in `.deadline-cli`, and installs `deadline[gui,console]` and a
   new enough `boto3` into it. Either way, the CLI stays out of ComfyUI's environment, so its Qt
   and AWS dependencies can't clash with ComfyUI's.
4. Writes `deadline_cloud_submitter.json` in the extension folder, with the CLI to run.
5. Checks the CLI, and prints a reminder to run `deadline config gui` if no default farm and
   queue are set.

Then restart ComfyUI. **Workflow > Submit to Deadline Cloud…** and the **Deadline** button next
to **Run** appear. If they don't, check the ComfyUI server log for lines that start with
`Deadline Cloud Submitter:`.

| Option | Description |
|---|---|
| `--comfyui PATH` | Required. The ComfyUI directory that contains `custom_nodes`, the one with `main.py`. For Comfy Desktop, it's the `ComfyUI` folder inside the installation, such as `~/ComfyUI-Installs/ComfyUI/ComfyUI`. |
| `--conda-channels "..."` | Optional. By default the submitter leaves **Conda Channels** at the queue's default, the Deadline Cloud channels. Pass channels here only if your studio wants every ComfyUI job to use different ones, such as its own channel first, then `conda-forge`. It's kept when you rerun the script without it. Pass `""` to go back to the queue's default. |
| `--deadline-cli PATH` | Use this Deadline Cloud CLI, 0.60.8 or later. The default is the first `deadline` on the `PATH`. |
| `--private-cli` | Install a private copy of the CLI in the extension folder, even if one is on the `PATH`. |
| `--link` | Symlink this checkout into `custom_nodes` instead of copying it, for development. |
| `--force` | Replace a `custom_nodes/deadline-cloud-for-comfyui` folder that isn't this extension. |

Run the script again to update the extension and the CLI. It doesn't prompt, so it can run from
configuration management such as Ansible. To uninstall, delete the extension folder from
`custom_nodes`.

Both the script and the extension check that the CLI is version 0.60.8 or later. The job
also needs botocore 1.43.105 or later in the CLI's Python, which the CLI itself doesn't
require. The script checks it, and the panel stops before uploading if it's too old.

## Configuration

The installer or setup script writes the config file. These environment variables override it. Set them in
the environment that starts ComfyUI.

| Environment variable | Default | Description |
|---|---|---|
| `COMFYUI_DEADLINE_PYTHON` | `deadline_python` in the installer config, then the CLI's Python | The executable Python runtime used for Deadline Cloud. The installer configures its private runtime automatically. |
| `COMFYUI_DEADLINE_CLI` | `deadline_cli` in the config, then `deadline` on the `PATH` | The Deadline Cloud CLI executable. |
| `COMFYUI_DEADLINE_CONDA_CHANNELS` | `conda_channels` in the config | Optional **Conda Channels** to fill in. When neither is set, the queue's default, the Deadline Cloud channels, is used. |
| `COMFYUI_DEADLINE_CONFIG` | `deadline_cloud_submitter.json` in the extension folder | Another config file to read. |
| `COMFYUI_DEADLINE_JOB_TEMPLATE` | The extension's job template | A job template file, YAML or JSON, to submit instead, such as your studio's version of the extension's template. It needs the same parameters, such as `WorkflowFile`, `InputDir` and `OutputDir`. Local models are attached only when the template also defines `ModelDirectory`, and it has to point ComfyUI at that folder. The extension's template does. |

## The job

The extension includes one job, `comfy_cli_job_template.yaml` in
`src/deadline/comfyui_submitter/`. It starts ComfyUI with `comfy launch` and runs the workflow with
`comfy run`, and it needs the `comfy` conda package.

The job's **Conda Channels** stays at the queue's default unless you change it in the panel
or [configure it](#configuration). The `comfy` package has to be in one of those channels. In the
panel's **Queue environment** section, clearing a field you changed, or **Use the queue's
default**, goes back to that default rather than sending an empty value. **Conda Packages** shows
the job's `comfy`. To add packages, keep `comfy` in the list, as in `comfy my-custom-nodes`.

Each task runs the workflow for up to **Workflow Timeout**, an hour by default. `comfy run` also
waits that long for ComfyUI to report progress, since loading a large model can take minutes
without any. Raise it for long video workflows.

The job runs the workflow once, as one task. Input files and models are attached to the job,
uploaded from this machine. The workers have to provide the custom nodes:

- **Input files.** The submitter lists the files in ComfyUI's input directory that the workflow
  loads, such as Load Image's image, in the job bundle's `asset_references.yaml`, with the whole
  folder that a folder loader reads. They're attached to the job, and path mapping points
  **Input Directory** at them on the worker.
- **Outputs.** Outputs are returned through job attachments. Download them with Deadline Cloud
  monitor or `deadline job download-output`.
- **Models.** Workers don't have your local models. The submitter finds each model file the workflow
  loads in ComfyUI's model folders, the way ComfyUI would, and places it in the job bundle under
  **Model Directory**, in that model's folder (`checkpoints/`, `loras/`, and so on). Those files are
  job attachments. Each one is uploaded once, and later jobs reuse it. On the worker, **Model
  Directory** is the folder those files are mapped to, and ComfyUI loads each model by the name
  already in the workflow. The job doesn't download models from the internet. When a model isn't on
  this machine, the panel warns about it, and the job fails unless the workers already have the
  model, such as in a shared folder that **Extra Model Paths Config** points at. In the panel,
  **Attachments** lists the models it will attach. The panel lists each model by its folder and
  name, such as `loras/style.safetensors`, since two folders can hold files of the same name. Remove
  one with its **✕** when the workers already have it, and it isn't uploaded.
- **Custom nodes.** Workers load the custom nodes in the job's conda environment, such as a
  custom nodes conda package added to **Conda Packages**, or in a `custom_nodes` folder that
  **Extra Model Paths Config** lists. The job fails with the import error when a custom node
  doesn't load.

## Host requirements

The panel's **Host requirements** section says which workers can run the job. It starts from the
job template's: the extension's template needs Linux and at least one GPU. Changing a field replaces
the template's requirements with what's in the form, and **Reset to defaults** goes back to them.
Clearing every field lets any worker run the job. The form starts from the template's first step,
and what you set applies to every step of the job.

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

Large models need a bigger worker than the template's minimum. On a 16 GiB GPU such as a g4dn's T4,
a video model with a large text encoder can run out of memory or take hours. For those workflows,
ask for at least 24 GiB of GPU memory and 64 GiB of memory, which means a g5.4xlarge (A10G) or
larger.

## Security

The extension's `/deadline` routes submit jobs with the Deadline Cloud credentials of the machine
that runs ComfyUI, read files on that machine, and open file dialogs and the Deadline Cloud
monitor login on its desktop. Anyone who can call them acts as that machine's user. The supported
setup is **one artist per ComfyUI server**: on their own machine, or on their own remote
workstation that they reach through remote desktop.

**What ComfyUI provides.** ComfyUI has no login. Unless it's started with
`--enable-cors-header`, it refuses requests that another website's page sends from a browser on
the same machine.

**What the extension adds.**

- The routes require a loopback peer address. A request from another address, or one
  with a `Forwarded`, `X-Forwarded-For` or `X-Real-IP` header from a reverse proxy, gets a 403.
- They only accept requests addressed to `localhost` or a loopback address such as `127.0.0.1`,
  so a website that points its own domain name at this machine can't pass for ComfyUI's page.
- They refuse requests that a browser marks as coming from another website, even when
  `--enable-cors-header` turns off ComfyUI's own check. When an `Origin` header is present, its
  scheme, host and port must match the ComfyUI request, even without browser fetch metadata.
- They only accept JSON, which a page from another website can't send without the server's
  permission.
- Submission progress, confirmation prompts and errors, and download progress go only to the
  browser tab that started them, not to every tab connected to the server.
- A submission requires a valid browser client ID. Only that client can confirm or cancel it,
  and confirmation answers are accepted only while a prompt is pending.
- Job bundles in `ComfyUI/user/deadline_submissions/` hold your workflows, prompts and file paths.
  On Linux and macOS that folder and each bundle in it can only be opened by the user that runs
  ComfyUI (mode `0700`), and the files the extension writes there can only be read by that user
  (`0600`), whatever the umask. Models staged in a bundle are often links to your files, so
  their permissions stay as they are, but they can only be reached through the bundle, which is
  deleted once its job is created. On Windows, bundles inherit the permissions of the ComfyUI
  user folder. The `deadline_cloud_submitter.json` config file is also `0600`.

**Local access.** Use a browser on the machine running ComfyUI and access it through
`localhost`.

The Host and Origin checks stay enabled. DNS rebinding can let another website call a reachable
server by pointing its domain name at that server. The routes require a local Host and reject
any Origin that does not match the request's scheme, host and port.

## Limitations

- The panel can't log out or switch AWS profile. Use `deadline config gui` for those.
- Adding attachments opens a file dialog on the ComfyUI machine's desktop: osascript on macOS,
  PowerShell on Windows and zenity on Linux.
- Deadline Cloud monitor login opens on the ComfyUI server's desktop, not in the browser.
- Each job runs the workflow once. To render variations, such as two seeds, submit a job for
  each.
- Custom nodes aren't attached. The workers have to provide them.
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
