// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// Runs one scenario against the real deadline_ui.js with ComfyUI stubbed out, and prints what
// happened as JSON. test_panel.py lays out the files and checks the result.
import { webcrypto } from "node:crypto";
import { readFileSync } from "node:fs";
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
    // Without a document, opening the popover fails after the prompt has been recorded.
  }
}
function confirmationAnswers() {
  return api.calls.filter(({ path }) => path === "/deadline/jobs/confirm").map(({ body }) => body);
}
// Opens another workflow tab, or goes back to one, as ComfyUI does, and reads it.
// Each workflow opened, by file name. Selecting its tab again opens the same object, as in ComfyUI.
const workflows = {};
async function openWorkflow(filename) {
  app.extensionManager.workflow.activeWorkflow = workflows[filename] ??= { filename, path: `workflows/${filename}` };
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
    this.className = "";
    this.style = {};
  }
  // As in the DOM, these properties are their attributes.
  get open() {
    return this.getAttribute("open") !== null;
  }
  set open(value) {
    this.toggleAttribute("open", value);
  }
  get hidden() {
    return this.getAttribute("hidden") !== null;
  }
  set hidden(value) {
    this.toggleAttribute("hidden", value);
  }
  toggleAttribute(name, on) {
    if (on) {
      this.attributes[name] = "";
    } else {
      delete this.attributes[name];
    }
  }
  get textContent() {
    return text(this);
  }
  set textContent(value) {
    this.children = [String(value)];
  }
  get classList() {
    const names = () => this.className.split(" ").filter(Boolean);
    return {
      contains: (name) => names().includes(name),
      add: (name) => (this.className = [...new Set([...names(), name])].join(" ")),
      toggle: (name, on) => (this.className = [...names().filter((other) => other !== name), ...(on ? [name] : [])].join(" ")),
    };
  }
  append(child) {
    this.children.push(child);
    if (child instanceof NodeStub) {
      child.parent = this;
    }
  }
  remove() {
    if (this.parent) {
      this.parent.children = this.parent.children.filter((child) => child !== this);
    }
  }
  setAttribute(name, value) {
    this.attributes[name] = value;
  }
  getAttribute(name) {
    return this.attributes[name] ?? null;
  }
  getBoundingClientRect() {
    return { left: 0, bottom: 0, width: 0 };
  }
  replaceChildren(...children) {
    this.children = [];
    children.forEach((child) => this.append(child));
  }
  querySelector(selector) {
    if (selector === ":scope > summary") {
      return this.children.find((child) => child instanceof NodeStub && child.tag === "summary") ?? null;
    }
    throw new Error(`Unsupported selector: ${selector}`);
  }
  querySelectorAll(selector) {
    return descendants(this).slice(1).filter((node) => {
      if (selector === "details") return node.tag === "details";
      if (selector === "[data-dl-scroll]") return node.getAttribute("data-dl-scroll") !== null;
      if (selector === "[name]") return node.getAttribute("name") !== null;
      if (selector === "[data-role='host-reset']") return node.getAttribute("data-role") === "host-reset";
      throw new Error(`Unsupported selector: ${selector}`);
    });
  }
  // Each event's listeners run in the order they were added, as one.
  addEventListener(name, handler) {
    const before = this.listeners[name];
    this.listeners[name] = before ? (event) => (before(event), handler(event)) : handler;
  }
  contains(node) {
    return descendants(this).includes(node);
  }
  focus() {
    document.activeElement = this;
  }
  setSelectionRange(start, end) {
    this.selection = [start, end];
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
  globalThis.HTMLInputElement = NodeStub;
  globalThis.HTMLTextAreaElement = NodeStub;
  globalThis.window = { innerWidth: 1280 };
  // The popover is added to the body, and the host requirements' Reset is found in it.
  const body = new NodeStub("body");
  globalThis.document = {
    createElement: (tag) => new NodeStub(tag),
    body,
    querySelector: () => null,
    querySelectorAll: (selector) => body.querySelectorAll(selector),
    addEventListener() {},
    removeEventListener() {},
  };
}
// The drawers in a node, outermost first, each with the drawers inside it.
function drawers(node) {
  return node.children
    .filter((child) => child instanceof NodeStub)
    .flatMap((child) => (child.tag === "details" ? [child] : drawers(child)));
}
const drawerTitle = (details) => text(details.children[0]);
function drawerTree(node) {
  return drawers(node).map((details) => [drawerTitle(details), drawerTree(details.children[1])]);
}
// The drawer with the title, among every drawer in the container.
const drawer = (container, title) => container.querySelectorAll("details").find((node) => drawerTitle(node) === title);
const openDrawers = (container) => container.querySelectorAll("details").filter((node) => node.open).map(drawerTitle);
const withClass = (node, className) => descendants(node).filter((child) => child.classList?.contains(className));
const buttonsIn = (node) => descendants(node).filter((child) => child.tag === "button");
// A new sidebar panel.
function renderedPanel() {
  const container = new NodeStub("div");
  return { container, view: ui.__createView(container, ui.__panelSections) };
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
  // The queue environment's Conda Channels, with the value the queue's details give the job, edited
  // and then cleared or reset in the panel.
  state.queue = {
    parameters: [{ name: "CondaChannels", type: "STRING", default: "deadline-cloud" }],
    values: { CondaChannels: "deadline-cloud-v2 deadline-cloud" },
    storageProfiles: [],
  };
  if (scenario === "queue-configured") {
    state.plan.queueValues = { CondaChannels: "s3://bucket/Conda/Default conda-forge" };
  }
  const sent = async () => (await submitAgain()).parameters.CondaChannels ?? null;
  result.unedited = await sent();
  ui.editQueueParameter("CondaChannels", "my-channel");
  result.edited = await sent();
  ui.editQueueParameter("CondaChannels", "");
  result.cleared = await sent();
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
    // Without a document, opening the popover for the confirmation fails after it's recorded.
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
  const view = ui.__createView(container, ["advanced"]);
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
  view.render(["advanced"]);
  result.inputScrollAfterAddingModels = list("inputs").scrollTop;
  result.newModelScroll = list("models").scrollTop;
  list("models").scrollTop = 320;
  click("/models/model-30.safetensors");
  result.scrollAfterRemovingModel = { inputs: list("inputs").scrollTop, models: list("models").scrollTop };
  click("/models/model-30.safetensors");
  result.scrollAfterRestoringModel = { inputs: list("inputs").scrollTop, models: list("models").scrollTop };
  state.plan.localModels = [];
  view.render(["advanced"]);
  result.inputScrollAfterModelsDisappear = list("inputs").scrollTop;
  result.attachmentsStillOpen = container.querySelectorAll("details")[0].open;
  // A custom node pack is removed and added back like a model, and the status warns about it.
  const pack = "/custom_nodes/studio-nodes";
  const localNodes = [{ name: "studio-nodes", path: pack, size: 3_200_000_000, files: 12_500, counted: true }];
  api.handlers["/deadline/plan"] = async (body) => ({ ...planFor(body), localNodes });
  await ui.loadPlan(true);
  view.render(["advanced"]);
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
  result.queueParametersWhileLoading = ui.__renderQueueParameters();
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
  ui.__createView(container, ["advanced"]);
  const shown = () => ({
    rows: rows().length,
    folder: text(row(folder)),
    folderClass: row(folder).className,
    expanded: buttons(folder)[0].getAttribute("aria-expanded"),
  });
  const sent = async () => (await submitAgain()).attachments;
  result.collapsed = { ...shown(), listed: rows().map((node) => node.attributes.title) };
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
if (scenario === "parse-bool") {
  // The panel reads a BOOL parameter as OpenJD and the submitter do.
  result.values = [
    ...[true, "true", "Yes", "ON", "1", 1],
    ...[false, "FALSE", "no", "off", "0", 0],
    // OpenJD refuses spaces around a BOOL.
    ...["", "garbage", 2, " true", "off "],
  ].map(ui.parseBool);
}
if (scenario === "popover") {
  // The popover: its job name, then why Submit can't proceed, its buttons and the one submission's
  // row: the submission in progress, or the job the last one created.
  installDom();
  const copied = [];
  Object.defineProperty(navigator, "clipboard", { value: { writeText: (text) => copied.push(text) }, configurable: true });
  const popover = () => {
    const container = new NodeStub("div");
    ui.__createView(container, ui.__popoverSections);
    return container;
  };
  const parts = (container) => container.children.at(-1).children.map(text);
  state.submission = { id: "a".repeat(32), starting: false, name: "fox", stage: "uploading", progress: 42 };
  const container = popover();
  result.name = text(container.children[0]);
  result.uploading = parts(container);
  api.emit("deadline.submitted", { submissionId: "a".repeat(32), status: "SUBMITTED", name: "fox", jobId: "job-1" });
  result.submitted = parts(container);
  buttonsIn(container).find((button) => button.attributes.title === "Copy job ID").listeners.click();
  result.copied = copied;
  // It shows each time the popover opens, until the next submit.
  result.reopened = parts(popover());
  const succeeded = async () => {
    await submitAgain();
    api.emit("deadline.submitted", { submissionId: state.submission.id, status: "SUBMITTED", name: "owl", jobId: "job-2" });
    return parts(container);
  };
  result.next = await succeeded();
  // A submit that fails, or is canceled, clears it.
  api.handlers["/deadline/jobs"] = async () => {
    throw new Error("Rejected");
  };
  await submitAgain();
  result.afterFailure = parts(container);
  api.handlers["/deadline/jobs"] = async (body) => ({ submissionId: body.submissionId });
  await succeeded();
  await submitAgain();
  api.emit("deadline.submitted", { submissionId: state.submission.id, status: "CANCELED", name: "owl" });
  result.afterCancel = parts(container);
  // Without a farm and queue, the note says to choose them, above the buttons. While the account is
  // checked, it says so.
  state.farmId = "";
  result.partsWithoutTarget = parts(popover());
  state.account = null;
  result.partsWhileChecking = parts(popover());
}
if (scenario === "toolbar-button") {
  // The toolbar button opens the popover when logged in, and the panel otherwise.
  installDom();
  const popover = () => descendants(document.body).find((node) => node.classList.contains("dl-popover"));
  const shown = () => ({
    popover: popover() ? text(popover().children.at(-1).children[0]) : null,
    panelOpened: app.commands.filter((id) => id === "Workspace.ToggleSidebarTab.deadline-cloud").length,
  });
  const status = (authenticationStatus) => async () => ({ authenticationStatus, farmId: "farm-1", queueId: "queue-1" });
  ui.togglePopover();
  result.loggedIn = shown();
  ui.togglePopover();
  result.toggledAgain = shown();
  api.handlers["/deadline/cloud/status"] = status("NEEDS_LOGIN");
  await ui.loadAccount(true);
  ui.togglePopover();
  result.notLoggedIn = shown();
  // Clicked before the check ends, the popover opens and says it's checking. When the check finds
  // the account isn't logged in, the panel opens in its place.
  const check = deferred();
  api.handlers["/deadline/cloud/status"] = () => check.promise;
  state.account = null;
  const checking = ui.loadAccount(true);
  ui.togglePopover();
  result.whileChecking = shown();
  check.resolve({ authenticationStatus: "NEEDS_LOGIN" });
  await checking;
  result.afterChecking = shown();
  const { container } = renderedPanel();
  result.panelDrawers = openDrawers(container);
  // The same when the account stops being logged in while the popover is open.
  api.handlers["/deadline/cloud/status"] = status("AUTHENTICATED");
  await ui.loadAccount(true);
  ui.togglePopover();
  result.loggedInAgain = shown();
  api.handlers["/deadline/cloud/status"] = async () => {
    throw new Error("The security token included in the request is expired");
  };
  await ui.loadAccount(true);
  result.afterFailedCheck = shown();
}
if (scenario === "list-parameters") {
  // The job runs as one task, so the lists' parameters aren't sent, and nor is splitTasks.
  state.plan.sources = [{ parameter: "List1Items" }];
  state.plan.parameters = [
    { name: "List1Items", type: "STRING", default: "1-3" },
    { name: "Seed", type: "INT", default: 5 },
  ];
  const sent = await submitAgain();
  result.parameters = sent.parameters;
  result.splitTasks = "splitTasks" in sent;
}
// A template's parameters: a path the submitter fills in, a path the artist can set, a number, a
// list of choices, a checkbox, text, and one the panel hides, as ours hides Attach Custom Nodes.
const SAVED_PARAMETERS = [
  { name: "WorkflowFile", type: "PATH" },
  { name: "OutputDir", type: "PATH", dataFlow: "OUT", default: "./output", userInterface: { control: "CHOOSE_DIRECTORY" } },
  { name: "Frames", type: "PATH", objectType: "DIRECTORY" },
  { name: "Steps", type: "INT", default: 20, minValue: 1, maxValue: 100 },
  { name: "Mode", type: "STRING", default: "a", allowedValues: ["a", "b"] },
  { name: "Upscale", type: "BOOL", default: "true", userInterface: { control: "CHECK_BOX" } },
  { name: "Prompt", type: "STRING", default: "", maxLength: 20 },
  { name: "AttachCustomNodes", type: "BOOL", default: "true", userInterface: { control: "HIDDEN" } },
];
if (scenario === "saved-settings-written") {
  // What a submission saves in the workflow, and when ComfyUI saves the workflow too.
  state.plan.parameters = SAVED_PARAMETERS;
  state.queue = { parameters: [{ name: "CondaPackages", type: "STRING", default: "" }], storageProfiles: [] };
  const edits = ui.__workflowEdits();
  Object.assign(edits.edits, {
    WorkflowFile: "/x.json",
    OutputDir: "/Users/me/out",
    Steps: "30",
    Upscale: "false",
    Prompt: "a fox",
  });
  edits.jobName = "My job";
  edits.jobDescription = "For the studio";
  state.settings.priority = "70";
  ui.__editHostForm().hardware["amount.worker.gpu"] = { min: "1" };
  ui.editQueueParameter("CondaPackages", "comfy my-nodes");
  ui.__attachmentEdits().added.filenames.push("/manual/clip.mov");
  // The same workflow, with what ComfyUI says about it.
  const workflow = app.extensionManager.workflow.activeWorkflow;
  const submitWith = async (flags, succeed = true) => {
    Object.assign(workflow, { isTemporary: undefined, isModified: undefined }, flags);
    app.graph.extra = {};
    app.commands = [];
    api.handlers["/deadline/jobs"] = async (body) => {
      if (!succeed) {
        throw new Error("Rejected");
      }
      return { submissionId: body.submissionId };
    };
    await submitAgain();
    return { saved: app.graph.extra.deadlineCloud ?? null, commands: app.commands };
  };
  result.saved = await submitWith({ isTemporary: false, isModified: false });
  result.modified = await submitWith({ isTemporary: false, isModified: true });
  result.temporary = await submitWith({ isTemporary: true, isModified: false });
  result.olderFrontend = await submitWith({});
  result.failed = await submitWith({ isTemporary: false, isModified: false }, false);
  // Writing the settings doesn't change what the panel plans from.
  result.sameLayoutKey =
    ui.layoutFreeKey({ nodes: [], extra: {} }) === ui.layoutFreeKey({ nodes: [], extra: { deadlineCloud: { version: 1 } } });
}
if (scenario === "saved-settings-loaded") {
  // A workflow's saved settings apply after its first plan, without what no longer applies.
  api.handlers["/deadline/plan"] = async (body) => ({ ...planFor(body), parameters: SAVED_PARAMETERS });
  const open = async (filename, saved) => {
    app.graph.extra = saved === undefined ? {} : { deadlineCloud: saved };
    await openWorkflow(filename);
    const edits = ui.__workflowEdits();
    return {
      jobName: edits.jobName,
      description: edits.jobDescription,
      settings: edits.settings,
      edits: edits.edits,
      hostRequirements: edits.hostForm && ui.__requirementsFromForm(edits.hostForm),
    };
  };
  result.loaded = await open("saved.json", {
    version: 1,
    jobName: "Saved name",
    description: "Saved description",
    priority: 80,
    targetTaskRunStatus: "SUSPENDED",
    maxWorkerCount: 0,
    maxRetriesPerTask: "3",
    parameters: {
      Steps: 500,
      Mode: "b",
      Upscale: "false",
      Prompt: "a fox",
      OutputDir: "/tmp/out",
      WorkflowFile: "/x.json",
      // Saved by an older panel, which showed it.
      AttachCustomNodes: "false",
      Gone: "x",
    },
    hostRequirements: { amounts: [{ name: "amount.worker.gpu", min: 2, max: 1 }] },
    farmId: "farm-2",
  });
  const sent = await submitAgain();
  result.sent = { priority: sent.settings.priority, status: sent.settings.targetTaskRunStatus, parameters: sent.parameters };
  // An Output Directory the field wouldn't take from the artist isn't loaded.
  result.relativeOutputDir = (await open("relative.json", { version: 1, parameters: { OutputDir: "out" } })).edits;
  // The localStorage settings stay the other workflows'.
  result.globalPriority = state.settings.priority;
  result.hosts = await open("hosts.json", {
    version: 1,
    hostRequirements: {
      attributes: [{ name: "attr.worker.os.family", anyOf: ["linux"] }],
      amounts: [{ name: "amount.worker.gpu", min: 1 }],
    },
  });
  // Too many to check, and a shape the form doesn't make.
  const many = Array.from({ length: 51 }, (_, index) => ({ name: `amount.license.${index}`, min: 1 }));
  result.tooMany = (await open("many.json", { version: 1, hostRequirements: { amounts: many } })).hostRequirements;
  result.otherShape = (await open("shape.json", {
    version: 1,
    hostRequirements: { amounts: [{ name: "amount.worker.gpu", min: 1, extra: true }] },
  })).hostRequirements;
  result.templateText = (await open("text.json", { version: 1, jobName: "{{Param.X}}", description: "a\u0007b" }));
  result.unknownVersion = await open("future.json", { version: 2, jobName: "Future" });
  result.malformed = await open("broken.json", "not settings");
  result.unsaved = await open("plain.json");
  // Back in the first tab, its settings are as they were, and the file's aren't applied again.
  ui.__workflowEdits().jobName = "Edited";
  await open("other.json");
  result.backAgain = (await open("plain.json", { version: 1, jobName: "From the file" })).jobName;
  // Closed and opened again, it starts afresh with what its file saved.
  app.extensionManager.workflow.openWorkflows = [workflows["other.json"]];
  result.reopened = (await open("plain.json", { version: 1, jobName: "From the file" })).jobName;
  // A name the artist types while the first plan loads stays, and the rest applies.
  app.extensionManager.workflow.openWorkflows = undefined;
  app.graph.extra = { deadlineCloud: { version: 1, jobName: "Saved", priority: 90 } };
  app.extensionManager.workflow.activeWorkflow = { filename: "typed.json", path: "workflows/typed.json" };
  ui.workflowLoaded();
  const slowPlan = deferred();
  api.handlers["/deadline/plan"] = async (body) => {
    await slowPlan.promise;
    return { ...planFor(body), parameters: SAVED_PARAMETERS };
  };
  const planning = ui.loadPlan(true);
  ui.__workflowEdits().jobName = "Typed";
  slowPlan.resolve();
  await planning;
  result.typed = { jobName: ui.__workflowEdits().jobName, settings: ui.__workflowEdits().settings };
}
if (scenario === "invalid-values") {
  // A value a field can't submit keeps Submit off, and says why.
  state.plan.parameters = SAVED_PARAMETERS;
  const blocked = (change) => {
    const edits = ui.__workflowEdits();
    const before = { ...edits, settings: { ...state.settings }, edits: { ...edits.edits } };
    change(edits);
    const blocker = ui.canSubmit() ? null : ui.submitBlocker();
    Object.assign(state.settings, before.settings);
    Object.assign(edits, {
      edits: before.edits,
      jobName: before.jobName,
      jobDescription: before.jobDescription,
      hostForm: null,
      badNumbers: new Set(),
    });
    return blocker;
  };
  const host = (change) => (edits) => change(ui.__editHostForm(), edits);
  result.blockers = [
    blocked(() => {}),
    blocked(() => (state.settings.priority = "101")),
    blocked(() => (state.settings.priority = "1e3")),
    blocked(() => (state.settings.priority = "1.0")),
    blocked(() => (state.settings.maxWorkerCount = "0")),
    blocked(() => (state.settings.maxRetriesPerTask = "two")),
    blocked((edits) => (edits.edits.Steps = "0")),
    blocked((edits) => (edits.edits.OutputDir = "output")),
    blocked((edits) => (edits.jobName = "  ")),
    blocked((edits) => (edits.jobName = "x".repeat(129))),
    blocked((edits) => (edits.jobName = "fox {{Param.X}}")),
    blocked((edits) => (edits.jobDescription = "one\u0000two")),
    blocked((edits) => (edits.edits.Mode = "c")),
    blocked((edits) => (edits.edits.Upscale = "maybe")),
    blocked((edits) => (edits.edits.Prompt = "a fox in the snow at dusk")),
    blocked(host((form) => (form.hardware["amount.worker.gpu"] = { min: "-1" }))),
    blocked(host((form) => form.amounts.push({ name: "worker.licenses", min: "1" }))),
    blocked(host((form) => form.amounts.push({ name: "license.nuke" }))),
    blocked(host((form) => form.attributes.push({ name: "software", mode: "anyOf", values: "nuke, 15" }))),
    blocked(host((form, edits) => edits.badNumbers.add("host:amount.worker.vcpu:min"))),
  ];
  // Text the browser couldn't read as a number keeps its error, and Submit off, after the field
  // renders again empty, until the artist edits the field.
  installDom();
  ui.__workflowEdits().badNumbers.add("setting:priority");
  const container = new NodeStub("div");
  const view = ui.__createView(container, ["properties"]);
  view.render(["properties"]);
  const errors = descendants(container).filter((node) => node.className === "dl-field-error");
  result.keptError = { shown: errors.map(text).filter(Boolean), blocker: ui.submitBlocker() };
  ui.__workflowEdits().badNumbers.delete("setting:priority");
  // A Windows path and a share are fine, and an output path can't be empty.
  state.settings.priority = 50;
  result.paths = ["C:\\out", "\\\\server\\out", "  /out  ", ""].map((path) => {
    ui.__workflowEdits().edits.OutputDir = path;
    return ui.submitBlocker();
  });
  // Paths are sent trimmed, and an empty one that isn't an output is sent empty.
  Object.assign(ui.__workflowEdits().edits, { OutputDir: "  /out  ", Frames: "  " });
  const sent = (await submitAgain()).parameters;
  result.sentPaths = { OutputDir: sent.OutputDir, Frames: sent.Frames };
}
if (scenario === "field-controls") {
  // The controls the panel gives parameters, including ones that don't name a control.
  installDom();
  state.plan.parameters = [
    { name: "Upscale", type: "BOOL", default: "false" },
    { name: "Frames", type: "PATH", objectType: "DIRECTORY" },
    { name: "Movie", type: "PATH", objectType: "FILE", dataFlow: "OUT" },
    { name: "Mask", type: "PATH", objectType: "FILE", dataFlow: "IN" },
    { name: "Sampler", type: "STRING", default: "ddim", allowedValues: ["euler", "dpmpp"] },
    { name: "Denoise", type: "STRING", default: "no", allowedValues: ["yes", "no"], userInterface: { control: "CHECK_BOX" } },
    { name: "InputDir", type: "PATH", userInterface: { control: "CHOOSE_DIRECTORY" } },
  ];
  const container = new NodeStub("div");
  ui.__createView(container, ["advanced"]);
  const fieldOf = (label) =>
    descendants(container).find((node) => node.tag === "label" && text(node.children[0]) === label);
  const controls = () =>
    Object.fromEntries(
      state.plan.parameters.map(({ name }) => {
        const nodes = descendants(fieldOf(name));
        const control = nodes.find((node) => ["input", "select"].includes(node.tag));
        const browse = nodes.find((node) => node.tag === "button");
        return [name, { tag: control.tag, type: control.attributes.type ?? null, browse: browse ? text(browse) : null }];
      })
    );
  result.controls = controls();
  const options = descendants(fieldOf("Sampler")).filter((node) => node.tag === "option").map(text);
  result.samplerOptions = options;
  // A STRING checkbox with choices sends the choice that reads as checked.
  descendants(fieldOf("Denoise")).find((node) => node.tag === "input").listeners.change({ target: { checked: true } });
  result.denoise = ui.__workflowEdits().edits.Denoise;
  // Choose input file opens a dialog for one file.
  api.handlers["/deadline/choose"] = async () => ({ paths: ["/masks/a.png"] });
  await descendants(fieldOf("Mask")).find((node) => node.tag === "button").listeners.click();
  result.chooseKind = api.calls.filter(({ path }) => path === "/deadline/choose").at(-1).body.kind;
}
if (scenario === "prototype-names") {
  // Parameters can have any name, such as ones that plain objects already have.
  state.plan.parameters = [
    { name: "constructor", type: "STRING", default: "a" },
    { name: "toString", type: "INT", default: 3 },
    { name: "__proto__", type: "STRING", default: "p" },
  ];
  state.plan.values = {};
  ui.__workflowEdits().edits.__proto__ = "q";
  const sent = await submitAgain();
  result.parameters = sent.parameters;
}
if (scenario === "renamed-workflow") {
  // Saving a workflow for the first time, or renaming it, changes its path but keeps its edits.
  const workflow = { filename: "Unsaved Workflow.json", path: "workflows/Unsaved Workflow.json" };
  app.extensionManager.workflow.activeWorkflow = workflow;
  ui.__workflowEdits().jobName = "Mine";
  Object.assign(workflow, { filename: "fox.json", path: "workflows/fox.json" });
  result.afterSaving = ui.__workflowEdits().jobName;
  // Another workflow at the path it had starts afresh.
  app.extensionManager.workflow.activeWorkflow = { filename: "Unsaved Workflow.json", path: "workflows/Unsaved Workflow.json" };
  result.atOldPath = ui.__workflowEdits().jobName;
}
if (scenario === "focus") {
  // A re-render, such as when the plan loads, keeps the focus and the caret in the job name.
  installDom();
  const container = new NodeStub("div");
  const view = ui.__createView(container, ui.__popoverSections);
  const jobName = () => descendants(container).find((node) => node.getAttribute("name") === "job-name");
  const before = jobName();
  Object.assign(before, { selectionStart: 2, selectionEnd: 5 });
  document.activeElement = before;
  view.render();
  const after = jobName();
  result.focus = { replaced: after !== before, focused: document.activeElement === after, selection: after.selection };
}
if (scenario === "browse-after-switching") {
  // A path chosen in a file dialog that closes after another workflow opened isn't set.
  installDom();
  state.plan.parameters = SAVED_PARAMETERS;
  const container = new NodeStub("div");
  ui.__createView(container, ["properties"]);
  const browse = descendants(container).find((node) => node.tag === "button" && text(node) === "Browse…");
  const chosen = deferred();
  api.handlers["/deadline/choose"] = () => chosen.promise;
  const choosing = browse.listeners.click();
  await settle();
  ui.workflowLoaded();
  chosen.resolve({ paths: ["/late/out"] });
  await choosing;
  result.afterSwitching = ui.__workflowEdits().edits.OutputDir ?? null;
  result.toasts = app.toasts.map(({ severity }) => severity);
  api.handlers["/deadline/choose"] = async () => ({ paths: ["/chosen/out"] });
  await browse.listeners.click();
  result.chosen = ui.__workflowEdits().edits.OutputDir;
}
// Our job template's parameters, which test_panel.py writes next to this file, as the plan has them.
const templateParameters = () => JSON.parse(readFileSync("template_parameters.json", "utf-8"));
// A queue with a storage profile and two queue environments' parameters, each with the group label
// the library gives it.
const ENVIRONMENT_QUEUE = {
  parameters: [
    ["CondaPackages", "Conda Packages", "Conda", ""],
    ["CondaChannels", "Conda Channels", "Conda", "deadline-cloud"],
    ["RezPackages", "Rez Packages", "Rez", ""],
  ].map(([name, label, environment, value]) => ({
    name,
    type: "STRING",
    default: value,
    userInterface: { label, groupLabel: `Queue Environment: ${environment}` },
  })),
  values: { CondaChannels: "deadline-cloud-v2 deadline-cloud" },
  storageProfiles: [{ storageProfileId: "sp-1", displayName: "Studio workstations" }],
};
if (scenario === "drawers") {
  // The panel with our template, logged in, with a farm and queue chosen.
  installDom();
  state.plan.parameters = templateParameters();
  state.plan.hostRequirements = {};
  api.handlers["/deadline/cloud/status"] = async () => ({
    authenticationStatus: "AUTHENTICATED",
    profile: "default",
    farmId: "farm-1",
    queueId: "queue-1",
  });
  api.handlers["/deadline/cloud/queue"] = async () => ENVIRONMENT_QUEUE;
  await ui.loadAccount(true);
  const { container } = renderedPanel();
  result.tree = drawerTree(container);
  result.open = openDrawers(container);
  result.summaries = container.querySelectorAll("details").map((details) =>
    details.children[0].children.map((child) =>
      child.classList.contains("dl-chevron") ? "chevron" : child.classList.contains("dl-grow") ? "title" : child.className
    )
  );
  const labels = (title) => withClass(drawer(container, title), "dl-label").map(text);
  const subheadings = (title) =>
    withClass(drawer(container, title), "dl-subheading").map((node) => [text(node), node.getAttribute("data-dl-tip")]);
  result.labels = Object.fromEntries(
    ["Job Properties", "Job submission settings", "Attachments", "Timeouts", "Host requirements"].map((title) => [
      title,
      labels(title),
    ])
  );
  result.subheadings = {
    "Job submission settings": subheadings("Job submission settings"),
    "Host requirements": subheadings("Host requirements"),
  };
  const placeholder = (name) => container.querySelectorAll("[name]").find((node) => node.getAttribute("name") === name).attributes.placeholder;
  result.placeholders = ["setting:maxRetriesPerTask", "setting:maxFailedTasksCount", "setting:maxWorkerCount"].map(placeholder);
  result.account = text(withClass(drawer(container, "Job submission settings"), "dl-meta")[0]);
  result.queueReset = withClass(container, "dl-link")
    .filter((node) => node.getAttribute("data-role") === "queue-reset")
    .map(text);
  // A custom template's other visible parameters have a drawer of their own, by group label.
  state.plan.parameters = [
    ...templateParameters(),
    { name: "Seed", type: "INT", default: 1, userInterface: { label: "Seed", groupLabel: "Sampler" } },
  ];
  const custom = renderedPanel().container;
  result.customTree = drawerTree(drawer(custom, "Advanced settings").children[1]);
  result.customJobParameters = descendants(drawer(custom, "Job parameters")).filter((node) =>
    ["dl-subheading", "dl-label"].includes(node.className)
  ).map(text);
}
if (scenario === "auto-open") {
  // Which drawers open themselves, the account line and Submit's reason, for each state the account
  // check ends in, and for values that can't be submitted.
  installDom();
  state.plan.parameters = templateParameters();
  api.handlers["/deadline/cloud/queue"] = async () => ENVIRONMENT_QUEUE;
  const account = (authenticationStatus, ids = { farmId: "farm-1", queueId: "queue-1" }) => async () => ({
    authenticationStatus,
    profile: "default",
    ...ids,
  });
  const shown = ({ container }) => {
    const line = drawer(container, "Job submission settings").children[1];
    const notice = [...withClass(line, "dl-meta"), ...withClass(line, "dl-notice")][0];
    return {
      open: openDrawers(container),
      account: text(notice),
      dot: withClass(notice, "dl-dot")[0]?.className ?? null,
      blocker: ui.submitBlocker(),
      canSubmit: Boolean(ui.canSubmit()),
    };
  };
  const check = async (status) => {
    api.handlers["/deadline/cloud/status"] = status;
    await ui.loadAccount(true);
    return shown(renderedPanel());
  };
  // Still checking: nothing opens, and Submit says nothing about the account yet.
  api.handlers["/deadline/cloud/status"] = () => new Promise(() => {});
  state.account = null;
  ui.loadAccount(true);
  result.checking = shown(renderedPanel());
  result.notLoggedIn = await check(account("NEEDS_LOGIN"));
  state.loggingIn = true;
  result.loggingIn = shown(renderedPanel());
  state.loggingIn = false;
  result.badCredentials = await check(account("CONFIGURATION_ERROR"));
  result.failedCheck = await check(async () => {
    throw new Error("Couldn't reach Deadline Cloud.");
  });
  state.farmId = "";
  state.queueId = "";
  result.noTarget = await check(account("AUTHENTICATED", { farmId: "", queueId: "" }));
  result.farmNeeded = withClass(renderedPanel().container, "dl-needs").map((node) => node.tag);
  result.loggedIn = await check(account("AUTHENTICATED"));
  // A value that can't be submitted opens its drawer, and the drawers it's in. Fixed, they stay open.
  const edits = ui.__workflowEdits();
  edits.edits.WorkflowTimeout = "0";
  const panel = renderedPanel();
  result.badTimeout = shown(panel);
  edits.edits.WorkflowTimeout = "7200";
  panel.view.render();
  result.fixedTimeout = shown(panel);
  delete edits.edits.WorkflowTimeout;
  state.settings.priority = "101";
  result.badPriority = shown(renderedPanel());
  state.settings.priority = 50;
  // A check that fails after the account was logged in.
  result.failedRecheck = await check(async () => {
    throw new Error("Couldn't reach Deadline Cloud.");
  });
  result.failedRecheckLabels = withClass(drawer(renderedPanel().container, "Job submission settings"), "dl-label").map(text);
}
if (scenario === "saved-output-dir") {
  // A workflow that saved an Output Directory other than the plan's, ComfyUI's output folder, opens
  // Job Properties when its panel shows.
  installDom();
  api.handlers["/deadline/plan"] = async (body) => ({
    ...planFor(body),
    parameters: SAVED_PARAMETERS,
    values: { OutputDir: "/comfy/output" },
  });
  const opened = async (filename, saved) => {
    app.graph.extra = saved === undefined ? {} : { deadlineCloud: { version: 1, parameters: saved } };
    await openWorkflow(filename);
    const element = new NodeStub("div");
    const close = ui.renderPanel(element);
    await settle();
    const open = openDrawers(element);
    close();
    return open;
  };
  result.other = await opened("other.json", { OutputDir: "/renders/fox" });
  result.same = await opened("same.json", { OutputDir: "/comfy/output" });
  result.relative = await opened("relative.json", { OutputDir: "renders" });
  result.unsaved = await opened("unsaved.json");
}
if (scenario === "drawers-across-workflows") {
  // Drawers the artist opened stay open in another workflow, after its plan brings them back.
  installDom();
  api.handlers["/deadline/plan"] = async (body) => ({ ...planFor(body), parameters: templateParameters() });
  await openWorkflow("fox.json");
  const { container } = renderedPanel();
  drawer(container, "Advanced settings").open = true;
  drawer(container, "Timeouts").open = true;
  app.extensionManager.workflow.activeWorkflow = { filename: "hare.json", path: "workflows/hare.json" };
  ui.workflowLoaded();
  result.timeoutsWhilePlanning = Boolean(drawer(container, "Timeouts"));
  await ui.loadPlan(true);
  result.open = openDrawers(container);
}
if (scenario === "host-form") {
  // The host requirements form, which starts from our template's, so empty, and sends nothing until
  // it's edited.
  installDom();
  state.plan.hostRequirements = {};
  const container = new NodeStub("div");
  document.body.append(container);
  ui.__createView(container, ["advanced"]);
  const form = () => drawer(container, "Host requirements").children[1];
  const reset = () => buttonsIn(form()).find((node) => node.getAttribute("data-role") === "host-reset");
  const inputs = () => descendants(form()).filter((node) => node.tag === "input");
  const shown = () => ({
    checked: inputs().filter((node) => node.checked).length,
    values: inputs().filter((node) => node.attributes.type === "number" && node.value !== "").map((node) => node.value),
    resetHidden: reset().hidden,
  });
  const sent = async () => (await submitAgain()).hostRequirements ?? null;
  result.empty = {
    ...shown(),
    labels: withClass(form(), "dl-label").map(text),
    options: descendants(form()).filter((node) => node.tag === "label").map(text),
    buttons: buttonsIn(form()).map(text),
    sent: await sent(),
  };
  const gpuMemory = inputs().find((node) => node.attributes["aria-label"] === "GPU memory (GiB) minimum");
  gpuMemory.value = "16";
  gpuMemory.listeners.input({ target: { value: "16", validity: { badInput: false } } });
  result.edited = { resetHidden: reset().hidden, sent: await sent() };
  reset().listeners.click();
  result.reset = { ...shown(), sent: await sent() };
}
// Exits once the result is written. Node writes to a pipe asynchronously on macOS, so exiting
// straight away would cut off a long result, such as a request with 5,000 files.
process.stdout.write(`${JSON.stringify(result)}\n`, () => process.exit(0));
