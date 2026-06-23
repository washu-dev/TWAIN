import axios, { AxiosInstance } from 'axios';
import { API_CONFIG } from '@/constants/theme';

class APIClient {
  private client: AxiosInstance;

  constructor() {
    this.client = axios.create({
      baseURL: API_CONFIG.baseURL,
      timeout: API_CONFIG.timeout,
      headers: {
        'Content-Type': 'application/json',
      },
    });
  }

  async getGreetings() {
    try {
      const response = await this.client.get('/api/greetings');
      return response.data;
    } catch (error) {
      throw new Error(`Failed to fetch greetings: ${error instanceof Error ? error.message : 'Unknown error'}`);
    }
  }

  setBaseURL(url: string) {
    this.client.defaults.baseURL = url;
  }
}

export const apiClient = new APIClient();
