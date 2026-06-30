import os
from dotenv import load_dotenv
import requests

load_dotenv()

# Interface to make calls via API to agent
class AgentInterface:
    def __init__(self):
        # VARIABLES
        self.apiKey = os.getenv("API_KEY")
        self.clientId = os.getenv("CLIENT_ID")
        self.apiSecret = os.getenv("CLIENT_SECRET")
        self.total_cost = 0.0
        self.call_count = 0
        self.api_quota_prior = None
        self.api_quota_remaining = None

        # Access request to api/creation of headers
        resp = requests.post(
            "https://login.microsoftonline.com/4ccca3b5-71cd-4e6d-974b-4d9beb96c6d6/oauth2/v2.0/token",
            data={
                "grant_type":    "client_credentials",
                "client_id":     f"{self.clientId}",
                "client_secret": f"{self.apiSecret}",
                "scope":         "api://bbeee386-60d6-4ba4-b9a7-631763f66065/.default",
            }
        )

        resp.raise_for_status()
        token = resp.json()["access_token"]
        self.headers = {"Authorization": f"Bearer {token}",
                   "X-Api-Key": self.apiKey,
                   "Content-Type": "application/json"}


    # Prompt agent and get response
    def callAgent(self, prompt, model="claude-opus-4-8",max_tokens=1024,system=""):
        _json = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}]
        }
        if (system != ""):
            _json["system"] = system

        resp = requests.post(
            "https://aiapi.wustl.edu/models/v2/messages",
            headers=self.headers,
            json=_json
        )

        resp.raise_for_status()
        data = resp.json()
        print(resp.json())
        call_cost = data.get("apiCostThisCall", 0)
        self.total_cost += call_cost
        self.call_count += 1
        self.api_quota_prior = data.get("apiQuotaPriorToThisCall")
        self.api_quota_remaining = data.get("apiQuotaRemaining")
        print(f"{call_cost * 100:.2f} cents used on API call "
              f"(cumulative: ${self.total_cost:.4f}, "
              f"API quota remaining: ${self.api_quota_remaining})")
        return data

if __name__ == "__main__":
    agent = AgentInterface();
