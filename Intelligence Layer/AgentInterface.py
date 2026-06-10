import os
from dotenv import load_dotenv
import requests

load_dotenv()

class AgentInterface:
    def __init__(self):
        # VARIABLES
        self.apiKey = os.getenv("API_KEY")
        self.clientId = os.getenv("CLIENT_ID")
        self.apiSecret = os.getenv("CLIENT_SECRET")

        # Set up headers
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

    # Call agent (test for now, will be replaced with custom message in callagent)
    def CallAgent(self, jsonQuery):

        # Call to agent
        resp = requests.post(
            "https://aiapi.wustl.edu/models/v2/messages",
            headers=self.headers,
            json=jsonQuery
        )

        # {
        #     "model": "claude-sonnet-4-6",
        #     "max_tokens": 1024,
        #     "messages": [{"role": "user", "content": query}]
        # }

        resp.raise_for_status()
        return resp.json()

if __name__ == "__main__":
    agent = AgentInterface()
