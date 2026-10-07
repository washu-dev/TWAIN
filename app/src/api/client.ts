import axios, { AxiosInstance } from 'axios';
import { API_CONFIG } from '@/constants/theme';

export type ConversationStatus =
  | 'running'
  | 'awaiting_input'
  | 'awaiting_approval'
  | 'cancelling'
  | 'completed'
  | 'error'
  | 'rejected'
  | 'cancelled';

export type MessageRole = 'user' | 'assistant' | 'system';
/**
 * One kind per gate that can ask the researcher something, so a gate's question
 * is recognised by identity rather than by matching its prompt text. Mirrors
 * runner/bridges.py and the messages_kind_check migration. 'clarification' is
 * still the CLARIFY question, and remains the kind on questions recorded before
 * the others existed.
 */
export type MessageKind =
  | 'chat'
  | 'clarification'
  | 'heavy_confirm'
  | 'validation_gate'
  | 'revision_request'
  | 'approval_request'
  | 'approval_response'
  | 'terminate';

export interface Message {
  id: number;
  role: MessageRole;
  content: string;
  kind: MessageKind;
  state: string | null;
  created_at: string;
}

/** One in-stage checklist step (a `stage.progress` run event; see runner job_activity.py). */
export interface StageProgress {
  stage: string;
  step: string;
  status: 'active' | 'done' | 'failed';
  label: string;
  detail?: Record<string, unknown>;
}

/** New stdout from the running Slurm job (a `job.log` run event). */
export interface JobLog {
  job_id: string;
  text: string;
  skipped_bytes?: number;
  truncated?: boolean;
}

/** Where a run stopped and why (the `failure` of a `run.error` event; runner error_handler.describe_failure). */
export interface RunFailure {
  stage: string;
  stage_label: string;
  /** One line, in plain words. */
  headline: string;
  cause?: string | null;
  /** The full error, tail-trimmed. */
  detail?: string;
  next_step?: string;
  category?: string;
  recoverable?: boolean;
  outcome?: string | null;
  job_id?: string | null;
  /** The tail of the Slurm job's stderr, where the job says why it stopped. */
  job_stderr?: string | null;
}

export type ActivityEvent =
  | { id: number; event_type: 'stage.progress'; created_at: string; payload: StageProgress }
  | { id: number; event_type: 'job.log'; created_at: string; payload: JobLog }
  | { id: number; event_type: 'run.error'; created_at: string;
      payload: { state: string; failure?: RunFailure } };

export interface ActivityPage {
  data: ActivityEvent[];
  /** Pass back as `after` to receive only newer events. */
  next_after: number;
}

export interface Conversation {
  id: string;
  title?: string;
  status: ConversationStatus;
  current_state: string;
  created_at: string;
  updated_at: string;
  /**
   * When the run's CURRENT activity began: the newest `run.started` event, which
   * the orchestrator publishes once per slice (start, resume, rerun). Null before
   * a run has ever been driven. Drives the live elapsed display -- neither
   * `created_at` (conversation opened) nor `updated_at` (rewritten on every
   * status change) means "running for this long".
   */
  started_at?: string | null;
  messages?: Message[];
}

/** One entry from the runner's capability snapshot. */
export interface LibraryInfo {
  /** 'library' (a Python package TWAIN can build on) or 'calculator' (an engine). */
  kind: 'library' | 'calculator';
  name: string;
  import_name?: string | null;
  version?: string | null;
  description?: string | null;
  /** Whether this deployment can actually run it right now. */
  installed: boolean;
  /** Which cluster env provides it, when installed. */
  env?: string | null;
  /** How it was found, or why it wasn't — shown so "no" is never unexplained. */
  detail?: string | null;
  /** The project's own site, from the registry. Null renders no link rather than
      a dead one. */
  homepage?: string | null;
  checked_at?: string | null;
}

export interface LibraryAvailability {
  libraries: LibraryInfo[];
  installed: number;
  total: number;
  checked_at?: string | null;
}

export interface ArtifactMeta {
  name: string;
  kind: string;
  size: number;
}

export interface ArtifactContent {
  name: string;
  kind: string;
  content: string;
}

// ── Reporting an issue from the run window ───────────────────────────────────
// Mirrors api/run_issue_github.CATEGORY_LABELS: each category triages under its
// own GitHub label. 'library' deliberately reuses the pipeline's LibraryAddition
// tag. Distinct from createIssue() below, which files a plain title+body issue.
export type IssueCategory = 'bug' | 'library' | 'result' | 'other';

// 'queued' means the report was saved against the run but no issue was filed —
// the deployment has no GitHub credentials. 'failed' means GitHub refused it.
export type RunIssueStatus = 'created' | 'queued' | 'failed';

export interface RunIssue {
  id: number;
  category: IssueCategory;
  title: string;
  description: string;
  status: RunIssueStatus;
  issue_number: number | null;
  issue_url: string | null;
  error: string | null;
  created_at: string;
}

/** The run snapshot that will be attached to an issue. Shape: api/run_issues.collect_run_context. */
export interface RunContext {
  run_id: string;
  title: string | null;
  status: string | null;
  current_state: string | null;
  selected_method: { libraries?: string[]; calculator?: string | null } | null;
  requested_property: string | null;
  safety_notes: string[];
  library_requests: { library?: string; status?: string; issue_url?: string | null }[];
  execution_result: Record<string, unknown> | string | null;
  errors: { event_type: string; payload: unknown; created_at: string }[];
  recent_messages: { role: string; kind: string; state: string | null; content: string }[];
  artifacts: ArtifactMeta[];
  truncated: boolean;
}

export interface IssueContext {
  run_context: RunContext;
  /** False when the deployment has no GitHub token — the form says so up front. */
  github_configured: boolean;
  repo: string | null;
  categories: IssueCategory[];
  submitted: RunIssue[];
}

// One run's budget snapshot (from the budget.json artifact the orchestrator
// writes each step). Costs are USD; iterations/wall-time are the run's rails.
export interface RunBudgetSnapshot {
  cost: number;
  max_cost: number;
  iterations: number;
  max_iterations: number;
  elapsed_seconds: number;
  wall_time_limit_seconds: number;
}

export interface BudgetArtifact {
  run?: RunBudgetSnapshot;
  global?: Record<string, unknown>;
}

export interface Report {
  conversation: Conversation;
  final_state: string;
  status: ConversationStatus;
  plan: Record<string, unknown> | string | null;
  execution_result: Record<string, unknown> | string | null;
  result: Record<string, unknown> | null;
  results_dir: string | null;
  /** Interpreted primary/secondary metrics (Epic 6), when the run produced any. */
  normalized_result: Record<string, unknown> | string | null;
  /** Cross-validation verdict + rationale (Epic 6), when validation ran. */
  validation: Record<string, unknown> | string | null;
  budget: BudgetArtifact | string | null;
  artifacts: ArtifactMeta[];
}

export interface CreatedIssue {
  number: number;
  url: string;
  repo: string;
}

// Per-user email notification preferences (users.notify_prefs). A missing kind
// means "send", so an empty object = all notifications on.
export interface NotifyPrefs {
  enabled: boolean;
  kinds: Record<string, boolean>;
}

// The notification kinds the runner emails about, in display order. Mirrors
// NOTIFY_KINDS in api/main.py / runner/notifications.py.
export const NOTIFY_KINDS = [
  'input',
  'approval',
  'completed',
  'failed',
  'terminated',
] as const;
export type NotifyKind = (typeof NOTIFY_KINDS)[number];

export interface AuthUser {
  id: string;
  email: string;
  name: string;
  role: 'user' | 'admin';
  notify_prefs?: Partial<NotifyPrefs> | null;
}

class APIClient {
  private client: AxiosInstance;
  private token: string | null = null;
  private tokenProvider: (() => Promise<string | null>) | null = null;
  private onUnauthorized: (() => void) | null = null;
  private refreshHandler: (() => Promise<string | null>) | null = null;

  constructor() {
    this.client = axios.create({
      baseURL: API_CONFIG.baseURL,
      timeout: API_CONFIG.timeout,
      headers: { 'Content-Type': 'application/json' },
    });
    // Attach the Entra bearer token to every request. `AuthProvider` registers a
    // token provider once MSAL has a signed-in account; the provider re-runs per
    // request so MSAL can refresh a token that has expired mid-session (long
    // Runner jobs). Falls back to a statically-set token, then to none (which the
    // API accepts only when AUTH_DISABLED is on).
    this.client.interceptors.request.use(async (config) => {
      const token = this.tokenProvider ? await this.tokenProvider() : this.token;
      if (token) {
        config.headers.Authorization = `Bearer ${token}`;
      }
      return config;
    });
    // On a 401, try a silent token refresh and retry the request once; only when
    // that fails (no refresh token, or Entra rejected it) drop the session so the
    // auth guard routes back to the login screen. Without the retry, the first
    // request after the access token expired logged the user out mid-session.
    this.client.interceptors.response.use(
      (response) => response,
      async (error) => {
        const original = error?.config as
          | (typeof error.config & { _retried?: boolean })
          | undefined;
        if (
          error?.response?.status === 401 &&
          original &&
          !original._retried &&
          this.refreshHandler
        ) {
          original._retried = true;
          let fresh: string | null = null;
          try {
            fresh = await this.refreshHandler();
          } catch {
            fresh = null;
          }
          if (fresh) {
            original.headers = original.headers ?? {};
            original.headers.Authorization = `Bearer ${fresh}`;
            return this.client.request(original);
          }
        }
        if (error?.response?.status === 401) {
          this.onUnauthorized?.();
        }
        return Promise.reject(error);
      },
    );
  }

  /**
   * Register a silent-refresh hook: returns a fresh bearer token to retry a
   * 401'd request with, or `null` when the session cannot be recovered.
   */
  setRefreshHandler(handler: (() => Promise<string | null>) | null) {
    this.refreshHandler = handler;
  }

  /**
   * Register a callback that yields a fresh bearer token per request (preferred),
   * or `null` to clear it. Takes precedence over {@link setAuthToken}.
   */
  setTokenProvider(provider: (() => Promise<string | null>) | null) {
    this.tokenProvider = provider;
  }

  setAuthToken(token: string | null) {
    this.token = token;
  }

  setUnauthorizedHandler(handler: (() => void) | null) {
    this.onUnauthorized = handler;
  }

  setBaseURL(url: string) {
    this.client.defaults.baseURL = url;
  }

  async health(): Promise<{ status: string }> {
    const response = await this.client.get('/api/health');
    return response.data;
  }

  /**
   * What TWAIN knows about, and which of it this cluster can actually run.
   *
   * Served from the snapshot the runner publishes -- the API cannot probe the
   * cluster envs itself. `checked_at` is how fresh that probe is, which matters:
   * a runner that has not restarted since a provision run reports the old answer.
   */
  async listLibraries(): Promise<LibraryAvailability> {
    const response = await this.client.get('/api/libraries');
    return response.data.data;
  }

  // ── Auth ────────────────────────────────────────────────────────────────────
  // The Entra access token is attached by the request interceptor; this returns
  // the authenticated user the API resolved from it (identity + role). Doubles as
  // the token-validity check on app start.
  async me(): Promise<AuthUser> {
    const response = await this.client.get('/api/me');
    return response.data.data;
  }

  // Replace the caller's email notification preferences (Settings page).
  async updateNotifyPrefs(prefs: NotifyPrefs): Promise<NotifyPrefs> {
    const response = await this.client.put('/api/me/notifications', prefs);
    return response.data.data;
  }

  // ── Conversations / chat (Phase 1) ─────────────────────────────────────────
  async startConversation(request: string, maxCost?: number | null): Promise<Conversation> {
    const body: { request: string; max_cost?: number } = { request };
    if (maxCost != null) body.max_cost = maxCost;
    const response = await this.client.post('/api/conversations', body);
    return response.data.data;
  }

  async listConversations(): Promise<Conversation[]> {
    const response = await this.client.get('/api/conversations');
    return response.data.data;
  }

  // Live activity (checklist steps + job log) newer than event `after`.
  async getActivity(id: string, after: number): Promise<ActivityPage> {
    const response = await this.client.get(`/api/conversations/${id}/activity`, {
      params: { after },
    });
    return response.data;
  }

  // The API's own release version and commit (#173); needs no credentials.
  async getVersion(): Promise<{ service: string; version: string; commit: string }> {
    const response = await this.client.get('/api/version');
    return response.data;
  }

  async getConversation(id: string): Promise<Conversation> {
    const response = await this.client.get(`/api/conversations/${id}`);
    return response.data.data;
  }

  async deleteConversation(id: string): Promise<void> {
    await this.client.delete(`/api/conversations/${id}`);
  }

  async sendMessage(id: string, content: string): Promise<Message> {
    const response = await this.client.post(`/api/conversations/${id}/messages`, {
      content,
    });
    return response.data.data;
  }

  async sendApproval(
    id: string,
    decision: 'approve' | 'reject',
    slurmRequest?: {
      cpu_count?: number;
      gpu_count?: number;
      ram?: number;
      max_time?: number;
    },
    /** The bar the result is judged against. Null target/tolerance means "no bar",
        which is a meaningful choice and is preserved as null rather than 0. */
    acceptanceMetrics?: {
      metric_name: string;
      target_value: number | null;
      tolerance: number | null;
    }[],
  ): Promise<Message> {
    const response = await this.client.post(`/api/conversations/${id}/approval`, {
      decision,
      ...(slurmRequest ? { slurm_request: slurmRequest } : {}),
      ...(acceptanceMetrics ? { acceptance_metrics: acceptanceMetrics } : {}),
    });
    return response.data.data;
  }

  async terminateConversation(id: string): Promise<Message> {
    const response = await this.client.post(`/api/conversations/${id}/terminate`);
    return response.data.data;
  }

  // Re-run a finished conversation from an earlier pipeline stage. Resets that
  // stage and everything after it; returns the conversation back in `running`.
  // With `feedback` (the mid-session revision path), the message is folded into
  // the run's intent before re-planning, so the new plan reflects it.
  /**
   * Re-run from an earlier stage. `request` replaces the opening prompt and is
   * only accepted with state 'INTAKE' — the one stage that re-reads it.
   */
  /**
   * Re-run from an earlier stage, optionally saying what should be different.
   *
   * `feedback` is accepted for any stage (the runner folds it into the intent, so
   * discovery/plan/codegen all see it). `request` replaces the opening prompt and
   * is INTAKE-only; `slurmRequest` re-runs the same plan with different resources
   * and is only accepted after PLAN — the API rejects either in the wrong place
   * rather than accepting an edit that would silently do nothing.
   */
  async rerunConversation(
    id: string,
    state: string,
    feedback?: string,
    request?: string,
    slurmRequest?: { cpu_count?: number; gpu_count?: number; ram?: number; max_time?: number },
  ): Promise<Conversation> {
    const response = await this.client.post(`/api/conversations/${id}/rerun`, {
      state,
      ...(feedback ? { feedback } : {}),
      ...(request ? { request } : {}),
      ...(slurmRequest ? { slurm_request: slurmRequest } : {}),
    });
    return response.data.data;
  }

  async getReport(id: string): Promise<Report> {
    const response = await this.client.get(`/api/conversations/${id}/report`);
    return response.data.data;
  }

  // `name` may contain a slash (e.g. run_bundle/main.py) — sent unencoded to
  // match the API's {name:path} route.
  async getArtifact(id: string, name: string): Promise<ArtifactContent> {
    const response = await this.client.get(`/api/conversations/${id}/artifacts/${name}`);
    return response.data.data;
  }

  // ── Reporting an issue from the run window ─────────────────────────────────
  /** What would be attached to an issue for this run — shown before submitting. */
  async getIssueContext(id: string): Promise<IssueContext> {
    const response = await this.client.get(`/api/conversations/${id}/issue-context`);
    return response.data.data;
  }

  /** File a GitHub issue about this run; the run's data is attached server-side. */
  async submitRunIssue(
    id: string,
    issue: { category: IssueCategory; title: string; description: string },
  ): Promise<RunIssue> {
    const response = await this.client.post(`/api/conversations/${id}/issues`, issue);
    return response.data.data;
  }

  async listRunIssues(id: string): Promise<RunIssue[]> {
    const response = await this.client.get(`/api/conversations/${id}/issues`);
    return response.data.data;
  }

  // ── GitHub issues ──────────────────────────────────────────────────────────
  // Opens an issue on the TWAIN repo. The submitter's email is added server-side
  // from the validated token, so it is not sent from here.
  async createIssue(title: string, body: string): Promise<CreatedIssue> {
    const response = await this.client.post('/api/issues', { title, body });
    return response.data.data;
  }

  // Absolute URL for the SSE progress stream. `EventSource` can't set an
  // Authorization header, so under auth this connection is rejected and
  // `useConversationStream` falls back to interval polling (which does carry the
  // bearer token via the axios interceptor), so the view still converges.
  streamUrl(id: string): string {
    const base = this.client.defaults.baseURL ?? '';
    return `${base}/api/conversations/${id}/stream`;
  }
}

export const apiClient = new APIClient();
