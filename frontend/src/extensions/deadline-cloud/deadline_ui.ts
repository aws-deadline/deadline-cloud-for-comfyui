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
interface Plan {
  name: string;
  template: string;
  parameters: Parameter[];
  values?: Record<string, ParameterValue>;
  queueValues?: Record<string, string>;
  attachments?: PathLists;
  hostRequirements?: HostRequirements;
  // id is the model's folder and name, such as checkpoints/sd.safetensors. Two models can share a
  // name in different folders.
  localModels?: { id: string; name: string; directory: string | null; path: string }[];
  warnings?: string[];
}

interface Submission {
  id: string;
  starting: boolean;
  message?: string;
  name?: string;
  stage?: string;
  progress?: number;
  confirm?: { message: string; default?: boolean } | null;
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
  status?: "SUBMITTED" | "FAILED" | "CANCELED";
  jobId?: string;
  farmId?: string;
  queueId?: string;
  outputDir?: string;
  error?: string;
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
const PLAN_DEBOUNCE_MS = 1000;
const TOOLTIP_DELAY_MS = 200;
const TOOLTIP_GAP = 6;
const TOOLTIP_MARGIN = 8;
// The submitter fills these in when it creates the job bundle.
const AUTOMATIC_PARAMETERS = new Set(["WorkflowFile"]);
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
  // The chosen queue's environment parameters and storage profiles.
  queue: QueueDetails | null;
  cloudError: string | null;
  farmId: string;
  queueId: string;
  storageProfileId: string;
  plan: Plan | null;
  // The workflow the plan was made from, as workflowKey gives it.
  planKey: string | null;
  planError: string | null;
  planning: boolean;
  // The artist's values, which win over the ones the submitter fills in.
  edits: Record<string, ParameterValue>;
  queueEdits: Record<string, ParameterValue>;
  jobName: string | null;
  jobDescription: string;
  // The host requirements form, once the artist edits it. Until then the job template's apply.
  hostForm: HostForm | null;
  settings: Settings;
  removedAttachments: Set<string>;
  addedAttachments: PathLists;
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
  cloudError: null,
  farmId: "",
  queueId: "",
  storageProfileId: "",
  plan: null,
  planKey: null,
  planError: null,
  planning: false,
  edits: {},
  queueEdits: {},
  jobName: null,
  jobDescription: "",
  hostForm: null,
  settings: { ...DEFAULT_SETTINGS, ...readStorage<Settings>(SETTINGS_KEY, {}) },
  removedAttachments: new Set(),
  addedAttachments: { filenames: [], directories: [] },
  submission: null,
  jobs: readStorage<Job[]>(JOBS_KEY, []),
  jobStatus: {},
};

type SectionName =
  | "account"
  | "target"
  | "status"
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
  const name = app.extensionManager.workflow?.activeWorkflow?.filename || "ComfyUI workflow";
  return { workflow: output, ui_workflow: workflow, name };
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
      state.storageProfileId ||= account.storageProfileId ?? "";
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
  } catch (error) {
    state.cloudError = messageOf(error);
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
    state.queues = (await cloud<{ queues: Queue[] }>("queues", { farmId: state.farmId })).queues;
  } catch (error) {
    state.cloudError = messageOf(error);
  }
  refresh("target");
  if (state.queueId && state.queues?.some((queue) => queue.queueId === state.queueId)) {
    await loadQueue();
  }
}

async function loadQueue() {
  try {
    const queue = await cloud<QueueDetails>("queue", { farmId: state.farmId, queueId: state.queueId });
    state.queue = queue;
    if (!queue.storageProfiles.some((profile) => profile.storageProfileId === state.storageProfileId)) {
      state.storageProfileId = "";
    }
  } catch (error) {
    state.cloudError = messageOf(error);
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

// Reads the workflow again, unless it's unchanged. force reads it anyway.
export async function loadPlan(force = false) {
  let body: WorkflowBody;
  try {
    body = await currentWorkflow();
  } catch (error) {
    state.planError = `Couldn't convert the workflow to API format: ${messageOf(error)}`;
    refresh("status", "submit");
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
  }
  refresh();
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
// graph, so clear it before a submission can mix the two.
export function workflowLoaded() {
  clearTimeout(planTimer);
  requestedKey = null;
  graphKey = null;
  state.plan = null;
  state.planKey = null;
  state.planError = null;
  if (views.size) {
    refresh();
    planTimer = setTimeout(() => loadPlan());
  }
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

function attachments(): PathLists {
  const listed = state.plan?.attachments ?? { filenames: [], directories: [] };
  const keep = (paths: string[]) => paths.filter((path) => !state.removedAttachments.has(path));
  return {
    filenames: [...keep(listed.filenames), ...keep(state.addedAttachments.filenames)],
    directories: [...keep(listed.directories), ...keep(state.addedAttachments.directories)],
  };
}

function jobName() {
  return state.jobName ?? `ComfyUI - ${state.plan?.name ?? "workflow"}`;
}

export function canSubmit(): boolean {
  return (
    state.account?.authenticationStatus === "AUTHENTICATED" &&
    Boolean(state.farmId) &&
    Boolean(state.queueId) &&
    state.plan !== null &&
    !state.planning &&
    !state.planError &&
    !state.submission &&
    !hostFormError()
  );
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
  for (const parameter of state.queue?.parameters ?? []) {
    if (parameter.name in state.queueEdits || parameter.name in (plan?.queueValues ?? {})) {
      parameters[parameter.name] = queueParameterValue(parameter);
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
  };
}

// The current workflow, when the plan was made from it. The parameters and attachments come from
// the plan, so a workflow edited since then (while the plan is debounced or still loading) would
// be sent with another revision's files. Then the workflow is read again and this returns null.
async function plannedWorkflow(): Promise<WorkflowBody | null> {
  const body = await currentWorkflow();
  if (state.plan && workflowKey(body) === state.planKey) {
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
      storageProfileId: state.storageProfileId,
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
  submission.confirm = null;
  refresh("submit");
  try {
    await postJson("/deadline/jobs/confirm", { submissionId: submission.id, accept });
  } catch (error) {
    toast("error", messageOf(error));
  }
}

async function cancelSubmission() {
  if (!state.submission?.id || state.submission.starting) {
    return;
  }
  try {
    await postJson("/deadline/jobs/cancel", { submissionId: state.submission.id });
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
    submission.confirm = { message: detail.message ?? "", default: detail.default };
    refresh("submit");
    if (!views.size) {
      openDialog();
    }
  });
  api.addEventListener<SubmissionEvent>("deadline.submitted", ({ detail }) => {
    const ours = detail.submissionId === state.submission?.id;
    if (detail.status === "SUBMITTED") {
      toast("success", `Submitted ${detail.name} as ${detail.jobId}.`, 10000);
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

function section(title: string, open: boolean, ...children: Children[]) {
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
            async (value: string) => {
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
                async (value: string) => {
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
            (value: string) => {
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
  const children: Children[] = [];
  if (state.planError) {
    children.push(h("div", { className: "dl-notice dl-error" }, state.planError));
  }
  if (plan) {
    children.push(
      h(
        "div",
        { className: "dl-summary" },
        h("strong", null, plan.name),
        h("span", { className: "dl-muted" }, ` · ${plan.template}`)
      )
    );
    for (const warning of plan.warnings ?? []) {
      children.push(h("div", { className: "dl-notice dl-warn-box" }, warning));
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
      checked: String(value).toLowerCase() === "true",
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
  const groups = new Map<string, Parameter[]>();
  for (const parameter of plan.parameters) {
    const ui = parameter.userInterface ?? {};
    if (
      AUTOMATIC_PARAMETERS.has(parameter.name) ||
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
  return (state.plan?.localModels ?? [])
    .filter((model) => state.removedAttachments.has(model.path))
    .map((model) => model.id);
}

function attachmentItem(
  text: string,
  path: string,
  prefix: string | null,
  onRemove: () => void,
  onRestore: () => void
) {
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
      if (!rows.has(path)) {
        rows.set(path, { path, kind, manual, key: kind === "file" ? "filenames" : "directories" });
      }
    }
  }
  const localModels = state.plan.localModels ?? [];
  const attachedCount =
    [...rows.keys()].filter((path) => !state.removedAttachments.has(path)).length +
    localModels.length -
    excludedModels().length;
  const remove = (row: Row) => () => {
    if (row.manual) {
      added[row.key] = added[row.key].filter((path) => path !== row.path);
    } else {
      state.removedAttachments.add(row.path);
    }
    refresh("attachments");
  };
  const restore = (row: Row) => () => {
    state.removedAttachments.delete(row.path);
    refresh("attachments");
  };
  // Removing a model can make the job fail, so the status warns about it.
  const toggleModel = (model: { path: string }, removed: boolean) => () => {
    if (removed) {
      state.removedAttachments.add(model.path);
    } else {
      state.removedAttachments.delete(model.path);
    }
    refresh("status", "attachments");
  };
  // The dialog opens on the ComfyUI machine, whose files the job attaches.
  const add = (kind: string, key: keyof PathLists) => async () => {
    let paths: string[];
    try {
      ({ paths } = await postJson<{ paths: string[] }>("/deadline/choose", { kind }));
    } catch (error) {
      toast("error", messageOf(error), 8000);
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
          h("div", { className: "dl-label" }, "Models the workflow loads, attached automatically"),
          h(
            "ul",
            { className: "dl-files", "data-dl-scroll": "models" },
            localModels.map((model) =>
              attachmentItem(
                model.id,
                model.path,
                null,
                toggleModel(model, true),
                toggleModel(model, false)
              )
            )
          ),
        ]
      : null,
    rows.size
      ? h(
          "ul",
          { className: "dl-files", "data-dl-scroll": "inputs" },
          [...rows.values()].map((row) =>
            attachmentItem(
              row.path,
              row.path,
              row.kind === "folder" ? "▸ " : "",
              remove(row),
              restore(row)
            )
          )
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
  return h(
    "button",
    { className: "dl-button dl-primary dl-wide", disabled: !canSubmit(), onClick: submit },
    "Submit"
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
  const view = createView(root, [
    "account",
    "target",
    "status",
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
  return () => {
    views.delete(view);
  };
}

let dialog: { overlay: HTMLElement; view: View | null } | null = null;

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
  dialog.view = createView(body, ["account", "target", "status", "submit"]);
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
.dl-jobs li { border-bottom: 1px solid var(--dl-border); padding-bottom: 4px; }
.dl-destination { font-size: 12px; }
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
