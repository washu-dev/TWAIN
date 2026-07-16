"""LLM gateway client for the agent-mesh control plane.

:class:`AgentInterface` is the single place the pipeline talks to the hosted
LLM. It fetches an OAuth2 client-credentials token, then exposes
:meth:`call_agent` as the one call site every stage uses to prompt the model and
get a response (plus running cost/quota bookkeeping).

All deployment-specific values (OAuth tenant, scope, gateway endpoint, default
model, and token cap) are read from the environment with sensible fallbacks, so
the same code runs against a different gateway or model without edits. Secrets
(``API_KEY`` / ``CLIENT_ID`` / ``CLIENT_SECRET``) are already env-driven; this
just extends that pattern to the rest of the connection details.
"""
import logging
import os
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Deployment config -- overridable via env, defaulting to the current gateway.
DEFAULT_TENANT_ID = "4ccca3b5-71cd-4e6d-974b-4d9beb96c6d6"
DEFAULT_SCOPE = "api://bbeee386-60d6-4ba4-b9a7-631763f66065/.default"
DEFAULT_ENDPOINT = "https://aiapi.wustl.edu/models/v2/messages"
DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_MAX_TOKENS = 1024


class AgentInterface:
    """Authenticated client for the hosted LLM gateway.

    Reads credentials and connection details from the environment on
    construction and acquires a bearer token immediately, so a constructed
    instance is ready to :meth:`call_agent`.
    """

    def __init__(self) -> None:
        self.api_key: Optional[str] = os.getenv("API_KEY")
        self.client_id: Optional[str] = os.getenv("CLIENT_ID")
        self.client_secret: Optional[str] = os.getenv("CLIENT_SECRET")

        self.tenant_id: str = os.getenv("TWAIN_AGENT_TENANT_ID", DEFAULT_TENANT_ID)
        self.scope: str = os.getenv("TWAIN_AGENT_SCOPE", DEFAULT_SCOPE)
        self.endpoint: str = os.getenv("TWAIN_AGENT_ENDPOINT", DEFAULT_ENDPOINT)
        self.default_model: str = os.getenv("TWAIN_AGENT_MODEL", DEFAULT_MODEL)
        self.default_max_tokens: int = int(
            os.getenv("TWAIN_AGENT_MAX_TOKENS", DEFAULT_MAX_TOKENS)
        )

        self.total_cost = 0.0
        self.call_count = 0
        self.api_quota_prior = None
        self.api_quota_remaining = None

        token_url = (
            f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"
        )
        resp = requests.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": self.scope,
            },
        )
        resp.raise_for_status()
        token = resp.json()["access_token"]
        self.headers = {
            "Authorization": f"Bearer {token}",
            "X-Api-Key": self.api_key,
            "Content-Type": "application/json",
        }

    def call_agent(
        self,
        prompt: str,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        system: str = "",
    ) -> Dict[str, Any]:
        """Prompt the model and return the parsed gateway response.

        ``model``/``max_tokens`` fall back to the env-configured defaults. Cost
        and remaining API quota reported by the gateway are accumulated onto the
        instance for budget tracking.
        """
        payload: Dict[str, Any] = {
            "model": model or self.default_model,
            "max_tokens": max_tokens or self.default_max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system

        resp = requests.post(self.endpoint, headers=self.headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        logger.debug("agent gateway response: %s", data)

        call_cost = data.get("apiCostThisCall", 0)
        self.total_cost += call_cost
        self.call_count += 1
        self.api_quota_prior = data.get("apiQuotaPriorToThisCall")
        self.api_quota_remaining = data.get("apiQuotaRemaining")
        logger.info(
            "LLM call cost %.2f cents (cumulative $%.4f, quota remaining $%s)",
            call_cost * 100,
            self.total_cost,
            self.api_quota_remaining,
        )
        return data
