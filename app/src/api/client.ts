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
  private tokenProvider: (() => Promise<string | null>) | null = null;

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
