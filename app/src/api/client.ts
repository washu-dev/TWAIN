import axios, { AxiosInstance } from 'axios';
import { API_CONFIG } from '@/constants/theme';

export type ConversationStatus =
  | 'running'
  | 'awaiting_input'
  | 'awaiting_approval'
  | 'completed'
  | 'error'
  | 'rejected';

export type MessageRole = 'user' | 'assistant' | 'system';
export type MessageKind =
  | 'chat'
  | 'clarification'
  | 'approval_request'
  | 'approval_response';

export interface Message {
  id: number;
  role: MessageRole;
  content: string;
  kind: MessageKind;
  state: string | null;
  created_at: string;
}

export interface Conversation {
  id: string;
  title?: string;
  status: ConversationStatus;
  current_state: string;
  created_at: string;
  updated_at: string;
  messages?: Message[];
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
// Mirrors api/github_issues.CATEGORY_LABELS: each category triages under its own
// GitHub label. 'library' deliberately reuses the pipeline's LibraryAddition tag.
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

export interface Report {
  conversation: Conversation;
  final_state: string;
  status: ConversationStatus;
  plan: Record<string, unknown> | string | null;
  execution_result: Record<string, unknown> | string | null;
  budget: Record<string, unknown> | string | null;
  artifacts: ArtifactMeta[];
}

class APIClient {
  private client: AxiosInstance;
  private token: string | null = null;

  constructor() {
    this.client = axios.create({
      baseURL: API_CONFIG.baseURL,
      timeout: API_CONFIG.timeout,
      headers: { 'Content-Type': 'application/json' },
    });
    // Attach the bearer token when one is set (Entra sign-in lands in the
    // Phase 0 frontend; until then the API runs with AUTH_DISABLED in dev).
    this.client.interceptors.request.use((config) => {
      if (this.token) {
        config.headers.Authorization = `Bearer ${this.token}`;
      }
      return config;
    });
  }

  setAuthToken(token: string | null) {
    this.token = token;
  }

  setBaseURL(url: string) {
    this.client.defaults.baseURL = url;
  }

  async getGreetings() {
    const response = await this.client.get('/api/greetings');
    return response.data;
  }

  // ── Conversations / chat (Phase 1) ─────────────────────────────────────────
  async startConversation(request: string): Promise<Conversation> {
    const response = await this.client.post('/api/conversations', { request });
    return response.data.data;
  }

  async listConversations(): Promise<Conversation[]> {
    const response = await this.client.get('/api/conversations');
    return response.data.data;
  }

  async getConversation(id: string): Promise<Conversation> {
    const response = await this.client.get(`/api/conversations/${id}`);
    return response.data.data;
  }

  async sendMessage(id: string, content: string): Promise<Message> {
    const response = await this.client.post(`/api/conversations/${id}/messages`, {
      content,
    });
    return response.data.data;
  }

  async sendApproval(id: string, decision: 'approve' | 'reject'): Promise<Message> {
    const response = await this.client.post(`/api/conversations/${id}/approval`, {
      decision,
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
}

export const apiClient = new APIClient();
