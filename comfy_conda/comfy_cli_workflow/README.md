# ComfyUI workflow with comfy-cli

This job bundle runs a [ComfyUI](https://github.com/Comfy-Org/ComfyUI) workflow headlessly on
AWS Deadline Cloud GPU workers with [comfy-cli](https://github.com/Comfy-Org/comfy-cli), the
official ComfyUI command line tool. It runs one task per seed, and it uses the `comfy` conda
package, which provides ComfyUI and comfy-cli.

```
comfy_cli_workflow/
├── README.md
├── template.yaml
└── workflows/
    └── sd15_txt2img_api.json   # Default workflow: Stable Diffusion 1.5 text to image
```

## How it works

1. **The job environment** runs once per session, before the session's first task:
   - It downloads any models listed in `ModelDownloads` that aren't in the model cache yet,
     and verifies them against their SHA-256 checksums.
   - It writes an `extra_model_paths.yaml` that points ComfyUI at the cache.
   - It starts ComfyUI in the background with `comfy launch`, on a free local port, with the
     base directory in the session directory and the output directory set to `OutputDir`.
     It then waits until the server answers.
2. **Each task**:
   - runs `comfy run --print-prompt` to get the workflow in API format. comfy-cli converts
     workflows saved in the regular UI format, so you don't have to export them for the API;
   - sets every `seed` and `noise_seed` input to the task's seed, and adds `_seed<N>` to each
     `filename_prefix` so tasks don't overwrite each other's outputs;
   - runs the workflow with `comfy run --wait` and lists the output files.

   Tasks in the same session reuse the running server, so they don't load models again.
3. **When the session ends**, the job environment stops the server with `comfy stop`. Job
   attachments collects the files in `OutputDir`.

## Prerequisites

- **The `comfy` conda package** in an S3 conda channel that your queue can read. Build it from
  the [ComfyUI (comfy-cli install) recipe](../comfy/README.md).
- **A conda queue environment** whose `CondaChannels` lists that channel before `conda-forge`.
  The job sets `CondaPackages` to `comfy`.
- **A Linux GPU fleet** whose runtime driver is `latest` or `grid:r580`. The job requires one GPU.
- **Outbound internet access** on the workers, to download the default model from Hugging Face.

## Submitting the job

From the `comfy_conda` directory, submit the default workflow:

```
deadline bundle submit comfy_cli_workflow
```

To render more seeds, or your own workflow, saved normally or exported for the API:

```
deadline bundle submit comfy_cli_workflow \
    -p WorkflowFile=/path/to/my_workflow.json \
    -p Seeds=1-16
```

## Parameters

| Parameter | Default | Description |
|---|---|---|
| `WorkflowFile` | `workflows/sd15_txt2img_api.json` | A ComfyUI workflow, in UI or API format. |
| `Seeds` | `1-4` | The seeds to run, one task per seed. |
| `OutputDir` | `./output` | Where ComfyUI writes the outputs of Save nodes. |
| `ModelDownloads` | Stable Diffusion 1.5 (fp16) | Models to download into the cache, as space-separated `FOLDER/FILE=URL#sha256=HEX` entries. |
| `ModelCacheDir` | empty | The model cache directory on the worker. Empty uses the fleet's persistent volume, or `/var/tmp`. |
| `ExtraModelPathsConfig` | empty | An `extra_model_paths.yaml` on the worker, for model and `custom_nodes` folders on shared storage. |
| `ComfyUIArgs` | empty | Extra arguments for the ComfyUI server, such as `--lowvram`. |
| `ServerStartTimeout` | `600` | Seconds to wait for the server to start. |

## Models and custom nodes

Each worker downloads a model only once. Models are cached in
`$DEADLINE_PERSISTENT_MOUNT/comfyui-models` when the fleet has a
[persistent volume](https://docs.aws.amazon.com/deadline-cloud/latest/developerguide/smf-persistent-storage-dev.html),
and otherwise in `/var/tmp/comfyui-models`. For models and custom nodes on shared storage, write
an `extra_model_paths.yaml` that points at them and pass its path on the worker with
`ExtraModelPathsConfig`.

Don't use `comfy node install` or `comfy model download` into the conda environment in jobs. The
environment is shared and cached between jobs.

## License

ComfyUI and comfy-cli are distributed under the GPL-3.0 license. The default model,
[Stable Diffusion 1.5](https://huggingface.co/Comfy-Org/stable-diffusion-v1-5-archive), is
distributed under the CreativeML OpenRAIL-M license, which restricts some uses. Review the
license of every model you use.
