"""Minimal Azure OpenAI smoke test.

Use this before the larger Azure examples when you want to verify only
the direct LLM call path via ``AzureOpenAILLM``.

What this checks:
- Azure OpenAI endpoint is correct
- API key works
- deployment name is valid
- the model can return a basic chat completion

Required environment variables:
- AZURE_OPENAI_API_KEY
- AZURE_OPENAI_ENDPOINT
- AZURE_OPENAI_DEPLOYMENT_NAME

Optional:
- AZURE_OPENAI_TEST_PROMPT
- INQTRIX_LOG_ENABLED
- INQTRIX_LOG_LEVEL
- INQTRIX_LOG_CONSOLE
- OBSERVABILITY_PROFILE (``forensic`` for structured lineage in the log file;
  requires ``INQTRIX_LOG_LEVEL=DEBUG``; see ``docs/observability/logging.md``)

Run with::

    uv run python examples/provider_stacks/azure_smoke_tests/test_llm.py


===============================================================================
ONE-TIME AZURE SETUP (only needed before the FIRST run of this script)
===============================================================================

1. Azure free trial (or existing Azure subscription)
   - Sign up at https://azure.microsoft.com/free for a $200 / 30-day trial.
   - Note your Subscription ID (Portal -> "Subscriptions").

2. Resource Group
   - Portal -> "Resource groups" -> + Create
   - Name: e.g. ``rg-inqtrix-test``
   - Region: ``Sweden Central`` or ``East US 2`` (currently best
     availability for gpt-4.1 / gpt-4o; pick ONE region for everything
     to keep latency low).

3. Azure OpenAI resource
   - Portal -> "Create a resource" -> search "Azure OpenAI" -> Create
   - Subscription / Resource group: as above
   - Region: same as above
   - Name: e.g. ``oai-inqtrix-test``
   - Pricing tier: ``Standard S0``
   - After deploy: open the resource -> "Keys and Endpoint"
     -> copy KEY 1 and the Endpoint (form
     ``https://oai-inqtrix-test.openai.azure.com/``).

4. Model deployment(s)
   - In the resource: "Model deployments" -> "Manage deployments"
     opens the Microsoft Foundry portal.
   - + Deploy model -> ``gpt-4.1`` -> Deployment name e.g. ``gpt-4.1``
     -> Deployment type ``Standard`` or ``Global Standard``
     -> Tokens-per-minute e.g. 30K -> Deploy.
   - (Optional, recommended) deploy a second cheaper model
     (e.g. ``gpt-4.1-mini`` or ``gpt-4o-mini``) as a claim-extraction
     deployment and set ``AZURE_OPENAI_CLAIM_EXTRACT_DEPLOYMENT_NAME`` in
     ``.env`` accordingly.

5. ``.env`` in the repo root
   - AZURE_OPENAI_ENDPOINT=https://oai-inqtrix-test.openai.azure.com/
   - AZURE_OPENAI_API_KEY=<KEY 1>
   - AZURE_OPENAI_DEPLOYMENT_NAME=gpt-4.1

6. Run this script -- a green answer + token-count panel means the LLM
   path is working.

Note:
- For the Foundry web search variant (``test_foundry_web_search.py``),
  you DO need extra Azure components on top of this; see that file's
  docstring.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

from inqtrix import AzureOpenAIAPIError, AzureOpenAILLM
from inqtrix.exceptions import AgentRateLimited, AgentTimeout
from inqtrix.logging_config import configure_logging


load_dotenv()

_log_path = configure_logging(
    enabled=os.getenv("INQTRIX_LOG_ENABLED", "").lower() == "true",
    level=os.getenv("INQTRIX_LOG_LEVEL", "INFO"),
    console=os.getenv("INQTRIX_LOG_CONSOLE", "").lower() == "true",
)
if _log_path:
    print(f"Logging to {_log_path}")

PROMPT = os.getenv(
    "AZURE_OPENAI_TEST_PROMPT",
    "Antworte auf Deutsch in genau einem Satz: Dieser Test prueft, ob Azure OpenAI korrekt aufgerufen wird.",
)


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def main() -> None:
    azure_endpoint = _require_env("AZURE_OPENAI_ENDPOINT")
    azure_deployment = _require_env("AZURE_OPENAI_DEPLOYMENT_NAME")
    azure_api_key = _require_env("AZURE_OPENAI_API_KEY")

    llm = AzureOpenAILLM(
        azure_endpoint=azure_endpoint,
        api_key=azure_api_key,
        default_model=azure_deployment,
    )

    response = llm.complete_with_metadata(PROMPT)

    console = Console()
    console.print(
        Panel(
            response.content or "<empty response>",
            title="Azure OpenAI response",
            expand=False,
        )
    )
    console.print(
        Panel(
            "\n".join(
                [
                    f"Deployment: {response.model or azure_deployment}",
                    f"Prompt tokens: {response.prompt_tokens}",
                    f"Completion tokens: {response.completion_tokens}",
                ]
            ),
            title="Metadata",
            expand=False,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, AgentTimeout, AgentRateLimited, AzureOpenAIAPIError) as exc:
        raise SystemExit(str(exc)) from exc
