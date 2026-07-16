import axios, { AxiosInstance } from 'axios';
import { API_CONFIG } from '@/constants/theme';

export type ConversationStatus =
  | 'running'
  | 'awaiting_input'
  | 'awaiting_approval'
  | 'completed'
  | 'error'
  | 'rejected';

// Where the run's EXECUTE stage happens: the runner host itself, or a job
// submitted to the WashU RIS Slurm cluster. Omitted = the runner's default.
export type ComputeTarget = 'local' | 'slurm';

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
  async startConversation(
    request: string,
    computeTarget?: ComputeTarget,
  ): Promise<Conversation> {
    const response = await this.client.post('/api/conversations', {
      request,
      ...(computeTarget ? { compute_target: computeTarget } : {}),
    });
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

  async sendApproval(
    id: string,
    decision: 'approve' | 'reject',
    slurmRequest?: {
      cpu_count?: number;
      gpu_count?: number;
      ram?: number;
      max_time?: number;
    },
  ): Promise<Message> {
    const response = await this.client.post(`/api/conversations/${id}/approval`, {
      decision,
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
}

export const apiClient = new APIClient();
