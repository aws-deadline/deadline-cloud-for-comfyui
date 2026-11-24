// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// The Deadline Cloud sidebar panel and submit dialog, which render from the same state.
import { app, type ToastMessage } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// ---------------------------------------------------------------------------------------------
// What the extension's /deadline routes and the Deadline Cloud library send back

type ParameterValue = string | number | boolean;

interface Account {
  authenticationStatus: string;
  profile?: string;
  farmId?: string;
  queueId?: string;
  storageProfileId?: string;
  jobAttachmentsFileSystem?: string;
}

interface Farm {
  farmId: string;
  displayName: string;
}

interface Queue {
  queueId: string;
  displayName: string;
}

interface StorageProfile {
  storageProfileId: string;
  displayName: string;
}

// An OpenJD job parameter definition, from the job template or a queue environment.
interface Parameter {
  name: string;
  type?: string;
  default?: ParameterValue;
  description?: string;
  minValue?: number;
  maxValue?: number;
  allowedValues?: ParameterValue[];
  userInterface?: { control?: string; label?: string; groupLabel?: string };
}

interface QueueDetails {
  parameters: Parameter[];
  storageProfiles: StorageProfile[];
}

interface PathLists {
  filenames: string[];
  directories: string[];
}

// A folder loader's folder, and the files attached from it, which the plan's filenames lists too.
interface FolderFiles {
  path: string;
  files: string[];
}

// The input files and folders the workflow loads. The panel lists each folder's files under it.
interface PlannedAttachments extends PathLists {
  folders?: FolderFiles[];
}

interface AttributeRequirement {
  name: string;
  anyOf?: string[];
  allOf?: string[];
}

interface AmountRequirement {
  name: string;
  min?: number;
  max?: number;
}

interface HostRequirements {
  attributes?: AttributeRequirement[];
  amounts?: AmountRequirement[];
}

// A list in the workflow that runs as tasks, such as a Create List node, as the plan gives it.
interface TaskList {
  name: string;
  // The job parameter that holds the range of items to run.
  parameter: string;
  label: string;
  title: string;
  kind: string;
  values: string[];
  count: number;
  range: string;
}

// What /deadline/plan returns for the workflow.
// A custom node pack the job attaches, and its size and file count. counted is false when the
// pack had more files than the server counts.
interface LocalNode {
  name: string;
  path: string;
  size?: number;
  files?: number;
  counted?: boolean;
}

interface Plan {
  name: string;
  template: string;
  parameters: Parameter[];
  values?: Record<string, ParameterValue>;
  queueValues?: Record<string, string>;
  attachments?: PlannedAttachments;
  hostRequirements?: HostRequirements;
  // id is the model's folder and name, such as checkpoints/sd.safetensors. Two models can share a
  // name in different folders.
  localModels?: { id: string; name: string; directory: string | null; path: string }[];
  localNodes?: LocalNode[];
  warnings?: string[];
  nodeWarnings?: string[];
  // Warnings about splitting the lists into tasks, which only apply to Multiple tasks.
  taskWarnings?: string[];
  sources?: TaskList[];
}

// A confirmation prompt the server relayed. Its answer names promptId, so it can't answer another.
interface Confirmation {
  message: string;
  default?: boolean;
  promptId?: string;
}

interface Submission {
  id: string;
  starting: boolean;
  message?: string;
  name?: string;
  stage?: string;
  progress?: number;
  confirm?: Confirmation | null;
}

interface Job {
  jobId: string;
  name: string;
  farmId: string;
  queueId: string;
  submittedAt: string;
  // Where Download puts the outputs, on the ComfyUI machine. Jobs saved by older panels have none.
  outputDir?: string;
}

interface JobStatus {
  jobId: string;
  taskRunStatus?: string;
  lifecycleStatus?: string;
  taskRunStatusCounts?: Record<string, number>;
  error?: string;
  // The job doesn't exist, or these credentials can't see it, so it isn't polled again.
  notFound?: boolean;
}

// The deadline.* events the routes send over ComfyUI's websocket.
interface SubmissionEvent {
  submissionId: string;
  name?: string;
  kind?: string;
  stage?: string;
  progress?: number;
  message?: string;
  default?: boolean;
  promptId?: string;
  status?: "SUBMITTED" | "FAILED" | "CANCELED";
  jobId?: string;
  farmId?: string;
  queueId?: string;
  outputDir?: string;
  error?: string;
  // A job that was created although the submission didn't finish, or may have been.
  warning?: string;
}

interface DownloadEvent {
  jobId: string;
  status: "DOWNLOADING" | "DOWNLOADED" | "FAILED";
  progress?: string;
  message?: string;
  fileCount?: number;
  // The folder the CLI downloaded the files into.
  directory?: string;
  error?: string;
}

// What the artist changed in one workflow's attachments: the files the job doesn't attach, by
// path, and the ones they added with the file dialog.
interface AttachmentEdits {
  removed: Set<string>;
  added: PathLists;
}

// The host requirements form. Its numbers are kept as the text in the fields.
interface RangeFields {
  min?: string;
  max?: string;
}

interface HostForm {
  choices: Record<string, string[]>;
  hardware: Record<string, RangeFields>;
  amounts: (RangeFields & { name: string })[];
  attributes: { name: string; mode: "anyOf" | "allOf"; values: string }[];
}

type Settings = Record<string, string | number | boolean>;

export const SUMMARY = "Deadline Cloud";
export const SIDEBAR_TAB_ID = "deadline-cloud";

const SETTINGS_KEY = "DeadlineCloud.Submitter.JobSettings";
const JOBS_KEY = "DeadlineCloud.Submitter.RecentJobs";
const MAX_RECENT_JOBS = 20;
const JOB_POLL_MS = 20000;
// The value of the storage profile select's None, when it has a "Choose…" placeholder as well.
const NO_STORAGE_PROFILE = "none";
// The open workflow tab when the frontend doesn't say which it is.
const DEFAULT_TAB = "ComfyUI workflow";
const PLAN_DEBOUNCE_MS = 1000;
const TOOLTIP_DELAY_MS = 200;
const TOOLTIP_GAP = 6;
const TOOLTIP_MARGIN = 8;
// The submitter fills these in when it creates the job bundle.
const AUTOMATIC_PARAMETERS = new Set(["WorkflowFile"]);
const ATTACH_NODES_PARAMETER = "AttachCustomNodes";
const DEFAULT_SETTINGS: Settings = {
  requirePathsExist: false,
  priority: 50,
  targetTaskRunStatus: "READY",
  maxFailedTasksCount: "",
  maxRetriesPerTask: "",
  maxWorkerCount: "",
  jobAttachmentsFileSystem: "",
};
const DONE_STATUSES = new Set(["SUCCEEDED", "FAILED", "CANCELED"]);
// How many of a field's chosen items it previews.
const PREVIEW_ITEMS = 3;

function readStorage<T>(key: string, fallback: T): T {
  try {
    const value = JSON.parse(localStorage.getItem(key) ?? "null");
    return value ?? fallback;
  } catch {
    return fallback;
  }
}

function writeStorage(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Private windows and full storage only lose the remembered settings.
  }
}

export function toast(severity: ToastMessage["severity"], detail: string, life = 5000) {
  app.extensionManager.toast.add({ severity, summary: SUMMARY, detail, life });
}

// ---------------------------------------------------------------------------------------------
// State

interface State {
  account: Account | null;
  accountError: string | null;
  loggingIn: boolean;
  farms: Farm[] | null;
  queues: Queue[] | null;
  // The chosen queue's environment parameters and storage profiles, once they load.
  queue: QueueDetails | null;
  // The farm and queue that queue is for, as targetKey gives them.
  queueKey: string | null;
  queueError: string | null;
  cloudError: string | null;
  farmId: string;
  queueId: string;
  // The storage profile to submit with: null until the artist chooses one or a default loads, and
  // "" for None, which a reload of the account's defaults doesn't replace.
  storageProfileId: string | null;
  // A default or earlier storage profile that the chosen queue doesn't list, so it was cleared.
  storageProfileCleared: string | null;
  plan: Plan | null;
  // The workflow the plan was made from, as workflowKey gives it.
  planKey: string | null;
  // The workflow tab the plan was made from, as workflowTab gives it.
  planTab: string | null;
  planError: string | null;
  planning: boolean;
  // The artist's values, which win over the ones the submitter fills in.
  edits: Record<string, ParameterValue>;
  // What each edited list field's node held when the artist edited the field.
  itemEditBases: Record<string, string>;
  // The workflows whose lists the artist split into tasks, by name. Any other workflow runs as one
  // task, with each list's items in it, as in ComfyUI.
  splitWorkflows: Set<string>;
  queueEdits: Record<string, ParameterValue>;
  jobName: string | null;
  jobDescription: string;
  // The host requirements form, once the artist edits it. Until then the job template's apply.
  hostForm: HostForm | null;
  settings: Settings;
  // Each workflow tab's attachment changes, so files added for one workflow never go with another.
  attachmentEdits: Map<string, AttachmentEdits>;
  // The folders whose files the attachments list shows, by path. The rest show as one row each.
  openFolders: Set<string>;
  submission: Submission | null;
  jobs: Job[];
  jobStatus: Record<string, JobStatus>;
}

const state: State = {
  account: null,
  accountError: null,
  loggingIn: false,
  farms: null,
  queues: null,
  queue: null,
  queueKey: null,
  queueError: null,
  cloudError: null,
  farmId: "",
  queueId: "",
  storageProfileId: null,
  storageProfileCleared: null,
  plan: null,
  planKey: null,
  planTab: null,
  planError: null,
  planning: false,
  edits: {},
  itemEditBases: {},
  splitWorkflows: new Set(),
  queueEdits: {},
  jobName: null,
  jobDescription: "",
  hostForm: null,
  settings: { ...DEFAULT_SETTINGS, ...readStorage<Settings>(SETTINGS_KEY, {}) },
  attachmentEdits: new Map(),
  openFolders: new Set(),
  submission: null,
  jobs: readStorage<Job[]>(JOBS_KEY, []),
  jobStatus: {},
};

type SectionName =
  | "account"
  | "target"
  | "status"
  | "tasks"
  | "jobSettings"
  | "parameters"
  | "queueParameters"
  | "hostRequirements"
  | "attachments"
  | "submit"
  | "jobs";

interface View {
  render(names?: SectionName[] | null): void;
}

const views = new Set<View>();

// Re-renders the named sections of every open view, or all of them.
function refresh(...sections: SectionName[]) {
  for (const view of views) {
    view.render(sections.length ? sections : null);
  }
}

// ---------------------------------------------------------------------------------------------
// Server calls

// The message of anything thrown, which isn't always an Error.
function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

// An error from a /deadline route, with its HTTP status.
class RouteError extends Error {
  constructor(
    message: string,
    readonly status: number
  ) {
    super(message);
  }
}

async function errorMessage(response: Response): Promise<string> {
  try {
    return (await response.json()).error ?? `The server returned ${response.status}.`;
  } catch {
    // Not JSON, for example a 404 from an older server.
    return `The server returned ${response.status}.`;
  }
}

// Posts to a /deadline route. T is what the route returns, which the server is trusted to send.
async function postJson<T>(path: string, body?: object): Promise<T> {
  let response: Response;
  try {
    response = await api.fetchApi(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
  } catch (error) {
    throw new Error(`Couldn't reach the ComfyUI server: ${messageOf(error)}`);
  }
  if (!response.ok) {
    throw new RouteError(await errorMessage(response), response.status);
  }
  return response.json() as Promise<T>;
}

function cloud<T>(command: string, body?: object): Promise<T> {
  return postJson<T>(`/deadline/cloud/${command}`, body);
}

// The workflow in both formats, as the routes take it.
interface WorkflowBody {
  workflow: Record<string, unknown>;
  ui_workflow: Record<string, unknown>;
  name: string;
}

export async function currentWorkflow(): Promise<WorkflowBody> {
  // The UI-format workflow carries the model folders that loaders and templates record.
  const { output, workflow } = await app.graphToPrompt();
  const name = app.extensionManager.workflow?.activeWorkflow?.filename || DEFAULT_TAB;
  return { workflow: output, ui_workflow: workflow, name };
}

// The open workflow tab: its path, which no other open tab has, or its file name on frontends that
// don't give the path. Each tab keeps its own attachment changes.
function workflowTab(): string {
  const workflow = app.extensionManager.workflow?.activeWorkflow;
  return workflow?.path || workflow?.filename || DEFAULT_TAB;
}

// ---------------------------------------------------------------------------------------------
// Loading

let accountLoad: Promise<void> | null = null;

export function loadAccount(force = false) {
  if (accountLoad && !force) {
    return accountLoad;
  }
  accountLoad = (async () => {
    state.accountError = null;
    refresh("account");
    try {
      const account = await cloud<Account>("status");
      state.account = account;
      state.farmId ||= account.farmId ?? "";
      state.queueId ||= account.queueId ?? "";
      // Only fills in a profile nobody chose. None, which the artist chose, stays None.
      if (state.storageProfileId === null && account.storageProfileId) {
        state.storageProfileId = account.storageProfileId;
      }
      if (!state.settings.jobAttachmentsFileSystem && account.jobAttachmentsFileSystem) {
        state.settings.jobAttachmentsFileSystem = account.jobAttachmentsFileSystem;
      }
    } catch (error) {
      state.accountError = messageOf(error);
    }
    refresh();
    if (state.account?.authenticationStatus === "AUTHENTICATED") {
      // Recent jobs waits for the account, so poll now rather than at the next timer.
      pollJobs();
      await loadFarms();
    }
  })();
  return accountLoad;
}

async function loadFarms() {
  state.cloudError = null;
  try {
    state.farms = (await cloud<{ farms: Farm[] }>("farms")).farms;
    // A default farm that isn't listed can't be shown as chosen, so it isn't submitted to.
    if (state.farmId && !state.farms.some((farm) => farm.farmId === state.farmId)) {
      state.farmId = "";
      state.queueId = "";
      forgetQueue();
    }
  } catch (error) {
    state.cloudError = messageOf(error);
  }
  refresh("target");
  if (state.farmId) {
    await loadQueues();
  }
}

// Identifies the farm and queue that queue details are for.
function targetKey(farmId: string, queueId: string): string {
  return JSON.stringify([farmId, queueId]);
}

// Each request for queue details, so an answer that a newer request replaced, such as an earlier
// one for the same queue, is ignored.
let queueRequest = 0;

// Forgets the chosen queue's details and what came from them, when the farm or queue changes: its
// parameter values and storage profile were for that queue only.
function forgetQueue() {
  queueRequest++;
  state.queue = null;
  state.queueKey = null;
  state.queueError = null;
  state.queueEdits = {};
  state.storageProfileId = null;
  state.storageProfileCleared = null;
}

async function loadQueues() {
  const farmId = state.farmId;
  state.queues = null;
  // The details of a queue from before are no use until this farm's queues are listed again.
  queueRequest++;
  state.queue = null;
  state.queueKey = null;
  state.queueError = null;
  refresh("target", "queueParameters", "submit");
  try {
    const { queues } = await cloud<{ queues: Queue[] }>("queues", { farmId });
    // Each request runs its own bridge, so an earlier farm's answer can come last.
    if (state.farmId !== farmId) {
      return;
    }
    state.queues = queues;
    // A default queue that isn't listed can't be shown as chosen, so it isn't submitted to.
    if (state.queueId && !queues.some((queue) => queue.queueId === state.queueId)) {
      state.queueId = "";
      forgetQueue();
    }
  } catch (error) {
    if (state.farmId !== farmId) {
      return;
    }
    state.cloudError = messageOf(error);
  }
  refresh("target");
  if (state.queueId && state.queues?.some((queue) => queue.queueId === state.queueId)) {
    await loadQueue();
  }
}

async function loadQueue() {
  const { farmId, queueId } = state;
  const key = targetKey(farmId, queueId);
  const request = ++queueRequest;
  // Whether the answer is still the one to use: from the latest request, for the chosen queue.
  // An earlier queue's answer, or an earlier one for this queue, can come last.
  const current = () => request === queueRequest && targetKey(state.farmId, state.queueId) === key;
  // Until this queue's details load, nothing from another queue's is shown or submitted.
  state.queue = null;
  state.queueKey = null;
  state.queueError = null;
  refresh("target", "queueParameters", "submit");
  try {
    const queue = await cloud<QueueDetails>("queue", { farmId, queueId });
    if (!current()) {
      return;
    }
    state.queue = queue;
    state.queueKey = key;
    // A default or earlier profile this queue doesn't list is cleared, and Submit says why.
    const chosen = state.storageProfileId;
    if (chosen && !queue.storageProfiles.some((profile) => profile.storageProfileId === chosen)) {
      state.storageProfileCleared = chosen;
      state.storageProfileId = null;
    }
  } catch (error) {
    if (!current()) {
      return;
    }
    state.queueError = `Couldn't load the queue's details: ${messageOf(error)}`;
  }
  refresh("target", "queueParameters", "submit");
}

// The artist chose a farm in the Farm select.
export async function selectFarm(farmId: string) {
  state.farmId = farmId;
  state.queueId = "";
  forgetQueue();
  await loadQueues();
  saveDefaults();
}

// The artist chose a queue in the Queue select.
export async function selectQueue(queueId: string) {
  state.queueId = queueId;
  forgetQueue();
  await loadQueue();
  saveDefaults();
}

// The artist chose a storage profile, or None, which is "" or NO_STORAGE_PROFILE.
export function chooseStorageProfile(value: string) {
  state.storageProfileId = value === NO_STORAGE_PROFILE ? "" : value;
  state.storageProfileCleared = null;
  refresh("target", "submit");
  saveDefaults();
}

async function saveDefaults() {
  try {
    await cloud("set-defaults", {
      farmId: state.farmId,
      queueId: state.queueId,
      storageProfileId: state.storageProfileId ?? "",
    });
  } catch (error) {
    toast("warn", `Couldn't save the farm and queue as your defaults: ${messageOf(error)}`);
  }
}

async function login() {
  state.loggingIn = true;
  refresh("account");
  try {
    await cloud("login");
    toast("success", "Logged in to Deadline Cloud.");
  } catch (error) {
    toast("error", messageOf(error), 10000);
  }
  state.loggingIn = false;
  await loadAccount(true);
}

// Identifies what runs: the API workflow and its name.
function workflowKey(body: WorkflowBody): string {
  return JSON.stringify([body.workflow, body.name]);
}

let planRequest = 0;
// The workflow of the latest plan request, so moving nodes around doesn't plan again.
let requestedKey: string | null = null;

// Each time a workflow is opened or its tab selected, so what was started for the workflow before,
// such as a file dialog, doesn't land in the next one.
let workflowGeneration = 0;

// Reads the workflow again, unless it's unchanged. force reads it anyway.
export async function loadPlan(force = false) {
  let body: WorkflowBody;
  const tab = workflowTab();
  const generation = workflowGeneration;
  try {
    body = await currentWorkflow();
  } catch (error) {
    state.planError = `Couldn't convert the workflow to API format: ${messageOf(error)}`;
    refresh("status", "submit");
    return;
  }
  // Another workflow opened while this one was read. workflowLoaded plans that one.
  if (generation !== workflowGeneration) {
    return;
  }
  const key = workflowKey(body);
  if (!force && key === requestedKey && !state.planError) {
    return;
  }
  const request = ++planRequest;
  requestedKey = key;
  state.planning = true;
  refresh("status", "submit");
  let plan: Plan | null = null;
  let planError: string | null = null;
  try {
    // A newer plan from this tab replaces this one, but one from another tab doesn't.
    plan = await postJson<Plan>("/deadline/plan", { ...body, clientId: api.clientId });
  } catch (error) {
    if (error instanceof RouteError && error.status === 409) {
      // The server dropped it for a newer plan.
      if (request === planRequest) {
        state.planning = false;
        refresh("status", "submit");
      }
      return;
    }
    planError = messageOf(error);
  }
  if (request !== planRequest) {
    return; // A newer plan replaced this one.
  }
  state.planning = false;
  state.planError = planError;
  if (plan) {
    state.plan = plan;
    state.planKey = key;
    state.planTab = tab;
    dropStaleItemEdits(plan.sources ?? []);
  }
  refresh();
}

// What a list field's node holds: an edit made to the field only applies while it's unchanged.
function itemEditBase(source: TaskList) {
  return JSON.stringify([state.plan?.name, source.count, source.range, source.values]);
}

// A list field follows its node again once the node's values change, or the node is gone.
function dropStaleItemEdits(sources: TaskList[]) {
  const current = new Map(sources.map((source) => [source.parameter, itemEditBase(source)]));
  for (const [parameter, base] of Object.entries(state.itemEditBases)) {
    if (current.get(parameter) !== base) {
      delete state.edits[parameter];
      delete state.itemEditBases[parameter];
    }
  }
}

let planTimer: ReturnType<typeof setTimeout> | undefined;
let graphKey: string | null = null;

// Layout that doesn't change what runs: positions, node sizes and collapsing, groups and the canvas
// view.
const LAYOUT_KEYS = new Set(["pos", "size", "flags"]);

function withoutLayout(key: string, value: unknown): unknown {
  if (key === "groups" || key === "ds") {
    return undefined;
  }
  if (value && typeof value === "object" && !Array.isArray(value) && "id" in value) {
    return Object.fromEntries(Object.entries(value).filter(([name]) => !LAYOUT_KEYS.has(name)));
  }
  return value;
}

// Comparing the change tracker's workflow without the layout is much cheaper than graphToPrompt.
export function layoutFreeKey(workflow: unknown): string {
  return JSON.stringify(workflow, withoutLayout);
}

// ComfyUI's graphChanged event carries the changed workflow in UI format.
function schedulePlan(event?: CustomEvent<unknown>) {
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
// graph, so clear it before a submission can mix the two. A plan still loading for it is dropped.
export function workflowLoaded() {
  clearTimeout(planTimer);
  workflowGeneration++;
  planRequest++;
  requestedKey = null;
  graphKey = null;
  state.plan = null;
  state.planKey = null;
  state.planTab = null;
  state.planning = false;
  state.planError = null;
  if (views.size) {
    refresh();
    planTimer = setTimeout(() => loadPlan());
  }
}

// ---------------------------------------------------------------------------------------------
// Task lists

const RANGE_PART = /^(-?\d+)(?:\s*-\s*(-?\d+)(?:\s*:\s*(\d+))?)?$/;
// One part of a list's item range, as the submitter's _ITEM_RANGE_PART reads it.
const ITEM_RANGE_PART = /^\d{1,9}(?:\s*-\s*\d{1,9}(?:\s*:\s*\d{1,9})?)?$/;

interface RangePart {
  start: number;
  end: number;
  step: number;
  // The last number the part reaches, which can be less than end.
  last: number;
}

// The parts of an OpenJD integer range expression, such as "1-4,7,10-20:2", or null when it isn't
// one. Like OpenJD, it refuses empty parts and parts that overlap, such as "1-3,2".
function parseRange(expression: ParameterValue): RangePart[] | null {
  const parts: RangePart[] = [];
  for (const part of String(expression).split(",")) {
    const text = part.trim();
    if (!text) {
      return null;
    }
    const match = RANGE_PART.exec(text);
    if (!match) {
      return null;
    }
    const start = Number(match[1]);
    const end = Number(match[2] ?? match[1]);
    const step = Number(match[3] ?? 1);
    if (end < start || step < 1) {
      return null;
    }
    parts.push({ start, end, step, last: start + Math.floor((end - start) / step) * step });
  }
  const sorted = [...parts].sort((a, b) => a.start - b.start);
  for (let index = 1; index < sorted.length; index++) {
    if (sorted[index - 1].last >= sorted[index].start) {
      return null;
    }
  }
  return parts.length ? parts : null;
}

// Counts the values of an OpenJD integer range expression, or returns null when it isn't one.
export function countRange(expression: ParameterValue): number | null {
  const parts = parseRange(expression);
  return parts
    ? parts.reduce((sum, { start, end, step }) => sum + Math.floor((end - start) / step) + 1, 0)
    : null;
}

// A list's range of items to split into tasks.
function itemsValue(source: TaskList): string {
  return String(state.edits[source.parameter] ?? source.range);
}

// Whether the open workflow's lists split into tasks. Each workflow keeps its own choice, so
// switching tabs keeps it and a newly opened workflow starts as one task.
export function splitsTasks(): boolean {
  return state.plan !== null && state.splitWorkflows.has(state.plan.name);
}

// Splits the open workflow's lists into tasks, or runs it as one task.
export function setSplit(split: boolean) {
  if (!state.plan) {
    return;
  }
  if (split) {
    state.splitWorkflows.add(state.plan.name);
  } else {
    state.splitWorkflows.delete(state.plan.name);
  }
  refresh("tasks", "status", "submit");
}

function plural(count: number, noun: string) {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

// A field's number of tasks, or null with an error. A field holds item numbers from 1 to the list's
// count, so the farm never gets an item that doesn't exist. Unsplit, every list counts as one.
function fieldCount(source: TaskList, value: string): { count: number | null; error: string | null } {
  // "0" keeps the list whole in each task, as the submitter's check_item_range takes it.
  if (!splitsTasks() || String(value).trim() === "0") {
    return { count: 1, error: null };
  }
  const parts = parseRange(value);
  // Item numbers have at most 9 digits, as the submitter reads them.
  const short = String(value)
    .split(",")
    .every((part) => ITEM_RANGE_PART.test(part.trim()));
  const valid =
    short && (parts?.every(({ start, last }) => start >= 1 && last <= source.count) ?? false);
  return valid
    ? { count: countRange(value), error: null }
    : {
        count: null,
        error: `${source.title}: use item numbers from 1 to ${source.count}, such as 1-${source.count} or 2,3, or 0 for all of them in each task.`,
      };
}

// The number of tasks, from every combination of the sources, or null when a field has an error.
export function taskCount(
  sources: TaskList[],
  valueOf: (source: TaskList) => string
): number | null {
  let total = 1;
  for (const source of sources) {
    const { count } = fieldCount(source, valueOf(source));
    if (count === null) {
      return null;
    }
    total *= count;
  }
  return total;
}

// The most tasks a job can run, as the submitter's MAX_TASKS allows.
export const MAX_TASKS = 10000;

// The number of tasks, or null when a field has an error. It can be over MAX_TASKS.
function uncheckedTaskCount() {
  const sources = state.plan?.sources ?? [];
  return sources.length ? taskCount(sources, itemsValue) : 1;
}

// The number of tasks, or null when a field has an error or there are more than MAX_TASKS. One task
// is never over the limit: fieldCount counts every list as 1 unless the lists are split.
function currentTaskCount() {
  const total = uncheckedTaskCount();
  return splitsTasks() && total !== null && total > MAX_TASKS ? null : total;
}

// Why the job can't be submitted with this many tasks, if it can't.
function taskLimitError() {
  const total = splitsTasks() ? uncheckedTaskCount() : null;
  return total !== null && total > MAX_TASKS
    ? `The job would run ${total.toLocaleString()} tasks, over the limit of ${MAX_TASKS.toLocaleString()}. Choose fewer items, or 0 to run a list whole.`
    : null;
}

// How the fields multiply into tasks, such as "3 × 2 = 6 tasks", leaving out fields of one item.
function taskMath() {
  const total = currentTaskCount();
  if (total === null) {
    return "";
  }
  const factors = (state.plan?.sources ?? [])
    .map((source) => fieldCount(source, itemsValue(source)).count ?? 1)
    .filter((count) => count > 1);
  return factors.length > 1 ? `${factors.join(" × ")} = ${plural(total, "task")}` : plural(total, "task");
}

// The first item numbers a range selects, in the order the tasks run them.
function selectedItems(expression: string) {
  const numbers: number[] = [];
  for (const { start, last, step } of parseRange(expression) ?? []) {
    for (let number = start, taken = 0; number <= last && taken <= PREVIEW_ITEMS; number += step, taken++) {
      numbers.push(number);
    }
  }
  return numbers.sort((a, b) => a - b).slice(0, PREVIEW_ITEMS + 1);
}

// What a field's items are, such as the first prompts of a Create List or images of a folder.
export function itemsPreview(source: TaskList, value: string): string {
  if (!source.values?.length || fieldCount(source, value).error) {
    return "";
  }
  if (String(value).trim() === "0") {
    return "All of them, in each task";
  }
  const numbers = selectedItems(value);
  const shown = numbers
    .slice(0, PREVIEW_ITEMS)
    .map((number) => source.values[number - 1] ?? `item ${number}`);
  return numbers.length > PREVIEW_ITEMS ? `${shown.join(", ")}, …` : shown.join(", ");
}

// ---------------------------------------------------------------------------------------------
// Submitting

function parameterValue(parameter: Parameter): ParameterValue {
  if (parameter.name in state.edits) {
    return state.edits[parameter.name];
  }
  const filled = state.plan?.values?.[parameter.name];
  return filled ?? parameter.default ?? "";
}

// A BOOL job parameter's value as OpenJD reads it, or null when OpenJD would refuse it. The
// submitter reads it the same way.
export function parseBool(value: ParameterValue): boolean | null {
  // OpenJD refuses a value with spaces around it, such as " true".
  const text = String(value).toLowerCase();
  if (["true", "yes", "on", "1"].includes(text)) {
    return true;
  }
  if (["false", "no", "off", "0"].includes(text)) {
    return false;
  }
  return null;
}

// Templates without the switch don't list any custom nodes.
function attachesNodes() {
  const parameter = state.plan?.parameters.find(({ name }) => name === ATTACH_NODES_PARAMETER);
  return !parameter || parseBool(parameterValue(parameter)) !== false;
}

// What a queue parameter is without the artist's edit: the submitter's configured value, then the
// job template's value when the template defines the same parameter, which the job sends in its
// place, such as Conda Packages' comfy, then the queue environment's default.
function queueDefault(parameter: Parameter): ParameterValue {
  const configured = state.plan?.queueValues?.[parameter.name];
  if (configured !== undefined) {
    return configured;
  }
  const template = state.plan?.parameters?.find(({ name }) => name === parameter.name);
  if (template) {
    return parameterValue(template);
  }
  return parameter.default ?? "";
}

function queueParameterValue(parameter: Parameter): ParameterValue {
  if (parameter.name in state.queueEdits) {
    return state.queueEdits[parameter.name];
  }
  return queueDefault(parameter);
}

// Sets the artist's value for a queue parameter. An empty value goes back to the default rather
// than sending an empty one, which would replace the queue's default, such as its conda channels.
export function editQueueParameter(name: string, value: ParameterValue | null | undefined) {
  if (value === "" || value === null || value === undefined) {
    delete state.queueEdits[name];
  } else {
    state.queueEdits[name] = value;
  }
}

// The artist's attachment changes for the workflow tab the plan was made from. Each tab keeps its
// own, so files added to one workflow, or removed from it, never change another's job.
function attachmentEdits(): AttachmentEdits {
  const tab = state.planTab;
  if (tab === null) {
    return { removed: new Set(), added: { filenames: [], directories: [] } };
  }
  let edits = state.attachmentEdits.get(tab);
  if (!edits) {
    edits = { removed: new Set(), added: { filenames: [], directories: [] } };
    state.attachmentEdits.set(tab, edits);
  }
  return edits;
}

// The folder loaders' folders that attach files, each with the files attached from it.
function plannedFolders(): FolderFiles[] {
  return (state.plan?.attachments?.folders ?? []).filter((folder) => folder.files.length);
}

// The input files and folders the job attaches: the planned ones, with every file in a folder
// loader's folder, less those removed, and the ones added, each once.
function attachments(): PathLists {
  const listed = state.plan?.attachments ?? { filenames: [], directories: [] };
  const { removed, added } = attachmentEdits();
  const keep = (...lists: string[][]) => [
    ...new Set(lists.flat().filter((path) => !removed.has(path))),
  ];
  const folderFiles = plannedFolders().map((folder) => folder.files);
  return {
    filenames: keep(listed.filenames, ...folderFiles, added.filenames),
    directories: keep(listed.directories, added.directories),
  };
}

function jobName() {
  return state.jobName ?? `ComfyUI - ${state.plan?.name ?? "workflow"}`;
}

// Whether the chosen farm and queue are the ones the panel shows: listed, so their selects show
// them, rather than a default that isn't, or one from before a list failed to load.
function targetShown(): boolean {
  return (
    Boolean(state.farmId) &&
    Boolean(state.queueId) &&
    Boolean(state.farms?.some((farm) => farm.farmId === state.farmId)) &&
    Boolean(state.queues?.some((queue) => queue.queueId === state.queueId))
  );
}

// Whether the details loaded are the chosen queue's: its parameters and storage profiles.
function queueDetailsLoaded(): boolean {
  return state.queue !== null && state.queueKey === targetKey(state.farmId, state.queueId);
}

// A default or earlier storage profile was cleared because the queue doesn't list it, and the
// artist hasn't chosen another, or None, since. Submitting without one would upload files that
// are on shared storage.
function storageProfileUnresolved(): boolean {
  return (
    state.storageProfileCleared !== null &&
    state.storageProfileId === null &&
    Boolean(state.queue?.storageProfiles.length)
  );
}

// The storage profile to submit with: the chosen one, if the loaded queue lists it, or "" for
// None. canSubmit waits for the queue's details, so a profile isn't dropped while they load.
function shownStorageProfileId(): string {
  const chosen = state.storageProfileId;
  const listed = state.queue?.storageProfiles.some((profile) => profile.storageProfileId === chosen);
  return chosen && listed ? chosen : "";
}

export function canSubmit(): boolean {
  return (
    state.account?.authenticationStatus === "AUTHENTICATED" &&
    targetShown() &&
    queueDetailsLoaded() &&
    !storageProfileUnresolved() &&
    state.plan !== null &&
    !state.planning &&
    !state.planError &&
    !state.submission &&
    !hostFormError() &&
    currentTaskCount() !== null
  );
}

// Why Submit is off, when the farm, queue or storage profile is the reason. Other reasons, such as
// the account or the workflow, show where they are.
export function submitBlocker(): string | null {
  if (state.account?.authenticationStatus !== "AUTHENTICATED" || state.submission) {
    return null;
  }
  if (!targetShown()) {
    const listing =
      !state.cloudError && (state.farms === null || (Boolean(state.farmId) && state.queues === null));
    return listing ? "Loading the farms and queues…" : "Choose a farm and a queue to submit to.";
  }
  if (!queueDetailsLoaded()) {
    return state.queueError ? state.queueError : "Loading the queue's details…";
  }
  if (storageProfileUnresolved()) {
    return `The queue doesn't list the storage profile ${state.storageProfileCleared}, so it isn't used. Choose a storage profile, or None to upload every input file.`;
  }
  return null;
}

// What the job bundle is made from, whether it's submitted or saved.
function bundleRequest() {
  const plan = state.plan;
  const parameters: Record<string, ParameterValue> = {};
  for (const parameter of plan?.parameters ?? []) {
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
  // Only the chosen queue's own parameters. queueEdits is cleared when the queue changes.
  const queueParameters = queueDetailsLoaded() ? state.queue?.parameters ?? [] : [];
  for (const parameter of queueParameters) {
    if (parameter.name in state.queueEdits || parameter.name in (plan?.queueValues ?? {})) {
      parameters[parameter.name] = queueParameterValue(parameter);
    }
  }
  // The lists' ranges, when the artist splits them into tasks. Otherwise the job has no task
  // parameters to send them to.
  for (const source of plan?.sources ?? []) {
    if (splitsTasks()) {
      parameters[source.parameter] = itemsValue(source);
    } else {
      delete parameters[source.parameter];
    }
  }
  const settings: Record<string, ParameterValue> = { name: jobName() };
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
    excludedNodes: excludedNodes(),
    splitTasks: splitsTasks(),
  };
}

// The current workflow, when the plan was made from it. The parameters and attachments come from
// the plan, so a workflow edited since then (while the plan is debounced or still loading) would
// be sent with another revision's files. Then the workflow is read again and this returns null.
async function plannedWorkflow(): Promise<WorkflowBody | null> {
  const tab = workflowTab();
  const body = await currentWorkflow();
  // The same workflow in another tab has its own attachments, so it's planned again too.
  if (state.plan && workflowKey(body) === state.planKey && tab === state.planTab) {
    return body;
  }
  toast("info", "The workflow changed since it was read. Check the job again, then submit.", 8000);
  loadPlan();
  return null;
}

// A submission ID made here, so events the server sends before it answers the request still
// match. crypto.randomUUID needs a secure context, which ComfyUI served over http to another
// machine isn't.
function newSubmissionId() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

const CREATING_MESSAGE = "Creating the job bundle…";

export async function submit() {
  if (!canSubmit()) {
    return;
  }
  const id = newSubmissionId();
  state.submission = { id, starting: true, message: CREATING_MESSAGE };
  refresh("submit", "status");
  let body: WorkflowBody | null = null;
  try {
    body = await plannedWorkflow();
  } catch (error) {
    toast("error", `Couldn't convert the workflow to API format: ${messageOf(error)}`, 10000);
  }
  if (!body) {
    state.submission = null;
    refresh("submit", "status");
    return;
  }
  // Built right after the check, from the plan of this same workflow.
  const request = bundleRequest();
  try {
    const result = await postJson<{ submissionId?: string; warnings?: string[] }>("/deadline/jobs", {
      ...body,
      submissionId: id,
      // The server sends this submission's events to this tab only.
      clientId: api.clientId,
      farmId: state.farmId,
      queueId: state.queueId,
      storageProfileId: shownStorageProfileId(),
      ...request,
    });
    // Events may already have updated the submission, or finished it and cleared it.
    const submission = state.submission;
    if (submission?.id === id) {
      submission.id = result.submissionId ?? id;
      submission.starting = false;
      submission.name = String(request.settings.name);
      if (submission.message === CREATING_MESSAGE) {
        submission.message = "Starting…";
      }
    }
    for (const warning of result.warnings ?? []) {
      toast("warn", warning, 15000);
    }
  } catch (error) {
    if (state.submission?.id === id) {
      state.submission = null;
    }
    toast("error", messageOf(error), 10000);
  }
  refresh("submit", "status");
}

async function answerConfirmation(accept: boolean) {
  const submission = state.submission;
  if (!submission?.confirm) {
    return;
  }
  const { promptId } = submission.confirm;
  submission.confirm = null;
  refresh("submit");
  try {
    // The prompt's own ID, so this answer can't answer a later prompt of the submission.
    await postJson("/deadline/jobs/confirm", {
      submissionId: submission.id,
      clientId: api.clientId,
      promptId,
      accept,
    });
  } catch (error) {
    toast("error", messageOf(error));
  }
}

async function cancelSubmission() {
  if (!state.submission?.id || state.submission.starting) {
    return;
  }
  try {
    await postJson("/deadline/jobs/cancel", {
      submissionId: state.submission.id,
      clientId: api.clientId,
    });
  } catch (error) {
    toast("error", messageOf(error));
  }
}

function rememberJob(job: Job) {
  state.jobs = [job, ...state.jobs.filter((other) => other.jobId !== job.jobId)].slice(
    0,
    MAX_RECENT_JOBS
  );
  writeStorage(JOBS_KEY, state.jobs);
}

export function listenForSubmissions() {
  api.addEventListener<SubmissionEvent>("deadline.progress", ({ detail }) => {
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
  api.addEventListener<SubmissionEvent>("deadline.confirm", ({ detail }) => {
    const submission = state.submission;
    if (!submission || detail.submissionId !== submission.id) {
      return;
    }
    // The answer echoes promptId, which the server checks is the prompt pending now.
    submission.confirm = {
      message: detail.message ?? "",
      default: detail.default,
      promptId: detail.promptId,
    };
    refresh("submit");
    if (!views.size) {
      openDialog();
    }
  });
  api.addEventListener<SubmissionEvent>("deadline.submitted", ({ detail }) => {
    const ours = detail.submissionId === state.submission?.id;
    if (detail.status === "SUBMITTED") {
      if (detail.warning) {
        toast("warn", `Submitted ${detail.name} as ${detail.jobId}. ${detail.warning}`, 15000);
      } else {
        toast("success", `Submitted ${detail.name} as ${detail.jobId}.`, 10000);
      }
      if (detail.jobId && detail.farmId && detail.queueId) {
        rememberJob({
          jobId: detail.jobId,
          name: detail.name ?? detail.jobId,
          farmId: detail.farmId,
          queueId: detail.queueId,
          submittedAt: new Date().toISOString(),
          outputDir: detail.outputDir,
        });
        pollJobs();
      }
    } else if (detail.status === "FAILED") {
      toast("error", `Couldn't submit ${detail.name}. ${detail.error ?? ""}`, 15000);
    } else if (detail.status === "CANCELED" && ours) {
      toast(
        detail.warning ? "warn" : "info",
        `Canceled submitting ${detail.name}.${detail.warning ? ` ${detail.warning}` : ""}`,
        detail.warning ? 15000 : undefined
      );
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

let pollTimer: ReturnType<typeof setTimeout> | undefined;
// One poll runs at a time. A request during one polls again when it ends, so a new job isn't
// left waiting for the next timer.
let polling = false;
let pollAgain = false;

async function pollJobs() {
  if (polling) {
    pollAgain = true;
    return;
  }
  polling = true;
  try {
    await pollOnce();
  } finally {
    polling = false;
  }
  if (pollAgain) {
    pollAgain = false;
    await pollJobs();
  }
}

async function pollOnce() {
  clearTimeout(pollTimer);
  // A deleted job, or one these credentials can't see, isn't asked about again. Other errors,
  // such as throttling or expired credentials, are tried again next time.
  const pending = state.jobs.filter((job) => {
    const status = state.jobStatus[job.jobId];
    return !status?.notFound && !DONE_STATUSES.has(status?.taskRunStatus ?? "");
  });
  const byQueue = new Map<string, Job[]>();
  for (const job of pending) {
    const key = `${job.farmId}/${job.queueId}`;
    byQueue.set(key, [...(byQueue.get(key) ?? []), job]);
  }
  if (state.account?.authenticationStatus === "AUTHENTICATED") {
    for (const jobs of byQueue.values()) {
      try {
        const result = await cloud<{ jobs: JobStatus[] }>("jobs", {
          farmId: jobs[0].farmId,
          queueId: jobs[0].queueId,
          jobIds: jobs.map((job) => job.jobId),
        });
        for (const job of result.jobs) {
          // A passing error keeps the last status the job had, and its Download button.
          const previous = state.jobStatus[job.jobId];
          if (job.error && !job.notFound && previous && !previous.error) {
            continue;
          }
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

// What h() can put in an element. Empty values are skipped, and arrays are flattened.
type Child = Node | string | number | null | undefined | false;
type Children = Child | Children[];

// An element's properties: event listeners as onClick and so on, className, value and checked,
// and any other attribute. Empty values are skipped.
type Props = { [event: `on${string}`]: ((event: Event) => void) | undefined } & Record<string, unknown>;

function flatten(children: Children[]): Child[] {
  return children.flatMap((child) => (Array.isArray(child) ? flatten(child) : [child]));
}

function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  props: Props | null = {},
  ...children: Children[]
): HTMLElementTagNameMap[K] {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(props ?? {})) {
    if (value === undefined || value === null || value === false) {
      continue;
    }
    if (key.startsWith("on")) {
      element.addEventListener(key.slice(2).toLowerCase(), value as EventListener);
    } else if (key === "className") {
      element.className = String(value);
    } else if (key === "value" || key === "checked") {
      (element as unknown as Record<string, unknown>)[key] = value;
    } else {
      element.setAttribute(key, value === true ? "" : String(value));
    }
  }
  for (const child of flatten(children)) {
    if (child !== null && child !== undefined && child !== false) {
      element.append(child instanceof Node ? child : String(child));
    }
  }
  return element;
}

// The value or checked state of the input, select or textarea an event came from.
function inputValue(event: Event): string {
  return (event.target as HTMLInputElement).value;
}

function inputChecked(event: Event): boolean {
  return (event.target as HTMLInputElement).checked;
}

function field(label: string, control: HTMLElement, help?: string) {
  if (help) {
    control.setAttribute("aria-description", help);
  }
  return h(
    "label",
    {
      className:
        (control as HTMLInputElement).type === "checkbox" ? "dl-field dl-field-check" : "dl-field",
    },
    h("span", { className: "dl-label", "data-dl-tip": help || null }, label),
    control
  );
}

// One tooltip on the page, because the panel's scrolling would clip a tooltip placed inside it.
let tooltipElement: HTMLElement | null = null;
let tooltipTarget: HTMLElement | null = null;
let tooltipTimer: ReturnType<typeof setTimeout> | undefined;

function hideTooltip() {
  clearTimeout(tooltipTimer);
  tooltipElement?.remove();
  tooltipElement = null;
  tooltipTarget = null;
}

function showTooltip(target: HTMLElement) {
  if (!target.isConnected) {
    return;
  }
  const tooltip = h("div", { className: "dl-tooltip", role: "tooltip" }, target.dataset.dlTip);
  tooltipElement = tooltip;
  document.body.append(tooltip);
  const anchor = target.getBoundingClientRect();
  const box = tooltip.getBoundingClientRect();
  const maxLeft = window.innerWidth - box.width - TOOLTIP_MARGIN;
  const above = anchor.top - box.height - TOOLTIP_GAP;
  tooltip.style.left = `${Math.max(TOOLTIP_MARGIN, Math.min(anchor.left, maxLeft))}px`;
  tooltip.style.top = `${above >= TOOLTIP_MARGIN ? above : anchor.bottom + TOOLTIP_GAP}px`;
}

function listenForTooltips() {
  document.addEventListener("mouseover", (event) => {
    const target = (event.target as Element | null)?.closest?.<HTMLElement>("[data-dl-tip]") ?? null;
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

// A select of [value, text] options.
function select(
  options: string[][],
  value: string,
  onChange: (value: string) => void,
  placeholder?: string
) {
  return h(
    "select",
    { className: "dl-input", onChange: (event: Event) => onChange(inputValue(event)) },
    placeholder ? h("option", { value: "", selected: !value }, placeholder) : null,
    options.map(([optionValue, text]) =>
      h("option", { value: optionValue, selected: optionValue === value }, text)
    )
  );
}

function section(title: Children, open: boolean, ...children: Children[]) {
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
  // Only the chosen queue's own profiles, once its details load.
  const profiles = queueDetailsLoaded() ? state.queue?.storageProfiles ?? [] : [];
  const profileOptions = profiles.map((profile) => [profile.storageProfileId, profile.displayName]);
  // After a default profile was cleared, None has to be chosen like any profile, so the select
  // starts on a placeholder rather than on None.
  const profileSelect = storageProfileUnresolved()
    ? select(
        [[NO_STORAGE_PROFILE, "None"], ...profileOptions],
        "",
        chooseStorageProfile,
        "Choose a storage profile"
      )
    : select(profileOptions, state.storageProfileId ?? "", chooseStorageProfile, "None");
  return h(
    "div",
    { className: "dl-grid" },
    state.cloudError ? h("div", { className: "dl-notice dl-error" }, state.cloudError) : null,
    state.queueError
      ? h(
          "div",
          { className: "dl-notice dl-error" },
          state.queueError,
          h("button", { className: "dl-link", onClick: () => loadQueue() }, "Try again")
        )
      : null,
    field(
      "Farm",
      farms
        ? select(
            farms.map((farm) => [farm.farmId, farm.displayName]),
            state.farmId,
            selectFarm,
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
                selectQueue,
                "Choose a queue"
              )
            : h("span", { className: "dl-muted" }, "Loading…"),
          "The queue in this farm that runs the job."
        )
      : null,
    profiles.length
      ? field(
          "Storage profile",
          profileSelect,
          "Tells Deadline Cloud which of your paths are on shared storage, so they aren't uploaded."
        )
      : null
  );
}

function renderStatus() {
  const plan = state.plan;
  const children: Children[] = [];
  if (state.planError) {
    children.push(h("div", { className: "dl-notice dl-error" }, state.planError));
  }
  const limitError = plan ? taskLimitError() : null;
  if (limitError) {
    children.push(h("div", { className: "dl-notice dl-error" }, limitError));
  }
  if (plan) {
    // A workflow split into tasks says how many it runs.
    const count = currentTaskCount();
    const tasks =
      plan.sources?.length && splitsTasks()
        ? ` · ${count === null ? "check the tasks below" : plural(count, "task")}`
        : "";
    children.push(
      h(
        "div",
        { className: "dl-summary" },
        h("strong", null, plan.name),
        h("span", { className: "dl-muted" }, `${tasks} · ${plan.template}`)
      )
    );
    // With Attach Custom Nodes off, the workers provide the custom nodes.
    const ignored = attachesNodes() ? [] : plan.nodeWarnings ?? [];
    const warnings = [...(plan.warnings ?? []), ...(splitsTasks() ? plan.taskWarnings ?? [] : [])];
    for (const warning of warnings) {
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
          `Not attaching ${excluded.join(", ")}. The job fails unless the workers already have it, for example through Extra Model Paths Config.`
        )
      );
    }
    const largePacks = attachesNodes()
      ? (plan.localNodes ?? []).filter(
          (pack) => isLargePack(pack) && !attachmentEdits().removed.has(pack.path)
        )
      : [];
    for (const pack of largePacks) {
      children.push(
        h(
          "div",
          { className: "dl-notice dl-warn-box" },
          `The custom node pack ${pack.name} is ${packSize(pack)}. A job that uses one of its nodes uploads the whole pack. Remove it under Attachments if the workers already have it.`
        )
      );
    }
    const removedPacks = attachesNodes() ? excludedNodes() : [];
    if (removedPacks.length) {
      children.push(
        h(
          "div",
          { className: "dl-notice dl-warn-box" },
          `Not attaching the custom nodes in ${removedPacks.join(", ")}. The job fails unless the workers already have them, such as from a custom nodes conda package.`
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

// Updates what depends on the list fields without re-rendering them, so they keep focus.
function updateCounts() {
  for (const view of views) {
    view.render(["status", "submit"]);
  }
  for (const element of document.querySelectorAll<HTMLElement>("[data-role='task-math']")) {
    element.textContent = taskMath();
  }
  for (const source of state.plan?.sources ?? []) {
    const value = itemsValue(source);
    // The sidebar and the dialog can both show the field.
    for (const element of document.querySelectorAll<HTMLInputElement>(`[data-items="${source.parameter}"]`)) {
      if (element !== document.activeElement) {
        element.value = value;
      }
    }
    for (const element of document.querySelectorAll<HTMLElement>(`[data-preview="${source.parameter}"]`)) {
      element.textContent = itemsPreview(source, value);
    }
    for (const element of document.querySelectorAll<HTMLElement>(`[data-item-error="${source.parameter}"]`)) {
      const { error } = fieldCount(source, value);
      element.textContent = error ?? "";
      element.hidden = !error;
    }
  }
}

function taskListField(source: TaskList) {
  const value = itemsValue(source);
  const { error } = fieldCount(source, value);
  return h(
    "div",
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
      "data-items": source.parameter,
      onInput: (event: Event) => {
        state.edits[source.parameter] = inputValue(event);
        state.itemEditBases[source.parameter] = itemEditBase(source);
        updateCounts();
      },
    }),
    h("span", { className: "dl-preview dl-path", "data-preview": source.parameter }, itemsPreview(source, value)),
    h(
      "div",
      { className: "dl-notice dl-error", "data-item-error": source.parameter, hidden: !error },
      error ?? ""
    )
  );
}

function renderTasks() {
  const sources = state.plan?.sources ?? [];
  if (!sources.length) {
    return null;
  }
  const mode = select(
    [
      ["one", "One task"],
      ["split", "Multiple tasks"],
    ],
    splitsTasks() ? "split" : "one",
    (value: string) => setSplit(value === "split")
  );
  const title = ["Tasks", h("span", { className: "dl-task-math", "data-role": "task-math" }, taskMath())];
  if (!splitsTasks()) {
    return section(
      title,
      true,
      mode,
      h(
        "p",
        { className: "dl-help" },
        "The workflow runs as one task, as in ComfyUI. Choose Multiple tasks to run each item as its own task."
      )
    );
  }
  return section(
    title,
    true,
    mode,
    h(
      "p",
      { className: "dl-help" },
      "Each field below runs its items as separate tasks, and fields multiply. Narrow a field to the items to run by number, such as 1-2 or 2,3, or set it to 0 to run that list whole in each task."
    ),
    sources.map(taskListField)
  );
}

function parameterControl(
  parameter: Parameter,
  value: ParameterValue,
  onChange: (value: string) => void
): HTMLElement {
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
      checked: parseBool(value) === true,
      onChange: (event: Event) => onChange(inputChecked(event) ? "true" : "false"),
    });
  }
  if (control === "MULTILINE_EDIT") {
    return h("textarea", {
      className: "dl-input",
      rows: 3,
      value: String(value),
      onInput: (event: Event) => onChange(inputValue(event)),
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
    onInput: (event: Event) => onChange(inputValue(event)),
  });
}

function renderParameters() {
  const plan = state.plan;
  if (!plan) {
    return null;
  }
  // The lists' fields are in the Tasks section.
  const taskLists = new Set((plan.sources ?? []).map((source) => source.parameter));
  const groups = new Map<string, Parameter[]>();
  for (const parameter of plan.parameters) {
    const ui = parameter.userInterface ?? {};
    if (
      AUTOMATIC_PARAMETERS.has(parameter.name) ||
      taskLists.has(parameter.name) ||
      ui.control === "HIDDEN"
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
          parameterControl(parameter, parameterValue(parameter), (value: string) => {
            state.edits[parameter.name] = value;
            if (parameter.name === ATTACH_NODES_PARAMETER) {
              refresh("status", "attachments", "submit");
            }
          }),
          parameter.description
        )
      )
    )
  );
}

function renderQueueParameters() {
  const loaded = queueDetailsLoaded() ? state.queue?.parameters ?? [] : [];
  const parameters = loaded.filter(
    (parameter) => parameter.userInterface?.control !== "HIDDEN"
  );
  if (!parameters.length) {
    return null;
  }
  return section(
    "Queue environment",
    false,
    parameters.map((parameter) => {
      const name = parameter.name;
      const reset = h(
        "button",
        {
          className: "dl-link",
          "data-role": "queue-reset",
          hidden: !(name in state.queueEdits),
          onClick: () => {
            editQueueParameter(name, "");
            refresh("queueParameters");
          },
        },
        "Use the queue's default"
      );
      const control = parameterControl(parameter, queueParameterValue(parameter), (value: string) => {
        editQueueParameter(name, value);
        reset.hidden = !(name in state.queueEdits);
      });
      // A cleared field shows the default it goes back to.
      if (control instanceof HTMLInputElement || control instanceof HTMLTextAreaElement) {
        control.placeholder = String(queueDefault(parameter));
      }
      return h(
        "div",
        { className: "dl-queue-parameter" },
        field(parameter.userInterface?.label || name, control, parameter.description),
        reset
      );
    })
  );
}

// Each hardware row: capability, label and scale. Memory capabilities are in MiB, and the fields
// are in GiB.
const HOST_HARDWARE: [string, string, number][] = [
  ["amount.worker.vcpu", "vCPUs", 1],
  ["amount.worker.memory", "Memory (GiB)", 1024],
  ["amount.worker.gpu", "GPUs", 1],
  ["amount.worker.gpu.memory", "GPU memory (GiB)", 1024],
  ["amount.worker.disk.scratch", "Scratch space (GiB)", 1],
];
const HOST_CHOICES: [string, string, string[]][] = [
  ["attr.worker.os.family", "Operating system", ["linux", "macos", "windows"]],
  ["attr.worker.cpu.arch", "CPU architecture", ["x86_64", "arm64"]],
];

function formFromRequirements(requirements?: HostRequirements): HostForm {
  const form: HostForm = { choices: {}, hardware: {}, amounts: [], attributes: [] };
  for (const attribute of requirements?.attributes ?? []) {
    // Values that have no checkbox, such as aarch64, keep the attribute as a custom one, so
    // editing the form doesn't drop them.
    const options = HOST_CHOICES.find(([name]) => name === attribute.name)?.[2];
    const known = options && attribute.anyOf?.every((value) => options.includes(value));
    if (known && attribute.anyOf) {
      form.choices[attribute.name] = [...attribute.anyOf];
    } else {
      const mode = attribute.allOf ? ("allOf" as const) : ("anyOf" as const);
      form.attributes.push({
        name: attribute.name.replace(/^attr\./, ""),
        mode,
        values: (attribute[mode] ?? []).join(", "),
      });
    }
  }
  for (const amount of requirements?.amounts ?? []) {
    const known = HOST_HARDWARE.find(([name]) => name === amount.name);
    const toField = (value?: number) =>
      value === undefined ? "" : String(value / (known?.[2] ?? 1));
    const fields = { min: toField(amount.min), max: toField(amount.max) };
    if (known) {
      form.hardware[amount.name] = fields;
    } else {
      form.amounts.push({ name: amount.name.replace(/^amount\./, ""), ...fields });
    }
  }
  return form;
}

function amountRequirement(name: string, fields: RangeFields, scale = 1): AmountRequirement {
  const requirement: AmountRequirement = { name };
  for (const key of ["min", "max"] as const) {
    if (fields[key] !== "" && fields[key] !== undefined) {
      requirement[key] = Number(fields[key]) * scale;
    }
  }
  return requirement;
}

function requirementsFromForm(form: HostForm): Required<HostRequirements> {
  const attributes: AttributeRequirement[] = [];
  for (const [name] of HOST_CHOICES) {
    if (form.choices[name]?.length) {
      attributes.push({ name, anyOf: form.choices[name] });
    }
  }
  for (const attribute of form.attributes) {
    const values = attribute.values.split(",").map((value) => value.trim()).filter(Boolean);
    const name = `attr.${attribute.name.trim()}`;
    attributes.push(attribute.mode === "allOf" ? { name, allOf: values } : { name, anyOf: values });
  }
  const amounts: AmountRequirement[] = [];
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
    if (amount.min !== undefined && amount.max !== undefined && amount.min > amount.max) {
      return `The min of ${label} is more than its max.`;
    }
  }
  for (const attribute of attributes) {
    if (!attribute.name.slice("attr.".length)) {
      return "Name each custom attribute.";
    }
    if (!(attribute.anyOf ?? attribute.allOf ?? []).length) {
      return `Add a value for ${attribute.name.slice("attr.".length)}.`;
    }
  }
  return null;
}

function hostForm(): HostForm {
  return state.hostForm ?? formFromRequirements(state.plan?.hostRequirements);
}

// The first edit copies the job template's requirements, so they aren't lost by changing one.
function editHostForm(): HostForm {
  state.hostForm ??= formFromRequirements(state.plan?.hostRequirements);
  return state.hostForm;
}

function hostFormEdited() {
  for (const element of document.querySelectorAll<HTMLElement>("[data-role='host-error']")) {
    const error = hostFormError();
    element.textContent = error ?? "";
    element.hidden = !error;
  }
  for (const element of document.querySelectorAll<HTMLElement>("[data-role='host-reset']")) {
    element.hidden = !state.hostForm;
  }
  refresh("submit");
}

function hostNumber(placeholder: string, value: string | undefined, onInput: (value: string) => void) {
  return h("input", {
    className: "dl-input",
    type: "number",
    min: 0,
    step: "any",
    placeholder,
    value: value ?? "",
    onInput: (event: Event) => {
      onInput(inputValue(event));
      hostFormEdited();
    },
  });
}

function renderHostRequirements() {
  if (!state.plan) {
    return null;
  }
  const form = hostForm();
  const changed = (rerender = false) => {
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
              onChange: (event: Event) => {
                const edited = editHostForm();
                const chosen = new Set(edited.choices[name] ?? []);
                if (inputChecked(event)) {
                  chosen.add(option);
                } else {
                  chosen.delete(option);
                }
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
    const edit = (key: keyof RangeFields) => (value: string) => {
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
  const remove = (key: "amounts" | "attributes", index: number) =>
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
  const customName = (placeholder: string, key: "amounts" | "attributes", index: number) =>
    h("input", {
      className: "dl-input",
      placeholder,
      value: form[key][index].name,
      onInput: (event: Event) => {
        editHostForm()[key][index].name = inputValue(event);
        hostFormEdited();
      },
    });
  const amountRows = form.amounts.map((amount, index) => {
    const edit = (field: keyof RangeFields) => (value: string) => {
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
          (value: string) => {
            editHostForm().attributes[index].mode = value === "allOf" ? "allOf" : "anyOf";
            hostFormEdited();
          }
        ),
        h("input", {
          className: "dl-input",
          placeholder: "Values, separated by commas",
          value: attribute.values,
          onInput: (event: Event) => {
            editHostForm().attributes[index].values = inputValue(event);
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
          "data-dl-tip": "Go back to the job template's host requirements.",
          hidden: !state.hostForm,
          onClick: () => {
            state.hostForm = null;
            changed(true);
          },
        },
        "Reset to defaults"
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
  if (hostFormError() || !state.plan || state.planning) {
    toast("error", hostFormError() ?? "Wait for the workflow to be read.", 8000);
    return;
  }
  try {
    const body = await plannedWorkflow();
    if (!body) {
      return;
    }
    const result = await postJson<{ job_bundle: string; warnings?: string[] }>("/deadline/bundle", {
      ...body,
      ...bundleRequest(),
    });
    toast("success", `Saved the job bundle to ${result.job_bundle}`, 20000);
    for (const warning of result.warnings ?? []) {
      toast("warn", warning, 20000);
    }
  } catch (error) {
    toast("error", `Couldn't save the job bundle: ${messageOf(error)}`, 10000);
  }
}

function renderJobSettings() {
  const settings = state.settings;
  const set = (key: string) => (value: ParameterValue) => {
    settings[key] = value;
    writeStorage(SETTINGS_KEY, settings);
  };
  const number = (key: string, placeholder: string, min?: number, max?: number) =>
    h("input", {
      className: "dl-input",
      type: "number",
      min,
      max,
      placeholder,
      value: settings[key],
      onInput: (event: Event) => set(key)(inputValue(event)),
    });
  return section(
    "Job settings",
    false,
    field(
      "Name",
      h("input", {
        className: "dl-input",
        value: jobName(),
        onInput: (event: Event) => {
          state.jobName = inputValue(event);
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
        onInput: (event: Event) => {
          state.jobDescription = inputValue(event);
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
        String(settings.targetTaskRunStatus),
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
        String(settings.jobAttachmentsFileSystem),
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
        onChange: (event: Event) => set("requirePathsExist")(inputChecked(event)),
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

function excludedModels(): string[] {
  const { removed } = attachmentEdits();
  return (state.plan?.localModels ?? [])
    .filter((model) => removed.has(model.path))
    .map((model) => model.id);
}

// The panel warns about a pack above either of these. A job that uses the pack uploads it.
const LARGE_PACK_BYTES = 1_000_000_000;
const LARGE_PACK_FILES = 10_000;

// A size in bytes, such as 1.5 GB. Storage and transfer sizes count in thousands.
export function formatSize(bytes: number): string {
  const units = ["bytes", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit += 1;
  }
  return unit === 0 ? `${bytes} bytes` : `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
}

function packSize(pack: LocalNode): string | null {
  if (pack.size === undefined || pack.files === undefined) {
    return null;
  }
  const files = `${pack.files.toLocaleString("en-US")} files`;
  return pack.counted === false
    ? `at least ${formatSize(pack.size)} in over ${files}`
    : `${formatSize(pack.size)} in ${files}`;
}

function isLargePack(pack: LocalNode): boolean {
  return (
    pack.counted === false ||
    (pack.size ?? 0) > LARGE_PACK_BYTES ||
    (pack.files ?? 0) > LARGE_PACK_FILES
  );
}

// The custom node packs the artist removed. With Attach Custom Nodes off, none are attached.
function excludedNodes(): string[] {
  const { removed } = attachmentEdits();
  return (state.plan?.localNodes ?? [])
    .filter((pack) => removed.has(pack.path))
    .map((pack) => pack.name);
}

// The custom node packs the job attaches.
function attachedNodes(): LocalNode[] {
  const { removed } = attachmentEdits();
  return attachesNodes() ? (state.plan?.localNodes ?? []).filter((pack) => !removed.has(pack.path)) : [];
}

// "3 files", "1 folder" and so on, joined as "3 files, 1 folder and 2 models".
function listText(parts: string[]): string {
  return parts.length > 1 ? `${parts.slice(0, -1).join(", ")} and ${parts.at(-1)}` : parts[0] ?? "";
}

// How many added paths the summary names before it says how many more there are.
const SUMMARY_PATHS = 5;

// A number of files, such as 5,000 files.
function fileCount(count: number): string {
  return `${count.toLocaleString("en-US")} file${count === 1 ? "" : "s"}`;
}

// "1 folder (5,000 files)" for the folder loaders' folders that attach files, and with whole
// folders the artist added, "3 folders (5,000 files in 1 of them)".
function foldersText(groups: number, groupedFiles: number, directories: number): string | null {
  const folders = groups + directories;
  if (!groups) {
    return folders ? plural(folders, "folder") : null;
  }
  const within = directories ? ` in ${groups} of them` : "";
  return `${plural(folders, "folder")} (${fileCount(groupedFiles)}${within})`;
}

// What the job attaches: the planned input files less those removed, plus those added, and the
// models and custom node packs. A folder loader's folder counts as one folder of the files it
// attaches. Both views show it next to Submit, so the artist sees what this workflow's job
// uploads, including what they added, wherever they submit from.
export function attachmentSummary(): string | null {
  if (!state.plan) {
    return null;
  }
  const { filenames, directories } = attachments();
  const attached = new Set(filenames);
  const folders = plannedFolders();
  const inFolders = new Set(folders.flatMap((folder) => folder.files));
  const groups = folders.filter((folder) => folder.files.some((path) => attached.has(path))).length;
  const groupedFiles = filenames.filter((path) => inFolders.has(path)).length;
  const models = (state.plan.localModels ?? []).length - excludedModels().length;
  const packs = attachedNodes().length;
  const files = filenames.length - groupedFiles;
  const parts = [
    files ? plural(files, "file") : null,
    foldersText(groups, groupedFiles, directories.length),
    models ? plural(models, "model") : null,
    packs ? plural(packs, "custom node pack") : null,
  ].filter((part): part is string => part !== null);
  // The paths the artist added that the workflow doesn't load, and so only go because of them.
  const listed = state.plan.attachments ?? { filenames: [], directories: [] };
  const planned = new Set([...listed.filenames, ...inFolders, ...listed.directories]);
  const { added, removed } = attachmentEdits();
  const extra = [...new Set([...added.filenames, ...added.directories])].filter(
    (path) => !removed.has(path) && !planned.has(path)
  );
  const more = extra.length > SUMMARY_PATHS ? `, and ${extra.length - SUMMARY_PATHS} more` : "";
  const shown = extra.length
    ? ` You added ${extra.slice(0, SUMMARY_PATHS).join(", ")}${more}.`
    : "";
  return `${parts.length ? `Attaches ${listText(parts)}.` : "Attaches no files."}${shown}`;
}

// Adds files or a folder with a file dialog, which opens on the ComfyUI machine, whose files the
// job attaches. A dialog that's still open when another workflow opens adds nothing: its files
// were chosen for the workflow before.
export async function chooseAttachments(kind: string, key: keyof PathLists) {
  const generation = workflowGeneration;
  const tab = state.planTab;
  let paths: string[];
  try {
    ({ paths } = await postJson<{ paths: string[] }>("/deadline/choose", { kind }));
  } catch (error) {
    toast("error", messageOf(error), 8000);
    return;
  }
  if (generation !== workflowGeneration || tab === null || state.planTab !== tab) {
    if (paths.length) {
      toast(
        "warn",
        "The workflow changed while the file dialog was open, so the files you chose weren't added. Add them again.",
        10000
      );
    }
    return;
  }
  const edits = attachmentEdits();
  for (const path of paths) {
    if (!edits.added[key].includes(path)) {
      edits.added[key].push(path);
    }
    edits.removed.delete(path);
  }
  refresh("attachments", "submit");
}

function attachmentItem(
  text: string,
  path: string,
  prefix: string | null,
  onRemove: () => void,
  onRestore: () => void,
  // A file listed under its folder.
  nested = false
) {
  const removed = attachmentEdits().removed.has(path);
  const className = [nested ? "dl-nested" : null, removed ? "dl-removed" : null].filter(Boolean);
  return h(
    "li",
    { title: path, className: className.join(" ") || null },
    prefix ? h("span", { className: "dl-muted" }, prefix) : null,
    h("span", { className: "dl-path" }, text),
    removed
      ? h("button", { className: "dl-link", onClick: onRestore }, "Add back")
      : h("button", { className: "dl-link", title: "Don't attach", onClick: onRemove }, "✕")
  );
}

// A folder loader's folder as one row, which opens to list its files. Its ✕ removes every file
// in it, and Add back restores them all.
function folderItem(
  folder: FolderFiles,
  open: boolean,
  onToggle: () => void,
  onRemove: () => void,
  onRestore: () => void
) {
  const { removed } = attachmentEdits();
  const attached = folder.files.filter((path) => !removed.has(path)).length;
  // A removed folder is struck through, like a removed file. One that's partly removed says how
  // many of its files are attached.
  const total = fileCount(folder.files.length);
  const partly = attached > 0 && attached < folder.files.length;
  const count = partly ? `${attached.toLocaleString("en-US")} of ${total}` : total;
  return h(
    "li",
    { title: folder.path, className: attached ? "dl-folder" : "dl-folder dl-removed" },
    h(
      "button",
      {
        className: "dl-link dl-folder-toggle",
        "aria-expanded": open ? "true" : "false",
        onClick: onToggle,
      },
      h("span", { className: "dl-muted" }, open ? "▾ " : "▸ "),
      h("span", { className: "dl-path" }, `${folder.path} (${count})`)
    ),
    attached
      ? h("button", { className: "dl-link", title: "Don't attach the folder", onClick: onRemove }, "✕")
      : h("button", { className: "dl-link", onClick: onRestore }, "Add back")
  );
}

// A file's path in its folder, such as sub/a.png for /input/cats/sub/a.png in /input/cats.
function pathInFolder(path: string, folder: string): string {
  const separator = path.charAt(folder.length);
  return path.startsWith(folder) && (separator === "/" || separator === "\\")
    ? path.slice(folder.length + 1)
    : path;
}

function renderAttachments() {
  if (!state.plan) {
    return null;
  }
  // The files the workflow loads stay listed after you remove them, so you can add them back.
  const planned = state.plan.attachments ?? { filenames: [], directories: [] };
  // A folder loader's files are listed under their folder, which shows as one row until opened.
  const folders = plannedFolders();
  const inFolders = new Set(folders.flatMap((folder) => folder.files));
  // This workflow's own changes. Another workflow tab has its own.
  const { added, removed } = attachmentEdits();
  interface Row {
    path: string;
    kind: "file" | "folder";
    // Added with the file dialog rather than found in the workflow.
    manual: boolean;
    key: keyof PathLists;
  }
  const rows = new Map<string, Row>();
  const lists: [string[], Row["kind"], boolean][] = [
    [planned.filenames, "file", false],
    [planned.directories, "folder", false],
    [added.filenames, "file", true],
    [added.directories, "folder", true],
  ];
  for (const [paths, kind, manual] of lists) {
    for (const path of paths) {
      if (!rows.has(path) && !(kind === "file" && inFolders.has(path))) {
        rows.set(path, { path, kind, manual, key: kind === "file" ? "filenames" : "directories" });
      }
    }
  }
  const localModels = state.plan.localModels ?? [];
  const localNodes = attachesNodes() ? state.plan.localNodes ?? [] : [];
  const attachedCount =
    [...rows.keys()].filter((path) => !removed.has(path)).length +
    folders.filter((folder) => folder.files.some((path) => !removed.has(path))).length +
    localModels.length -
    excludedModels().length +
    attachedNodes().length;
  // Each change re-renders Submit too, whose summary lists what the job attaches.
  const remove = (row: Row) => () => {
    if (row.manual) {
      added[row.key] = added[row.key].filter((path) => path !== row.path);
    } else {
      removed.add(row.path);
    }
    refresh("attachments", "submit");
  };
  const restore = (row: Row) => () => {
    removed.delete(row.path);
    refresh("attachments", "submit");
  };
  // A folder's ✕ and Add back change every file in it.
  const changeFolder = (folder: FolderFiles, remove: boolean) => () => {
    for (const path of folder.files) {
      if (remove) {
        removed.add(path);
      } else {
        removed.delete(path);
      }
    }
    refresh("attachments", "submit");
  };
  const toggleFolder = (folder: FolderFiles) => () => {
    if (!state.openFolders.delete(folder.path)) {
      state.openFolders.add(folder.path);
    }
    refresh("attachments");
  };
  const rowItem = (row: Row) =>
    attachmentItem(row.path, row.path, row.kind === "folder" ? "▸ " : "", remove(row), restore(row));
  // A folder's row, and its files only while it's open, so a folder of thousands of files is one
  // row until the artist opens it.
  const folderItems = (folder: FolderFiles) => {
    const open = state.openFolders.has(folder.path);
    const row = folderItem(
      folder,
      open,
      toggleFolder(folder),
      changeFolder(folder, true),
      changeFolder(folder, false)
    );
    if (!open) {
      return [row];
    }
    const files = folder.files.map((path) => {
      const file: Row = { path, kind: "file", manual: false, key: "filenames" };
      const text = pathInFolder(path, folder.path);
      return attachmentItem(text, path, null, remove(file), restore(file), true);
    });
    return [row, ...files];
  };
  // The workflow's own files, then its folders, then the paths the artist added.
  const rowList = [...rows.values()];
  const plannedFiles = rowList.filter((row) => row.kind === "file" && !row.manual).length;
  // Removing a model or a custom node pack can make the job fail. The status warns about it.
  const toggleFound = (found: { path: string }, remove: boolean) => () => {
    if (remove) {
      removed.add(found.path);
    } else {
      removed.delete(found.path);
    }
    refresh("status", "attachments", "submit");
  };
  const add = (kind: string, key: keyof PathLists) => () => chooseAttachments(kind, key);
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
          h("div", { className: "dl-label" }, "Models the workflow loads, attached automatically"),
          h(
            "ul",
            { className: "dl-files", "data-dl-scroll": "models" },
            localModels.map((model) =>
              attachmentItem(
                model.id,
                model.path,
                null,
                toggleFound(model, true),
                toggleFound(model, false)
              )
            )
          ),
        ]
      : null,
    localNodes.length
      ? [
          h("div", { className: "dl-label" }, "Custom nodes the workflow uses, attached automatically"),
          h(
            "ul",
            { className: "dl-files" },
            localNodes.map((pack) =>
              attachmentItem(
                packSize(pack) ? `${pack.name} (${packSize(pack)})` : pack.name,
                pack.path,
                null,
                toggleFound(pack, true),
                toggleFound(pack, false)
              )
            )
          ),
        ]
      : null,
    rows.size || folders.length
      ? h(
          "ul",
          { className: "dl-files", "data-dl-scroll": "inputs" },
          rowList.slice(0, plannedFiles).map(rowItem),
          folders.map(folderItems),
          rowList.slice(plannedFiles).map(rowItem)
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
    const stages: Record<string, string> = {
      hashing: "Hashing input files",
      uploading: "Uploading input files",
    };
    const stage = stages[submission.stage ?? ""];
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
      submission.id && !submission.starting
        ? h("button", { className: "dl-button", onClick: cancelSubmission }, "Cancel")
        : null
    );
  }
  const count = currentTaskCount();
  const blocker = submitBlocker();
  const summary = attachmentSummary();
  return h(
    "div",
    { className: "dl-submit" },
    summary ? h("div", { className: "dl-muted dl-message", "data-role": "attachment-summary" }, summary) : null,
    h(
      "button",
      { className: "dl-button dl-primary dl-wide", disabled: !canSubmit(), onClick: submit },
      state.plan?.sources?.length && splitsTasks() && count ? `Submit ${plural(count, "task")}` : "Submit"
    ),
    blocker ? h("div", { className: "dl-muted dl-message", "data-role": "submit-blocker" }, blocker) : null
  );
}

function statusText(job: Job): string {
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

// Downloads by job ID.
const downloads: Record<string, { status: "DOWNLOADING"; progress?: string }> = {};

function canDownload(job: Job): boolean {
  const counts = state.jobStatus[job.jobId]?.taskRunStatusCounts ?? {};
  return (counts.SUCCEEDED ?? 0) > 0 && downloads[job.jobId]?.status !== "DOWNLOADING";
}

function downloadText(job: Job): string | null {
  const download = downloads[job.jobId];
  if (download?.status !== "DOWNLOADING") {
    return null;
  }
  const percent = Number(download.progress);
  return `Downloading outputs${Number.isFinite(percent) ? ` · ${percent}%` : "…"}`;
}

async function downloadOutputs(job: Job) {
  downloads[job.jobId] = { status: "DOWNLOADING" };
  refresh("jobs");
  try {
    await postJson("/deadline/jobs/download", {
      clientId: api.clientId,
      farmId: job.farmId,
      queueId: job.queueId,
      jobId: job.jobId,
    });
  } catch (error) {
    delete downloads[job.jobId];
    toast("error", messageOf(error), 10000);
    refresh("jobs");
  }
}

function listenForDownloads() {
  api.addEventListener<DownloadEvent>("deadline.download", ({ detail }) => {
    const job = state.jobs.find((other) => other.jobId === detail.jobId);
    const name = job?.name ?? detail.jobId;
    if (detail.status === "DOWNLOADING") {
      downloads[detail.jobId] = { status: "DOWNLOADING", progress: detail.progress };
    } else {
      delete downloads[detail.jobId];
      if (detail.status === "DOWNLOADED" && !detail.fileCount) {
        // The CLI's message says there was nothing to download.
        toast("info", `${name}: ${detail.message}`, 10000);
      } else if (detail.status === "DOWNLOADED") {
        const directory = detail.directory ?? job?.outputDir;
        const destination = directory
          ? `to ${directory}`
          : "into the output directory it was submitted with";
        toast("success", `${detail.message} from ${name} ${destination}.`, 10000);
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
          job.outputDir
            ? h(
                "div",
                { className: "dl-path dl-muted dl-destination", title: job.outputDir },
                `Downloads to ${job.outputDir}`
              )
            : null,
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

const RENDERERS: Record<SectionName, () => Children> = {
  account: renderAccount,
  target: renderTarget,
  status: renderStatus,
  tasks: renderTasks,
  jobSettings: renderJobSettings,
  parameters: renderParameters,
  queueParameters: renderQueueParameters,
  hostRequirements: renderHostRequirements,
  attachments: renderAttachments,
  submit: renderSubmit,
  jobs: renderJobs,
};

// A view renders some of the sections into a container, and re-renders them when they change.
function createView(container: HTMLElement, sections: SectionName[]): View {
  const slots: Partial<Record<SectionName, HTMLElement>> = {};
  for (const name of sections) {
    const slot = h("div", { className: `dl-slot dl-slot-${name}` });
    slots[name] = slot;
    container.append(slot);
  }
  const view: View = {
    render(names) {
      for (const name of names ?? sections) {
        const slot = slots[name];
        if (!slot) {
          continue;
        }
        // Keep the open/closed state of collapsible sections across re-renders.
        const open = [...slot.querySelectorAll("details")].map((details) => details.open);
        const scrollPositions = new Map(
          [...slot.querySelectorAll<HTMLElement>("[data-dl-scroll]")].map((list) => [
            list.getAttribute("data-dl-scroll"),
            list.scrollTop,
          ])
        );
        const content = RENDERERS[name]();
        slot.replaceChildren(
          ...flatten([content]).filter((child): child is Node | string | number => Boolean(child))
            .map((child) => (child instanceof Node ? child : String(child)))
        );
        slot.querySelectorAll("details").forEach((details, index) => {
          if (index < open.length) {
            details.open = open[index];
          }
        });
        slot.querySelectorAll<HTMLElement>("[data-dl-scroll]").forEach((list) => {
          list.scrollTop = scrollPositions.get(list.getAttribute("data-dl-scroll")) ?? 0;
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

const PANEL_SECTIONS: SectionName[] = [
  "account",
  "target",
  "status",
  "tasks",
  "jobSettings",
  "parameters",
  "queueParameters",
  "hostRequirements",
  "attachments",
  "jobs",
  // Last, and stuck to the bottom, with the summary of what the job attaches.
  "submit",
];

export function renderPanel(element: HTMLElement): () => void {
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
  const view = createView(root, PANEL_SECTIONS);
  loadAccount();
  loadPlan();
  pollJobs();
  return () => {
    views.delete(view);
  };
}

let dialog: { overlay: HTMLElement; view: View | null } | null = null;

// The dialog lists the attachments too, so the artist sees every file the job uploads before
// Submit, wherever they submit from.
const DIALOG_SECTIONS: SectionName[] = [
  "account",
  "target",
  "status",
  "tasks",
  "attachments",
  "submit",
];

export function openDialog() {
  injectStyles();
  if (dialog) {
    loadPlan();
    return;
  }
  const close = () => {
    if (dialog?.view) {
      views.delete(dialog.view);
    }
    dialog?.overlay.remove();
    document.removeEventListener("keydown", onKey);
    dialog = null;
  };
  const onKey = (event: KeyboardEvent) => {
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
      onClick: (event: Event) => {
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
  dialog.view = createView(body, DIALOG_SECTIONS);
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
.dl-task-math { float: right; font-weight: 400; color: var(--dl-muted); }
.dl-preview { color: var(--dl-muted); font-size: 11px; }
.dl-preview:empty { display: none; }
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
.dl-files { max-height: 240px; overflow-y: auto; }
.dl-files li { display: flex; align-items: center; gap: 4px; flex-shrink: 0; }
.dl-removed .dl-path { text-decoration: line-through; color: var(--dl-muted); }
.dl-path { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 0; }
.dl-folder-toggle { display: flex; align-items: center; gap: 4px; flex: 1; min-width: 0; padding: 0;
  color: inherit; text-align: left; }
.dl-files li.dl-nested { padding-left: 16px; }
.dl-jobs li { border-bottom: 1px solid var(--dl-border); padding-bottom: 4px; }
.dl-destination { font-size: 12px; }
.dl-jobs .dl-row { justify-content: space-between; font-size: 12px; }
.dl-progress, .dl-submit { display: flex; flex-direction: column; gap: 6px; }
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
