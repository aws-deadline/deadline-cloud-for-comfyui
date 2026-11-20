// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// Runs one scenario against the real deadline_ui.js with ComfyUI stubbed out, and prints what
// happened as JSON. test_panel.py lays out the files and checks the result.
import { webcrypto } from "node:crypto";
import { app } from "./scripts/app.js";
import { api } from "./scripts/api.js";

globalThis.crypto ??= webcrypto;
const ui = await import("./extension/web/deadline_ui.js");
const state = ui.__state;
ui.listenForSubmissions();

const settle = () => new Promise((resolve) => setTimeout(resolve, 10));
function deferred() {
  let resolve;
  const promise = new Promise((done) => (resolve = done));
  return { promise, resolve };
}
function planFor(body) {
  const image = body.workflow[1].inputs.image;
  return {
    name: "fox",
    parameters: [],
    values: {},
    sources: [],
    attachments: { filenames: [`/input/${image}`], directories: [] },
  };
}
function jobs() {
  return api.calls
    .filter(({ path }) => path === "/deadline/jobs")
    .map(({ body }) => ({
      image: body.workflow[1].inputs.image,
      attachments: body.attachments.filenames,
      submissionId: body.submissionId,
      clientId: body.clientId,
    }));
}

api.handlers["/deadline/cloud/status"] = async () => ({
  authenticationStatus: "AUTHENTICATED",
  farmId: "farm-1",
  queueId: "queue-1",
});
api.handlers["/deadline/cloud/farms"] = async () => ({ farms: [] });
api.handlers["/deadline/plan"] = async (body) => planFor(body);
api.handlers["/deadline/jobs"] = async (body) => ({ submissionId: body.submissionId });
await ui.loadAccount(true);
await ui.loadPlan(true);

const result = {};
const scenario = process.argv[2];

if (scenario === "edited-before-replanning") {
  // The artist edits the graph and submits before the debounced plan request starts.
  app.image = "new.png";
  await ui.submit();
  await settle();
  result.jobsAfterEdit = jobs();
  result.replanned = api.calls.filter(({ path }) => path === "/deadline/plan").length === 2;
  result.planClientIds = api.calls
    .filter(({ path }) => path === "/deadline/plan")
    .map(({ body }) => body.clientId);
  await ui.submit();
  result.jobs = jobs();
} else if (scenario === "edited-while-planning") {
  // The plan for the edited graph is still loading when the artist submits.
  app.image = "new.png";
  const slow = deferred();
  api.handlers["/deadline/plan"] = async (body) => {
    await slow.promise;
    return planFor(body);
  };
  const planning = ui.loadPlan();
  await settle();
  result.canSubmitWhilePlanning = Boolean(ui.canSubmit());
  await ui.submit();
  result.jobsWhilePlanning = jobs();
  slow.resolve();
  await planning;
  await ui.submit();
  result.jobs = jobs();
} else if (scenario === "queue-template-value") {
  // Conda Packages is a queue parameter that the job template also defines, as comfy.
  state.queue = {
    parameters: [{ name: "CondaPackages", type: "STRING", default: "" }],
    storageProfiles: [],
  };
  state.plan.parameters = [
    { name: "CondaPackages", type: "STRING", default: "comfy", userInterface: { control: "HIDDEN" } },
  ];
  const sent = async () => {
    state.submission = null;
    await ui.submit();
    await settle();
    return api.calls.at(-1).body.parameters.CondaPackages ?? null;
  };
  const field = state.queue.parameters[0];
  result.shown = ui.__queueParameterValue(field);
  result.unedited = await sent();
  ui.editQueueParameter("CondaPackages", "comfy my-nodes");
  result.edited = await sent();
  ui.editQueueParameter("CondaPackages", "");
  result.cleared = await sent();
  result.shownAfterClearing = ui.__queueParameterValue(field);
} else if (scenario.startsWith("queue-")) {
  // The queue environment's Conda Channels, edited and then cleared or reset in the panel.
  state.queue = {
    parameters: [{ name: "CondaChannels", type: "STRING", default: "deadline-cloud" }],
    storageProfiles: [],
  };
  if (scenario === "queue-configured") {
    state.plan.queueValues = { CondaChannels: "s3://bucket/Conda/Default conda-forge" };
  }
  ui.editQueueParameter("CondaChannels", "my-channel");
  await ui.submit();
  await settle();
  result.edited = api.calls.at(-1).body.parameters.CondaChannels ?? null;
  state.submission = null;
  ui.editQueueParameter("CondaChannels", "");
  await ui.submit();
  await settle();
  result.cleared = "CondaChannels" in api.calls.at(-1).body.parameters
    ? api.calls.at(-1).body.parameters.CondaChannels
    : null;
} else if (scenario === "save-bundle") {
  api.handlers["/deadline/bundle"] = async () => ({
    job_bundle: "/bundle",
    command: "deadline bundle submit /bundle --farm-id farm-1",
  });
  state.jobName = "Fox lookdev";
  await ui.__saveBundle();
  const { body } = api.calls.find(({ path }) => path === "/deadline/bundle");
  result.sent = {
    farmId: body.farmId,
    queueId: body.queueId,
    storageProfileId: body.storageProfileId,
    name: body.settings.name,
  };
  result.toasts = app.toasts.map(({ detail }) => detail);
} else if (scenario.startsWith("early-")) {
  // The server's event arrives before the response to the submission request.
  const response = deferred();
  api.handlers["/deadline/jobs"] = async (body) => {
    await response.promise;
    return { submissionId: body.submissionId };
  };
  const submitting = ui.submit();
  await settle();
  const [{ submissionId }] = jobs();
  const detail = { submissionId, name: "ComfyUI - fox" };
  try {
    if (scenario === "early-confirm") {
      api.emit("deadline.confirm", { ...detail, message: "Upload 5 GB?", default: true });
    } else {
      api.emit("deadline.submitted", { ...detail, status: "SUBMITTED", jobId: "job-1" });
    }
  } catch {
    // Without a document, opening the dialog for the confirmation fails after it's recorded.
  }
  response.resolve();
  await submitting;
  result.submission = state.submission;
  result.canSubmit = Boolean(ui.canSubmit());
  result.toasts = app.toasts.map(({ severity }) => severity);
} else if (scenario === "all-attachments") {
  // Render the real section and click its controls, including rows beyond the first 100.
  class NodeStub {
    constructor(tag) {
      this.tag = tag;
      this.children = [];
      this.attributes = {};
      this.listeners = {};
      this.scrollTop = 0;
      this.open = false;
    }
    append(child) {
      this.children.push(child);
    }
    setAttribute(name, value) {
      this.attributes[name] = value;
    }
    getAttribute(name) {
      return this.attributes[name] ?? null;
    }
    replaceChildren(...children) {
      this.children = children;
    }
    querySelectorAll(selector) {
      return descendants(this).slice(1).filter((node) => {
        if (selector === "details") return node.tag === "details";
        if (selector === "[data-dl-scroll]") return node.getAttribute("data-dl-scroll") !== null;
        throw new Error(`Unsupported selector: ${selector}`);
      });
    }
    addEventListener(name, handler) {
      this.listeners[name] = handler;
    }
  }
  globalThis.Node = NodeStub;
  globalThis.document = { createElement: (tag) => new NodeStub(tag) };
  const descendants = (node) => [
    node,
    ...node.children.filter((child) => child instanceof NodeStub).flatMap(descendants),
  ];
  const container = new NodeStub("div");
  const rows = () => descendants(container).filter((node) => node.tag === "li");
  const list = (key) => container.querySelectorAll("[data-dl-scroll]")
    .find((node) => node.getAttribute("data-dl-scroll") === key);
  const click = (path) => rows().find((row) => row.attributes.title === path)
    .children.find((child) => child.tag === "button").listeners.click();
  const external = "/outside/review-me.png";
  const manualFile = "/manual/clip.mov";
  const manualFolder = "/manual/frames";
  const planned = Array.from({ length: 105 }, (_, index) => `/input/image-${index}.png`);
  state.plan.attachments = { filenames: [...planned, external], directories: ["/outside/frames"] };
  state.addedAttachments = { filenames: [manualFile], directories: [manualFolder] };
  const view = ui.__createView(container, ["attachments"]);
  container.querySelectorAll("details")[0].open = true;
  list("inputs").scrollTop = 640;
  result.listed = rows().map((row) => row.attributes.title);
  click(external);
  result.scrollAfterRemoving = list("inputs").scrollTop;
  result.removedRowClass = rows().find((row) => row.attributes.title === external).className;
  await ui.submit();
  result.sentAfterRemoving = api.calls.filter(({ path }) => path === "/deadline/jobs").at(-1).body.attachments;
  state.submission = null;
  click(external);
  click(manualFile);
  click(manualFolder);
  result.scrollAfterRestoring = list("inputs").scrollTop;
  result.listedAfterRemovingManual = rows().map((row) => row.attributes.title);
  await ui.submit();
  result.sentAfterRestoring = api.calls.filter(({ path }) => path === "/deadline/jobs").at(-1).body.attachments;
  state.plan.localModels = Array.from({ length: 40 }, (_, index) => ({
    id: `checkpoints/model-${index}.safetensors`,
    path: `/models/model-${index}.safetensors`,
    name: `model-${index}.safetensors`,
    directory: "checkpoints",
  }));
  view.render(["attachments"]);
  result.inputScrollAfterAddingModels = list("inputs").scrollTop;
  result.newModelScroll = list("models").scrollTop;
  list("models").scrollTop = 320;
  click("/models/model-30.safetensors");
  result.scrollAfterRemovingModel = { inputs: list("inputs").scrollTop, models: list("models").scrollTop };
  click("/models/model-30.safetensors");
  result.scrollAfterRestoringModel = { inputs: list("inputs").scrollTop, models: list("models").scrollTop };
  state.plan.localModels = [];
  view.render(["attachments"]);
  result.inputScrollAfterModelsDisappear = list("inputs").scrollTop;
  result.attachmentsStillOpen = container.querySelectorAll("details")[0].open;
} else if (scenario === "same-name-models") {
  // A checkpoint and a LoRA share a file name. Removing one leaves the other attached.
  const model = (directory) => ({
    id: `${directory}/shared.safetensors`,
    name: "shared.safetensors",
    directory,
    path: `/models/${directory}/shared.safetensors`,
  });
  state.plan.localModels = [model("checkpoints"), model("loras")];
  state.removedAttachments.add("/models/loras/shared.safetensors");
  await ui.submit();
  await settle();
  result.excludedModels = api.calls.filter(({ path }) => path === "/deadline/jobs").at(-1).body.excludedModels;
} else if (scenario.startsWith("poll-")) {
  // Recent jobs' status, polled through the bridge's jobs command.
  const job = { jobId: "job-1", name: "fox", farmId: "farm-1", queueId: "queue-1", submittedAt: "" };
  state.jobs = [job];
  let running = 0;
  result.mostAtOnce = 0;
  const answer = deferred();
  let calls = 0;
  api.handlers["/deadline/cloud/jobs"] = async (body) => {
    calls += 1;
    running += 1;
    result.mostAtOnce = Math.max(result.mostAtOnce, running);
    if (scenario === "poll-overlapping") {
      await answer.promise;
    }
    running -= 1;
    const runningJob = { jobId: "job-1", taskRunStatus: "RUNNING", taskRunStatusCounts: { SUCCEEDED: 1 } };
    const status =
      scenario === "poll-error"
        ? { jobId: "job-1", error: "Not found", notFound: true }
        : scenario === "poll-transient" && calls === 2
          ? { jobId: "job-1", error: "Rate exceeded" }
          : runningJob;
    return { jobs: [status] };
  };
  if (scenario === "poll-overlapping") {
    // A job is submitted while the panel's first poll is still waiting for the bridge.
    const first = ui.__pollJobs();
    const second = ui.__pollJobs();
    const third = ui.__pollJobs();
    await settle();
    answer.resolve();
    await Promise.all([first, second, third]);
  } else if (scenario === "poll-transient") {
    // The second poll fails for a moment, such as when throttled.
    await ui.__pollJobs();
    await ui.__pollJobs();
    result.statusAfterError = state.jobStatus["job-1"].taskRunStatus ?? null;
    await ui.__pollJobs();
  } else if (scenario === "poll-after-account") {
    // Opening the panel loads the account, then polls straight away, not at the next timer.
    await ui.loadAccount(true);
    await settle();
  } else {
    await ui.__pollJobs();
    await ui.__pollJobs();
  }
  result.polls = api.calls.filter(({ path }) => path === "/deadline/cloud/jobs").length;
} else if (scenario === "host-round-trip") {
  // Editing the form rebuilds the requirements from it, so it has to hold every template value.
  const requirements = {
    attributes: [
      { name: "attr.worker.os.family", anyOf: ["linux"] },
      { name: "attr.worker.cpu.arch", anyOf: ["x86_64", "aarch64"] },
    ],
    amounts: [{ name: "amount.worker.gpu", min: 1 }],
  };
  result.requirements = ui.__requirementsFromForm(ui.__formFromRequirements(requirements));
} else if (scenario === "download-nothing") {
  state.jobs = [{ jobId: "job-1", name: "fox", farmId: "farm-1", queueId: "queue-1", submittedAt: "" }];
  const message = "There are no output files available for download at this moment.";
  api.emit("deadline.download", { jobId: "job-1", status: "DOWNLOADED", message, fileCount: 0 });
  result.toasts = app.toasts.map(({ severity, detail }) => ({ severity, detail }));
} else if (scenario === "submitted-output-dir") {
  api.emit("deadline.submitted", {
    submissionId: "s-1",
    status: "SUBMITTED",
    name: "fox",
    jobId: "job-1",
    farmId: "farm-1",
    queueId: "queue-1",
    outputDir: "/comfy/output",
  });
  result.outputDir = state.jobs[0].outputDir;
} else if (scenario.startsWith("downloaded-to-")) {
  // The CLI's folder wins. Without one, the folder the job was submitted with is named.
  const job = { jobId: "job-1", name: "fox", farmId: "farm-1", queueId: "queue-1", submittedAt: "" };
  state.jobs = [scenario === "downloaded-to-unknown" ? job : { ...job, outputDir: "/comfy/output" }];
  const directory = scenario === "downloaded-to-cli-folder" ? "/comfy/output/fox" : undefined;
  const message = "Downloaded 2 files";
  api.emit("deadline.download", { jobId: "job-1", status: "DOWNLOADED", message, fileCount: 2, directory });
  result.toasts = app.toasts.map(({ detail }) => detail);
}
console.log(JSON.stringify(result));
process.exit(0);
