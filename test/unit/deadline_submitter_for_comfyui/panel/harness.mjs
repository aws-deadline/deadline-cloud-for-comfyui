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
// Submits again, as a new submission, and returns its request.
async function submitAgain() {
  state.submission = null;
  const before = api.calls.length;
  await ui.submit();
  await settle();
  return api.calls.slice(before).find(({ path }) => path === "/deadline/jobs")?.body ?? null;
}
function emitConfirmation(message = "Upload?", defaultAnswer = true, promptId = "prompt-1") {
  try {
    api.emit("deadline.confirm", {
      submissionId: state.submission.id,
      message,
      default: defaultAnswer,
      promptId,
    });
  } catch {
    // Without a document, opening the dialog fails after the prompt has been recorded.
  }
}
function confirmationAnswers() {
  return api.calls.filter(({ path }) => path === "/deadline/jobs/confirm").map(({ body }) => body);
}
// Opens another workflow tab, or goes back to one, as ComfyUI does, and reads it.
async function openWorkflow(filename) {
  app.extensionManager.workflow.activeWorkflow = { filename, path: `workflows/${filename}` };
  ui.workflowLoaded();
  await ui.loadPlan(true);
}
// A stand-in for the DOM, enough to render the panel's sections and click their controls.
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
const descendants = (node) => [
  node,
  ...node.children.filter((child) => child instanceof NodeStub).flatMap(descendants),
];
const text = (node) =>
  node.children.map((child) => (child instanceof NodeStub ? text(child) : String(child))).join("");
function installDom() {
  globalThis.Node = NodeStub;
  globalThis.document = { createElement: (tag) => new NodeStub(tag) };
}
// What a view shows with the data-role, or null.
function shownText(container, role) {
  const node = descendants(container).find((child) => child.getAttribute?.("data-role") === role);
  return node ? text(node) : null;
}

api.handlers["/deadline/cloud/status"] = async () => ({
  authenticationStatus: "AUTHENTICATED",
  farmId: "farm-1",
  queueId: "queue-1",
});
// The account's default farm and queue are listed, so the panel shows them as chosen.
api.handlers["/deadline/cloud/farms"] = async () => ({ farms: [{ farmId: "farm-1", displayName: "Farm" }] });
api.handlers["/deadline/cloud/queues"] = async () => ({
  queues: [{ queueId: "queue-1", displayName: "Queue" }],
});
api.handlers["/deadline/cloud/queue"] = async () => ({ parameters: [], storageProfiles: [] });
api.handlers["/deadline/cloud/set-defaults"] = async () => ({});
api.handlers["/deadline/plan"] = async (body) => planFor(body);
api.handlers["/deadline/jobs"] = async (body) => ({ submissionId: body.submissionId });
await ui.loadAccount(true);
await ui.loadPlan(true);

const result = {};
const scenario = process.argv[2];

if (scenario === "answer-confirmation") {
  state.submission = { id: "a".repeat(32), starting: false };
  emitConfirmation();
  api.handlers["/deadline/jobs/confirm"] = async () => ({});
  await ui.__answerConfirmation(true);
  // Clicking again cannot send an answer once the pending prompt has been cleared.
  await ui.__answerConfirmation(true);
  result.answers = confirmationAnswers();
  result.pending = state.submission.confirm;
} else if (scenario === "answer-while-next-prompt-arrives") {
  state.submission = { id: "a".repeat(32), starting: false };
  emitConfirmation();
  const firstResponse = deferred();
  api.handlers["/deadline/jobs/confirm"] = async (body) => {
    if (body.accept) {
      emitConfirmation("Run hooks?", false, "prompt-2");
      await firstResponse.promise;
    }
    return {};
  };
  const firstAnswer = ui.__answerConfirmation(true);
  await settle();
  result.answersWhileFirstPending = confirmationAnswers();
  result.nextPrompt = state.submission.confirm;
  firstResponse.resolve();
  await firstAnswer;
  // Completing the old answer must preserve the next prompt until it is answered separately.
  await ui.__answerConfirmation(false);
  result.answers = confirmationAnswers();
  result.pending = state.submission.confirm;
} else if (scenario === "cancel-submission") {
  state.submission = { id: "a".repeat(32), starting: false };
  api.handlers["/deadline/jobs/cancel"] = async () => ({});
  await ui.__cancelSubmission();
  result.cancels = api.calls.filter(({ path }) => path === "/deadline/jobs/cancel").map(({ body }) => body);
} else if (scenario === "edited-before-replanning") {
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
      api.emit("deadline.confirm", {
        ...detail,
        message: "Upload 5 GB?",
        default: true,
        promptId: "prompt-1",
      });
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
  installDom();
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
  ui.__attachmentEdits().added = { filenames: [manualFile], directories: [manualFolder] };
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
  // A custom node pack is removed and added back like a model, and the status warns about it.
  const pack = "/custom_nodes/studio-nodes";
  const localNodes = [{ name: "studio-nodes", path: pack, size: 3_200_000_000, files: 12_500, counted: true }];
  api.handlers["/deadline/plan"] = async (body) => ({ ...planFor(body), localNodes });
  await ui.loadPlan(true);
  view.render(["attachments"]);
  result.packRow = text(rows().find((row) => row.attributes.title === pack));
  result.largePackStatus = text(ui.__renderers.status());
  click(pack);
  result.packRowRemoved = rows().find((row) => row.attributes.title === pack).className;
  result.packStatus = text(ui.__renderers.status());
  state.submission = null;
  await ui.submit();
  result.excludedNodes = api.calls.filter(({ path }) => path === "/deadline/jobs").at(-1).body.excludedNodes;
  state.submission = null;
  click(pack);
  result.packStatusAfterRestoring = text(ui.__renderers.status());
  await ui.submit();
  result.excludedNodesAfterRestoring = api.calls.filter(({ path }) => path === "/deadline/jobs").at(-1).body.excludedNodes;
} else if (scenario === "same-name-models") {
  // A checkpoint and a LoRA share a file name. Removing one leaves the other attached.
  const model = (directory) => ({
    id: `${directory}/shared.safetensors`,
    name: "shared.safetensors",
    directory,
    path: `/models/${directory}/shared.safetensors`,
  });
  state.plan.localModels = [model("checkpoints"), model("loras")];
  ui.__attachmentEdits().removed.add("/models/loras/shared.safetensors");
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
} else if (scenario.startsWith("task-lists")) {
  // A Create List and a folder of images, which run in every combination.
  const source = (name, kind, values) => ({
    name,
    parameter: `${name}Items`,
    label: name,
    title: name,
    kind,
    values,
    count: values.length,
    range: `1-${values.length}`,
  });
  const sources = [
    source("List1", "list", ["fox", "cat", "owl"]),
    source("Folder2", "folder", ["a.png", "b.png", "c.png", "d.png", "e.png"]),
  ];
  // A workflow runs as one task until the artist chooses to split its lists into tasks.
  result.splitByDefault = ui.splitsTasks();
  ui.setSplit(true);
  result.counts = ["1-4", "1-20:2", "7, 42, 99", "4-1", "x", "0", ""].map(ui.countRange);
  // OpenJD refuses overlapping parts and empty parts, so the panel does too.
  result.openjdCounts = ["1-3,2", "2,2", "1-5:2,2", "1,,2", "1,", "3,1", "1-5:2,6"].map(ui.countRange);
  result.tasks = ui.taskCount(sources, (item) => item.range);
  result.previews = ["2-5", "0", "x", "3,1"].map((value) => ui.itemsPreview(sources[1], value));
  // A field holds item numbers from 1 to the list's count, or 0 to run that list whole in each task.
  result.listCounts = ["1-3", "2,3", "0", "2-5", "-1", "0,1", "1-3:2", "1-4:3"].map((value) =>
    ui.taskCount([sources[0]], () => value)
  );
  // The panel sends each field's range as its job parameter, and won't submit a bad one.
  state.plan.sources = sources;
  state.plan.parameters = sources.map(({ parameter, range }) => ({
    name: parameter,
    type: "STRING",
    default: range,
    userInterface: { groupLabel: "Tasks" },
  }));
  state.edits.Folder2Items = "x";
  result.canSubmitBadRange = Boolean(ui.canSubmit());
  state.edits.Folder2Items = "1-2";
  result.canSubmit = Boolean(ui.canSubmit());
  await ui.submit();
  await settle();
  result.parameters = api.calls.filter(({ path }) => path === "/deadline/jobs").at(-1).body.parameters;
  result.splitRequest = api.calls.filter(({ path }) => path === "/deadline/jobs").at(-1).body.splitTasks;
  // One task: the job gets no list parameters, and the panel says not to split.
  state.submission = null;
  ui.setSplit(false);
  result.wholeTasks = ui.taskCount(sources, (item) => state.edits[item.parameter] ?? item.range);
  await ui.submit();
  await settle();
  const whole = api.calls.filter(({ path }) => path === "/deadline/jobs").at(-1).body;
  result.wholeParameters = whole.parameters;
  result.wholeRequest = whole.splitTasks;
  ui.setSplit(true);
  result.splitAgain = ui.taskCount(sources, (item) => state.edits[item.parameter] ?? item.range);
  // An edit follows its list until the list's items change.
  state.submission = null;
  const replan = async (values) => {
    api.handlers["/deadline/plan"] = async (body) => ({
      ...planFor(body),
      sources: [source("List1", "list", values)],
    });
    await ui.loadPlan(true);
    return state.edits.List1Items ?? null;
  };
  await replan(["fox", "cat", "owl"]);
  state.edits.List1Items = "2";
  state.itemEditBases.List1Items = ui.__itemEditBase(state.plan.sources[0]);
  result.editKept = await replan(["fox", "cat", "owl"]);
  result.editAfterChange = await replan(["fox", "cat", "hare"]);
  // Each workflow keeps its own choice: switching back to one keeps Multiple tasks, and another
  // workflow starts as one task.
  ui.setSplit(true);
  ui.workflowLoaded();
  result.splitWhileLoading = ui.splitsTasks();
  await ui.loadPlan(true);
  result.splitAfterSwitch = ui.splitsTasks();
  api.handlers["/deadline/plan"] = async (body) => ({ ...planFor(body), name: "hare" });
  ui.workflowLoaded();
  await ui.loadPlan(true);
  result.splitOtherWorkflow = ui.splitsTasks();
}
if (scenario === "dialog-sections") {
  result.sections = ui.__dialogSections;
}
if (scenario === "panel-sections") {
  result.sections = ui.__panelSections;
}
if (scenario === "switch-queue") {
  // Queue 1's details are loaded and its parameters edited. Then the artist chooses queue 2, whose
  // details are still loading.
  const queue2 = deferred();
  const parameters = (...names) => names.map((name) => ({ name, type: "STRING", default: "" }));
  api.handlers["/deadline/cloud/queues"] = async () => ({
    queues: [
      { queueId: "queue-1", displayName: "Queue 1" },
      { queueId: "queue-2", displayName: "Queue 2" },
    ],
  });
  api.handlers["/deadline/cloud/queue"] = (body) =>
    body.queueId === "queue-1"
      ? Promise.resolve({
          parameters: parameters("Queue1Parameter", "Shared"),
          storageProfiles: [{ storageProfileId: "sp-1", displayName: "Profile 1" }],
        })
      : queue2.promise;
  await ui.loadAccount(true);
  ui.chooseStorageProfile("sp-1");
  ui.editQueueParameter("Queue1Parameter", "for queue 1");
  ui.editQueueParameter("Shared", "edited for queue 1");
  const queue1 = await submitAgain();
  result.queue1Sent = { parameters: queue1.parameters, storageProfileId: queue1.storageProfileId };
  // That submission finished, so only the queue's details can keep Submit off.
  state.submission = null;
  result.canSubmitBeforeSwitching = Boolean(ui.canSubmit());
  const switching = ui.selectQueue("queue-2");
  await settle();
  result.queueWhileLoading = state.queue;
  result.queueParametersWhileLoading = ui.__renderers.queueParameters() ?? null;
  result.canSubmitWhileLoading = Boolean(ui.canSubmit());
  result.blockerWhileLoading = ui.submitBlocker();
  result.sentWhileLoading = await submitAgain();
  queue2.resolve({ parameters: parameters("Queue2Parameter", "Shared"), storageProfiles: [] });
  await switching;
  result.canSubmitLoaded = Boolean(ui.canSubmit());
  ui.editQueueParameter("Queue2Parameter", "for queue 2");
  const queue2Sent = await submitAgain();
  result.queue2Sent = {
    queueId: queue2Sent.queueId,
    parameters: queue2Sent.parameters,
    storageProfileId: queue2Sent.storageProfileId,
  };
}
if (scenario === "pending-queue-details") {
  // Submit waits for the chosen queue's details, and uses the latest request's answer.
  const answers = [deferred(), deferred()];
  let calls = 0;
  api.handlers["/deadline/cloud/queue"] = () => answers[calls++].promise;
  const first = ui.__loadQueue();
  await settle();
  result.canSubmitWhilePending = Boolean(ui.canSubmit());
  result.blockerWhilePending = ui.submitBlocker();
  result.sentWhilePending = await submitAgain();
  // The same queue's details again, such as after logging in, before the first answer comes.
  const second = ui.__loadQueue();
  answers[1].resolve({
    parameters: [{ name: "Newer" }],
    storageProfiles: [{ storageProfileId: "sp-new", displayName: "New" }],
  });
  await second;
  answers[0].resolve({ parameters: [{ name: "Older" }], storageProfiles: [] });
  await first;
  result.parameters = state.queue.parameters.map(({ name }) => name);
  result.profiles = state.queue.storageProfiles.map(({ storageProfileId }) => storageProfileId);
  result.canSubmitLoaded = Boolean(ui.canSubmit());
  result.blockerLoaded = ui.submitBlocker();
  // A failed load leaves Submit off, and says why.
  api.handlers["/deadline/cloud/queue"] = async () => {
    throw new Error("ThrottlingException");
  };
  await ui.__loadQueue();
  result.queueFailed = state.queue;
  result.canSubmitFailed = Boolean(ui.canSubmit());
  result.blockerFailed = ui.submitBlocker();
}
if (scenario.startsWith("storage-profile-")) {
  const status = (storageProfileId) => async () => ({
    authenticationStatus: "AUTHENTICATED",
    farmId: "farm-1",
    queueId: "queue-1",
    storageProfileId,
  });
  const profiles = [{ storageProfileId: "sp-1", displayName: "Profile 1" }];
  if (scenario === "storage-profile-none") {
    // The account's default profile is listed. The artist chooses None, which a reload keeps.
    api.handlers["/deadline/cloud/status"] = status("sp-1");
    const pending = deferred();
    api.handlers["/deadline/cloud/queue"] = () => pending.promise;
    const loading = ui.loadAccount(true);
    await settle();
    // While the queue's details load, the default stays chosen, and nothing can be submitted.
    result.whileLoading = {
      storageProfileId: state.storageProfileId,
      canSubmit: Boolean(ui.canSubmit()),
    };
    pending.resolve({ parameters: [], storageProfiles: profiles });
    await loading;
    result.defaultSent = (await submitAgain()).storageProfileId;
    ui.chooseStorageProfile("");
    api.handlers["/deadline/cloud/queue"] = async () => ({ parameters: [], storageProfiles: profiles });
    await ui.loadAccount(true);
    result.afterReload = state.storageProfileId;
    result.noneSent = (await submitAgain()).storageProfileId;
    result.savedDefaults = api.calls
      .filter(({ path }) => path === "/deadline/cloud/set-defaults")
      .map(({ body }) => body.storageProfileId);
  } else {
    // The account's default profile isn't one of the queue's, such as one that was deleted.
    api.handlers["/deadline/cloud/status"] = status("sp-gone");
    api.handlers["/deadline/cloud/queue"] = async () => ({ parameters: [], storageProfiles: profiles });
    await ui.loadAccount(true);
    result.storageProfileId = state.storageProfileId;
    result.canSubmit = Boolean(ui.canSubmit());
    result.blocker = ui.submitBlocker();
    result.sentBeforeChoosing = await submitAgain();
    // None from the select, which then has a placeholder too.
    ui.chooseStorageProfile("none");
    result.canSubmitAfterNone = Boolean(ui.canSubmit());
    result.blockerAfterNone = ui.submitBlocker();
    result.noneSent = (await submitAgain()).storageProfileId;
  }
}
if (scenario === "workflow-attachments") {
  // Files added to or removed from one workflow's attachments stay with that workflow's tab.
  const model = {
    id: "checkpoints/sd.safetensors",
    name: "sd.safetensors",
    directory: "checkpoints",
    path: "/models/sd.safetensors",
  };
  api.handlers["/deadline/plan"] = async (body) => ({ ...planFor(body), localModels: [model] });
  await openWorkflow("fox.json");
  api.handlers["/deadline/choose"] = async () => ({ paths: ["/manual/clip.mov"] });
  await ui.chooseAttachments("files", "filenames");
  ui.__attachmentEdits().removed.add("/input/old.png");
  ui.__attachmentEdits().removed.add(model.path);
  const sent = (body) => ({ attachments: body.attachments, excludedModels: body.excludedModels });
  result.fromA = sent(await submitAgain());
  await openWorkflow("hare.json");
  result.fromB = sent(await submitAgain());
  await openWorkflow("fox.json");
  result.backInA = sent(await submitAgain());
}
if (scenario === "late-file-dialog") {
  // The file dialog was opened for one workflow, and closes after the artist opened another.
  await openWorkflow("fox.json");
  const chosen = deferred();
  api.handlers["/deadline/choose"] = () => chosen.promise;
  const choosing = ui.chooseAttachments("files", "filenames");
  await settle();
  await openWorkflow("hare.json");
  chosen.resolve({ paths: ["/manual/late.png"] });
  await choosing;
  result.fromB = (await submitAgain()).attachments.filenames;
  await openWorkflow("fox.json");
  result.fromA = (await submitAgain()).attachments.filenames;
  result.toasts = app.toasts.map(({ severity }) => severity);
}
if (scenario === "attachment-summary") {
  // The sidebar and the dialog both show, next to Submit, what the job attaches.
  installDom();
  const sidebar = new NodeStub("div");
  const dialog = new NodeStub("div");
  const views = [ui.__createView(sidebar, ui.__panelSections), ui.__createView(dialog, ui.__dialogSections)];
  const model = (name) => ({
    id: `checkpoints/${name}`,
    name,
    directory: "checkpoints",
    path: `/models/${name}`,
  });
  const pack = { name: "studio-nodes", path: "/custom_nodes/studio-nodes", size: 10, files: 1, counted: true };
  api.handlers["/deadline/plan"] = async (body) => ({
    ...planFor(body),
    attachments: { filenames: ["/input/old.png", "/input/mask.png"], directories: [] },
    localModels: [model("a.safetensors"), model("b.safetensors")],
    localNodes: [pack],
  });
  await ui.loadPlan(true);
  result.planned = ui.attachmentSummary();
  for (const [kind, key, path] of [
    ["files", "filenames", "/outside/clip.mov"],
    ["folder", "directories", "/outside/frames"],
  ]) {
    api.handlers["/deadline/choose"] = async () => ({ paths: [path] });
    await ui.chooseAttachments(kind, key);
  }
  ui.__attachmentEdits().removed.add("/input/mask.png");
  ui.__attachmentEdits().removed.add("/models/b.safetensors");
  for (const view of views) {
    view.render(["submit"]);
  }
  result.summary = ui.attachmentSummary();
  result.sidebar = shownText(sidebar, "attachment-summary");
  result.dialog = shownText(dialog, "attachment-summary");
  const sent = await submitAgain();
  result.sent = {
    attachments: sent.attachments,
    excludedModels: sent.excludedModels,
    excludedNodes: sent.excludedNodes,
  };
}
if (scenario === "attachment-folders") {
  // A folder loader's folder of 5,000 files, and three files the workflow loads on their own.
  installDom();
  const container = new NodeStub("div");
  const rows = () => descendants(container).filter((node) => node.tag === "li");
  const row = (path) => rows().find((node) => node.attributes.title === path);
  const buttons = (path) => row(path).children.filter((child) => child.tag === "button");
  const click = (path, label) => buttons(path).find((button) => text(button) === label).listeners.click();
  const list = () => container.querySelectorAll("[data-dl-scroll]")
    .find((node) => node.getAttribute("data-dl-scroll") === "inputs");
  const folder = "/input/cats";
  const files = Array.from({ length: 5000 }, (_, index) => `${folder}/cat-${index}.png`);
  const loose = ["/input/fox.png", "/input/mask.png", "/input/owl.png"];
  state.plan.attachments = {
    filenames: [...loose, ...files],
    directories: [],
    folders: [{ path: folder, files }],
  };
  ui.__createView(container, ["attachments", "submit"]);
  const shown = () => ({
    rows: rows().length,
    folder: text(row(folder)),
    folderClass: row(folder).className,
    expanded: buttons(folder)[0].getAttribute("aria-expanded"),
    title: text(container.querySelectorAll("details")[0].children[0]),
    summary: ui.attachmentSummary(),
  });
  const sent = async () => (await submitAgain()).attachments;
  result.collapsed = { ...shown(), listed: rows().map((node) => node.attributes.title) };
  result.shownSummary = shownText(container, "attachment-summary");
  click(folder, "✕");
  result.folderRemoved = { ...shown(), sent: await sent() };
  click(folder, "Add back");
  result.folderRestored = { ...shown(), sent: await sent() };
  // Opening the folder lists its files by their paths in it.
  buttons(folder)[0].listeners.click();
  result.opened = {
    ...shown(),
    first: text(rows()[4]),
    firstClass: rows()[4].className,
  };
  list().scrollTop = 900;
  click(`${folder}/cat-42.png`, "✕");
  result.fileRemoved = {
    ...shown(),
    fileRow: text(row(`${folder}/cat-42.png`)),
    fileClass: row(`${folder}/cat-42.png`).className,
    scroll: list().scrollTop,
    sent: await sent(),
  };
  // Removing the folder and adding it back restores the file removed on its own too.
  click(folder, "✕");
  result.removedWhileOpen = { ...shown(), scroll: list().scrollTop, sent: await sent() };
  click(folder, "Add back");
  result.restoredWhileOpen = { ...shown(), sent: await sent() };
  buttons(folder)[0].listeners.click();
  result.closed = shown();
  // A folder the artist adds is attached whole, so the summary can't count its files.
  ui.__attachmentEdits().added.directories.push("/outside/frames");
  result.summaryWithAddedFolder = ui.attachmentSummary();
}
if (scenario === "late-queue-details") {
  // Each queue's details come from their own request. Queue 2's answer comes before queue 1's.
  const answers = { "queue-1": deferred(), "queue-2": deferred() };
  api.handlers["/deadline/cloud/queue"] = (body) => answers[body.queueId].promise;
  state.queueId = "queue-1";
  const first = ui.__loadQueue();
  state.queueId = "queue-2";
  const second = ui.__loadQueue();
  answers["queue-2"].resolve({
    parameters: [{ name: "Queue2Parameter" }],
    storageProfiles: [{ storageProfileId: "sp-2", displayName: "Profile 2" }],
  });
  await second;
  answers["queue-1"].resolve({ parameters: [{ name: "Queue1Parameter" }], storageProfiles: [] });
  await first;
  result.queueParameters = state.queue.parameters.map(({ name }) => name);
}
if (scenario === "target-not-listed") {
  // The configured default queue isn't in the farm's list, so the Queue select can't show it.
  api.handlers["/deadline/cloud/queues"] = async () => ({
    queues: [{ queueId: "queue-2", displayName: "Other queue" }],
  });
  state.queueId = "queue-1";
  state.storageProfileId = "sp-default";
  await ui.__loadQueues();
  result.queueId = state.queueId;
  result.storageProfileId = state.storageProfileId;
  result.canSubmitUnlisted = Boolean(ui.canSubmit());
  // A farm list that fails to load shows no farm as chosen either.
  api.handlers["/deadline/cloud/farms"] = async () => {
    throw new Error("AccessDenied");
  };
  state.queueId = "queue-2";
  state.farms = null;
  await ui.__loadFarms();
  result.canSubmitWithoutFarms = Boolean(ui.canSubmit());
}
if (scenario === "task-limit") {
  // Two lists whose items multiply past the most tasks a job can run, as the submitter allows.
  const source = (name, count) => ({
    name,
    parameter: `${name}Items`,
    label: name,
    title: name,
    kind: "list",
    values: [],
    count,
    range: `1-${count}`,
  });
  state.plan.sources = [source("List1", 100), source("List2", 101)];
  state.plan.parameters = state.plan.sources.map(({ parameter, range }) => ({
    name: parameter,
    type: "STRING",
    default: range,
    userInterface: { groupLabel: "Tasks" },
  }));
  result.maxTasks = ui.MAX_TASKS;
  // One task, the default, runs the lists whole, so their items don't multiply.
  ui.setSplit(false);
  result.canSubmitOneTask = Boolean(ui.canSubmit());
  ui.setSplit(true);
  result.canSubmitOverLimit = Boolean(ui.canSubmit());
  state.edits.List2Items = "1-100";
  result.canSubmitAtLimit = Boolean(ui.canSubmit());
}
if (scenario === "parse-bool") {
  // The panel reads a BOOL parameter as OpenJD and the submitter do.
  result.values = [
    ...[true, "true", "Yes", "ON", "1", 1],
    ...[false, "FALSE", "no", "off", "0", 0],
    // OpenJD refuses spaces around a BOOL.
    ...["", "garbage", 2, " true", "off "],
  ].map(ui.parseBool);
}
if (scenario === "item-range-contract") {
  // The cases that test_submit.py also checks against the submitter's check_item_range.
  const { readFileSync } = await import("node:fs");
  const cases = JSON.parse(readFileSync(new URL("./item_ranges.json", import.meta.url), "utf8"));
  ui.setSplit(true);
  const list = { name: "List5", parameter: "List5Items", label: "List5", title: "List5", kind: "list" };
  result.counts = cases.map(({ value, items }) =>
    ui.taskCount([{ ...list, values: [], count: items, range: `1-${items}` }], () => value)
  );
}
// Exits once the result is written. Node writes to a pipe asynchronously on macOS, so exiting
// straight away would cut off a long result, such as a request with 5,000 files.
process.stdout.write(`${JSON.stringify(result)}\n`, () => process.exit(0));
