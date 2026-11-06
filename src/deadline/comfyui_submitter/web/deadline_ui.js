// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// The Deadline Cloud sidebar panel and submit dialog, which render from the same state.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

export const SUMMARY = "Deadline Cloud";
export const SIDEBAR_TAB_ID = "deadline-cloud";

const SETTINGS_KEY = "DeadlineCloud.Submitter.JobSettings";
const JOBS_KEY = "DeadlineCloud.Submitter.RecentJobs";
const MAX_RECENT_JOBS = 20;
const JOB_POLL_MS = 20000;
const PLAN_DEBOUNCE_MS = 1000;
const TOOLTIP_DELAY_MS = 200;
const TOOLTIP_GAP = 6;
const TOOLTIP_MARGIN = 8;
// The submitter fills these in when it creates the job bundle.
const AUTOMATIC_PARAMETERS = new Set(["WorkflowFile"]);
const WORK_ITEMS_GROUP = "Work Items";
const ATTACH_NODES_PARAMETER = "AttachCustomNodes";
const DEFAULT_SETTINGS = {
  requirePathsExist: false,
  priority: 50,
  targetTaskRunStatus: "READY",
  maxFailedTasksCount: "",
  maxRetriesPerTask: "",
  maxWorkerCount: "",
  jobAttachmentsFileSystem: "",
};
const DONE_STATUSES = new Set(["SUCCEEDED", "FAILED", "CANCELED"]);
// The most attachments the panel lists. All of them are still attached.
const MAX_LISTED_ATTACHMENTS = 100;
// How many of a field's chosen items it previews.
const PREVIEW_ITEMS = 3;

function readStorage(key, fallback) {
  try {
    const value = JSON.parse(localStorage.getItem(key));
    return value ?? fallback;
  } catch {
    return fallback;
  }
}

function writeStorage(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Private windows and full storage only lose the remembered settings.
  }
}

export function toast(severity, detail, life = 5000) {
  app.extensionManager.toast.add({ severity, summary: SUMMARY, detail, life });
}

// ---------------------------------------------------------------------------------------------
// State

const state = {
  account: null,
  accountError: null,
  loggingIn: false,
  farms: null,
  queues: null,
  queue: null, // { parameters, storageProfiles } for the chosen queue
  cloudError: null,
  farmId: "",
  queueId: "",
  storageProfileId: "",
  plan: null,
  planError: null,
  planning: false,
  // The artist's values, which win over the ones the submitter fills in.
  edits: {},
  // What each edited work item field's node held when the artist edited the field.
  itemEditBases: {},
  queueEdits: {},
  jobName: null,
  jobDescription: "",
  // The host requirements form, once the artist edits it. Until then the job template's apply.
  hostForm: null,
  settings: { ...DEFAULT_SETTINGS, ...readStorage(SETTINGS_KEY, {}) },
  removedAttachments: new Set(),
  addedAttachments: { filenames: [], directories: [] },
  submission: null, // { id, stage, progress, message, confirm, starting }
  jobs: readStorage(JOBS_KEY, []),
  jobStatus: {},
};

const views = new Set();

// Re-renders the named sections of every open view, or all of them.
function refresh(...sections) {
  for (const view of views) {
    view.render(sections.length ? sections : null);
  }
}

// ---------------------------------------------------------------------------------------------
// Server calls

async function errorMessage(response) {
  try {
    return (await response.json()).error ?? `The server returned ${response.status}.`;
  } catch {
    // Not JSON, for example a 404 from an older server.
    return `The server returned ${response.status}.`;
  }
}

async function postJson(path, body) {
  let response;
  try {
    response = await api.fetchApi(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
  } catch (error) {
    throw new Error(`Couldn't reach the ComfyUI server: ${error.message ?? error}`);
  }
  if (!response.ok) {
    const error = new Error(await errorMessage(response));
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function cloud(command, body) {
  return postJson(`/deadline/cloud/${command}`, body);
}

export async function currentWorkflow() {
  // The UI-format workflow carries the model download links that templates embed.
  const { output, workflow } = await app.graphToPrompt();
  const name = app.extensionManager.workflow?.activeWorkflow?.filename || "ComfyUI workflow";
  return { workflow: output, ui_workflow: workflow, name };
}

// ---------------------------------------------------------------------------------------------
// Loading

let accountLoad = null;

export function loadAccount(force = false) {
  if (accountLoad && !force) {
    return accountLoad;
  }
  accountLoad = (async () => {
    state.accountError = null;
    refresh("account");
    try {
      state.account = await cloud("status");
      state.farmId ||= state.account.farmId;
      state.queueId ||= state.account.queueId;
      state.storageProfileId ||= state.account.storageProfileId;
      if (!state.settings.jobAttachmentsFileSystem && state.account.jobAttachmentsFileSystem) {
        state.settings.jobAttachmentsFileSystem = state.account.jobAttachmentsFileSystem;
      }
    } catch (error) {
      state.accountError = error.message;
    }
    refresh();
    if (state.account?.authenticationStatus === "AUTHENTICATED") {
      await loadFarms();
    }
  })();
  return accountLoad;
}

async function loadFarms() {
  state.cloudError = null;
  try {
    state.farms = (await cloud("farms")).farms;
  } catch (error) {
    state.cloudError = error.message;
  }
  refresh("target");
  if (state.farmId) {
    await loadQueues();
  }
}

async function loadQueues() {
  state.queues = null;
  state.queue = null;
  refresh("target", "queueParameters");
  try {
    state.queues = (await cloud("queues", { farmId: state.farmId })).queues;
  } catch (error) {
    state.cloudError = error.message;
  }
  refresh("target");
  if (state.queueId && state.queues?.some((queue) => queue.queueId === state.queueId)) {
    await loadQueue();
  }
}

async function loadQueue() {
  try {
    state.queue = await cloud("queue", { farmId: state.farmId, queueId: state.queueId });
    const profiles = state.queue.storageProfiles;
    if (!profiles.some((profile) => profile.storageProfileId === state.storageProfileId)) {
      state.storageProfileId = "";
    }
  } catch (error) {
    state.cloudError = error.message;
  }
  refresh("target", "queueParameters");
}

async function saveDefaults() {
  try {
    await cloud("set-defaults", {
      farmId: state.farmId,
      queueId: state.queueId,
      storageProfileId: state.storageProfileId,
    });
  } catch (error) {
    toast("warn", `Couldn't save the farm and queue as your defaults: ${error.message}`);
  }
}

async function login() {
  state.loggingIn = true;
  refresh("account");
  try {
    await cloud("login");
    toast("success", "Logged in to Deadline Cloud.");
  } catch (error) {
    toast("error", error.message, 10000);
  }
  state.loggingIn = false;
  await loadAccount(true);
}

let planRequest = 0;
// The workflow of the latest plan request, so moving nodes around doesn't plan again.
let requestedKey = null;

// Reads the workflow again, unless it's unchanged. force reads it anyway.
export async function loadPlan(force = false) {
  let body;
  try {
    body = await currentWorkflow();
  } catch (error) {
    state.planError = `Couldn't convert the workflow to API format: ${error.message ?? error}`;
    refresh("status", "submit");
    return;
  }
  const key = JSON.stringify([body.workflow, body.name]);
  if (!force && key === requestedKey && !state.planError) {
    return;
  }
  const request = ++planRequest;
  requestedKey = key;
  state.planning = true;
  refresh("status");
  let plan = null;
  let planError = null;
  try {
    plan = await postJson("/deadline/plan", body);
  } catch (error) {
    if (error.status === 409) {
      // The server dropped it for a newer plan.
      if (request === planRequest) {
        state.planning = false;
        refresh("status");
      }
      return;
    }
    planError = error.message ?? String(error);
  }
  if (request !== planRequest) {
    return; // A newer plan replaced this one.
  }
  state.planning = false;
  state.planError = planError;
  if (plan) {
    state.plan = plan;
    dropStaleItemEdits(plan.sources ?? []);
  }
  refresh();
}

function itemEditBase(source) {
  return `${source.count}:${source.range}`;
}

// A work item field follows its node again once the node's values change, or the node is gone.
function dropStaleItemEdits(sources) {
  const current = new Map(sources.map((source) => [source.parameter, itemEditBase(source)]));
  for (const [parameter, base] of Object.entries(state.itemEditBases)) {
    if (current.get(parameter) !== base) {
      delete state.edits[parameter];
      delete state.itemEditBases[parameter];
    }
  }
}

let planTimer = null;
let graphKey = null;

// Layout that doesn't change what runs: positions, node sizes and collapsing, groups and the canvas
// view.
const LAYOUT_KEYS = new Set(["pos", "size", "flags"]);

function withoutLayout(key, value) {
  if (key === "groups" || key === "ds") {
    return undefined;
  }
  if (value && typeof value === "object" && !Array.isArray(value) && "id" in value) {
    return Object.fromEntries(Object.entries(value).filter(([name]) => !LAYOUT_KEYS.has(name)));
  }
  return value;
}

// Comparing the change tracker's workflow without the layout is much cheaper than graphToPrompt.
export function layoutFreeKey(workflow) {
  return JSON.stringify(workflow, withoutLayout);
}

function schedulePlan(event) {
  if (!views.size) {
    return;
  }
  const workflow = event?.detail;
  if (workflow && typeof workflow === "object") {
    const key = layoutFreeKey(workflow);
    if (key === graphKey) {
      return;
    }
    graphKey = key;
  }
  clearTimeout(planTimer);
  planTimer = setTimeout(() => loadPlan(), PLAN_DEBOUNCE_MS);
}

// Opening a workflow or selecting its tab doesn't send graphChanged. The plan is for the previous
// graph, so clear it before a submission can mix the two.
export function workflowLoaded() {
  clearTimeout(planTimer);
  requestedKey = null;
  graphKey = null;
  state.plan = null;
  state.planError = null;
  if (views.size) {
    refresh();
    planTimer = setTimeout(() => loadPlan());
  }
}

// ---------------------------------------------------------------------------------------------
// Work items

// Counts the values of an OpenJD integer range expression, such as "1-4,7,10-20:2".
export function countRange(expression) {
  let count = 0;
  for (const part of String(expression).split(",")) {
    const text = part.trim();
    if (!text) {
      continue;
    }
    const match = /^(-?\d+)(?:\s*-\s*(-?\d+)(?:\s*:\s*(\d+))?)?$/.exec(text);
    if (!match) {
      return null;
    }
    const [start, end, step] = [Number(match[1]), Number(match[2] ?? match[1]), Number(match[3] ?? 1)];
    if (end < start || step < 1) {
      return null;
    }
    count += Math.floor((end - start) / step) + 1;
  }
  return count || null;
}

function itemsValue(source) {
  return state.edits[source.parameter] ?? source.range;
}

function plural(count, noun) {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

// Groups the sources that run together, in workflow order. Each group's count is its number of
// tasks, or null with an error.
export function workItemGroups(sources, valueOf) {
  const groups = new Map();
  for (const source of sources) {
    const key = source.pairWith ? `pair:${source.pairWith}` : `source:${source.name}`;
    if (!groups.has(key)) {
      groups.set(key, { key, pairWith: source.pairWith || null, sources: [], count: null, error: null });
    }
    groups.get(key).sources.push(source);
  }
  for (const group of groups.values()) {
    const counts = group.sources.map((source) => countRange(valueOf(source)));
    const invalid = counts.indexOf(null);
    if (invalid !== -1) {
      group.error = `${group.sources[invalid].title}: use a range such as 1-4 or 7,42.`;
    } else if (new Set(counts).size > 1) {
      const items = group.sources.map((source, index) => `${source.title} has ${counts[index]}`);
      group.error = `Paired fields need the same number of items: ${items.join(", ")}.`;
    } else {
      group.count = counts[0];
    }
  }
  return [...groups.values()];
}

// The number of tasks, from every combination of the groups. Returns null when a group has an error.
export function taskCount(sources, valueOf) {
  let total = 1;
  for (const group of workItemGroups(sources, valueOf)) {
    if (group.count === null) {
      return null;
    }
    total *= group.count;
  }
  return total;
}

function currentGroups() {
  return workItemGroups(state.plan?.sources ?? [], itemsValue);
}

function currentTaskCount() {
  const sources = state.plan?.sources ?? [];
  return sources.length ? taskCount(sources, itemsValue) : 1;
}

// How the groups multiply into tasks, such as "3 × 2 = 6 tasks", leaving out groups of one task.
function taskMath() {
  const total = currentTaskCount();
  if (total === null) {
    return "";
  }
  const factors = currentGroups()
    .map((group) => group.count)
    .filter((count) => count > 1);
  return factors.length > 1 ? `${factors.join(" × ")} = ${plural(total, "task")}` : plural(total, "task");
}

// The item numbers in a range expression, in order. Only called for ranges that countRange accepts.
function selectedItems(expression) {
  const numbers = [];
  for (const part of String(expression).split(",")) {
    const match = /^(-?\d+)(?:\s*-\s*(-?\d+)(?:\s*:\s*(\d+))?)?$/.exec(part.trim());
    if (match) {
      const [start, end, step] = [Number(match[1]), Number(match[2] ?? match[1]), Number(match[3] ?? 1)];
      for (let number = start; number <= end && numbers.length < PREVIEW_ITEMS + 1; number += step) {
        numbers.push(number);
      }
    }
  }
  return numbers;
}

// What a field's items are. Integer fields show their values already, so they have none.
export function itemsPreview(source, value) {
  if (source.kind === "integer" || !source.values?.length || countRange(value) === null) {
    return "";
  }
  if (String(value).trim() === "0") {
    return source.kind === "prompt" ? "A random choice for each task" : "All of them, in each task";
  }
  const numbers = selectedItems(value);
  const shown = numbers.slice(0, PREVIEW_ITEMS).map((number) => source.values[number - 1] ?? `item ${number}`);
  return numbers.length > PREVIEW_ITEMS ? `${shown.join(", ")}, …` : shown.join(", ");
}

// ---------------------------------------------------------------------------------------------
// Submitting

function parameterValue(parameter) {
  if (parameter.name in state.edits) {
    return state.edits[parameter.name];
  }
  const filled = state.plan?.values?.[parameter.name];
  return filled ?? parameter.default ?? "";
}

// Templates without the switch don't list any custom nodes.
function attachesNodes() {
  const parameter = state.plan?.parameters.find(({ name }) => name === ATTACH_NODES_PARAMETER);
  return !parameter || String(parameterValue(parameter)).toLowerCase() !== "false";
}

function queueParameterValue(parameter) {
  if (parameter.name in state.queueEdits) {
    return state.queueEdits[parameter.name];
  }
  return state.plan?.queueValues?.[parameter.name] ?? parameter.default ?? "";
}

function attachments() {
  const listed = state.plan?.attachments ?? { filenames: [], directories: [] };
  const keep = (paths) => paths.filter((path) => !state.removedAttachments.has(path));
  return {
    filenames: [...keep(listed.filenames), ...keep(state.addedAttachments.filenames)],
    directories: [...keep(listed.directories), ...keep(state.addedAttachments.directories)],
  };
}

function jobName() {
  return state.jobName ?? `ComfyUI - ${state.plan?.name ?? "workflow"}`;
}

export function canSubmit() {
  return (
    state.account?.authenticationStatus === "AUTHENTICATED" &&
    state.farmId &&
    state.queueId &&
    state.plan &&
    !state.planError &&
    !state.submission &&
    !hostFormError() &&
    currentTaskCount() !== null
  );
}

// What the job bundle is made from, whether it's submitted or saved.
function bundleRequest() {
  const parameters = {};
  for (const parameter of state.plan.parameters) {
    if (AUTOMATIC_PARAMETERS.has(parameter.name)) {
      continue;
    }
    const value = parameterValue(parameter);
    // An empty number can't be sent, so the template's default applies.
    if (value === "" && (parameter.type === "INT" || parameter.type === "FLOAT")) {
      continue;
    }
    parameters[parameter.name] = value;
  }
  for (const parameter of state.queue?.parameters ?? []) {
    if (parameter.name in state.queueEdits || parameter.name in (state.plan.queueValues ?? {})) {
      parameters[parameter.name] = queueParameterValue(parameter);
    }
  }
  const settings = { name: jobName() };
  for (const [key, value] of Object.entries(state.settings)) {
    if (value !== "" && value !== null) {
      settings[key] =
        key === "targetTaskRunStatus" ||
        key === "jobAttachmentsFileSystem" ||
        key === "requirePathsExist"
          ? value
          : Number(value);
    }
  }
  return {
    parameters,
    settings,
    description: state.jobDescription.trim(),
    hostRequirements: state.hostForm ? requirementsFromForm(state.hostForm) : undefined,
    attachments: attachments(),
    excludedModels: excludedModels(),
  };
}

export async function submit() {
  if (!canSubmit()) {
    return;
  }
  const request = bundleRequest();

  state.submission = { starting: true, message: "Creating the job bundle…" };
  refresh("submit", "status");
  try {
    const result = await postJson("/deadline/jobs", {
      ...(await currentWorkflow()),
      farmId: state.farmId,
      queueId: state.queueId,
      storageProfileId: state.storageProfileId,
      ...request,
    });
    state.submission = { id: result.submissionId, message: "Starting…", name: request.settings.name };
    for (const warning of result.warnings ?? []) {
      toast("warn", warning, 15000);
    }
  } catch (error) {
    state.submission = null;
    toast("error", error.message, 10000);
  }
  refresh("submit", "status");
}

async function answerConfirmation(accept) {
  const submission = state.submission;
  if (!submission?.confirm) {
    return;
  }
  submission.confirm = null;
  refresh("submit");
  try {
    await postJson("/deadline/jobs/confirm", { submissionId: submission.id, accept });
  } catch (error) {
    toast("error", error.message);
  }
}

async function cancelSubmission() {
  if (!state.submission?.id) {
    return;
  }
  try {
    await postJson("/deadline/jobs/cancel", { submissionId: state.submission.id });
  } catch (error) {
    toast("error", error.message);
  }
}

function rememberJob(job) {
  state.jobs = [job, ...state.jobs.filter((other) => other.jobId !== job.jobId)].slice(
    0,
    MAX_RECENT_JOBS
  );
  writeStorage(JOBS_KEY, state.jobs);
}

export function listenForSubmissions() {
  api.addEventListener("deadline.progress", ({ detail }) => {
    const submission = state.submission;
    if (!submission || detail.submissionId !== submission.id) {
      return;
    }
    if (detail.kind === "progress") {
      submission.stage = detail.stage;
      submission.progress = detail.progress;
    }
    if (detail.message) {
      submission.message = detail.message;
    }
    refresh("submit");
  });
  api.addEventListener("deadline.confirm", ({ detail }) => {
    const submission = state.submission;
    if (!submission || detail.submissionId !== submission.id) {
      return;
    }
    submission.confirm = { message: detail.message, default: detail.default };
    refresh("submit");
    if (!views.size) {
      openDialog();
    }
  });
  api.addEventListener("deadline.submitted", ({ detail }) => {
    const ours = detail.submissionId === state.submission?.id;
    if (detail.status === "SUBMITTED") {
      toast("success", `Submitted ${detail.name} as ${detail.jobId}.`, 10000);
      if (detail.farmId && detail.queueId) {
        rememberJob({
          jobId: detail.jobId,
          name: detail.name,
          farmId: detail.farmId,
          queueId: detail.queueId,
          submittedAt: new Date().toISOString(),
        });
        pollJobs();
      }
    } else if (detail.status === "FAILED") {
      toast("error", `Couldn't submit ${detail.name}. ${detail.error ?? ""}`, 15000);
    } else if (detail.status === "CANCELED" && ours) {
      toast("info", `Canceled submitting ${detail.name}.`);
    }
    if (ours) {
      state.submission = null;
      refresh("submit", "status", "jobs");
    }
  });
  api.addEventListener("graphChanged", schedulePlan);
  listenForDownloads();
}

// ---------------------------------------------------------------------------------------------
// Job status

let pollTimer = null;

async function pollJobs() {
  clearTimeout(pollTimer);
  const pending = state.jobs.filter(
    (job) => !DONE_STATUSES.has(state.jobStatus[job.jobId]?.taskRunStatus)
  );
  const byQueue = new Map();
  for (const job of pending) {
    const key = `${job.farmId}/${job.queueId}`;
    byQueue.set(key, [...(byQueue.get(key) ?? []), job]);
  }
  if (state.account?.authenticationStatus === "AUTHENTICATED") {
    for (const jobs of byQueue.values()) {
      try {
        const result = await cloud("jobs", {
          farmId: jobs[0].farmId,
          queueId: jobs[0].queueId,
          jobIds: jobs.map((job) => job.jobId),
        });
        for (const job of result.jobs) {
          state.jobStatus[job.jobId] = job;
        }
      } catch {
        // Try again next time.
      }
    }
    refresh("jobs");
  }
  if (views.size && pending.length) {
    pollTimer = setTimeout(pollJobs, JOB_POLL_MS);
  }
}

// ---------------------------------------------------------------------------------------------
// Rendering

function h(tag, props = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(props ?? {})) {
    if (value === undefined || value === null || value === false) {
      continue;
    }
    if (key.startsWith("on")) {
      element.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "className") {
      element.className = value;
    } else if (key === "value" || key === "checked") {
      element[key] = value;
    } else {
      element.setAttribute(key, value === true ? "" : value);
    }
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) {
      element.append(child instanceof Node ? child : String(child));
    }
  }
  return element;
}

function field(label, control, help) {
  if (help) {
    control.setAttribute("aria-description", help);
  }
  return h(
    "label",
    { className: control.type === "checkbox" ? "dl-field dl-field-check" : "dl-field" },
    h("span", { className: "dl-label", "data-dl-tip": help || null }, label),
    control
  );
}

// One tooltip on the page, because the panel's scrolling would clip a tooltip placed inside it.
let tooltipElement = null;
let tooltipTarget = null;
let tooltipTimer = null;

function hideTooltip() {
  clearTimeout(tooltipTimer);
  tooltipElement?.remove();
  tooltipElement = null;
  tooltipTarget = null;
}

function showTooltip(target) {
  if (!target.isConnected) {
    return;
  }
  tooltipElement = h("div", { className: "dl-tooltip", role: "tooltip" }, target.dataset.dlTip);
  document.body.append(tooltipElement);
  const anchor = target.getBoundingClientRect();
  const box = tooltipElement.getBoundingClientRect();
  const maxLeft = window.innerWidth - box.width - TOOLTIP_MARGIN;
  const above = anchor.top - box.height - TOOLTIP_GAP;
  tooltipElement.style.left = `${Math.max(TOOLTIP_MARGIN, Math.min(anchor.left, maxLeft))}px`;
  tooltipElement.style.top = `${above >= TOOLTIP_MARGIN ? above : anchor.bottom + TOOLTIP_GAP}px`;
}

function listenForTooltips() {
  document.addEventListener("mouseover", (event) => {
    const target = event.target.closest?.("[data-dl-tip]") ?? null;
    if (target === tooltipTarget) {
      return;
    }
    hideTooltip();
    if (target) {
      tooltipTarget = target;
      tooltipTimer = setTimeout(() => showTooltip(target), TOOLTIP_DELAY_MS);
    }
  });
  document.addEventListener("mouseout", (event) => {
    if (!event.relatedTarget) {
      hideTooltip();
    }
  });
  document.addEventListener("scroll", hideTooltip, true);
  document.addEventListener("pointerdown", hideTooltip, true);
}

function select(options, value, onChange, placeholder) {
  return h(
    "select",
    { className: "dl-input", onChange: (event) => onChange(event.target.value) },
    placeholder ? h("option", { value: "", selected: !value }, placeholder) : null,
    options.map(([optionValue, text]) =>
      h("option", { value: optionValue, selected: optionValue === value }, text)
    )
  );
}

function section(title, open, ...children) {
  return h(
    "details",
    { className: "dl-section", open },
    h("summary", {}, title),
    h("div", { className: "dl-section-body" }, ...children)
  );
}

function renderAccount() {
  if (state.accountError) {
    return h(
      "div",
      { className: "dl-notice dl-error" },
      state.accountError,
      h("button", { className: "dl-link", onClick: () => loadAccount(true) }, "Try again")
    );
  }
  const account = state.account;
  if (!account) {
    return h("div", { className: "dl-muted" }, "Checking your Deadline Cloud credentials…");
  }
  const profile = account.profile ? ` (${account.profile})` : "";
  if (account.authenticationStatus === "AUTHENTICATED") {
    return h("div", { className: "dl-account" }, h("span", { className: "dl-dot dl-ok" }), `Logged in${profile}`);
  }
  if (account.authenticationStatus === "NEEDS_LOGIN") {
    return h(
      "div",
      { className: "dl-account" },
      h("span", { className: "dl-dot dl-warn" }),
      state.loggingIn ? "Finish logging in with Deadline Cloud monitor…" : `Not logged in${profile}`,
      state.loggingIn ? null : h("button", { className: "dl-button dl-small", onClick: login }, "Log in")
    );
  }
  return h(
    "div",
    { className: "dl-notice dl-error" },
    `The Deadline Cloud credentials${profile} don't work (${account.authenticationStatus}). `,
    "Check the AWS profile in Deadline Cloud monitor or the Deadline Cloud configuration.",
    h("button", { className: "dl-link", onClick: () => loadAccount(true) }, "Check again")
  );
}

function renderTarget() {
  if (state.account?.authenticationStatus !== "AUTHENTICATED") {
    return null;
  }
  const farms = state.farms;
  const queues = state.queues;
  const profiles = state.queue?.storageProfiles ?? [];
  return h(
    "div",
    { className: "dl-grid" },
    state.cloudError ? h("div", { className: "dl-notice dl-error" }, state.cloudError) : null,
    field(
      "Farm",
      farms
        ? select(
            farms.map((farm) => [farm.farmId, farm.displayName]),
            state.farmId,
            async (value) => {
              state.farmId = value;
              state.queueId = "";
              state.storageProfileId = "";
              await loadQueues();
              saveDefaults();
            },
            "Choose a farm"
          )
        : h("span", { className: "dl-muted" }, "Loading…"),
      "The Deadline Cloud farm to submit to."
    ),
    state.farmId
      ? field(
          "Queue",
          queues
            ? select(
                queues.map((queue) => [queue.queueId, queue.displayName]),
                state.queueId,
                async (value) => {
                  state.queueId = value;
                  state.storageProfileId = "";
                  await loadQueue();
                  saveDefaults();
                },
                "Choose a queue"
              )
            : h("span", { className: "dl-muted" }, "Loading…"),
          "The queue in this farm that runs the job."
        )
      : null,
    profiles.length
      ? field(
          "Storage profile",
          select(
            profiles.map((profile) => [profile.storageProfileId, profile.displayName]),
            state.storageProfileId,
            (value) => {
              state.storageProfileId = value;
              saveDefaults();
            },
            "None"
          ),
          "Tells Deadline Cloud which of your paths are on shared storage, so they aren't uploaded."
        )
      : null
  );
}

function renderStatus() {
  const plan = state.plan;
  const children = [];
  if (state.planError) {
    children.push(h("div", { className: "dl-notice dl-error" }, state.planError));
  }
  if (plan) {
    const count = currentTaskCount();
    const text = count === null ? "Check the work items below." : plural(count, "task");
    children.push(
      h(
        "div",
        { className: "dl-summary" },
        h("strong", { "data-role": "task-count" }, text),
        h("span", { className: "dl-muted" }, ` · ${plan.template}`)
      )
    );
    // With Attach Custom Nodes off, the workers provide the custom nodes.
    const ignored = attachesNodes() ? [] : plan.nodeWarnings ?? [];
    for (const warning of plan.warnings ?? []) {
      if (!ignored.includes(warning)) {
        children.push(h("div", { className: "dl-notice dl-warn-box" }, warning));
      }
    }
    const excluded = excludedModels();
    if (excluded.length) {
      children.push(
        h(
          "div",
          { className: "dl-notice dl-warn-box" },
          `Not attaching ${excluded.join(", ")}. The job fails unless Model Downloads fetches it or the workers already have it in the model cache or an extra model path.`
        )
      );
    }
  }
  children.push(
    h(
      "button",
      { className: "dl-link", onClick: () => loadPlan(true), disabled: state.planning },
      state.planning ? "Reading the workflow…" : "Read the workflow again"
    )
  );
  return h("div", { className: "dl-status" }, children);
}

function groupCountText(group) {
  return group.count === null ? "" : plural(group.count, "task");
}

// Updates what depends on the work item fields without re-rendering them, so they keep focus.
function updateCounts() {
  for (const view of views) {
    view.render(["status", "submit"]);
  }
  const groups = new Map(currentGroups().map((group) => [group.key, group]));
  for (const element of document.querySelectorAll("[data-role='task-math']")) {
    element.textContent = taskMath();
  }
  for (const element of document.querySelectorAll("[data-group-count]")) {
    element.textContent = groupCountText(groups.get(element.dataset.groupCount) ?? { count: null });
  }
  for (const element of document.querySelectorAll("[data-group-error]")) {
    const error = groups.get(element.dataset.groupError)?.error ?? "";
    element.textContent = error;
    element.hidden = !error;
  }
  for (const source of state.plan?.sources ?? []) {
    for (const element of document.querySelectorAll(`[data-preview="${source.parameter}"]`)) {
      element.textContent = itemsPreview(source, itemsValue(source));
    }
  }
}

function workItemField(source) {
  const preview = itemsPreview(source, itemsValue(source));
  return h(
    "label",
    { className: "dl-field" },
    h(
      "span",
      { className: "dl-label dl-path", "data-dl-tip": `${source.label}: ${source.count} items` },
      source.title
    ),
    h("input", {
      className: "dl-input",
      value: itemsValue(source),
      placeholder: source.range,
      onInput: (event) => {
        state.edits[source.parameter] = event.target.value;
        state.itemEditBases[source.parameter] = itemEditBase(source);
        updateCounts();
      },
    }),
    h("span", { className: "dl-preview dl-path", "data-preview": source.parameter }, preview)
  );
}

function renderWorkItems() {
  const sources = state.plan?.sources ?? [];
  if (!sources.length) {
    return null;
  }
  return section(
    ["Work items", h("span", { className: "dl-task-math", "data-role": "task-math" }, taskMath())],
    true,
    h(
      "p",
      { className: "dl-help" },
      "Each group below runs as separate tasks, and groups multiply. Number fields hold the values to run, such as 1-4 or 7,42. Other fields pick items by number, such as 1-2."
    ),
    currentGroups().map((group) =>
      h(
        "div",
        { className: `dl-item-group${group.pairWith ? " dl-item-pair" : ""}` },
        h(
          "div",
          { className: "dl-group-header" },
          h("span", { className: "dl-group-name" }, group.pairWith ?? ""),
          h("span", { className: "dl-muted", "data-group-count": group.key }, groupCountText(group))
        ),
        group.sources.map(workItemField),
        h(
          "div",
          {
            className: "dl-notice dl-error",
            "data-group-error": group.key,
            hidden: !group.error,
          },
          group.error ?? ""
        )
      )
    )
  );
}

function parameterControl(parameter, value, onChange) {
  const ui = parameter.userInterface ?? {};
  const control = ui.control ?? "";
  if (control === "DROPDOWN_LIST" || (parameter.allowedValues && control !== "CHECK_BOX")) {
    return select(
      (parameter.allowedValues ?? []).map((allowed) => [String(allowed), String(allowed)]),
      String(value),
      onChange
    );
  }
  if (control === "CHECK_BOX") {
    return h("input", {
      type: "checkbox",
      checked: String(value).toLowerCase() === "true",
      onChange: (event) => onChange(event.target.checked ? "true" : "false"),
    });
  }
  if (control === "MULTILINE_EDIT") {
    return h("textarea", {
      className: "dl-input",
      rows: 3,
      value: String(value),
      onInput: (event) => onChange(event.target.value),
    });
  }
  const number = parameter.type === "INT" || parameter.type === "FLOAT";
  return h("input", {
    className: "dl-input",
    type: number ? "number" : "text",
    min: parameter.minValue,
    max: parameter.maxValue,
    step: parameter.type === "FLOAT" ? "any" : null,
    value: String(value),
    onInput: (event) => onChange(event.target.value),
  });
}

function renderParameters() {
  const plan = state.plan;
  if (!plan) {
    return null;
  }
  const workItems = new Set((plan.sources ?? []).map((source) => source.parameter));
  const groups = new Map();
  for (const parameter of plan.parameters) {
    const ui = parameter.userInterface ?? {};
    if (
      AUTOMATIC_PARAMETERS.has(parameter.name) ||
      workItems.has(parameter.name) ||
      ui.control === "HIDDEN" ||
      ui.groupLabel === WORK_ITEMS_GROUP
    ) {
      continue;
    }
    const group = ui.groupLabel || "Job parameters";
    groups.set(group, [...(groups.get(group) ?? []), parameter]);
  }
  return [...groups].map(([group, parameters]) =>
    section(
      group,
      false,
      parameters.map((parameter) =>
        field(
          parameter.userInterface?.label || parameter.name,
          parameterControl(parameter, parameterValue(parameter), (value) => {
            state.edits[parameter.name] = value;
            if (parameter.name === ATTACH_NODES_PARAMETER) {
              refresh("status", "attachments");
            }
          }),
          parameter.description
        )
      )
    )
  );
}

function renderQueueParameters() {
  const parameters = (state.queue?.parameters ?? []).filter(
    (parameter) => parameter.userInterface?.control !== "HIDDEN"
  );
  if (!parameters.length) {
    return null;
  }
  return section(
    "Queue environment",
    false,
    parameters.map((parameter) =>
      field(
        parameter.userInterface?.label || parameter.name,
        parameterControl(parameter, queueParameterValue(parameter), (value) => {
          state.queueEdits[parameter.name] = value;
        }),
        parameter.description
      )
    )
  );
}

// Each hardware row: capability, label and scale. Memory capabilities are in MiB, and the fields
// are in GiB.
const HOST_HARDWARE = [
  ["amount.worker.vcpu", "vCPUs", 1],
  ["amount.worker.memory", "Memory (GiB)", 1024],
  ["amount.worker.gpu", "GPUs", 1],
  ["amount.worker.gpu.memory", "GPU memory (GiB)", 1024],
  ["amount.worker.disk.scratch", "Scratch space (GiB)", 1],
];
const HOST_CHOICES = [
  ["attr.worker.os.family", "Operating system", ["linux", "macos", "windows"]],
  ["attr.worker.cpu.arch", "CPU architecture", ["x86_64", "arm64"]],
];

function formFromRequirements(requirements) {
  const form = { choices: {}, hardware: {}, amounts: [], attributes: [] };
  for (const attribute of requirements?.attributes ?? []) {
    const known = HOST_CHOICES.some(([name]) => name === attribute.name);
    if (known && attribute.anyOf) {
      form.choices[attribute.name] = [...attribute.anyOf];
    } else {
      const mode = attribute.allOf ? "allOf" : "anyOf";
      form.attributes.push({
        name: attribute.name.replace(/^attr\./, ""),
        mode,
        values: (attribute[mode] ?? []).join(", "),
      });
    }
  }
  for (const amount of requirements?.amounts ?? []) {
    const known = HOST_HARDWARE.find(([name]) => name === amount.name);
    const toField = (value) => (value === undefined ? "" : String(value / (known?.[2] ?? 1)));
    const fields = { min: toField(amount.min), max: toField(amount.max) };
    if (known) {
      form.hardware[amount.name] = fields;
    } else {
      form.amounts.push({ name: amount.name.replace(/^amount\./, ""), ...fields });
    }
  }
  return form;
}

function amountRequirement(name, fields, scale = 1) {
  const requirement = { name };
  for (const key of ["min", "max"]) {
    if (fields[key] !== "" && fields[key] !== undefined) {
      requirement[key] = Number(fields[key]) * scale;
    }
  }
  return requirement;
}

function requirementsFromForm(form) {
  const attributes = [];
  for (const [name] of HOST_CHOICES) {
    if (form.choices[name]?.length) {
      attributes.push({ name, anyOf: form.choices[name] });
    }
  }
  for (const attribute of form.attributes) {
    const values = attribute.values.split(",").map((value) => value.trim()).filter(Boolean);
    attributes.push({ name: `attr.${attribute.name.trim()}`, [attribute.mode]: values });
  }
  const amounts = [];
  for (const [name, , scale] of HOST_HARDWARE) {
    const requirement = amountRequirement(name, form.hardware[name] ?? {}, scale);
    if (Object.keys(requirement).length > 1) {
      amounts.push(requirement);
    }
  }
  for (const amount of form.amounts) {
    amounts.push(amountRequirement(`amount.${amount.name.trim()}`, amount));
  }
  return { attributes, amounts };
}

function hostFormError() {
  if (!state.hostForm) {
    return null;
  }
  const { amounts, attributes } = requirementsFromForm(state.hostForm);
  for (const amount of amounts) {
    const label = amount.name.replace(/^amount\.(worker\.)?/, "");
    if (!amount.name.slice("amount.".length)) {
      return "Name each custom amount.";
    }
    if (amount.min === undefined && amount.max === undefined) {
      return `Set a min or a max for ${label}.`;
    }
    if (amount.min > amount.max) {
      return `The min of ${label} is more than its max.`;
    }
  }
  for (const attribute of attributes) {
    if (!attribute.name.slice("attr.".length)) {
      return "Name each custom attribute.";
    }
    if (!(attribute.anyOf ?? attribute.allOf).length) {
      return `Add a value for ${attribute.name.slice("attr.".length)}.`;
    }
  }
  return null;
}

function hostForm() {
  return state.hostForm ?? formFromRequirements(state.plan?.hostRequirements);
}

// The first edit copies the job template's requirements, so they aren't lost by changing one.
function editHostForm() {
  state.hostForm ??= formFromRequirements(state.plan?.hostRequirements);
  return state.hostForm;
}

function hostFormEdited() {
  for (const element of document.querySelectorAll("[data-role='host-error']")) {
    const error = hostFormError();
    element.textContent = error ?? "";
    element.hidden = !error;
  }
  for (const element of document.querySelectorAll("[data-role='host-reset']")) {
    element.hidden = !state.hostForm;
  }
  refresh("submit");
}

function hostNumber(placeholder, value, onInput) {
  return h("input", {
    className: "dl-input",
    type: "number",
    min: 0,
    step: "any",
    placeholder,
    value: value ?? "",
    onInput: (event) => {
      onInput(event.target.value);
      hostFormEdited();
    },
  });
}

function renderHostRequirements() {
  if (!state.plan) {
    return null;
  }
  const form = hostForm();
  const changed = (rerender) => {
    if (rerender) {
      refresh("hostRequirements");
    }
    hostFormEdited();
  };
  const choiceRows = HOST_CHOICES.map(([name, label, options]) =>
    h(
      "div",
      { className: "dl-field" },
      h(
        "span",
        { className: "dl-label", "data-dl-tip": "Workers must have one of the ones you check. Check none to allow any." },
        label
      ),
      h(
        "div",
        { className: "dl-row dl-choices" },
        options.map((option) =>
          h(
            "label",
            { className: "dl-row" },
            h("input", {
              type: "checkbox",
              checked: form.choices[name]?.includes(option) ?? false,
              onChange: (event) => {
                const edited = editHostForm();
                const chosen = new Set(edited.choices[name] ?? []);
                chosen[event.target.checked ? "add" : "delete"](option);
                edited.choices[name] = options.filter((item) => chosen.has(item));
                changed();
              },
            }),
            option
          )
        )
      )
    )
  );
  const hardwareRows = HOST_HARDWARE.map(([name, label]) => {
    const edit = (key) => (value) => {
      const edited = editHostForm();
      edited.hardware[name] = { ...edited.hardware[name], [key]: value };
    };
    return h(
      "div",
      { className: "dl-field" },
      h(
        "span",
        { className: "dl-label", "data-dl-tip": "The least and the most a worker can have to run this job. Leave either empty for no limit." },
        label
      ),
      h(
        "div",
        { className: "dl-range" },
        hostNumber("Min", form.hardware[name]?.min, edit("min")),
        hostNumber("Max", form.hardware[name]?.max, edit("max"))
      )
    );
  });
  const remove = (key, index) =>
    h(
      "button",
      {
        className: "dl-link",
        title: "Remove",
        onClick: () => {
          editHostForm()[key].splice(index, 1);
          changed(true);
        },
      },
      "✕"
    );
  const customName = (placeholder, key, index) =>
    h("input", {
      className: "dl-input",
      placeholder,
      value: form[key][index].name,
      onInput: (event) => {
        editHostForm()[key][index].name = event.target.value;
        hostFormEdited();
      },
    });
  const amountRows = form.amounts.map((amount, index) => {
    const edit = (field) => (value) => {
      editHostForm().amounts[index][field] = value;
    };
    return h(
      "div",
      { className: "dl-custom" },
      h("div", { className: "dl-row" }, customName("Amount name, such as license.nuke", "amounts", index), remove("amounts", index)),
      h(
        "div",
        { className: "dl-range" },
        hostNumber("Min", amount.min, edit("min")),
        hostNumber("Max", amount.max, edit("max"))
      )
    );
  });
  const attributeRows = form.attributes.map((attribute, index) =>
    h(
      "div",
      { className: "dl-custom" },
      h("div", { className: "dl-row" }, customName("Attribute name, such as software", "attributes", index), remove("attributes", index)),
      h(
        "div",
        { className: "dl-range" },
        select(
          [
            ["anyOf", "Any of"],
            ["allOf", "All of"],
          ],
          attribute.mode,
          (value) => {
            editHostForm().attributes[index].mode = value;
            hostFormEdited();
          }
        ),
        h("input", {
          className: "dl-input",
          placeholder: "Values, separated by commas",
          value: attribute.values,
          onInput: (event) => {
            editHostForm().attributes[index].values = event.target.value;
            hostFormEdited();
          },
        })
      )
    )
  );
  const error = hostFormError();
  return section(
    "Host requirements",
    false,
    h(
      "p",
      { className: "dl-help" },
      "Only workers that meet these run the job. If no worker in the queue's fleets does, its tasks wait."
    ),
    choiceRows,
    hardwareRows,
    amountRows,
    attributeRows,
    h(
      "div",
      { className: "dl-row" },
      h(
        "button",
        {
          className: "dl-button dl-small",
          onClick: () => {
            editHostForm().amounts.push({ name: "", min: "", max: "" });
            changed(true);
          },
        },
        "Add amount"
      ),
      h(
        "button",
        {
          className: "dl-button dl-small",
          onClick: () => {
            editHostForm().attributes.push({ name: "", mode: "anyOf", values: "" });
            changed(true);
          },
        },
        "Add attribute"
      ),
      h(
        "button",
        {
          className: "dl-link",
          "data-role": "host-reset",
          hidden: !state.hostForm,
          onClick: () => {
            state.hostForm = null;
            changed(true);
          },
        },
        "Use the template's"
      )
    ),
    h(
      "div",
      { className: "dl-notice dl-error", "data-role": "host-error", hidden: !error },
      error ?? ""
    )
  );
}

async function saveBundle() {
  if (hostFormError() || !state.plan) {
    toast("error", hostFormError() ?? "Wait for the workflow to be read.", 8000);
    return;
  }
  try {
    const result = await postJson("/deadline/bundle", {
      ...(await currentWorkflow()),
      ...bundleRequest(),
    });
    toast("success", `Saved the job bundle to ${result.job_bundle}`, 20000);
    for (const warning of result.warnings ?? []) {
      toast("warn", warning, 20000);
    }
  } catch (error) {
    toast("error", `Couldn't save the job bundle: ${error.message ?? error}`, 10000);
  }
}

function renderJobSettings() {
  const settings = state.settings;
  const set = (key) => (value) => {
    settings[key] = value;
    writeStorage(SETTINGS_KEY, settings);
  };
  const number = (key, placeholder, min, max) =>
    h("input", {
      className: "dl-input",
      type: "number",
      min,
      max,
      placeholder,
      value: settings[key],
      onInput: (event) => set(key)(event.target.value),
    });
  return section(
    "Job settings",
    false,
    field(
      "Name",
      h("input", {
        className: "dl-input",
        value: jobName(),
        onInput: (event) => {
          state.jobName = event.target.value;
        },
      }),
      "The job's name in Deadline Cloud monitor."
    ),
    field(
      "Description",
      h("textarea", {
        className: "dl-input",
        rows: 2,
        maxlength: 2048,
        placeholder: "The job template's description",
        value: state.jobDescription,
        onInput: (event) => {
          state.jobDescription = event.target.value;
        },
      }),
      "The job's description in Deadline Cloud monitor."
    ),
    field("Priority", number("priority", "50", 0, 100), "0–100. Jobs with a higher priority run first."),
    field(
      "Initial state",
      select(
        [
          ["READY", "Ready"],
          ["SUSPENDED", "Suspended"],
        ],
        settings.targetTaskRunStatus,
        set("targetTaskRunStatus")
      ),
      "Suspended jobs wait until you resume them in Deadline Cloud monitor."
    ),
    field("Max failed tasks", number("maxFailedTasksCount", "Queue default", 0), "The job fails after this many failed tasks."),
    field("Max retries per task", number("maxRetriesPerTask", "Queue default", 0), "How many times a failed task is retried."),
    field("Max workers", number("maxWorkerCount", "No limit", 1), "The most workers that can run this job at once."),
    field(
      "Job attachments",
      select(
        [
          ["COPIED", "Copied to the worker"],
          ["VIRTUAL", "Virtual file system"],
        ],
        settings.jobAttachmentsFileSystem,
        set("jobAttachmentsFileSystem"),
        "Default"
      ),
      "How workers read the attached input files. Virtual loads them as needed, on workers that support it."
    ),
    field(
      "Require all input paths exist",
      h("input", {
        type: "checkbox",
        checked: Boolean(settings.requirePathsExist),
        onChange: (event) => set("requirePathsExist")(event.target.checked),
      }),
      "Stop the submission if an attached file or folder is missing, instead of submitting without it."
    ),
    h(
      "button",
      {
        className: "dl-button dl-small",
        title: "Create the job bundle without submitting it, to submit later with deadline bundle submit.",
        onClick: saveBundle,
      },
      "Save job bundle"
    )
  );
}

function excludedModels() {
  return (state.plan?.localModels ?? [])
    .filter((model) => state.removedAttachments.has(model.path))
    .map((model) => model.name);
}

function attachmentItem(text, path, prefix, onRemove, onRestore) {
  const removed = state.removedAttachments.has(path);
  return h(
    "li",
    { title: path, className: removed ? "dl-removed" : null },
    prefix ? h("span", { className: "dl-muted" }, prefix) : null,
    h("span", { className: "dl-path" }, text),
    removed
      ? h("button", { className: "dl-link", onClick: onRestore }, "Add back")
      : h("button", { className: "dl-link", title: "Don't attach", onClick: onRemove }, "✕")
  );
}

function renderAttachments() {
  if (!state.plan) {
    return null;
  }
  // The files the workflow loads stay listed after you remove them, so you can add them back.
  const planned = state.plan.attachments ?? { filenames: [], directories: [] };
  const added = state.addedAttachments;
  const rows = new Map();
  for (const [paths, kind, manual] of [
    [planned.filenames, "file", false],
    [planned.directories, "folder", false],
    [added.filenames, "file", true],
    [added.directories, "folder", true],
  ]) {
    for (const path of paths) {
      if (!rows.has(path)) {
        rows.set(path, { path, kind, manual, key: kind === "file" ? "filenames" : "directories" });
      }
    }
  }
  const localModels = state.plan.localModels ?? [];
  const localNodes = attachesNodes() ? state.plan.localNodes ?? [] : [];
  const attachedCount =
    [...rows.keys()].filter((path) => !state.removedAttachments.has(path)).length +
    localModels.length -
    excludedModels().length +
    localNodes.length;
  const remove = (row) => () => {
    if (row.manual) {
      added[row.key] = added[row.key].filter((path) => path !== row.path);
    } else {
      state.removedAttachments.add(row.path);
    }
    refresh("attachments");
  };
  const restore = (row) => () => {
    state.removedAttachments.delete(row.path);
    refresh("attachments");
  };
  // Removing a model can make the job fail, so the status warns about it.
  const toggleModel = (model, removed) => () => {
    state.removedAttachments[removed ? "add" : "delete"](model.path);
    refresh("status", "attachments");
  };
  // The dialog opens on the ComfyUI machine, whose files the job attaches.
  const add = (kind, key) => async () => {
    let paths;
    try {
      ({ paths } = await postJson("/deadline/choose", { kind }));
    } catch (error) {
      toast("error", error.message, 8000);
      return;
    }
    for (const path of paths) {
      if (!state.addedAttachments[key].includes(path)) {
        state.addedAttachments[key].push(path);
      }
      state.removedAttachments.delete(path);
    }
    refresh("attachments");
  };
  return section(
    `Attachments (${attachedCount})`,
    false,
    h(
      "p",
      { className: "dl-help" },
      "The input files and folders uploaded with the job, from the ComfyUI machine. Outputs come back to the output directory."
    ),
    localModels.length
      ? [
          h("div", { className: "dl-label" }, "Models with no download link, attached automatically"),
          h(
            "ul",
            { className: "dl-files" },
            localModels.map((model) =>
              attachmentItem(
                model.name,
                model.path,
                null,
                toggleModel(model, true),
                toggleModel(model, false)
              )
            )
          ),
        ]
      : null,
    localNodes.length
      ? [
          h("div", { className: "dl-label" }, "Custom nodes with no registry pack, attached automatically"),
          h(
            "ul",
            { className: "dl-files" },
            localNodes.map((pack) =>
              h("li", { title: pack.path }, h("span", { className: "dl-path" }, pack.name))
            )
          ),
        ]
      : null,
    rows.size
      ? h(
          "ul",
          { className: "dl-files" },
          [...rows.values()]
            .slice(0, MAX_LISTED_ATTACHMENTS)
            .map((row) =>
              attachmentItem(
                row.path,
                row.path,
                row.kind === "folder" ? "▸ " : "",
                remove(row),
                restore(row)
              )
            ),
          rows.size > MAX_LISTED_ATTACHMENTS
            ? h("li", { className: "dl-muted" }, `and ${rows.size - MAX_LISTED_ATTACHMENTS} more`)
            : null
        )
      : h("div", { className: "dl-muted" }, "The workflow loads no input files."),
    h(
      "div",
      { className: "dl-row" },
      h("button", { className: "dl-button dl-small", onClick: add("files", "filenames") }, "Add files…"),
      h("button", { className: "dl-button dl-small", onClick: add("folder", "directories") }, "Add folder…")
    )
  );
}

function renderSubmit() {
  const submission = state.submission;
  if (submission) {
    const percent = typeof submission.progress === "number" ? Math.round(submission.progress) : null;
    const stage = { hashing: "Hashing input files", uploading: "Uploading input files" }[submission.stage];
    return h(
      "div",
      { className: "dl-progress" },
      h("div", {}, stage ? `${stage}${percent === null ? "" : ` · ${percent}%`}` : "Submitting…"),
      percent === null ? null : h("progress", { max: 100, value: percent }),
      submission.message ? h("div", { className: "dl-muted dl-message" }, submission.message) : null,
      submission.confirm
        ? h(
            "div",
            { className: "dl-notice dl-warn-box" },
            h("div", { className: "dl-message" }, submission.confirm.message),
            h(
              "div",
              { className: "dl-row" },
              h("button", { className: "dl-button dl-primary", onClick: () => answerConfirmation(true) }, "Continue"),
              h("button", { className: "dl-button", onClick: () => answerConfirmation(false) }, "Don't submit")
            )
          )
        : null,
      submission.id
        ? h("button", { className: "dl-button", onClick: cancelSubmission }, "Cancel")
        : null
    );
  }
  const count = currentTaskCount();
  const label = count ? `Submit ${count} ${count === 1 ? "task" : "tasks"}` : "Submit";
  return h(
    "button",
    { className: "dl-button dl-primary dl-wide", disabled: !canSubmit(), onClick: submit },
    label
  );
}

function statusText(job) {
  const status = state.jobStatus[job.jobId];
  if (!status) {
    return "…";
  }
  if (status.error) {
    return "Unknown";
  }
  const counts = status.taskRunStatusCounts ?? {};
  const total = Object.values(counts).reduce((sum, count) => sum + count, 0);
  const done = counts.SUCCEEDED ?? 0;
  const failed = counts.FAILED ?? 0;
  const progress = total ? ` · ${done}/${total}${failed ? `, ${failed} failed` : ""}` : "";
  const name = status.taskRunStatus ?? status.lifecycleStatus ?? "";
  return `${name.charAt(0)}${name.slice(1).toLowerCase().replaceAll("_", " ")}${progress}`;
}

// Downloads by job ID: { status, progress }.
const downloads = {};

function canDownload(job) {
  const counts = state.jobStatus[job.jobId]?.taskRunStatusCounts ?? {};
  return (counts.SUCCEEDED ?? 0) > 0 && downloads[job.jobId]?.status !== "DOWNLOADING";
}

function downloadText(job) {
  const download = downloads[job.jobId];
  if (download?.status !== "DOWNLOADING") {
    return null;
  }
  const percent = Number(download.progress);
  return `Downloading outputs${Number.isFinite(percent) ? ` · ${percent}%` : "…"}`;
}

async function downloadOutputs(job) {
  downloads[job.jobId] = { status: "DOWNLOADING" };
  refresh("jobs");
  try {
    await postJson("/deadline/jobs/download", {
      farmId: job.farmId,
      queueId: job.queueId,
      jobId: job.jobId,
    });
  } catch (error) {
    delete downloads[job.jobId];
    toast("error", error.message, 10000);
    refresh("jobs");
  }
}

function listenForDownloads() {
  api.addEventListener("deadline.download", ({ detail }) => {
    const job = state.jobs.find((other) => other.jobId === detail.jobId);
    const name = job?.name ?? detail.jobId;
    if (detail.status === "DOWNLOADING") {
      downloads[detail.jobId] = { status: "DOWNLOADING", progress: detail.progress };
    } else {
      delete downloads[detail.jobId];
      if (detail.status === "DOWNLOADED") {
        toast("success", `${detail.message} from ${name} into the output directory it was submitted with.`, 10000);
      } else {
        toast("error", `Couldn't download the outputs of ${name}. ${detail.error ?? ""}`, 15000);
      }
    }
    refresh("jobs");
  });
}

function renderJobs() {
  if (!state.jobs.length) {
    return null;
  }
  return section(
    "Recent jobs",
    true,
    h(
      "ul",
      { className: "dl-jobs" },
      state.jobs.map((job) =>
        h(
          "li",
          { title: `${job.jobId}\nSubmitted ${new Date(job.submittedAt).toLocaleString()}` },
          h("div", { className: "dl-path" }, job.name),
          h(
            "div",
            { className: "dl-row dl-muted" },
            h("span", {}, downloadText(job) ?? statusText(job)),
            canDownload(job)
              ? h("button", { className: "dl-link", onClick: () => downloadOutputs(job) }, "Download")
              : null,
            h(
              "button",
              {
                className: "dl-link",
                onClick: () => {
                  navigator.clipboard?.writeText(job.jobId);
                  toast("info", `Copied ${job.jobId}.`, 2000);
                },
              },
              "Copy job ID"
            )
          )
        )
      )
    ),
    h(
      "button",
      {
        className: "dl-link",
        onClick: () => {
          state.jobs = [];
          writeStorage(JOBS_KEY, []);
          refresh("jobs");
        },
      },
      "Clear"
    )
  );
}

const RENDERERS = {
  account: renderAccount,
  target: renderTarget,
  status: renderStatus,
  workItems: renderWorkItems,
  jobSettings: renderJobSettings,
  parameters: renderParameters,
  queueParameters: renderQueueParameters,
  hostRequirements: renderHostRequirements,
  attachments: renderAttachments,
  submit: renderSubmit,
  jobs: renderJobs,
};

// A view renders some of the sections into a container, and re-renders them when they change.
function createView(container, sections) {
  const slots = {};
  for (const name of sections) {
    slots[name] = h("div", { className: `dl-slot dl-slot-${name}` });
    container.append(slots[name]);
  }
  const view = {
    render(names) {
      for (const name of names ?? sections) {
        const slot = slots[name];
        if (!slot) {
          continue;
        }
        // Keep the open/closed state of collapsible sections across re-renders.
        const open = [...slot.querySelectorAll("details")].map((details) => details.open);
        const content = RENDERERS[name]();
        slot.replaceChildren(...[content].flat().filter(Boolean));
        slot.querySelectorAll("details").forEach((details, index) => {
          if (index < open.length) {
            details.open = open[index];
          }
        });
      }
    },
  };
  views.add(view);
  view.render();
  return view;
}

// ---------------------------------------------------------------------------------------------
// The sidebar panel and the dialog

export function renderPanel(element) {
  injectStyles();
  element.replaceChildren();
  // The sidebar's wrapper has no height, so without this the panel grows instead of scrolling, and
  // the sticky submit button moves off screen.
  element.style.height = "100%";
  const root = h(
    "div",
    { className: "dl-root dl-panel" },
    h(
      "div",
      { className: "dl-header" },
      h("i", { className: "pi pi-cloud-upload" }),
      h("span", {}, "Deadline Cloud")
    )
  );
  element.append(root);
  const view = createView(root, [
    "account",
    "target",
    "status",
    "workItems",
    "jobSettings",
    "parameters",
    "queueParameters",
    "hostRequirements",
    "attachments",
    "jobs",
    // Last, and stuck to the bottom.
    "submit",
  ]);
  loadAccount();
  loadPlan();
  pollJobs();
  return () => views.delete(view);
}

let dialog = null;

export function openDialog() {
  injectStyles();
  if (dialog) {
    loadPlan();
    return;
  }
  const close = () => {
    views.delete(dialog.view);
    dialog.overlay.remove();
    document.removeEventListener("keydown", onKey);
    dialog = null;
  };
  const onKey = (event) => {
    if (event.key === "Escape" && !state.submission?.confirm) {
      close();
    }
  };
  const body = h("div", { className: "dl-dialog-body" });
  const box = h(
    "div",
    { className: "dl-root dl-dialog", role: "dialog", "aria-label": "Submit to Deadline Cloud" },
    h(
      "div",
      { className: "dl-header" },
      h("i", { className: "pi pi-cloud-upload" }),
      h("span", {}, "Submit to Deadline Cloud"),
      h("button", { className: "dl-link dl-close", title: "Close", onClick: close }, "✕")
    ),
    body,
    h(
      "div",
      { className: "dl-dialog-footer" },
      h(
        "button",
        {
          className: "dl-link",
          onClick: () => {
            close();
            openPanel();
          },
        },
        "All settings…"
      )
    )
  );
  const overlay = h(
    "div",
    {
      className: "dl-overlay",
      onClick: (event) => {
        if (event.target === overlay && !state.submission) {
          close();
        }
      },
    },
    box
  );
  document.body.append(overlay);
  document.addEventListener("keydown", onKey);
  dialog = { overlay, view: null };
  dialog.view = createView(body, ["account", "target", "status", "workItems", "submit"]);
  loadAccount();
  loadPlan();
}

export function openPanel() {
  const workspace = app.extensionManager;
  if (workspace.sidebarTab?.activeSidebarTabId !== SIDEBAR_TAB_ID) {
    // The frontend registers this command for each sidebar tab. It closes the tab if it's open.
    workspace.command.execute(`Workspace.ToggleSidebarTab.${SIDEBAR_TAB_ID}`);
  }
}

// ---------------------------------------------------------------------------------------------
// Styles, from ComfyUI's theme variables so they follow its light and dark palettes

let stylesInjected = false;

function injectStyles() {
  if (stylesInjected) {
    return;
  }
  stylesInjected = true;
  listenForTooltips();
  document.head.append(
    h(
      "style",
      {},
      `
.dl-root { --dl-fg: var(--fg-color, #ddd); --dl-muted: var(--descrip-text, #999);
  --dl-input: var(--comfy-input-bg, #222); --dl-border: var(--border-color, #444);
  --dl-bg: var(--comfy-menu-bg, #353535); --dl-accent: var(--p-primary-color, #3b82f6);
  color: var(--dl-fg); font-size: 13px; display: flex; flex-direction: column; gap: 10px; }
.dl-panel { padding: 12px 12px 0; height: 100%; overflow-y: auto; box-sizing: border-box; }
.dl-slot-submit { position: sticky; bottom: 0; z-index: 1; margin-top: auto; padding: 10px 0 12px;
  background: var(--dl-bg); border-top: 1px solid var(--dl-border); }
.dl-dialog .dl-slot-submit { padding-bottom: 4px; }
.dl-header { display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 14px; }
.dl-slot:empty { display: none; }
.dl-task-math { float: right; font-weight: 400; color: var(--dl-muted); }
.dl-item-group { margin-bottom: 10px; }
.dl-item-pair { border-left: 2px solid var(--dl-border); padding-left: 8px; }
.dl-group-header { display: flex; justify-content: space-between; gap: 8px; font-size: 12px; margin-bottom: 4px; }
.dl-group-name { font-weight: 600; }
.dl-preview { color: var(--dl-muted); font-size: 11px; }
.dl-preview:empty { display: none; }
.dl-notice[hidden] { display: none; }
.dl-grid { display: flex; flex-direction: column; gap: 8px; }
.dl-field { display: flex; flex-direction: column; gap: 3px; margin-bottom: 6px; }
.dl-link[hidden] { display: none; }
.dl-range { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
.dl-choices { flex-wrap: wrap; gap: 10px; }
.dl-custom { display: flex; flex-direction: column; gap: 4px; margin-bottom: 8px; }
.dl-field-check { flex-direction: row-reverse; justify-content: flex-end; align-items: center; gap: 6px; }
.dl-field-check input { margin: 0; flex: none; }
.dl-label { color: var(--dl-muted); font-size: 12px; }
.dl-label[data-dl-tip] { align-self: flex-start; max-width: 100%; cursor: help;
  text-decoration: underline dotted; text-underline-offset: 2px; }
.dl-field-check .dl-label[data-dl-tip] { align-self: center; }
.dl-tooltip { position: fixed; z-index: 3000; max-width: 320px; padding: 7px 9px; border-radius: 6px;
  background: var(--comfy-menu-bg, #353535); color: var(--fg-color, #ddd);
  border: 1px solid var(--border-color, #444); box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
  font-size: 12px; line-height: 1.4; pointer-events: none; }
.dl-input { background: var(--dl-input); color: var(--dl-fg); border: 1px solid var(--dl-border);
  border-radius: 6px; padding: 5px 7px; font: inherit; width: 100%; box-sizing: border-box; min-width: 0; }
textarea.dl-input { resize: vertical; }
.dl-section { border: 1px solid var(--dl-border); border-radius: 8px; padding: 0 10px; }
.dl-section > summary { cursor: pointer; padding: 8px 0; font-weight: 600; }
.dl-section-body { padding-bottom: 8px; }
.dl-help { color: var(--dl-muted); font-size: 12px; margin: 0 0 8px; }
.dl-muted { color: var(--dl-muted); }
.dl-row { display: flex; align-items: center; gap: 6px; }
.dl-button { background: var(--dl-input); color: var(--dl-fg); border: 1px solid var(--dl-border);
  border-radius: 6px; padding: 6px 12px; cursor: pointer; font: inherit; white-space: nowrap; }
.dl-button:disabled { opacity: 0.5; cursor: default; }
.dl-primary { background: var(--dl-accent); border-color: var(--dl-accent); color: #fff; font-weight: 600; }
.dl-small { padding: 3px 8px; font-size: 12px; }
.dl-wide { width: 100%; padding: 9px; }
.dl-link { background: none; border: none; color: var(--dl-accent); cursor: pointer; padding: 0 2px; font: inherit; }
.dl-link:disabled { color: var(--dl-muted); cursor: default; }
.dl-account { display: flex; align-items: center; gap: 8px; }
.dl-dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.dl-ok { background: #22c55e; } .dl-warn { background: #f59e0b; }
.dl-notice { border-radius: 6px; padding: 7px 9px; font-size: 12px; }
.dl-error { background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.5); }
.dl-warn-box { background: rgba(245, 158, 11, 0.12); border: 1px solid rgba(245, 158, 11, 0.45); }
.dl-status { display: flex; flex-direction: column; gap: 6px; align-items: flex-start; }
.dl-summary strong { font-size: 15px; }
.dl-files, .dl-jobs { list-style: none; margin: 0 0 8px; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.dl-files li { display: flex; align-items: center; gap: 4px; }
.dl-removed .dl-path { text-decoration: line-through; color: var(--dl-muted); }
.dl-path { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 0; }
.dl-jobs li { border-bottom: 1px solid var(--dl-border); padding-bottom: 4px; }
.dl-jobs .dl-row { justify-content: space-between; font-size: 12px; }
.dl-progress { display: flex; flex-direction: column; gap: 6px; }
.dl-progress progress { width: 100%; }
.dl-message { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 200px; overflow-y: auto; }
.dl-overlay { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.5); z-index: 2000;
  display: flex; align-items: center; justify-content: center; padding: 16px; }
.dl-dialog { background: var(--dl-bg); border: 1px solid var(--dl-border); border-radius: 12px;
  width: min(440px, 100%); max-height: calc(100vh - 32px); padding: 16px; box-sizing: border-box;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.4); }
.dl-dialog-body { display: flex; flex-direction: column; gap: 10px; overflow-y: auto; min-height: 0; }
.dl-dialog-footer { display: flex; justify-content: flex-end; }
.dl-close { margin-left: auto; color: var(--dl-muted); }
`
    )
  );
}
