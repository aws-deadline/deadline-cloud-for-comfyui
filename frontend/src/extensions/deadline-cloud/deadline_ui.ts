// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// The Deadline Cloud sidebar panel and quick submit popover, which render from the same state.
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
  minLength?: number;
  maxLength?: number;
  allowedValues?: ParameterValue[];
  // A PATH's: FILE or DIRECTORY, and IN, OUT, INOUT or NONE.
  objectType?: string;
  dataFlow?: string;
  userInterface?: { control?: string; label?: string; groupLabel?: string; singleStepDelta?: number };
}

interface QueueDetails {
  parameters: Parameter[];
  storageProfiles: StorageProfile[];
  // The values the job sends for parameters the artist and the config don't set, such as Conda
  // Channels with deadline-cloud-v2.
  values?: Record<string, string>;
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
  // The lists in the workflow, such as Create List nodes, by the job parameter that would hold the
  // range of their items to run as tasks.
  sources?: { parameter: string }[];
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

// The job the last submission created, which the popover shows until the next submit.
interface Submitted {
  name: string;
  jobId: string;
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
  error?: string;
  // A job that was created although the submission didn't finish, or may have been.
  warning?: string;
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

// What the artist set for one workflow tab. Each tab keeps its own, so a value set for one workflow
// never goes with another's job.
interface WorkflowEdits {
  // The artist's values, which win over the ones the submitter fills in.
  edits: Record<string, ParameterValue>;
  queueEdits: Record<string, ParameterValue>;
  jobName: string | null;
  jobDescription: string;
  // The host requirements form, once the artist edits it. Until then the job template's apply.
  hostForm: HostForm | null;
  // The job settings the workflow saved, which win over the ones in localStorage.
  settings: Settings;
  // Whether the settings saved in the workflow were applied, which happens after its first plan.
  loadedSaved: boolean;
  // The files added to or removed from the job's attachments, so they never go with another's.
  attachments: AttachmentEdits;
  // The keys of the number fields that hold text that isn't a number. A number field's value reads
  // as empty then, so this keeps its error until the artist edits it again.
  badNumbers: Set<string>;
  // Job Properties opens on its next render, once, when the workflow's saved Output Directory isn't
  // ComfyUI's output folder, so the artist sees where the outputs go.
  openProperties: boolean;
}

export const SUMMARY = "Deadline Cloud";
export const SIDEBAR_TAB_ID = "deadline-cloud";
// The frontend registers this command for each sidebar tab. It closes the tab if it's open.
const TOGGLE_TAB_COMMAND = `Workspace.ToggleSidebarTab.${SIDEBAR_TAB_ID}`;

const SETTINGS_KEY = "DeadlineCloud.Submitter.JobSettings";
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
// The job template's parameters that Job Properties and Timeouts show. Advanced settings' Job
// parameters shows any others a custom template has.
const OUTPUT_DIR_PARAMETER = "OutputDir";
const TIMEOUT_PARAMETERS = new Set(["ServerStartTimeout", "WorkflowTimeout"]);
const DEFAULT_SETTINGS: Settings = {
  priority: 50,
  targetTaskRunStatus: "READY",
  maxFailedTasksCount: "",
  maxRetriesPerTask: "",
  maxWorkerCount: "",
  jobAttachmentsFileSystem: "",
};
// The largest whole number Deadline Cloud takes for a job's limits.
const MAX_INT = 2147483647;
// The job settings that are whole numbers. An empty field leaves the setting to its default.
const NUMBER_SETTINGS: Record<
  string,
  { label: string; placeholder: string; help: string; min: number; max: number }
> = {
  priority: {
    label: "Priority",
    placeholder: "50",
    help: "0–100. Jobs with a higher priority run first.",
    min: 0,
    max: 100,
  },
  maxFailedTasksCount: {
    label: "Maximum failed tasks count",
    placeholder: "Default",
    help: "The job fails after this many failed tasks.",
    min: 0,
    max: MAX_INT,
  },
  maxRetriesPerTask: {
    label: "Maximum retries per task",
    placeholder: "Default",
    help: "How many times a failed task is retried.",
    min: 0,
    max: MAX_INT,
  },
  maxWorkerCount: {
    label: "Maximum worker count",
    placeholder: "No limit",
    help: "The most workers that can run this job at once.",
    min: 1,
    max: MAX_INT,
  },
};
// The longest job name and description Deadline Cloud takes.
const JOB_NAME_LIMIT = 128;
const DESCRIPTION_LIMIT = 2048;
// What a job name can't hold: template expressions, which OpenJD would read, and control
// characters. A description can't have control characters other than tabs and line breaks.
const NAME_FORBIDDEN = /\{\{|\}\}|[\x00-\x1f\x7f-\x9f]/;
const DESCRIPTION_FORBIDDEN = /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]/;
// A path that's absolute on POSIX, on Windows, or on a Windows share.
const ABSOLUTE_PATH = /^(\/|[A-Za-z]:[\\/]|\\\\)/;
// The job settings a workflow saves. The others stay in localStorage only.
const SAVED_SETTINGS = ["targetTaskRunStatus", ...Object.keys(NUMBER_SETTINGS)];
// The most host requirements, and values of them, that a workflow can load.
const SAVED_HOST_LIMIT = 50;
const CHECKING_MESSAGE = "Checking your Deadline Cloud credentials…";

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
  settings: Settings;
  // Each workflow tab's edits, by workflowTab.
  workflowEdits: Map<string, WorkflowEdits>;
  // The folders whose files the attachments list shows, by path. The rest show as one row each.
  openFolders: Set<string>;
  submission: Submission | null;
  // Kept in memory only, so a reload clears it.
  submitted: Submitted | null;
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
  workflowEdits: new Map(),
  settings: { ...DEFAULT_SETTINGS, ...readStorage<Settings>(SETTINGS_KEY, {}) },
  openFolders: new Set(),
  submission: null,
  submitted: null,
};

type SectionName =
  // The panel's
  | "header"
  | "status"
  | "properties"
  | "submission"
  | "advanced"
  | "submit"
  // The popover's: its job name field, and the rest of it
  | "name"
  | "popover";

interface View {
  render(names?: SectionName[] | null): void;
}

const views = new Set<View>();

// Re-renders the named sections of every open view, or all of them. The popover shows a little of
// every section, so it re-renders with any of them.
function refresh(...sections: SectionName[]) {
  for (const view of views) {
    view.render(sections.length ? [...sections, "popover"] : null);
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

// A key for each workflow, which its tab's edits are kept by. ComfyUI keeps a workflow's object when
// it's saved for the first time or renamed, which changes its path, so the key stays.
const tabKeys = new WeakMap<object, string>();
let tabCount = 0;

function tabKey(workflow: object): string {
  let key = tabKeys.get(workflow);
  if (key === undefined) {
    key = String(++tabCount);
    tabKeys.set(workflow, key);
  }
  return key;
}

// The open workflow tab. Each keeps its own edits.
function workflowTab(): string {
  const workflow = app.extensionManager.workflow?.activeWorkflow;
  return workflow ? tabKey(workflow) : DEFAULT_TAB;
}

// ---------------------------------------------------------------------------------------------
// Loading

let accountLoad: Promise<void> | null = null;

// Where the farm, queue, storage profile and queue parameters show: Job submission settings, and
// Submit.
const TARGET_SECTIONS: SectionName[] = ["submission", "submit"];

export function loadAccount(force = false) {
  if (accountLoad && !force) {
    return accountLoad;
  }
  accountLoad = (async () => {
    state.accountError = null;
    refresh("submission");
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
      // A failed check leaves no account to submit with, rather than the last one that worked.
      state.account = null;
      state.accountError = messageOf(error);
    }
    refresh();
    // The popover is for submitting. Logging in, or fixing the credentials, happens in the panel.
    if (closePopover && accountNeedsAttention()) {
      closePopover();
      openPanel();
    }
    if (state.account?.authenticationStatus === "AUTHENTICATED") {
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
  refresh(...TARGET_SECTIONS);
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
  // Each tab's queue parameter values were for that queue too.
  for (const edits of state.workflowEdits.values()) {
    edits.queueEdits = Object.create(null);
    for (const key of edits.badNumbers) {
      if (key.startsWith("queue:")) {
        edits.badNumbers.delete(key);
      }
    }
  }
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
  refresh(...TARGET_SECTIONS);
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
  refresh(...TARGET_SECTIONS);
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
  refresh(...TARGET_SECTIONS);
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
  refresh(...TARGET_SECTIONS);
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
  refresh(...TARGET_SECTIONS);
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
  refresh("submission");
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
  refresh("header", "submit");
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
        refresh("header", "submit");
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
    // The settings the workflow saved apply once, after its first plan.
    const edits = workflowEdits(tab);
    if (!edits.loadedSaved) {
      edits.loadedSaved = true;
      applySavedSettings(edits, savedSettings);
    }
  }
  refresh();
}

let planTimer: ReturnType<typeof setTimeout> | undefined;
let graphKey: string | null = null;

// Layout that doesn't change what runs: positions, node sizes and collapsing, groups and the canvas
// view.
const LAYOUT_KEYS = new Set(["pos", "size", "flags"]);

function withoutLayout(key: string, value: unknown): unknown {
  // The settings the panel saves in the workflow don't change what runs either.
  if (key === "groups" || key === "ds" || key === "deadlineCloud") {
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
  savedSettings = app.graph.extra.deadlineCloud;
  // A workflow closed and opened again starts afresh, with its own saved settings.
  const open = app.extensionManager.workflow?.openWorkflows?.map(tabKey);
  for (const tab of open ? state.workflowEdits.keys() : []) {
    if (!open?.includes(tab)) {
      state.workflowEdits.delete(tab);
    }
  }
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
// Submitting

// A record's own value for a parameter's name, which can be any name, such as constructor.
function own<T>(record: Record<string, T> | undefined, name: string): T | undefined {
  return record && Object.hasOwn(record, name) ? record[name] : undefined;
}

function parameterValue(parameter: Parameter): ParameterValue {
  return own(workflowEdits().edits, parameter.name) ?? own(state.plan?.values, parameter.name) ?? parameter.default ?? "";
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
// place, such as Conda Packages' comfy, then the value the queue's details give the job, such as
// Conda Channels with deadline-cloud-v2, then the queue environment's default.
function queueDefault(parameter: Parameter): ParameterValue {
  const configured = own(state.plan?.queueValues, parameter.name);
  if (configured !== undefined) {
    return configured;
  }
  const template = state.plan?.parameters?.find(({ name }) => name === parameter.name);
  if (template) {
    return parameterValue(template);
  }
  return own(state.queue?.values, parameter.name) ?? parameter.default ?? "";
}

function queueParameterValue(parameter: Parameter): ParameterValue {
  return own(workflowEdits().queueEdits, parameter.name) ?? queueDefault(parameter);
}

// Sets the artist's value for a queue parameter. An empty value goes back to the default rather
// than sending an empty one, which would replace the queue's default, such as its conda channels.
export function editQueueParameter(name: string, value: ParameterValue | null | undefined) {
  if (value === "" || value === null || value === undefined) {
    delete workflowEdits().queueEdits[name];
  } else {
    workflowEdits().queueEdits[name] = value;
  }
}

// What the artist set for the open workflow tab, or for the tab given.
function workflowEdits(tab = workflowTab()): WorkflowEdits {
  let edits = state.workflowEdits.get(tab);
  if (!edits) {
    // By parameter name, which can be any name, such as __proto__.
    edits = {
      edits: Object.create(null),
      queueEdits: Object.create(null),
      jobName: null,
      jobDescription: "",
      hostForm: null,
      settings: {},
      loadedSaved: false,
      attachments: { removed: new Set(), added: { filenames: [], directories: [] } },
      badNumbers: new Set(),
      openProperties: false,
    };
    state.workflowEdits.set(tab, edits);
  }
  return edits;
}

// The artist's attachment changes for the workflow tab the plan was made from.
function attachmentEdits(): AttachmentEdits {
  return state.planTab === null
    ? { removed: new Set(), added: { filenames: [], directories: [] } }
    : workflowEdits(state.planTab).attachments;
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
  return workflowEdits().jobName ?? `ComfyUI - ${state.plan?.name ?? "workflow"}`.slice(0, JOB_NAME_LIMIT);
}

function jobNameError(value = jobName()): string | null {
  const name = value.trim();
  if (!name) {
    return "Enter a job name.";
  }
  if (NAME_FORBIDDEN.test(name)) {
    return "The job name can't have {{, }} or control characters.";
  }
  return name.length > JOB_NAME_LIMIT ? `The job name can be at most ${JOB_NAME_LIMIT} characters.` : null;
}

function descriptionError(value = workflowEdits().jobDescription): string | null {
  if (DESCRIPTION_FORBIDDEN.test(value)) {
    return "The description can't have control characters other than tabs and line breaks.";
  }
  return value.length > DESCRIPTION_LIMIT
    ? `The description can be at most ${DESCRIPTION_LIMIT} characters.`
    : null;
}

// Why a number field's text can't be submitted, or null. An empty field uses its default.
function numberError(label: string, value: unknown, integer: boolean, min?: number, max?: number) {
  const text = value === null || value === undefined ? "" : String(value).trim();
  if (!text) {
    return null;
  }
  const number = Number(text);
  const valid =
    (integer ? /^[+-]?\d+$/.test(text) : Number.isFinite(number)) &&
    (min === undefined || number >= min) &&
    (max === undefined || number <= max);
  const range =
    min !== undefined && max !== undefined
      ? ` from ${min} to ${max}`
      : min !== undefined
        ? ` of at least ${min}`
        : max !== undefined
          ? ` of at most ${max}`
          : "";
  return valid ? null : `${label} must be ${integer ? "a whole number" : "a number"}${range}.`;
}

function settingError(key: string, value = setting(key)) {
  const { label, min, max } = NUMBER_SETTINGS[key];
  return numberError(label, value, true, min, max);
}

// Why a job setting's number field can't be submitted, or null.
function settingFieldError(key: string) {
  return badNumberError(`setting:${key}`) ?? settingError(key);
}

function parameterLabel(parameter: Parameter) {
  return parameter.userInterface?.label || parameter.name;
}

function lengthError(label: string, text: string, min?: number, max?: number) {
  if (min !== undefined && text.length < min) {
    return `${label} needs at least ${plural(min, "character")}.`;
  }
  return max !== undefined && text.length > max ? `${label} can have at most ${plural(max, "character")}.` : null;
}

// Why a parameter's value can't be submitted, or null: one its choices don't have, or a number out
// of range, for example. An empty number isn't sent, so the template's default applies. A path the
// artist set must be absolute; the template's and the plan's can be relative.
function parameterError(parameter: Parameter, value: ParameterValue, edited = false) {
  const { type, allowedValues, minLength, maxLength } = parameter;
  const label = parameterLabel(parameter);
  const text = String(value);
  if (allowedValues && !allowedValues.map(String).includes(text)) {
    return `Choose a value for ${label}.`;
  }
  if (type === "BOOL") {
    return parseBool(value) === null ? `${label} must be true or false.` : null;
  }
  if (type === "INT" || type === "FLOAT") {
    return numberError(label, value, type === "INT", parameter.minValue, parameter.maxValue);
  }
  if (type === "PATH") {
    const path = text.trim();
    // An output path, such as Output Directory, that's empty leaves the job's outputs nowhere.
    if (!path && parameter.dataFlow === "OUT") {
      return `${label} can't be empty.`;
    }
    if (edited && path && !ABSOLUTE_PATH.test(path)) {
      return `${label} must be an absolute path.`;
    }
    return lengthError(label, path, minLength, maxLength);
  }
  return type === "STRING" ? lengthError(label, text, minLength, maxLength) : null;
}

// The error of a number field that holds text that isn't a number, by its key.
function badNumberError(key: string) {
  return workflowEdits().badNumbers.has(key) ? "Enter a number." : null;
}

// Why a job template parameter's field can't be submitted, or null.
function templateParameterError(parameter: Parameter) {
  const edited = Object.hasOwn(workflowEdits().edits, parameter.name);
  return badNumberError(`parameter:${parameter.name}`) ?? parameterError(parameter, parameterValue(parameter), edited);
}

// Why a queue environment parameter's field can't be submitted, or null.
function queueParameterError(parameter: Parameter) {
  const edited = Object.hasOwn(workflowEdits().queueEdits, parameter.name);
  return badNumberError(`queue:${parameter.name}`) ?? parameterError(parameter, queueParameterValue(parameter), edited);
}

// Why a field the artist can edit holds a value that can't be submitted: the first one, or null.
function fieldError(): string | null {
  const errors = [
    ...Object.keys(NUMBER_SETTINGS).map(settingFieldError),
    ...shownParameters().map(templateParameterError),
    ...shownQueueParameters().map(queueParameterError),
  ];
  return jobNameError() ?? descriptionError() ?? errors.find(Boolean) ?? hostFormError();
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

// Whether the farms, or the chosen farm's queues, are still loading.
function listingTargets(): boolean {
  return !state.cloudError && (state.farms === null || (Boolean(state.farmId) && state.queues === null));
}

// Whether the artist has to choose a farm or a queue: none is shown, and none is still loading.
function needsTarget(): boolean {
  return !targetShown() && !listingTargets();
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
    !fieldError()
  );
}

// Whether the account check ended in anything but logged in: not logged in, logging in,
// credentials that don't work, or a check that failed. Not while it's checking.
function accountNeedsAttention(): boolean {
  return (
    Boolean(state.accountError) ||
    (state.account !== null && state.account.authenticationStatus !== "AUTHENTICATED")
  );
}

// Why Submit is off, when the account, the farm, queue, storage profile or a field's value is the
// reason. Other reasons, such as the workflow, show where they are.
export function submitBlocker(): string | null {
  if (state.submission) {
    return null;
  }
  if (state.account?.authenticationStatus !== "AUTHENTICATED") {
    if (state.accountError || (state.account && state.account.authenticationStatus !== "NEEDS_LOGIN")) {
      return "Check your Deadline Cloud credentials to submit.";
    }
    // Nothing while the account is still being checked.
    return state.account ? "Log in to Deadline Cloud to submit." : null;
  }
  if (!targetShown()) {
    return listingTargets() ? "Loading the farms and queues…" : "Choose a farm and a queue to submit to.";
  }
  if (!queueDetailsLoaded()) {
    return state.queueError ? state.queueError : "Loading the queue's details…";
  }
  if (storageProfileUnresolved()) {
    return `The queue doesn't list the storage profile ${state.storageProfileCleared}, so it isn't used. Choose a storage profile, or None to upload every input file.`;
  }
  return fieldError();
}

// Why the popover's Submit can't proceed, or null. The popover gives way to the panel once the
// account turns out not to be logged in, so it only says it's checking.
function quickNote(): string | null {
  if (state.account?.authenticationStatus !== "AUTHENTICATED") {
    return CHECKING_MESSAGE;
  }
  if (needsTarget()) {
    return "Choose a farm and queue in Additional settings first.";
  }
  const reading = state.planning || !state.plan ? "Reading the workflow…" : null;
  return submitBlocker() ?? state.planError ?? reading;
}

function trimmedPath(parameter: Parameter, value: ParameterValue) {
  return parameter.type === "PATH" ? String(value).trim() : value;
}

// What the job bundle is made from.
function bundleRequest() {
  const plan = state.plan;
  const parameters: Record<string, ParameterValue> = Object.create(null);
  for (const parameter of plan?.parameters ?? []) {
    if (AUTOMATIC_PARAMETERS.has(parameter.name)) {
      continue;
    }
    const value = trimmedPath(parameter, parameterValue(parameter));
    // An empty number can't be sent, so the template's default applies.
    if (value === "" && (parameter.type === "INT" || parameter.type === "FLOAT")) {
      continue;
    }
    parameters[parameter.name] = value;
  }
  // Only the chosen queue's own parameters. queueEdits is cleared when the queue changes.
  const queueParameters = queueDetailsLoaded() ? state.queue?.parameters ?? [] : [];
  const { queueEdits, jobDescription, hostForm } = workflowEdits();
  // The ones the artist, the config or the queue's details set. The others keep the queue's default.
  const sent = [queueEdits, plan?.queueValues ?? {}, state.queue?.values ?? {}];
  for (const parameter of queueParameters) {
    if (sent.some((values) => Object.hasOwn(values, parameter.name))) {
      parameters[parameter.name] = trimmedPath(parameter, queueParameterValue(parameter));
    }
  }
  // The lists' ranges. The job runs as one task, so it has no task parameters to send them to.
  for (const source of plan?.sources ?? []) {
    delete parameters[source.parameter];
  }
  const settings: Record<string, ParameterValue> = { name: jobName().trim() };
  // Only the settings the panel has. localStorage can still hold a removed one, such as Require all
  // input paths exist, which the server now always sets.
  for (const key of Object.keys(DEFAULT_SETTINGS)) {
    const value = setting(key);
    if (value !== "" && value !== null) {
      settings[key] = key === "targetTaskRunStatus" || key === "jobAttachmentsFileSystem" ? value : Number(value);
    }
  }
  return {
    parameters,
    settings,
    description: jobDescription.trim(),
    hostRequirements: hostForm ? requirementsFromForm(hostForm) : undefined,
    attachments: attachments(),
    excludedModels: excludedModels(),
    excludedNodes: excludedNodes(),
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
  // A submit that fails or is canceled leaves no row for the job before.
  state.submitted = null;
  refresh("submit");
  let body: WorkflowBody | null = null;
  try {
    body = await plannedWorkflow();
  } catch (error) {
    toast("error", `Couldn't convert the workflow to API format: ${messageOf(error)}`, 10000);
  }
  if (!body) {
    state.submission = null;
    refresh("submit");
    return;
  }
  // Built right after the check, from the plan of this same workflow.
  const request = bundleRequest();
  const saved = settingsToSave();
  const tab = workflowTab();
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
    saveInWorkflow(saved, tab);
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
  refresh("submit");
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
      openPopover();
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
      if (ours && detail.jobId) {
        state.submitted = { name: detail.name ?? detail.jobId, jobId: detail.jobId };
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
      refresh("submit");
    }
  });
  api.addEventListener("graphChanged", schedulePlan);
}

// ---------------------------------------------------------------------------------------------
// Settings saved in the workflow

const SAVED_VERSION = 1;

// What the workflow opened last saved in its extra.deadlineCloud, which its tab applies after its
// first plan.
let savedSettings: unknown;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// The job template's parameters a workflow saves: numbers, checkboxes and choices, and Output
// Directory, as the absolute path the artist chose, which anyone the workflow is shared with sees.
// No other free text or paths: Extra Model Paths Config and Extra ComfyUI Arguments, say, could
// point the workers at other custom nodes, so a shared workflow mustn't set them.
function savesParameter(parameter: Parameter) {
  const { name, type, allowedValues } = parameter;
  return (
    name === OUTPUT_DIR_PARAMETER ||
    type === "INT" ||
    type === "FLOAT" ||
    type === "BOOL" ||
    (type === "STRING" && Boolean(allowedValues))
  );
}

// What a workflow saves of the settings it was submitted with, to load when it's opened again.
// Workflows are shared, and ComfyUI saves them in the images it makes, so this is only what's safe
// to share: no free text parameters, no paths but Output Directory, and no farm, queue, queue
// environment or attachments.
function settingsToSave(): Record<string, unknown> {
  const edits = workflowEdits();
  const saved: Record<string, unknown> = { version: SAVED_VERSION };
  if (edits.jobName !== null) {
    saved.jobName = edits.jobName;
  }
  if (edits.jobDescription) {
    saved.description = edits.jobDescription;
  }
  for (const key of SAVED_SETTINGS) {
    const value = setting(key);
    if (value !== "" && value !== null && value !== undefined) {
      saved[key] = key === "targetTaskRunStatus" ? value : Number(value);
    }
  }
  const parameters = shownParameters().filter(
    (parameter) => Object.hasOwn(edits.edits, parameter.name) && savesParameter(parameter)
  );
  if (parameters.length) {
    saved.parameters = Object.fromEntries(parameters.map(({ name }) => [name, edits.edits[name]]));
  }
  if (edits.hostForm) {
    saved.hostRequirements = requirementsFromForm(edits.hostForm);
  }
  return saved;
}

// Saves the settings a job was submitted with in its workflow. ComfyUI saves the workflow when it's
// in a file and had no other changes. Any other workflow keeps them until the artist saves it.
function saveInWorkflow(saved: Record<string, unknown>, tab: string) {
  // The artist opened another workflow while the job was submitted.
  if (workflowTab() !== tab) {
    return;
  }
  const workflow = app.extensionManager.workflow?.activeWorkflow;
  const unchanged = workflow?.isTemporary === false && workflow.isModified === false;
  app.graph.extra.deadlineCloud = saved;
  if (unchanged) {
    app.extensionManager.command.execute("Comfy.SaveWorkflow");
  }
}

// Whether a saved value is one the parameter's field takes from the artist, so a path must be
// absolute.
function isParameterValue(parameter: Parameter, value: unknown): value is ParameterValue {
  const types = ["string", "number", "boolean"];
  return types.includes(typeof value) && !parameterError(parameter, value as ParameterValue, true);
}

// The host requirements form for saved requirements, when they pass its checks. A crafted workflow
// can't freeze the tab with thousands of them.
function savedHostForm(saved: unknown): HostForm | null {
  if (!isRecord(saved)) {
    return null;
  }
  try {
    const { attributes = [], amounts = [] } = saved as HostRequirements;
    const entries = attributes.reduce(
      (count, { anyOf, allOf }) => count + 1 + (anyOf?.length ?? 0) + (allOf?.length ?? 0),
      amounts.length
    );
    if (entries > SAVED_HOST_LIMIT) {
      return null;
    }
    const form = formFromRequirements(saved as HostRequirements);
    return hostFormError(form) ? null : form;
  } catch {
    // Not the shape the form makes.
    return null;
  }
}

// Applies the settings a workflow saved to its tab's edits, once its plan has loaded. It skips
// values the plan doesn't define or the fields wouldn't take, and all of them when the version is
// unknown.
function applySavedSettings(edits: WorkflowEdits, saved: unknown) {
  if (!isRecord(saved) || saved.version !== SAVED_VERSION) {
    return;
  }
  const { jobName, description, parameters, hostRequirements } = saved;
  // A name the artist typed while the plan loaded stays.
  if (edits.jobName === null && typeof jobName === "string" && !jobNameError(jobName)) {
    edits.jobName = jobName;
  }
  if (typeof description === "string" && !descriptionError(description)) {
    edits.jobDescription = description;
  }
  for (const key of SAVED_SETTINGS) {
    const value = saved[key];
    const valid =
      key === "targetTaskRunStatus"
        ? value === "READY" || value === "SUSPENDED"
        : typeof value === "number" && !settingError(key, value);
    if (valid) {
      edits.settings[key] = value as string | number;
    }
  }
  if (isRecord(parameters)) {
    for (const parameter of shownParameters()) {
      const value = own(parameters, parameter.name);
      if (savesParameter(parameter) && isParameterValue(parameter, value)) {
        edits.edits[parameter.name] = value;
        const planned = String(own(state.plan?.values, parameter.name) ?? "").trim();
        if (parameter.name === OUTPUT_DIR_PARAMETER && String(value).trim() !== planned) {
          edits.openProperties = true;
        }
      }
    }
  }
  const form = savedHostForm(hostRequirements);
  if (form) {
    edits.hostForm = form;
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

// Text under a control, such as an error, which updates as the artist edits the control.
function liveText(control: HTMLElement, className: string, text: () => string | null) {
  const element = h("span", { className }, text());
  for (const name of ["input", "change"]) {
    control.addEventListener(name, () => {
      element.textContent = text() ?? "";
    });
  }
  return element;
}

// Each field error's ID, for its control's aria-describedby.
let fieldErrors = 0;

// A labeled field. error says what's wrong with its value, under it, as the artist edits it. action
// is a button beside the control, such as Browse….
function field(
  label: string,
  control: HTMLElement,
  help?: string,
  error?: () => string | null,
  action?: HTMLElement | null
) {
  if (help) {
    control.setAttribute("aria-description", help);
  }
  // The error's text, which sets aria-invalid too.
  const invalid = () => {
    const text = error?.() ?? null;
    control.setAttribute("aria-invalid", text ? "true" : "false");
    return text;
  };
  const message = error ? liveText(control, "dl-field-error", invalid) : null;
  if (message) {
    const id = `dl-field-error-${++fieldErrors}`;
    message.setAttribute("id", id);
    control.setAttribute("aria-describedby", id);
  }
  return h(
    "label",
    {
      className:
        (control as HTMLInputElement).type === "checkbox" ? "dl-field dl-field-check" : "dl-field",
    },
    h("span", { className: "dl-label", "data-dl-tip": help || null }, label),
    action ? h("div", { className: "dl-row" }, control, action) : control,
    message
  );
}

// A number field, with the browser's own arrows. Text the browser can't read as a number reads as
// empty, so the field's key is recorded, which keeps its error until it's edited again.
function numberInput(key: string, props: Props, onValue: (value: string) => void) {
  return h("input", {
    ...props,
    className: "dl-input",
    type: "number",
    name: key,
    onInput: (event: Event) => {
      const input = event.target as HTMLInputElement;
      const { badNumbers } = workflowEdits();
      if (input.validity.badInput) {
        badNumbers.add(key);
      } else {
        badNumbers.delete(key);
      }
      onValue(input.value);
    },
  });
}

// One of the frontend's Lucide icons, such as icon("copy").
// Icons are decorative: every button with one also has its text or a title.
function icon(name: string, className = "") {
  return h("i", { className: `icon-[lucide--${name}] dl-icon ${className}`, "aria-hidden": "true" });
}

function iconButton(name: string, label: string, onClick: () => void, disabled = false) {
  return h("button", { className: "dl-icon-button", title: label, disabled, onClick }, icon(name));
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
    placeholder ? h("option", { value: "", selected: !value, "data-dl-placeholder": true }, placeholder) : null,
    options.map(([optionValue, text]) =>
      h("option", { value: optionValue, selected: optionValue === value }, text)
    )
  );
}

// A drawer. createView keeps it open or closed across renders, and opens it when open is true, such
// as when it holds a field with an error.
function section(title: string, open: boolean, ...children: Children[]) {
  return h(
    "details",
    { className: "dl-section", open },
    h("summary", {}, icon("chevron-right", "dl-chevron"), h("span", { className: "dl-grow" }, title)),
    h("div", { className: "dl-section-body" }, ...children)
  );
}

function subheading(text: string, tip?: string) {
  return h("div", { className: "dl-subheading", "data-dl-tip": tip, "aria-description": tip }, text);
}

function warning(className: string, text: string) {
  return h("div", { className }, icon("triangle-alert"), h("span", {}, text));
}

function renderHeader() {
  return h(
    "div",
    { className: "dl-header" },
    h("i", { className: "dl-logo" }),
    h("span", { className: "dl-title" }, "Deadline Cloud"),
    iconButton("refresh-cw", "Read the workflow again", () => loadPlan(true), state.planning),
    iconButton("x", "Close", () => app.extensionManager.command.execute(TOGGLE_TAB_COMMAND))
  );
}

// The AWS profile, such as " (default)", to follow "Logged in".
function profileText() {
  return state.account?.profile ? ` (${state.account.profile})` : "";
}

// The account's state, in Job submission settings: logged in, checking, not logged in, or why its
// credentials can't be used.
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
    return h("div", { className: "dl-meta" }, CHECKING_MESSAGE);
  }
  if (account.authenticationStatus === "AUTHENTICATED") {
    return h("div", { className: "dl-meta" }, h("span", { className: "dl-dot dl-ok" }), `Logged in${profileText()}`);
  }
  if (account.authenticationStatus === "NEEDS_LOGIN") {
    return h(
      "div",
      { className: "dl-meta", "data-role": "account" },
      h("span", { className: "dl-dot dl-warn" }),
      state.loggingIn ? "Finish logging in with Deadline Cloud monitor…" : `Not logged in${profileText()}`,
      state.loggingIn ? null : h("button", { className: "dl-button dl-small", onClick: login }, "Log in")
    );
  }
  return h(
    "div",
    { className: "dl-notice dl-error" },
    `The Deadline Cloud credentials${profileText()} don't work (${account.authenticationStatus}). `,
    "Check the AWS profile in Deadline Cloud monitor or the Deadline Cloud configuration.",
    h("button", { className: "dl-link", onClick: () => loadAccount(true) }, "Check again")
  );
}

// Farm and Queue, once logged in. A select whose choice Submit needs has a warning border.
function renderTarget() {
  const farms = state.farms;
  const queues = state.queues;
  const farmSelect = farms
    ? select(
        farms.map((farm) => [farm.farmId, farm.displayName]),
        state.farmId,
        selectFarm,
        "Choose a farm"
      )
    : null;
  const queueSelect = queues
    ? select(
        queues.map((queue) => [queue.queueId, queue.displayName]),
        state.queueId,
        selectQueue,
        "Choose a queue"
      )
    : null;
  if (needsTarget()) {
    (state.farmId ? queueSelect : farmSelect)?.classList.add("dl-needs");
  }
  return [
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
      farmSelect ?? h("span", { className: "dl-muted" }, "Loading…"),
      "The Deadline Cloud farm to submit to."
    ),
    state.farmId
      ? field(
          "Queue",
          queueSelect ?? h("span", { className: "dl-muted" }, "Loading…"),
          "The queue in this farm that runs the job."
        )
      : null,
    renderAccount(),
    storageProfileField(),
    renderQueueParameters(),
  ];
}

function storageProfileField() {
  // Only the chosen queue's own profiles, once its details load.
  const profiles = queueDetailsLoaded() ? state.queue?.storageProfiles ?? [] : [];
  if (!profiles.length) {
    return null;
  }
  const profileOptions = profiles.map((profile) => [profile.storageProfileId, profile.displayName]);
  // After a default profile was cleared, None has to be chosen like any profile, so the select
  // starts on a placeholder rather than on None. Otherwise None is a choice like the others.
  const unresolved = storageProfileUnresolved();
  const profileSelect = unresolved
    ? select(
        [[NO_STORAGE_PROFILE, "None"], ...profileOptions],
        "",
        chooseStorageProfile,
        "Choose a storage profile"
      )
    : select([["", "None"], ...profileOptions], state.storageProfileId ?? "", chooseStorageProfile);
  profileSelect.classList.toggle("dl-needs", unresolved);
  return field(
    "Default storage profile",
    profileSelect,
    "Tells Deadline Cloud which of your paths are on shared storage, so they aren't uploaded."
  );
}

// The plan's warnings, and the panel's own about what it doesn't attach.
function planWarnings(): string[] {
  const plan = state.plan;
  if (!plan) {
    return [];
  }
  // With Attach Custom Nodes off, the workers provide the custom nodes.
  const ignored = attachesNodes() ? [] : plan.nodeWarnings ?? [];
  const warnings = (plan.warnings ?? []).filter((warning) => !ignored.includes(warning));
  const excluded = excludedModels();
  if (excluded.length) {
    warnings.push(
      `Not attaching ${excluded.join(", ")}. The job fails unless the workers already have it.`
    );
  }
  const largePacks = attachesNodes()
    ? (plan.localNodes ?? []).filter(
        (pack) => isLargePack(pack) && !attachmentEdits().removed.has(pack.path)
      )
    : [];
  for (const pack of largePacks) {
    warnings.push(
      `The custom node pack ${pack.name} is ${packSize(pack)}. A job that uses one of its nodes uploads the whole pack. Remove it under Attachments if the workers already have it.`
    );
  }
  const removedPacks = attachesNodes() ? excludedNodes() : [];
  if (removedPacks.length) {
    warnings.push(
      `Not attaching the custom nodes in ${removedPacks.join(", ")}. The job fails unless the workers already have them, such as from a custom nodes conda package.`
    );
  }
  return warnings;
}

// The job name field, which the panel and the popover both show. other is the section of the
// other one, which re-renders to show the new name.
function jobNameInput(other: SectionName, onEnter?: () => void) {
  return h("input", {
    className: "dl-input",
    name: "job-name",
    maxlength: JOB_NAME_LIMIT,
    value: jobName(),
    onInput: (event: Event) => {
      workflowEdits().jobName = inputValue(event);
      refresh(other, "submit");
    },
    onKeydown:
      onEnter &&
      ((event: Event) => {
        // Enter also ends composing text with an input method, which doesn't submit.
        const key = event as KeyboardEvent;
        if (key.key === "Enter" && !key.isComposing && key.keyCode !== 229) {
          onEnter();
        }
      }),
  });
}

function renderStatus() {
  return h(
    "div",
    { className: "dl-status" },
    field("Job name", jobNameInput("name"), "The job's name in Deadline Cloud monitor.", () =>
      jobNameError()
    ),
    state.planError ? h("div", { className: "dl-notice dl-error" }, state.planError) : null,
    planWarnings().map((text) => warning("dl-notice dl-warn-box", text))
  );
}

// The job settings: the workflow's own, when it saved them, or the ones every workflow starts with.
function setting(key: string) {
  const saved = workflowEdits().settings;
  return key in saved ? saved[key] : state.settings[key];
}

// Sets a job setting. One the workflow saved stays the workflow's own. Others are kept in
// localStorage, for every workflow that didn't save its own.
function setSetting(key: string, value: ParameterValue) {
  const saved = workflowEdits().settings;
  if (key in saved) {
    saved[key] = value;
  } else {
    state.settings[key] = value;
    writeStorage(SETTINGS_KEY, state.settings);
  }
  refresh("submit");
}

// half makes the field half the drawer's width, when it has no other beside it.
function numberSetting(key: string, half = false) {
  const { label, placeholder, help, min, max } = NUMBER_SETTINGS[key];
  const input = numberInput(`setting:${key}`, { min, max, placeholder, value: setting(key) }, (value) =>
    setSetting(key, value)
  );
  input.classList.toggle("dl-half", half);
  return field(label, input, help, () => settingFieldError(key));
}

// Job Properties: the description, the job settings and the template's Output Directory. It opens
// by itself when one of them holds a value that can't be submitted, and when the workflow brought
// its own Output Directory.
function renderProperties() {
  const output = shownParameters().filter(({ name }) => name === OUTPUT_DIR_PARAMETER);
  const edits = workflowEdits();
  const savedOutput = edits.openProperties;
  edits.openProperties = false;
  const open =
    savedOutput ||
    Boolean(descriptionError()) ||
    Object.keys(NUMBER_SETTINGS).some((key) => settingFieldError(key)) ||
    output.some((parameter) => templateParameterError(parameter));
  return section(
    "Job Properties",
    open,
    field(
      "Description",
      h("textarea", {
        className: "dl-input",
        rows: 2,
        maxlength: DESCRIPTION_LIMIT,
        placeholder: "The job template's description",
        value: workflowEdits().jobDescription,
        name: "description",
        onInput: (event: Event) => {
          workflowEdits().jobDescription = inputValue(event);
          refresh("submit");
        },
      }),
      "The job's description in Deadline Cloud monitor.",
      () => descriptionError()
    ),
    h(
      "div",
      { className: "dl-grid2" },
      numberSetting("priority"),
      field(
        "Initial state",
        select(
          [
            ["READY", "Ready"],
            ["SUSPENDED", "Suspended"],
          ],
          String(setting("targetTaskRunStatus")),
          (value) => setSetting("targetTaskRunStatus", value)
        ),
        "Suspended jobs wait until you resume them in Deadline Cloud monitor."
      )
    ),
    h("div", { className: "dl-grid2" }, numberSetting("maxRetriesPerTask"), numberSetting("maxFailedTasksCount")),
    numberSetting("maxWorkerCount", true),
    output.map(parameterField)
  );
}

// Job submission settings: the farm, queue, account, storage profile and queue parameters. It opens
// by itself when the account isn't logged in or Submit needs a choice or a value from it.
function renderSubmissionSettings() {
  const loggedIn = state.account?.authenticationStatus === "AUTHENTICATED";
  const open =
    accountNeedsAttention() ||
    (loggedIn &&
      (needsTarget() ||
        Boolean(state.queueError) ||
        storageProfileUnresolved() ||
        shownQueueParameters().some((parameter) => queueParameterError(parameter))));
  return section("Job submission settings", open, loggedIn ? renderTarget() : renderAccount());
}

// Advanced settings, with a drawer for the attachments, the timeouts, the host requirements and any
// other parameters of a custom template. A drawer with a value that can't be submitted opens by
// itself, and so does Advanced settings.
function renderAdvanced() {
  const parameters = shownParameters().filter(({ name }) => name !== OUTPUT_DIR_PARAMETER);
  const timeouts = parameters.filter(({ name }) => TIMEOUT_PARAMETERS.has(name));
  const others = parameters.filter(({ name }) => !TIMEOUT_PARAMETERS.has(name));
  const invalid = (list: Parameter[]) => list.some((parameter) => templateParameterError(parameter));
  const hostInvalid = Boolean(hostFormError());
  return section(
    "Advanced settings",
    invalid(parameters) || hostInvalid,
    h(
      "div",
      { className: "dl-drawers" },
      section("Attachments", false, renderAttachments()),
      timeouts.length ? section("Timeouts", invalid(timeouts), timeouts.map(parameterField)) : null,
      state.plan ? section("Host requirements", hostInvalid, renderHostRequirements()) : null,
      others.length ? section("Job parameters", invalid(others), byGroup(others, parameterField)) : null
    )
  );
}

// The OpenJD controls that choose a path, and the kind of file dialog each opens. Choose output file
// has none, since the dialog only picks files that exist.
const PATH_CONTROLS: Record<string, string> = {
  CHOOSE_DIRECTORY: "folder",
  CHOOSE_INPUT_FILE: "file",
};

// The parameter's control, or the one OpenJD gives a parameter that doesn't name one.
function controlOf({ type, objectType, dataFlow, userInterface }: Parameter): string {
  if (userInterface?.control) {
    return userInterface.control;
  }
  if (type === "BOOL") {
    return "CHECK_BOX";
  }
  if (type !== "PATH") {
    return "";
  }
  if (objectType === "DIRECTORY") {
    return "CHOOSE_DIRECTORY";
  }
  return objectType === "FILE" && dataFlow === "OUT" ? "CHOOSE_OUTPUT_FILE" : "CHOOSE_INPUT_FILE";
}

// key identifies the field among the panel's number fields, such as parameter:Steps.
function parameterControl(
  parameter: Parameter,
  value: ParameterValue,
  key: string,
  onChange: (value: ParameterValue) => void
): HTMLElement {
  const control = controlOf(parameter);
  const allowed = parameter.allowedValues?.map(String);
  if (control === "CHECK_BOX") {
    return h("input", {
      type: "checkbox",
      checked: parseBool(value) === true,
      onChange: (event: Event) => {
        const checked = inputChecked(event);
        // A STRING with choices sends the one that reads as the box's state, such as yes.
        const choice = parameter.allowedValues?.find((option) => parseBool(option) === checked);
        onChange(choice ?? (checked ? "true" : "false"));
      },
    });
  }
  if (control === "DROPDOWN_LIST" || allowed) {
    // A value the choices don't have, such as a default that isn't one, shows as Choose….
    return select(
      (allowed ?? []).map((option) => [option, option]),
      String(value),
      onChange,
      allowed?.includes(String(value)) ? undefined : "Choose…"
    );
  }
  if (control === "MULTILINE_EDIT") {
    return h("textarea", {
      className: "dl-input",
      rows: 3,
      value: String(value),
      onInput: (event: Event) => onChange(inputValue(event)),
    });
  }
  if (parameter.type === "INT" || parameter.type === "FLOAT") {
    const step = parameter.userInterface?.singleStepDelta ?? (parameter.type === "FLOAT" ? "any" : null);
    const { minValue: min, maxValue: max } = parameter;
    return numberInput(key, { min, max, step, value: String(value) }, onChange);
  }
  return h("input", {
    className: "dl-input",
    value: String(value),
    onInput: (event: Event) => onChange(inputValue(event)),
  });
}

// Browse… for a parameter that chooses a path, or null. The dialog opens on the ComfyUI machine. A
// path chosen for another workflow, tab or queue, which opened while it was open, isn't set.
function browseButton(parameter: Parameter, onChange: (value: ParameterValue) => void) {
  const kind = own(PATH_CONTROLS, controlOf(parameter));
  if (!kind) {
    return null;
  }
  const browse = async () => {
    const opened = () => JSON.stringify([workflowGeneration, workflowTab(), targetKey(state.farmId, state.queueId)]);
    const before = opened();
    const [path] = await choosePaths(kind);
    if (!path) {
      return;
    }
    if (opened() !== before) {
      toast("warn", "The workflow or queue changed while the file dialog was open, so the path wasn't set.", 10000);
      return;
    }
    onChange(path);
    refresh("properties", "submission", "advanced");
  };
  const label = `Browse for ${parameterLabel(parameter)}`;
  return h("button", { className: "dl-button", "aria-label": label, onClick: browse }, "Browse…");
}

// The job template's parameters the panel shows. The submitter fills in some, and the lists' aren't
// sent.
function shownParameters(): Parameter[] {
  const lists = new Set((state.plan?.sources ?? []).map((source) => source.parameter));
  return (state.plan?.parameters ?? []).filter(
    ({ name, userInterface }) =>
      !AUTOMATIC_PARAMETERS.has(name) && !lists.has(name) && userInterface?.control !== "HIDDEN"
  );
}

// The chosen queue's environment parameters the panel shows, once they load.
function shownQueueParameters(): Parameter[] {
  const loaded = queueDetailsLoaded() ? state.queue?.parameters ?? [] : [];
  return loaded.filter((parameter) => parameter.userInterface?.control !== "HIDDEN");
}

// A job template parameter's field, with Browse… for a path.
function parameterField(parameter: Parameter) {
  const key = `parameter:${parameter.name}`;
  const onChange = (value: ParameterValue) => {
    workflowEdits().edits[parameter.name] = value;
    if (parameter.name === ATTACH_NODES_PARAMETER) {
      refresh("status", "advanced", "submit");
    } else {
      refresh("submit");
    }
  };
  const control = parameterControl(parameter, parameterValue(parameter), key, onChange);
  control.setAttribute("name", key);
  return field(
    parameterLabel(parameter),
    control,
    parameter.description,
    () => templateParameterError(parameter),
    browseButton(parameter, onChange)
  );
}

// The parameters by their groupLabel, each group under a subheading, such as a custom template's
// Workflow Parameters. render makes each one's field.
function byGroup(parameters: Parameter[], render: (parameter: Parameter) => Children) {
  const groups = new Map<string, Parameter[]>();
  for (const parameter of parameters) {
    const group = parameter.userInterface?.groupLabel || "";
    groups.set(group, [...(groups.get(group) ?? []), parameter]);
  }
  return [...groups].map(([group, list]) => [group ? subheading(group) : null, list.map(render)]);
}

// The queue's environment parameters, under the library's group label of each environment, such as
// Queue Environment: Conda.
function renderQueueParameters() {
  return byGroup(shownQueueParameters(), (parameter) => {
    const { name } = parameter;
    const key = `queue:${name}`;
    const edited = () => Object.hasOwn(workflowEdits().queueEdits, name);
    const reset = h(
      "button",
      {
        className: "dl-link",
        "data-role": "queue-reset",
        hidden: !edited(),
        onClick: () => {
          editQueueParameter(name, "");
          refresh("submission", "submit");
        },
      },
      "Use the default"
    );
    const onChange = (value: ParameterValue) => {
      editQueueParameter(name, value);
      reset.hidden = !edited();
      refresh("submit");
    };
    const control = parameterControl(parameter, queueParameterValue(parameter), key, onChange);
    control.setAttribute("name", key);
    // A cleared field shows the default it goes back to.
    if (control instanceof HTMLInputElement || control instanceof HTMLTextAreaElement) {
      control.placeholder = String(queueDefault(parameter));
    }
    return h(
      "div",
      { className: "dl-queue-parameter" },
      field(
        parameterLabel(parameter),
        control,
        parameter.description,
        () => queueParameterError(parameter),
        browseButton(parameter, onChange)
      ),
      reset
    );
  });
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

// Custom host requirement names and values, as the submitter checks them: a name of identifiers
// separated by periods, whose first isn't one OpenJD keeps for its own, and values that start with
// a letter or underscore.
const CAPABILITY_NAME = /^[a-zA-Z_][a-zA-Z0-9_]{0,63}(\.[a-zA-Z_][a-zA-Z0-9_]{0,63})*$/;
const RESERVED_CAPABILITY_NAMES = ["worker", "job", "step", "task"];
const ATTRIBUTE_VALUE = /^[a-zA-Z_][a-zA-Z0-9_-]{0,99}$/;

// Why a custom amount's or attribute's name can't be submitted, or null. A standard one, such as
// worker.gpu, is fine.
function capabilityNameError(name: string, kind: "amount" | "attribute", standard: string[]) {
  if (!name) {
    return `Name each custom ${kind}.`;
  }
  const prefix = kind === "amount" ? "amount." : "attr.";
  const valid = CAPABILITY_NAME.test(name) && !RESERVED_CAPABILITY_NAMES.includes(name.split(".")[0]);
  return valid || standard.includes(prefix + name)
    ? null
    : `Name ${name} with letters, numbers and underscores, separated by periods, not starting with worker, job, step or task.`;
}

// Why a min and max can't be submitted, or null. key identifies their number fields.
function rangeError(label: string, fields: RangeFields, key: string) {
  const bad = badNumberError(`${key}:min`) ?? badNumberError(`${key}:max`);
  if (bad) {
    return bad;
  }
  const [min, max] = [fields.min, fields.max].map((text) => (text?.trim() ? Number(text) : undefined));
  if ([min, max].some((value) => value !== undefined && !(value >= 0))) {
    return `The min and max of ${label} must be 0 or more.`;
  }
  return min !== undefined && max !== undefined && min > max ? `The min of ${label} is more than its max.` : null;
}

function amountError(amount: HostForm["amounts"][number], index: number) {
  const name = amount.name.trim();
  const set = amount.min?.trim() || amount.max?.trim();
  return (
    capabilityNameError(name, "amount", HOST_HARDWARE.map(([standard]) => standard)) ??
    rangeError(name, amount, `host:amount:${index}`) ??
    (set ? null : `Set a min or a max for ${name}.`)
  );
}

function attributeError(attribute: HostForm["attributes"][number]) {
  const name = attribute.name.trim();
  const values = attribute.values.split(",").map((value) => value.trim()).filter(Boolean);
  const nameError = capabilityNameError(name, "attribute", HOST_CHOICES.map(([standard]) => standard));
  if (nameError || !values.length) {
    return nameError ?? `Add a value for ${name}.`;
  }
  return values.every((value) => ATTRIBUTE_VALUE.test(value))
    ? null
    : `The values of ${name} need letters, numbers, underscores and dashes, each starting with a letter or underscore.`;
}

function hostFormError(form = workflowEdits().hostForm) {
  if (!form) {
    return null;
  }
  const errors = [
    ...HOST_HARDWARE.map(([name, label]) => rangeError(label, form.hardware[name] ?? {}, `host:${name}`)),
    ...form.amounts.map(amountError),
    ...form.attributes.map(attributeError),
  ];
  return errors.find(Boolean) ?? null;
}

function hostForm(): HostForm {
  return workflowEdits().hostForm ?? formFromRequirements(state.plan?.hostRequirements);
}

// The first edit copies the job template's requirements, so they aren't lost by changing one.
function editHostForm(): HostForm {
  const edits = workflowEdits();
  edits.hostForm ??= formFromRequirements(state.plan?.hostRequirements);
  return edits.hostForm;
}

function hostFormEdited() {
  for (const element of document.querySelectorAll<HTMLElement>("[data-role='host-reset']")) {
    element.hidden = !workflowEdits().hostForm;
  }
  refresh("submit");
}

// A Min or Max field of a host requirement, such as vCPUs minimum. key identifies the row's fields.
function hostNumber(label: string, end: "Min" | "Max", key: string, value: string | undefined, onInput: (value: string) => void) {
  const name = `${label} ${end === "Min" ? "minimum" : "maximum"}`;
  const props = { min: 0, step: "any", placeholder: end, value: value ?? "", "aria-label": name };
  return numberInput(`${key}:${end.toLowerCase()}`, props, (text) => {
    onInput(text);
    hostFormEdited();
  });
}

// A row of the host requirements form, with its error under it.
function hostRow(className: string, error: () => string | null, ...children: Children[]) {
  const row = h("div", { className }, ...children);
  row.append(liveText(row, "dl-field-error", error));
  return row;
}

// Advanced settings shows it once the plan has loaded, with the template's host requirements.
function renderHostRequirements() {
  const form = hostForm();
  const changed = (rerender = false) => {
    if (rerender) {
      // The rows change, so text that wasn't a number goes with the fields that held it.
      for (const key of workflowEdits().badNumbers) {
        if (key.startsWith("host:")) {
          workflowEdits().badNumbers.delete(key);
        }
      }
      refresh("advanced");
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
        { className: "dl-row dl-choices", role: "group", "aria-label": label },
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
    return hostRow(
      "dl-field",
      () => rangeError(label, hostForm().hardware[name] ?? {}, `host:${name}`),
      h(
        "span",
        { className: "dl-label", "data-dl-tip": "The least and the most a worker can have to run this job. Leave either empty for no limit." },
        label
      ),
      h(
        "div",
        { className: "dl-range" },
        hostNumber(label, "Min", `host:${name}`, form.hardware[name]?.min, edit("min")),
        hostNumber(label, "Max", `host:${name}`, form.hardware[name]?.max, edit("max"))
      )
    );
  });
  const remove = (key: "amounts" | "attributes", index: number) =>
    h(
      "button",
      {
        className: "dl-link",
        title: "Remove",
        "aria-label": `Remove ${form[key][index].name || `the custom ${key === "amounts" ? "amount" : "attribute"}`}`,
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
      "aria-label": key === "amounts" ? "Custom amount name" : "Custom attribute name",
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
    const label = amount.name || "Custom amount";
    return hostRow(
      "dl-custom",
      () => amountError(hostForm().amounts[index], index),
      h("div", { className: "dl-row" }, customName("Amount name, such as license.nuke", "amounts", index), remove("amounts", index)),
      h(
        "div",
        { className: "dl-range" },
        hostNumber(label, "Min", `host:amount:${index}`, amount.min, edit("min")),
        hostNumber(label, "Max", `host:amount:${index}`, amount.max, edit("max"))
      )
    );
  });
  const attributeRows = form.attributes.map((attribute, index) =>
    hostRow(
      "dl-custom",
      () => attributeError(hostForm().attributes[index]),
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
          "aria-label": `Values of ${attribute.name || "the custom attribute"}`,
          value: attribute.values,
          onInput: (event: Event) => {
            editHostForm().attributes[index].values = inputValue(event);
            hostFormEdited();
          },
        })
      )
    )
  );
  return [
    choiceRows,
    hardwareRows,
    subheading(
      "Custom host requirements",
      "Capabilities your farm defines on its workers, such as a license count or installed software."
    ),
    amountRows,
    attributeRows,
    h(
      "div",
      { className: "dl-row dl-wrap" },
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
      )
    ),
    // It resets the whole form, not only the custom rows above it.
    h(
      "button",
      {
        className: "dl-link",
        "data-role": "host-reset",
        "data-dl-tip": "Go back to the job template's host requirements.",
        hidden: !workflowEdits().hostForm,
        onClick: () => {
          workflowEdits().hostForm = null;
          changed(true);
        },
      },
      "Reset all to defaults"
    ),
  ];
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

function plural(count: number, noun: string) {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

// A number of files, such as 5,000 files.
function fileCount(count: number): string {
  return `${count.toLocaleString("en-US")} file${count === 1 ? "" : "s"}`;
}

// Adds files or a folder with a file dialog, which opens on the ComfyUI machine, whose files the
// job attaches. A dialog that's still open when another workflow opens adds nothing: its files
// were chosen for the workflow before.
export async function chooseAttachments(kind: string, key: keyof PathLists) {
  const generation = workflowGeneration;
  const tab = state.planTab;
  const paths = await choosePaths(kind);
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
  refresh("advanced", "submit");
}

// Opens a file or folder dialog on the ComfyUI machine, and returns the chosen paths. An error, such
// as another dialog being open, shows as a toast.
async function choosePaths(kind: string): Promise<string[]> {
  try {
    return (await postJson<{ paths: string[] }>("/deadline/choose", { kind })).paths;
  } catch (error) {
    toast("error", messageOf(error), 8000);
    return [];
  }
}

function attachmentItem(
  text: string,
  path: string,
  iconName: string,
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
    icon(iconName),
    h("span", { className: "dl-path" }, text),
    removed
      ? h("button", { className: "dl-link", onClick: onRestore }, "Add back")
      : h("button", { className: "dl-link", title: "Don't attach", "aria-label": `Don't attach ${text}`, onClick: onRemove }, "✕")
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
    icon("folder"),
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
      ? h(
          "button",
          { className: "dl-link", title: "Don't attach the folder", "aria-label": `Don't attach ${folder.path}`, onClick: onRemove },
          "✕"
        )
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

// Advanced settings' Attachments: the files the job attaches, and how it attaches them.
function renderAttachments() {
  return [
    renderAttachmentList(),
    field(
      "Job attachments",
      select(
        [
          ["COPIED", "Copied to the worker"],
          ["VIRTUAL", "Virtual file system"],
        ],
        String(state.settings.jobAttachmentsFileSystem),
        (value) => setSetting("jobAttachmentsFileSystem", value),
        "Default"
      ),
      "How workers read the attached input files. Virtual loads them as needed, on workers that support it."
    ),
  ];
}

function renderAttachmentList() {
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
  // Each change re-renders Submit too, which checks the job again.
  const remove = (row: Row) => () => {
    if (row.manual) {
      added[row.key] = added[row.key].filter((path) => path !== row.path);
    } else {
      removed.add(row.path);
    }
    refresh("advanced", "submit");
  };
  const restore = (row: Row) => () => {
    removed.delete(row.path);
    refresh("advanced", "submit");
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
    refresh("advanced", "submit");
  };
  const toggleFolder = (folder: FolderFiles) => () => {
    if (!state.openFolders.delete(folder.path)) {
      state.openFolders.add(folder.path);
    }
    refresh("advanced");
  };
  const rowItem = (row: Row) => attachmentItem(row.path, row.path, row.kind, remove(row), restore(row));
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
      return attachmentItem(text, path, "file", remove(file), restore(file), true);
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
    refresh("status", "advanced", "submit");
  };
  const add = (kind: string, key: keyof PathLists) => () => chooseAttachments(kind, key);
  return [
    localModels.length
      ? h(
          "ul",
          { className: "dl-files", "data-dl-scroll": "models" },
          localModels.map((model) =>
            attachmentItem(model.id, model.path, "box", toggleFound(model, true), toggleFound(model, false))
          )
        )
      : null,
    localNodes.length
      ? h(
          "ul",
          { className: "dl-files" },
          localNodes.map((pack) =>
            attachmentItem(
              packSize(pack) ? `${pack.name} (${packSize(pack)})` : pack.name,
              pack.path,
              "package",
              toggleFound(pack, true),
              toggleFound(pack, false)
            )
          )
        )
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
    ),
  ];
}

const STAGES: Record<string, string> = {
  hashing: "Hashing input files",
  uploading: "Uploading input files",
};

// The submission in progress, and the confirmation the server waits for. It's the popover's row,
// and the panel's footer: a status tile, the name, a status line, a progress bar, and Cancel.
function renderSubmission(submission: Submission) {
  const percent = typeof submission.progress === "number" ? Math.round(submission.progress) : null;
  const stage = STAGES[submission.stage ?? ""];
  return [
    h(
      "div",
      { className: "dl-job" },
      h("span", { className: "dl-tile" }, icon("upload")),
      h(
        "div",
        { className: "dl-job-text" },
        h("div", { className: "dl-job-name" }, h("span", { className: "dl-path" }, submission.name ?? jobName())),
        h("span", { className: "dl-muted" }, stage ? `${stage}${percent === null ? "" : ` · ${percent}%`}` : "Submitting…"),
        percent === null ? null : h("span", { className: "dl-bar" }, h("span", { style: `width: ${percent}%` }))
      ),
      submission.starting
        ? null
        : h("button", { className: "dl-button dl-small", onClick: cancelSubmission }, "Cancel")
    ),
    submission.message ? h("div", { className: "dl-muted dl-message dl-job-note" }, submission.message) : null,
    submission.confirm
      ? h(
          "div",
          { className: "dl-notice dl-warn-box dl-confirm" },
          h("div", { className: "dl-message" }, submission.confirm.message),
          h(
            "div",
            { className: "dl-row" },
            h("button", { className: "dl-button dl-primary", onClick: () => answerConfirmation(true) }, "Continue"),
            h("button", { className: "dl-button", onClick: () => answerConfirmation(false) }, "Don't submit")
          )
        )
      : null,
  ];
}

function renderSubmit() {
  if (state.submission) {
    return h("div", { className: "dl-submit" }, renderSubmission(state.submission));
  }
  const blocker = submitBlocker();
  return h(
    "div",
    { className: "dl-submit" },
    h(
      "button",
      { className: "dl-button dl-primary dl-wide", disabled: !canSubmit(), onClick: submit },
      h("i", { className: "dl-logo" }),
      "Submit to Deadline Cloud"
    ),
    blocker ? h("div", { className: "dl-muted dl-message dl-blocker", "data-role": "submit-blocker" }, blocker) : null
  );
}

function renderName() {
  return field("Job name", jobNameInput("status", submit));
}

// The job the last submission created, until the next submit.
function renderSubmitted({ name, jobId }: Submitted) {
  return h(
    "div",
    { className: "dl-row dl-submitted", "data-role": "submitted" },
    icon("circle-check", "dl-success"),
    h("span", { className: "dl-grow" }, `Submitted ${name} as `, h("span", { className: "dl-nowrap" }, jobId)),
    iconButton("copy", "Copy job ID", () => {
      navigator.clipboard?.writeText(jobId);
      toast("info", `Copied ${jobId}.`, 2000);
    })
  );
}

// The rest of the popover: why Submit can't proceed, its buttons, and the submission's row.
function renderPopover() {
  const note = quickNote();
  const warnings = planWarnings().length;
  const submission = state.submission;
  const row = submission ? renderSubmission(submission) : state.submitted && renderSubmitted(state.submitted);
  return [
    note ? warning("dl-note", note) : null,
    warnings ? warning("dl-note", `This job has ${plural(warnings, "warning")}. See Additional settings.`) : null,
    h(
      "div",
      { className: "dl-row" },
      h(
        "button",
        {
          className: "dl-button dl-ghost",
          onClick: () => {
            closePopover?.();
            openPanel();
          },
        },
        icon("sliders-horizontal"),
        "Additional settings"
      ),
      h(
        "button",
        { className: "dl-button dl-primary", disabled: !canSubmit(), onClick: submit },
        h("i", { className: "dl-logo" }),
        "Submit"
      )
    ),
    row ? h("div", { className: "dl-pop-status" }, row) : null,
  ];
}

const RENDERERS: Record<SectionName, () => Children> = {
  header: renderHeader,
  status: renderStatus,
  properties: renderProperties,
  submission: renderSubmissionSettings,
  advanced: renderAdvanced,
  submit: renderSubmit,
  name: renderName,
  popover: renderPopover,
};

// A view renders some of the sections into a container, and re-renders them when they change.
// A drawer's title, which createView keeps its state by.
function drawerTitle(details: Element): string {
  return details.querySelector(":scope > summary")?.textContent ?? "";
}

function createView(container: HTMLElement, sections: SectionName[]): View {
  const slots: Partial<Record<SectionName, HTMLElement>> = {};
  // The drawers open in this view, by title. A drawer that isn't rendered for a while, such as
  // Timeouts while the workflow is read again, keeps its state for when it's back.
  const openDrawers = new Set<string>();
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
        // Keep drawers open across re-renders. One the renderer opens opens too.
        for (const details of slot.querySelectorAll("details")) {
          const title = drawerTitle(details);
          if (details.open) {
            openDrawers.add(title);
          } else {
            openDrawers.delete(title);
          }
        }
        const scrollPositions = new Map(
          [...slot.querySelectorAll<HTMLElement>("[data-dl-scroll]")].map((list) => [
            list.getAttribute("data-dl-scroll"),
            list.scrollTop,
          ])
        );
        // The field the artist is in, by its name, so a re-render keeps the focus and the caret.
        const active = document.activeElement as HTMLInputElement | null;
        const focused = active && slot.contains(active) ? active.getAttribute("name") : null;
        const { selectionStart, selectionEnd } = active ?? {};
        const content = RENDERERS[name]();
        slot.replaceChildren(
          ...flatten([content]).filter((child): child is Node | string | number => Boolean(child))
            .map((child) => (child instanceof Node ? child : String(child)))
        );
        const field = focused
          ? [...slot.querySelectorAll<HTMLInputElement>("[name]")].find((element) => element.getAttribute("name") === focused)
          : null;
        field?.focus();
        if (field && typeof selectionStart === "number" && typeof selectionEnd === "number") {
          field.setSelectionRange(selectionStart, selectionEnd);
        }
        for (const details of slot.querySelectorAll("details")) {
          if (openDrawers.has(drawerTitle(details))) {
            details.open = true;
          }
        }
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
// The sidebar panel and the popover

const PANEL_SECTIONS: SectionName[] = [
  "header",
  "status",
  "properties",
  "submission",
  "advanced",
  // Last, and stuck to the bottom.
  "submit",
];

const POPOVER_SECTIONS: SectionName[] = ["name", "popover"];

export function renderPanel(element: HTMLElement): () => void {
  element.replaceChildren();
  // The sidebar's wrapper has no height, so without this the panel grows instead of scrolling, and
  // the sticky submit button moves off screen.
  element.style.height = "100%";
  const root = h("div", { className: "dl-root dl-panel" });
  element.append(root);
  const view = createView(root, PANEL_SECTIONS);
  loadAccount();
  loadPlan();
  return () => {
    views.delete(view);
  };
}

let closePopover: (() => void) | null = null;

export function togglePopover() {
  if (closePopover) {
    closePopover();
  } else {
    openPopover();
  }
}

export function openPopover() {
  // Logging in, or fixing the credentials, happens in the panel.
  if (accountNeedsAttention()) {
    closePopover?.();
    openPanel();
    refresh("submission");
    return;
  }
  if (closePopover) {
    loadPlan();
    return;
  }
  const box = h("div", {
    className: "dl-root dl-popover",
    role: "dialog",
    "aria-label": "Submit to Deadline Cloud",
  });
  document.body.append(box);
  const view = createView(box, POPOVER_SECTIONS);
  // Under the button, and on screen, like the tooltip.
  const button = document.querySelector(".dl-action-button")?.getBoundingClientRect();
  const maxLeft = window.innerWidth - box.getBoundingClientRect().width - TOOLTIP_MARGIN;
  box.style.left = `${Math.max(TOOLTIP_MARGIN, Math.min(button?.left ?? TOOLTIP_MARGIN, maxLeft))}px`;
  box.style.top = `${(button?.bottom ?? 0) + TOOLTIP_GAP}px`;
  // A confirmation the server waits for stays open until it's answered.
  const onKey = (event: KeyboardEvent) => {
    if (event.key === "Escape" && !state.submission?.confirm) {
      closePopover?.();
    }
  };
  const onPointer = (event: PointerEvent) => {
    const target = event.target as Element;
    if (!state.submission?.confirm && !box.contains(target) && !target.closest(".dl-action-button")) {
      closePopover?.();
    }
  };
  document.addEventListener("keydown", onKey);
  document.addEventListener("pointerdown", onPointer, true);
  closePopover = () => {
    views.delete(view);
    box.remove();
    document.removeEventListener("keydown", onKey);
    document.removeEventListener("pointerdown", onPointer, true);
    closePopover = null;
  };
  loadAccount();
  loadPlan();
}

export function openPanel() {
  const workspace = app.extensionManager;
  if (workspace.sidebarTab?.activeSidebarTabId !== SIDEBAR_TAB_ID) {
    workspace.command.execute(TOGGLE_TAB_COMMAND);
  }
}

// ---------------------------------------------------------------------------------------------
// Styles, from ComfyUI's theme variables so they follow its light and dark palettes. Each takes
// the frontend's name, then the older one, then a value of its own.

// The logo of the deadline-cloud library, from its src/deadline/client/ui/resources/deadline_logo.svg.
const LOGO_SVG = `<?xml version="1.0" encoding="UTF-8"?><svg id="b" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 217.86 244"><defs><style>.h{fill:url(#e);}.h,.i{fill-rule:evenodd;stroke-width:0px;}.i{fill:#fff;}</style><linearGradient id="e" x1="13.15" y1="26.22" x2="228.57" y2="241.65" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#f90"/><stop offset="1" stop-color="#c33c1a"/></linearGradient></defs><g id="c"><g id="d"><path class="h" d="m215.58,57.17L111.01.53c-1.29-.71-2.86-.71-4.16,0L2.28,57.17c-1.4.76-2.28,2.23-2.28,3.83v122c0,1.6.88,3.07,2.28,3.83l104.57,56.64c.65.35,1.36.53,2.08.53s1.43-.17,2.08-.53l104.57-56.64c1.4-.76,2.28-2.23,2.28-3.83V61c0-1.6-.88-3.07-2.28-3.83h0Z"/><g id="f"><path id="g" class="i" d="m174.28,87.14v52.29h-8.12l-.59-.36v-49.24l-56.64-28.32-56.64,28.32v64.41l54.49,29.18c0,.11.02.23.03.34l-3.97,7.42-56.96-30.5c-1.42-.75-2.3-2.23-2.3-3.84v-69.71c0-1.65.93-3.16,2.41-3.9l61-30.5c1.22-.61,2.67-.61,3.9,0l61,30.5c1.48.74,2.41,2.25,2.41,3.9h0Zm-13.08,93.39l-17.43,10.46-17.42-10.46v-21.21l17.43-10.46,17.43,10.46v21.21Zm-56.64-60.42l17.43-10.46,17.43,10.46v21.21s-17.43,10.46-17.43,10.46l-17.42-10.46v-21.21Zm63.24,33.01l-19.68-11.81v-23.67c0-1.53-.8-2.95-2.11-3.73l-21.79-13.07c-1.38-.83-3.11-.83-4.49,0l-21.79,13.07c-1.31.78-2.11,2.2-2.11,3.73v26.14c0,1.53.81,2.95,2.11,3.73l19.67,11.81v23.67c0,1.53.81,2.95,2.11,3.73l21.78,13.07c.69.42,1.47.62,2.24.62s1.55-.2,2.24-.62l21.79-13.07c1.31-.78,2.11-2.2,2.11-3.73v-26.14c0-1.53-.8-2.95-2.11-3.73h0ZM110.84,26.65l82.9,43.57c1.43.75,2.33,2.24,2.33,3.86v95.86c0,1.62-.89,3.1-2.33,3.86l-12.93,6.81v-9.85s6.54-3.45,6.54-3.45v-90.59l-78.53-41.28L30.5,76.7v90.59l78.53,41.28,13.82-7.28,8.74,5.25-20.52,10.81c-.63.33-1.33.5-2.03.5s-1.39-.17-2.02-.5l-82.9-43.57c-1.43-.75-2.33-2.24-2.33-3.86v-95.86c0-1.62.89-3.1,2.33-3.86L106.79,26.65c1.27-.67,2.78-.67,4.05,0h0Z"/></g></g></g></svg>`;

export function injectStyles() {
  listenForTooltips();
  document.head.append(
    h(
      "style",
      {},
      `
.dl-root, .dl-tooltip, .dl-action-button {
  --dl-bg: var(--background, var(--comfy-menu-bg, #171718));
  --dl-fg: var(--foreground, var(--fg-color, #fff));
  --dl-muted: var(--muted-foreground, var(--descrip-text, #8a8a8a));
  --dl-surface: var(--secondary, var(--comfy-input-bg, #262729));
  --dl-hover: var(--accent, #313235);
  --dl-border: var(--border, var(--border-color, #494a50));
  --dl-primary: var(--primary, var(--primary-background, #0b8ce9));
  --dl-warning: var(--warning, #fd9903);
  --dl-destructive: var(--destructive, #b33a3a);
  --dl-success: var(--color-success, #00cd72);
  --dl-radius: var(--radius-md, 6px);
  --dl-radius-lg: var(--radius-lg, 8px);
  font-family: var(--font-inter, Inter, sans-serif); }
.dl-logo { display: inline-block; width: 16px; height: 16px; flex: none;
  background: url("data:image/svg+xml,${encodeURIComponent(LOGO_SVG)}") center / contain no-repeat; }
.side-bar-button-icon.dl-logo { width: 18px; height: 18px; }
button.dl-action-button { height: 32px; padding: 0 12px; gap: 6px; border-radius: var(--radius-lg, 8px);
  font-size: 14px; font-weight: 300; background: var(--dl-primary); color: #fff; }
.dl-root { color: var(--dl-fg); font-size: 14px; }
.dl-root, .dl-root * { box-sizing: border-box; }
.dl-root [hidden], .dl-slot:empty { display: none; }
.dl-icon { width: 16px; height: 16px; flex: none; }
.dl-grow { flex: 1; min-width: 0; }
.dl-muted { color: var(--dl-muted); }
.dl-row { display: flex; align-items: center; gap: 8px; }
.dl-wrap { flex-wrap: wrap; }
.dl-panel { height: 100%; overflow-y: auto; display: flex; flex-direction: column; background: var(--dl-bg); }
.dl-panel > .dl-slot { padding: 0 16px; }
.dl-panel > .dl-slot-status { padding-top: 16px; }
.dl-panel > .dl-slot-properties { padding-top: 8px; }
.dl-panel > .dl-slot-header { position: sticky; top: 0; z-index: 1; padding: 0; background: var(--dl-bg); }
.dl-header { display: flex; align-items: center; gap: 4px; height: 64px; padding: 0 12px 0 16px;
  border-bottom: 1px solid var(--dl-border); }
.dl-header .dl-logo { width: 22px; height: 22px; margin-right: 6px; }
.dl-title { flex: 1; font-size: 18px; font-weight: 600; }
.dl-icon-button { width: 32px; height: 32px; display: inline-flex; align-items: center; justify-content: center;
  flex: none; padding: 0; border: 0; border-radius: var(--dl-radius-lg); background: none; color: var(--dl-muted); cursor: pointer; }
.dl-icon-button:hover:not(:disabled) { background: var(--dl-hover); color: var(--dl-fg); }
.dl-status, .dl-field, .dl-queue-parameter, .dl-custom, .dl-submit { display: flex; flex-direction: column; gap: 6px; }
.dl-status { gap: 12px; }
.dl-slot-status .dl-input { height: 36px; font-size: 15px; }
.dl-label { color: var(--dl-muted); font-size: 12px; }
.dl-label[data-dl-tip] { align-self: flex-start; max-width: 100%; cursor: help;
  text-decoration: underline dotted color-mix(in srgb, currentColor 40%, transparent); text-underline-offset: 3px; }
.dl-field-check { flex-direction: row-reverse; justify-content: flex-end; align-items: center; gap: 8px; }
.dl-field-check input { width: 16px; height: 16px; margin: 0; flex: none; accent-color: var(--dl-primary); }
.dl-field-check .dl-label { color: var(--dl-fg); font-size: 14px; }
.dl-field-error { color: color-mix(in srgb, var(--dl-destructive) 70%, var(--dl-fg)); font-size: 12px; }
.dl-field-error:empty { display: none; }
.dl-input { width: 100%; min-width: 0; height: 32px; padding: 0 10px; border: 1px solid transparent;
  border-radius: var(--dl-radius-lg); background: var(--dl-surface); color: var(--dl-fg); font: inherit; outline: none; }
.dl-input:focus { border-color: var(--dl-border); }
.dl-input[aria-invalid="true"] { border-color: color-mix(in srgb, var(--dl-destructive) 70%, var(--dl-fg)); }
/* A choice Submit needs, such as an unset farm. */
select.dl-input.dl-needs { border-color: color-mix(in srgb, var(--dl-warning) 70%, var(--dl-fg)); }
/* A select's text lines up with an input's, and its placeholder is muted like one, but None isn't. */
select.dl-input { padding-left: 6px; }
select.dl-input:has(> option[data-dl-placeholder]:checked) { color: var(--dl-muted); }
select.dl-input option { color: var(--dl-fg); }
.dl-half { width: calc(50% - 5px); }
textarea.dl-input { height: auto; padding: 8px 10px; resize: vertical; }
.dl-grid2, .dl-range { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; align-items: start; }
.dl-meta { display: flex; align-items: center; gap: 6px; min-width: 0; color: var(--dl-muted); font-size: 12px; }
.dl-dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.dl-ok { background: var(--dl-success); } .dl-warn { background: var(--dl-warning); }
.dl-meta .dl-button { margin-left: 4px; }
.dl-notice { padding: 8px 10px; border-radius: var(--dl-radius-lg); background: var(--dl-surface);
  font-size: 12px; line-height: 1.45; }
.dl-error { background: color-mix(in srgb, var(--dl-destructive) 20%, transparent); }
.dl-warn-box { display: flex; align-items: flex-start; gap: 8px; }
.dl-warn-box > .dl-icon { margin-top: 1px; color: var(--dl-warning); }
.dl-confirm { flex-direction: column; }
.dl-message { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 200px; overflow-y: auto; }
.dl-section > summary { display: flex; align-items: center; gap: 8px; height: 36px; margin: 0 -8px; padding: 0 8px;
  border-radius: var(--dl-radius); cursor: pointer; list-style: none; }
.dl-section > summary::-webkit-details-marker { display: none; }
.dl-section > summary:hover { background: var(--dl-hover); }
.dl-chevron { color: var(--dl-muted); transition: transform 0.15s; }
.dl-section[open] > summary .dl-chevron { transform: rotate(90deg); }
.dl-section-body { display: flex; flex-direction: column; gap: 12px; padding: 4px 0 12px; }
/* A drawer's contents line up with its title, after the chevron, and so do the drawers inside it. */
.dl-panel .dl-section > .dl-section-body { padding-left: 24px; }
.dl-drawers { display: flex; flex-direction: column; }
.dl-section-body > .dl-link { align-self: flex-start; padding-left: 0; }
.dl-subheading { margin-top: 6px; color: var(--dl-fg); font-size: 13px; font-weight: 500; }
.dl-subheading[data-dl-tip] { align-self: flex-start; cursor: help;
  text-decoration: underline dotted color-mix(in srgb, currentColor 40%, transparent); text-underline-offset: 3px; }
.dl-choices { flex-wrap: wrap; gap: 4px 12px; }
.dl-choices input[type="checkbox"] { width: 14px; height: 14px; margin: 0; flex: none; accent-color: var(--dl-primary); }
.dl-button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: 32px; padding: 0 12px;
  border: 0; border-radius: var(--dl-radius-lg); background: var(--dl-surface); color: var(--dl-fg); font: inherit;
  white-space: nowrap; cursor: pointer; }
.dl-button:hover:not(:disabled) { background: var(--dl-hover); }
.dl-button:disabled, .dl-icon-button:disabled { opacity: 0.5; cursor: default; }
.dl-primary { background: var(--dl-primary); color: #fff; }
button.dl-action-button:hover, .dl-primary:hover:not(:disabled) {
  background: color-mix(in srgb, var(--dl-primary) 88%, #fff); }
.dl-ghost { padding: 0 8px; background: none; color: var(--dl-muted); }
.dl-ghost:hover { color: var(--dl-fg); }
.dl-small { height: 28px; padding: 0 10px; font-size: 13px; }
.dl-wide { width: 100%; height: 36px; }
.dl-link { padding: 0 2px; border: 0; background: none; color: var(--dl-muted); font: inherit; font-size: 12px;
  white-space: nowrap; cursor: pointer; }
.dl-link:hover { color: var(--dl-fg); }
.dl-queue-parameter .dl-link { align-self: flex-start; padding-left: 0; }
.dl-files { list-style: none; margin: 0; padding: 0; max-height: 240px; overflow-y: auto; }
.dl-files li { display: flex; align-items: center; gap: 8px; min-height: 30px; flex-shrink: 0; font-size: 13px; }
.dl-files li.dl-nested { padding-left: 24px; }
.dl-removed .dl-path { text-decoration: line-through; color: var(--dl-muted); }
.dl-path { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dl-folder-toggle { display: flex; align-items: center; gap: 4px; flex: 1; min-width: 0; padding: 0; color: inherit;
  font-size: 13px; text-align: left; }
.dl-job { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; padding: 6px 0; }
.dl-tile { width: 40px; height: 40px; flex: none; display: inline-flex; align-items: center; justify-content: center;
  border-radius: var(--dl-radius); background: var(--dl-surface); }
.dl-tile .dl-icon { width: 18px; height: 18px; }
.dl-success { color: var(--dl-success); }
.dl-job-text { flex: 1 1 90px; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.dl-job-name { display: flex; min-width: 0; }
.dl-job-note { margin: -4px 0 4px 52px; font-size: 13px; }
.dl-job-text .dl-muted { font-size: 13px; }
.dl-bar { display: block; height: 3px; border-radius: 2px; background: var(--dl-surface); overflow: hidden; }
.dl-bar span { display: block; height: 100%; background: var(--dl-primary); }
.dl-panel > .dl-slot-submit { position: sticky; bottom: 0; z-index: 1; margin-top: auto; padding: 12px 16px 16px;
  border-top: 1px solid var(--dl-border); background: var(--dl-bg); }
.dl-submit { gap: 10px; }
.dl-blocker { font-size: 12px; line-height: 1.45; }
.dl-popover { position: fixed; z-index: var(--z-index-overlay, 1700); width: 380px; max-width: calc(100vw - 16px);
  display: flex; flex-direction: column; gap: 10px; padding: 12px; border: 1px solid var(--dl-border);
  border-radius: var(--dl-radius-lg); background: var(--dl-bg); box-shadow: var(--shadow-lg, 0 2px 16px rgb(0 0 0 / 0.4)); }
.dl-slot-popover { display: flex; flex-direction: column; gap: 10px; }
.dl-popover .dl-primary { margin-left: auto; }
.dl-note { display: flex; gap: 8px; color: var(--dl-muted); font-size: 12px; line-height: 1.45; }
.dl-note .dl-icon { width: 14px; height: 14px; margin-top: 2px; color: var(--dl-warning); }
/* The popover's one row: the submission in progress, or the job it created. */
.dl-pop-status { margin: 2px -12px 0; padding: 12px 12px 0; border-top: 1px solid var(--dl-border); }
.dl-pop-status .dl-job { padding: 0; }
.dl-submitted { align-items: flex-start; font-size: 13px; line-height: 1.45; }
.dl-submitted > .dl-icon { margin-top: 2px; }
.dl-submitted .dl-grow { overflow-wrap: anywhere; }
.dl-submitted .dl-icon-button { width: 24px; height: 24px; }
.dl-nowrap { white-space: nowrap; }
.dl-tooltip { position: fixed; z-index: calc(var(--z-index-overlay, 1700) + 1); max-width: 320px; padding: 6px 8px;
  border: 1px solid var(--dl-border); border-radius: var(--dl-radius); background: var(--dl-bg); color: var(--dl-fg);
  box-shadow: var(--shadow-lg, 0 2px 16px rgb(0 0 0 / 0.4)); font: 12px / 1.4 var(--font-inter, Inter, sans-serif);
  pointer-events: none; }
`
    )
  );
}
