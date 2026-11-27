# AWS Deadline Cloud for ComfyUI

> **Experimental — under development**
>
> This project is in early development and is not ready for production use.
> Features, interfaces, and setup instructions may change.

A ComfyUI extension that submits the open workflow to [AWS Deadline Cloud](https://aws.amazon.com/deadline-cloud/),
from a **Submit to Deadline Cloud** button next to **Run** and a **Deadline Cloud** panel inside
ComfyUI. The job runs the workflow on Deadline Cloud GPU workers, with the models, input files and
custom nodes it uses attached to the job.

## What it adds

- A **Submit to Deadline Cloud** button next to **Run**, and
  **Workflow > Submit to Deadline Cloud**, which open a quick submit popover with:
  - **Job name**, the same name as in the panel
  - **Additional settings**, which opens the panel, and **Submit**. When something has to be set
    or fixed first, such as the farm and queue, a note says what, and **Submit** stays off until
    then. When the panel has warnings about the workflow, a line says how many.
  - one row for the submission: its hashing and upload progress, with **Cancel**, and any
    question to confirm, then the job's name and ID, with **Copy job ID**, until you submit
    again. Check the job in Deadline Cloud monitor, and download its outputs there or with
    `deadline job download-output`.

  When you aren't logged in, **Submit to Deadline Cloud** opens the panel instead, with
  **Job submission settings** open and **Log in**.
- A **Deadline Cloud** tab in ComfyUI's sidebar, with:
  - **Job name**, with any errors or warnings about the workflow under it
  - **Job Properties**: the job's description, priority and initial state,
    **Maximum retries per task**, **Maximum failed tasks count**, **Maximum worker count**, and
    **Output Directory**, with **Browse…**
  - **Job submission settings**: the farm and queue, your login status, with **Log in** for
    Deadline Cloud monitor profiles, the **Default storage profile** when the queue has storage
    profiles, and the queue environment's parameters, such as **Conda Channels**, under a heading
    for each environment, such as **Queue Environment: Conda**. The farm, queue and storage
    profile are saved as your Deadline Cloud defaults. **Submit** and **Submit to Deadline Cloud**
    wait until the chosen queue's details, its parameters and storage profiles, have loaded
  - **Advanced settings**, with:
    - **Attachments**: the input files and folders, models and custom node packs to attach, and
      how workers read attachments, under **Job attachments**. A folder loader's folder is one row
      with its number of files. Open it to list the files, and remove single files from it. Its
      **✕** removes every file in it, and **Add back** restores them all. You can remove files,
      folders, models and custom node packs, or add more with **Add files…** and
      **Add folder…**, which open a file dialog on the ComfyUI machine. Each workflow tab keeps
      its own changes.
    - **Timeouts**: **Server Start Timeout** and **Workflow Timeout**
    - [**Host requirements**](#host-requirements), starting from the job template's
    - **Job parameters**, only for a [custom job template](#configuration) with other parameters
      to show, under a heading for each of their groups
  - **Submit to Deadline Cloud**, with the reason when it's off

  Each section starts closed. A section opens by itself when it holds a value to fix or a choice
  that Submit needs. For example, **Job submission settings** opens when you aren't logged in, or
  no farm or queue is chosen.

When you submit, the extension:

1. Converts the open workflow to API format, the same as **Workflow > Export (API)**.
2. Creates a job bundle for the submission in
   `ComfyUI/user/deadline_submissions/<timestamp>_<workflow>/`, with a copy of the
   [job template](#the-job) and the workflow as `<workflow>_api.json`. The bundle is deleted once
   the job is created, or when you cancel. A failed submission's bundle is kept for a day, to look
   into or submit again.
3. Submits the job bundle with the Deadline Cloud library, with these parameters filled in. The
   panel shows **Job name**, and **Output Directory** with **Browse…**, and what you change there
   wins. It doesn't show the others:

   | Parameter | Value |
   |---|---|
   | Job name | `ComfyUI - <workflow name>` |
   | Workflow File | The saved API-format workflow |
   | Output Directory | ComfyUI's output directory |
   | Input Directory | ComfyUI's input directory, so load nodes on the worker find the attached input files |
   | Model Directory | The bundle's folder of attached models, so ComfyUI on the worker finds them |
   | Custom Nodes Directory | The bundle's folder of attached custom node packs |

4. Shows the hashing and upload progress, asks you to confirm when Deadline Cloud would (for
   example, to upload files outside the storage profile), and then shows the job ID. Follow the
   job in Deadline Cloud monitor.

The library runs in a separate process through `bridge.py`, with the Deadline Cloud CLI's own
Python, or with ComfyUI's Python and the installer's `dependencies` folder, so ComfyUI's
environment doesn't need boto3 or the deadline package.

## Requirements

- **ComfyUI 0.38.0 or later**, running on Python 3.13 or later, with frontend 1.53 or later.
- **Python 3.13 or later** to run the setup script. It can be any Python on the machine; it doesn't
  have to be ComfyUI's.
- **Node.js 22 or later**, with npm, for the setup script to build the panel. ComfyUI doesn't need
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

  The `comfy` packages for service-managed fleets are published to the `deadline-cloud-v2` conda
  channel, which the submitter adds to the queue's **Conda Channels**, and are built for Linux
  only. You can also build the `comfy` package from the `conda_recipes/comfy` sample in
  [deadline-cloud-samples](https://github.com/aws-deadline/deadline-cloud-samples). The job runs
  ComfyUI and its scripts with the Python in the `comfy` package's conda environment, not the
  fleet's own Python. The workers' Python version doesn't need to match anything here.

  See [The job](#the-job).

## Installing

### Windows and macOS installer

Quit ComfyUI, then run `DeadlineCloudForComfyUISubmitter-windows-x64-installer.exe` or
`DeadlineCloudForComfyUISubmitter-osx-installer.app`, and choose the folder whose `custom_nodes`
ComfyUI loads extensions from:

- Comfy Desktop: the `ComfyUI` folder inside the build's folder, such as
  `~/ComfyUI-Installs/ComfyUI/ComfyUI`. The installer suggests the first build that Comfy Desktop
  lists when `~/ComfyUI` isn't a ComfyUI folder.
- A git clone or the Windows portable build: the `ComfyUI` folder that contains `main.py`.
- ComfyUI started with `--base-directory`: that folder.

To install without prompts, pass `--mode unattended --comfyui-directory /path/to/ComfyUI`.
`--prefix` sets the installation folder, which holds the uninstaller. It's
`~/DeadlineCloudForComfyUISubmitter` by default.

The installer puts the extension in `custom_nodes/deadline-cloud-for-comfyui`, with the Deadline
Cloud library and its dependencies in the extension's `dependencies` folder. ComfyUI's own Python,
3.13 or 3.14, runs them in a separate process, so nothing is installed into ComfyUI's environment,
and the panel submits without the Deadline Cloud CLI. When ComfyUI runs another Python version, or
on a CPU the installer wasn't built for, the panel uses the Deadline Cloud CLI instead, as a source
installation does.

Restart ComfyUI after installing. To upgrade, run a newer installer with the same ComfyUI folder.
To uninstall, run `uninstall` in the installation folder. It removes the whole extension folder,
including its `deadline_cloud_submitter.json`. The installer stops if
`custom_nodes/deadline-cloud-for-comfyui` already exists and it didn't create it, such as a clone of
this repository.

### Installing from source

Get this repository, then run the setup script with the ComfyUI directory that contains
`custom_nodes`:

```
python install_submitter.py --comfyui /path/to/ComfyUI
```

The script:

1. Builds the panel. It runs `npm ci` and `npm run build` in `frontend/`, which compiles the
   panel's TypeScript into the JavaScript that ComfyUI serves.
2. Copies the extension into `ComfyUI/custom_nodes/deadline-cloud-for-comfyui`. It copies only the
   files the extension runs: its Python package, the job template, the compiled panel, the setup
   script, `__init__.py`, `LICENSE`, `NOTICE` and `README.md`. Anything else in your checkout, such
   as `.npmrc`, credentials, logs or old builds, isn't copied. The script stops if one of those
   files is a symlink or a Windows junction, since a link can point anywhere. If you cloned the
   repository into `custom_nodes` yourself, it uses that copy where it is.
3. Finds the Deadline Cloud CLI. If `deadline` 0.60.8 or later is on the `PATH`, and it runs on a
   Python the extension can find, it uses that. The panel runs the CLI's own Python, so a
   standalone build of the CLI without one isn't used. Otherwise it creates a Python environment
   inside the extension folder, in `.deadline-cli`, and installs `deadline[gui,console]` and a
   new enough `boto3` into it. Either way, the CLI stays out of ComfyUI's environment, so its Qt
   and AWS dependencies can't clash with ComfyUI's.
4. Writes `deadline_cloud_submitter.json` in the extension folder, with the CLI to run.
5. Checks the CLI, and prints a reminder to run `deadline config gui` if no default farm and
   queue are set.

Then restart ComfyUI. **Workflow > Submit to Deadline Cloud** and the **Submit to Deadline Cloud**
button next to **Run** appear. If they don't, check the ComfyUI server log for lines that start with
`Deadline Cloud Submitter:`.

| Option | Description |
|---|---|
| `--comfyui PATH` | Required. The ComfyUI directory that contains `custom_nodes`, the one with `main.py`. For Comfy Desktop, it's the `ComfyUI` folder inside the installation, such as `~/ComfyUI-Installs/ComfyUI/ComfyUI`. |
| `--conda-channels "..."` | Optional. By default the submitter uses the queue's default **Conda Channels**, the Deadline Cloud channels, with `deadline-cloud-v2` added before `deadline-cloud`. Pass channels here only if your studio wants every ComfyUI job to use different ones, such as its own channel first, then `conda-forge`. They're used as you give them, without `deadline-cloud-v2` added. It's kept when you rerun the script without it. Pass `""` to go back to the default. |
| `--deadline-cli PATH` | Use this Deadline Cloud CLI, 0.60.8 or later. The default is the first `deadline` on the `PATH`. |
| `--private-cli` | Install a private copy of the CLI in the extension folder, even if one is on the `PATH`. |
| `--link` | Symlink this checkout into `custom_nodes` instead of copying it, for development. |
| `--force` | Replace a `custom_nodes/deadline-cloud-for-comfyui` folder that isn't a copy this installer made, such as another folder or a git checkout of the extension. Its files, including a checkout's history and uncommitted changes, are deleted. |

Run the script again to update the extension and the CLI. An update keeps the copy's private CLI
in `.deadline-cli` and its `deadline_cloud_submitter.json`. It doesn't prompt, so it can run from
configuration management such as Ansible. To uninstall, delete the extension folder from
`custom_nodes`.

Both the script and the extension check that the CLI is version 0.60.8 or later. The job
also needs botocore 1.43.105 or later in the CLI's Python, which the CLI itself doesn't
require. The script checks it, and the panel stops before uploading if it's too old.

## Configuration

The setup script writes the config file. These environment variables override it. Set them in
the environment that starts ComfyUI.

| Environment variable | Default | Description |
|---|---|---|
| `COMFYUI_DEADLINE_CLI` | `deadline_cli` in the config, then `deadline` on the `PATH` | The Deadline Cloud CLI executable. |
| `COMFYUI_DEADLINE_CONDA_CHANNELS` | `conda_channels` in the config | Optional **Conda Channels** to fill in. When neither is set, the job uses the queue's default with `deadline-cloud-v2` added, as described in [The job](#the-job). |
| `COMFYUI_DEADLINE_CONFIG` | `deadline_cloud_submitter.json` in the extension folder | Another config file to read. |
| `COMFYUI_DEADLINE_JOB_TEMPLATE` | The extension's job template | A job template file, YAML or JSON, to submit instead, such as your studio's version of the extension's template. It needs the same parameters, such as `WorkflowFile`, `InputDir` and `OutputDir`. Local models are attached only when the template also defines `ModelDirectory`, and it has to point ComfyUI at that folder. Custom node packs are attached only when the template defines `CustomNodesDirectory`, and it has to add that folder to ComfyUI's `custom_nodes` paths. The extension's template does both. The panel shows the template's other visible parameters under **Advanced settings > Job parameters**. |

## The job

The extension includes one job, `comfy_cli_job_template.yaml` in
`src/deadline/comfyui_submitter/`. It starts ComfyUI with `comfy launch` and runs the workflow with
`comfy run`, and it needs the `comfy` conda package.

Like the other Deadline Cloud submitters, the submitter adds the `deadline-cloud-v2` channel just
before `deadline-cloud` in the queue's default **Conda Channels**, unless it's already there, and
sends that value with the job. The `comfy` package is published to `deadline-cloud-v2`. Channels
that you [configure](#configuration), or change in the panel, are sent as they are instead, and
the `comfy` package has to be in one of them. The panel shows the queue's parameters under
**Job submission settings**. Clearing one you changed, or its **Use the default**, goes back to
its default rather than sending an empty value. **Conda Packages** shows the job's `comfy`. To
add packages, keep `comfy` in the list, as in `comfy my-custom-nodes`.

Each task runs the workflow for up to **Workflow Timeout**, 6 hours (21600 seconds) by default,
under **Advanced settings > Timeouts**. `comfy run` also waits that long for ComfyUI to report
progress, since loading a large model can take minutes without any. Raise it, up to 23 hours,
for long video workflows.

The job runs the workflow as one task, with [its lists](#workflows-with-lists) in it. Workers get
everything the workflow uses as job attachments, uploaded from this machine:

- **Input files.** The submitter reads only the inputs that ComfyUI's node definitions mark as
  files, such as Load Image's image, Load Video's file and Load Audio's audio, and the folder that
  a folder loader reads. It lists the files they name in ComfyUI's input directory, or with
  `[output]` or `[temp]`, in its output or temp directory, in the job bundle's
  `asset_references.yaml`. A folder loader's folder is attached as the files in it. Text in other
  inputs, such as a prompt, never attaches a file. Neither does an absolute path or a link that
  leads out of ComfyUI's folders. The panel warns about those, and you can attach any file or
  folder yourself with **Add files…** or **Add folder…** under
  **Advanced settings > Attachments**. Every input path the job attaches has to exist: a missing
  one stops the submission. The submitter sets **Input Directory** to ComfyUI's input directory,
  and on the worker, path mapping points it at the attached files.
- **Outputs.** Outputs are returned through job attachments. Download them with Deadline Cloud
  monitor or `deadline job download-output`.
- **Models.** Workers don't have your local models. The submitter finds each model file the workflow
  loads in ComfyUI's model folders, the way ComfyUI would, and places it in the job bundle under
  **Model Directory**, in that model's folder (`checkpoints/`, `loras/`, and so on). Those files are
  job attachments. Each one is uploaded once, and later jobs reuse it. On the worker, **Model
  Directory** is the folder those files are mapped to, and ComfyUI loads each model by the name
  already in the workflow. The job doesn't download models from the internet. When a model isn't on
  this machine, the panel warns about it, and the job fails unless the workers already have the
  model, such as in a shared folder that a [custom job template](#configuration)'s
  **Extra Model Paths Config** points at. In the panel, **Advanced settings > Attachments** lists
  the models it will attach. The panel lists each model by its folder and name, such as
  `loras/style.safetensors`, since two folders can hold files of the same name. Remove one with
  its **✕** when the workers already have it, and it isn't uploaded.
- **Custom nodes.** The submitter attaches the custom node pack of every custom node the workflow
  runs, however it was installed: from the Comfy Registry, a git checkout or your studio's own. It
  takes each pack from the folder ComfyUI loaded it from and places it under **Custom Nodes
  Directory**, without its `.git`, `__pycache__` or virtual environment folders. A single-file
  custom node is attached as its one `.py` file. On the worker, ComfyUI loads the packs from where
  they're mapped, and the job installs each pack's `requirements.txt` into a virtual environment
  layered on the conda environment. pip can't replace the conda environment's packages, such as
  `torch`, but a pack's own code, and any package pip builds from source, runs as the job's user
  and can write wherever that user can. When a pack's files can't be found
  on this machine, the panel warns about it. The job fails with the import error when a custom
  node doesn't load.

  A pack's `requirements.txt` can list packages, with extras, version specifiers, markers and
  `--hash` options, and `name @ URL` requirements for `https` and `git+https` URLs, such as
  `pkg @ git+https://github.com/org/pkg.git`. Every attached pack's file is checked before any
  are installed, and the job fails when one has a pip option, such as `--extra-index-url` or `-r`,
  a local path or file, an environment variable, another kind of URL, or isn't UTF-8 text. pip
  installs a checked copy of each file, never the pack's own. It still uses the package index the
  workers are set up with, such as through `PIP_INDEX_URL` in a queue environment.

  Anyone who can use the queue can read a job's attachments. Staging leaves out files named like
  credential stores, such as `.env`, `*.pem`, `.npmrc`, `hf_token` and `service_account.json`,
  and links that point outside the pack. A pack can still keep a key somewhere else, such as in
  its `config.json`. Check the packs listed under **Advanced settings > Attachments**, and remove
  one with its **✕** to leave it out of the job. The job then fails unless the workers already
  have that pack. Each pack is listed with its size and file count. A job that uses one of its
  nodes uploads the whole pack. The panel warns about a pack over 1 GB or 10,000 files, such as
  one that downloads models into its own folder.

  When the workers already provide every custom node and its dependencies, such as through a
  custom nodes conda package added to **Conda Packages**, a
  [custom job template](#configuration) can set **Attach Custom Nodes** to false by default. The
  job then ignores **Custom Nodes Directory**, and the packs aren't uploaded.

## Workflows with lists

A workflow runs as one task, as ComfyUI does locally. Every item of its lists, such as the items
wired into a **Create List** or the images in a **Load Image (from Folder)** folder, runs in that
task.

## Host requirements

The panel's **Advanced settings > Host requirements** says which workers can run the job. It starts
from the job template's. The extension's template sets none, so the job runs on any worker in the
queue's fleets unless you set some. Changing a field replaces the template's requirements with
what's in the form, and **Reset all to defaults** goes back to them. Clearing every field lets any
worker run the job. The form starts from the template's first step, and what you set applies to
every step of the job.

Ask for workers with a good GPU. A starting point is a minimum of 1 for **GPUs**, and a
**GPU memory (GiB)** minimum that fits the workflow's models. With no host requirements, a task can
land on a worker without a GPU. The job also needs `bash` on the worker, and `python`, which the
queue's conda environment provides with the `comfy` package. The `comfy` conda packages for
service-managed fleets are built for Linux only, so choose **linux** under **Operating system** when
the queue's fleets have other workers too.

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

**Add amount** and **Add attribute** are under **Custom host requirements**, for capabilities your
farm defines on its workers. If no worker in the queue's fleets meets the requirements, the job's
tasks wait until one does.

Large models need a bigger worker. On a 16 GiB GPU such as a g4dn's T4, a video model with a large
text encoder can run out of memory or take hours. For those workflows, ask for at least 24 GiB of
GPU memory and 64 GiB of memory, which means a g5.4xlarge (A10G) or larger.

## Settings saved in the workflow

When you submit, the panel saves the job's settings in the workflow, and loads them again when you
open it, so you set them once for each workflow. It saves:

- the job name and description, if you changed them
- the priority, initial state, **Maximum retries per task**, **Maximum failed tasks count** and
  **Maximum worker count**
- **Server Start Timeout** and **Workflow Timeout**, and a custom job template's other number,
  checkbox and dropdown parameters, if you changed them
- the **Output Directory** you chose, as its absolute path. Anyone who gets the workflow, or an
  image that ComfyUI saves it in, can see that path.
- the host requirements, if you changed them

It doesn't save the queue environment's parameters, such as **Conda Channels**, since they belong
to the queue, or the farm, queue and storage profile, anything under **Attachments**, or the job
template's hidden parameters, such as **Input Directory**. Settings that an older workflow saved
for fields the panel no longer shows, such as **Attach Custom Nodes**, are ignored.

If the workflow is saved to a file and had no other unsaved changes, the panel saves it for you.
Otherwise the settings are saved with the workflow the next time you save it. A workflow you share
carries its settings, and so does an image that ComfyUI saves with the workflow in it.

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
  with a header that a reverse proxy adds, such as `Forwarded`, `X-Forwarded-For`, `X-Real-IP` or
  `Via`, gets a 403. A proxy or port forwarder on this machine that adds none of these headers
  can't be told apart from a local client, so don't put one in front of ComfyUI.
- They only accept requests addressed to `localhost` or a loopback address such as `127.0.0.1`,
  so a website that points its own domain name at this machine can't pass for ComfyUI's page.
  Names under `.localhost`, and names with a trailing dot, are refused too.
- They refuse requests that a browser marks as coming from another website, even when
  `--enable-cors-header` turns off ComfyUI's own check. When an `Origin` header is present, its
  scheme, host and port must match the ComfyUI request, even without browser fetch metadata.
- They only accept JSON, which a page from another website can't send without the server's
  permission.
- Submission progress, confirmation prompts and errors are sent to the browser client ID that
  started them, not to every tab connected to the server. A client ID isn't a secret: ComfyUI
  lists it in the queue and history, and anyone who can reach ComfyUI can connect with it, so this
  keeps tabs apart rather than protecting the messages.
- A submission requires a valid browser client ID. Only that client can confirm or cancel it,
  and confirmation answers are accepted only while a prompt is pending. Once the job is being
  created, Cancel can't stop it, and a job that was created is always reported, even when a later
  step, such as waiting for it, fails.
- Each confirmation prompt has its own random ID, and an answer has to name the prompt that's
  pending now. A late or repeated answer, to a prompt that was already answered or that was
  declined because nobody answered it in time, gets a 409 and doesn't answer a later prompt. Only
  `"accept": true` or `false` is an answer: any other value gets a 400, and the prompt stays
  pending.
- Submit stays off until the details of the chosen farm and queue, their parameters and storage
  profiles, have loaded. Changing the queue forgets the previous queue's details, the queue
  parameters you edited for it and its storage profile, and an answer for a queue that's no
  longer chosen, or one that a newer request for the same queue replaced, is ignored. So a job is
  never submitted with another queue's parameters, or without its storage profile because the
  profiles hadn't loaded yet.
- A storage profile of **None** that you choose stays None, even when the panel reloads your
  defaults. A default or earlier storage profile that the chosen queue doesn't list is cleared,
  and Submit stays off, and says why, until you choose a profile or None. Without a storage
  profile, files on shared storage are uploaded too.
- The files you add or remove under **Advanced settings > Attachments**, and the models and
  custom node packs you remove, belong to the workflow tab you changed them in. Another
  workflow's job doesn't get them, and a file dialog that closes after you've opened another
  workflow adds nothing.
- Input files are uploaded to the queue's job attachments bucket, where anyone who can use the
  queue can read them. The submitter only attaches files that the workflow's file inputs and
  folder loaders name inside ComfyUI's input, output and temp folders. It checks them with links
  resolved, so a shared workflow can't attach other files on this machine.
- The job template's hidden paths, Input Directory, Model Directory and Custom Nodes Directory,
  are set by the extension, not by the request, so a request can't point a job's custom nodes at
  another folder.
- Job bundles in `ComfyUI/user/deadline_submissions/` hold your workflows, prompts and file paths.
  On Linux and macOS that folder and each bundle in it can only be opened by the user that runs
  ComfyUI (mode `0700`), and the files the extension writes there can only be read by that user
  (`0600`), whatever the umask. Models and custom nodes staged in a bundle are often links to
  your files and keep their permissions, but they can only be reached through the bundle, which
  is deleted once its job is created. On Windows, bundles inherit the permissions of the ComfyUI
  user folder. The `deadline_cloud_submitter.json` config file is also `0600`.
- Custom node packs are uploaded with their files to the queue's job attachments bucket, where
  anyone who can use the queue can read them. Credential files, such as `.env`, `*.key` and `*.pem`,
  are left out. A pack's settings files, such as `config.json`, are attached as they are, so keep
  API keys in environment variables on the workers instead.

**Local access.** Use a browser on the machine running ComfyUI and access it through
`localhost`.

The Host and Origin checks stay enabled. DNS rebinding can let another website call a reachable
server by pointing its domain name at that server. The routes require a local Host and reject
any Origin that does not match the request's scheme, host and port.

## Limitations

- The panel can't log out or switch AWS profile. Use `deadline config gui` for those.
- Adding attachments, or **Browse…** for a path, opens a file dialog on the ComfyUI machine's
  desktop: osascript on macOS, PowerShell on Windows and zenity on Linux.
- Deadline Cloud monitor login opens on the ComfyUI server's desktop, not in the browser.
- Custom nodes are attached from this machine. Each job uploads your local copy, and the worker
  runs it. A single-file node's own imports still have to exist on the worker, since it has no
  `requirements.txt` to install.
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
