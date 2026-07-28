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
  budget: BudgetArtifact | string | null;
  artifacts: ArtifactMeta[];
}

export interface AuthUser {
  id: string;
  email: string;
  name: string;
  role: 'user' | 'admin';
}

class APIClient {
  private client: AxiosInstance;
  private token: string | null = null;
  private tokenProvider: (() => Promise<string | null>) | null = null;
  private onUnauthorized: (() => void) | null = null;

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
    // Drop the session on any 401 so the auth guard routes back to the login screen.
    this.client.interceptors.response.use(
      (response) => response,
      (error) => {
        if (error?.response?.status === 401) {
          this.onUnauthorized?.();
        }
        return Promise.reject(error);
      },
    );
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

  // ── Auth ────────────────────────────────────────────────────────────────────
  // The Entra access token is attached by the request interceptor; this returns
  // the authenticated user the API resolved from it (identity + role). Doubles as
  // the token-validity check on app start.
  async me(): Promise<AuthUser> {
    const response = await this.client.get('/api/me');
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

  async sendApproval(id: string, decision: 'approve' | 'reject'): Promise<Message> {
    const response = await this.client.post(`/api/conversations/${id}/approval`, {
      decision,
    });
    return response.data.data;
  }

  // Re-run a finished conversation from an earlier pipeline stage. Resets that
  // stage and everything after it; returns the conversation back in `running`.
  async rerunConversation(id: string, state: string): Promise<Conversation> {
    const response = await this.client.post(`/api/conversations/${id}/rerun`, { state });
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
