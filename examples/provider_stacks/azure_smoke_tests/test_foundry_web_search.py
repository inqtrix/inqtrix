"""Minimal Azure Foundry Web Search smoke test.

Use this when you want to verify the search path via
``AzureFoundryWebSearch`` -- the Azure Foundry Agent Service path that
invokes a pre-created Foundry agent equipped with the Web Search tool.

For the full stack (``AzureOpenAILLM`` + ``AzureFoundryWebSearch``), see:

- ``examples/provider_stacks/azure_foundry_web_search.py``

===============================================================================
WHAT THIS SCRIPT CREATES IN AZURE: NOTHING per call.
===============================================================================
Every ``search()`` call is a single stateless POST to::

    POST <project_endpoint>/openai/v1/responses
    {
        "model": "<deployment from agent>",
        "input": "<the query>",
        "extra_body": {"agent_reference": {"type": "agent_reference",
                                            "name":  "<WEB_SEARCH_AGENT_NAME>",
                                            "version": "<optional>"}}
    }

There are NO threads, NO messages, NO runs that persist between calls
-- the provider deliberately uses the stateless Responses path, not
the Agent-Service Thread/Message/Run pipeline.

The ONLY persistent objects in Azure are:
- the Foundry project        (created during setup, stays)
- the model deployment       (created during setup, stays)
- the Foundry agent          (created during setup, stays; can be
                              versioned / edited in the Foundry portal)

Per-call side effects are limited to:
- one entry in your Azure cost analysis (token costs are higher than
  a normal LLM call because Azure injects the fetched page contents
  into the prompt -- ~20-30K prompt tokens is normal)
- the provider-managed web-search metering for the request

===============================================================================
ONE-TIME AZURE SETUP for variant B (Foundry Web Search)
===============================================================================

Prerequisite: the LLM smoke test (``test_llm.py``) has already been
run and works.  Steps 1-3 below extend that setup with a Foundry
project + agent. No separate search resource is needed.

1. Azure AI Foundry project
   - Portal -> "Create a resource" -> search "Azure AI Foundry"
     -> + Create -> "AI Foundry" (the new hub-less mode; classic
     "Hub + Project" also works).
   - Subscription / Resource group / Region: same as the OpenAI
     resource from ``test_llm.py``.
   - Name: e.g. ``aif-inqtrix-test``.
   - After deploy: open the resource in the portal -> "Launch
     Microsoft Foundry" -> opens https://ai.azure.com/.
   - In the Foundry portal: left nav "Projects" -> + New project
     -> name e.g. ``proj-inqtrix-test``.

2. Endpoint & key
   - Foundry project -> "Project Settings" (or "Overview")
     -> Endpoint, form
     ``https://aif-inqtrix-test.services.ai.azure.com/api/projects/proj-inqtrix-test``.
   - "Keys & Endpoint" -> copy API key (simplest auth path).

3. Model deployment IN the Foundry project
   - Foundry project -> "My assets" -> "Models + endpoints"
     -> + Deploy model -> ``gpt-4.1`` (or ``gpt-4o``)
     -> deployment name e.g. ``gpt-4.1``.
   - You can REUSE the same deployment from ``test_llm.py`` if it
     was created in this Foundry project; otherwise create a new
     one here.

4. Web Search agent
   - Foundry project -> "Agents" -> + New agent.
   - Name: e.g. ``web-search-agent``.
   - Model: the deployment from step 3.
   - Instructions: keep MINIMAL, e.g. "You are a research assistant.
     Answer with citations from the web."
     (Long agent instructions can interfere with Inqtrix's own
     prompts -- keep them short and neutral.)
   - Tools -> + Add tool -> "Web Search".
   - Save.  Note the Agent Name (and optionally Version).

5. ``.env`` -- add on top of what test_llm.py already needs:
   - AZURE_AI_PROJECT_ENDPOINT=https://aif-inqtrix-test.services.ai.azure.com/api/projects/proj-inqtrix-test
   - AZURE_AI_PROJECT_API_KEY=<Foundry project key from step 2>
   - WEB_SEARCH_AGENT_NAME=web-search-agent
   - WEB_SEARCH_AGENT_VERSION=             # optional, leave empty for latest

6. Run this script -- a green answer + non-empty Citations panel +
   prompt-token count in the 10-30K range proves that the agent
   actually used its Web Search tool.

===============================================================================
WHAT THIS SCRIPT CHECKS
===============================================================================
- Azure AI Foundry project endpoint is correct
- Authentication works (API key, Service Principal, or
  DefaultAzureCredential -- whichever is provided)
- the configured Web Search agent is reachable
- the agent actually invokes its Web Search tool (indirectly visible
  in prompt-token count 10-30K, vs ~50 without grounding)
- a single search call returns an answer and citations

===============================================================================
ENVIRONMENT VARIABLES
===============================================================================

Required:
- AZURE_AI_PROJECT_ENDPOINT
- WEB_SEARCH_AGENT_NAME

Optional:
- WEB_SEARCH_AGENT_VERSION
- AZURE_WEB_SEARCH_TEST_QUERY
- INQTRIX_LOG_ENABLED
- INQTRIX_LOG_LEVEL
- INQTRIX_LOG_CONSOLE
- OBSERVABILITY_PROFILE (``forensic`` for structured lineage in the log file;
  requires ``INQTRIX_LOG_LEVEL=DEBUG``; see ``docs/observability/logging.md``)

Authentication -- three options (pick ONE):
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Option 1 -- API Key (simplest)**
    Set in .env::

        AZURE_AI_PROJECT_API_KEY=abc123...

    Found in Azure Portal -> your Foundry project -> Project Settings
    -> Keys & Endpoint.

**Option 2 -- Service Principal**
    Set in .env::

        AZURE_TENANT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
        AZURE_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
        AZURE_CLIENT_SECRET=your-secret-value

    Created via Azure Portal -> App Registrations -> New Registration
    -> Certificates & Secrets.

**Option 3 -- DefaultAzureCredential (fallback)**
    Requires NO extra env vars.  Uses the first available credential:
    ``az login`` (Azure CLI), Managed Identity, VS Code Azure sign-in, etc.
    Install Azure CLI: ``brew install azure-cli && az login``

Notes:
- ``recency_filter``, ``language_filter``, and ``domain_filter`` are
  runtime HINTS only -- the Foundry agent's own tool config governs
  the actual search behaviour.
- If Azure returns an error but the provider falls back to an empty
  result, this script prints the provider's nonfatal notice so the
  root cause stays visible.
- Some prompts can trigger Azure content filtering or false positives.
  If that happens, override ``AZURE_WEB_SEARCH_TEST_QUERY`` with a
  simpler, current-events style query.

Run with::

    uv run python examples/provider_stacks/azure_smoke_tests/test_foundry_web_search.py
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

from inqtrix import AzureFoundryWebSearch, AzureFoundryWebSearchAPIError
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

QUERY = os.getenv(
    "AZURE_WEB_SEARCH_TEST_QUERY",
    "Was sind die heutigen Schlagzeilen bei Tagesschau und ZDFheute?",
)


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def main() -> None:
    project_endpoint = _require_env("AZURE_AI_PROJECT_ENDPOINT")
    agent_name = _require_env("WEB_SEARCH_AGENT_NAME")
    agent_version = os.getenv("WEB_SEARCH_AGENT_VERSION", "")

    # ── Auth: pick the first available credential ────────────────────
    api_key = os.getenv("AZURE_AI_PROJECT_API_KEY")
    tenant_id = os.getenv("AZURE_TENANT_ID")
    client_id = os.getenv("AZURE_CLIENT_ID")
    client_secret = os.getenv("AZURE_CLIENT_SECRET")

    # Option 1 — API Key (if AZURE_AI_PROJECT_API_KEY is set in .env)
    # → Simplest. No azure-identity or az login needed.
    search = AzureFoundryWebSearch(
        project_endpoint=project_endpoint,
        agent_name=agent_name,
        agent_version=agent_version,
        api_key=api_key,                # None → falls through to Entra ID
        tenant_id=tenant_id,            # None → ignored when api_key is set
        client_id=client_id,            # None → ignored when api_key is set
        client_secret=client_secret,    # None → ignored when api_key is set
    )
    # The constructor resolves auth automatically in this priority:
    #   1. api_key set          → uses static API key
    #   2. tenant/client/secret → uses Service Principal (ClientSecretCredential)
    #   3. none of the above    → uses DefaultAzureCredential (az login etc.)

    # --- Alternative: hardcoded API key (uncomment to test) ---
    # search = AzureFoundryWebSearch(
    #     project_endpoint=project_endpoint,
    #     agent_name=agent_name,
    #     agent_version=agent_version,
    #     api_key="your-api-key-from-azure-portal",
    # )

    # --- Alternative: hardcoded Service Principal (uncomment to test) ---
    # search = AzureFoundryWebSearch(
    #     project_endpoint=project_endpoint,
    #     agent_name=agent_name,
    #     agent_version=agent_version,
    #     tenant_id="your-tenant-id",
    #     client_id="your-client-id",
    #     client_secret="your-client-secret",
    # )

    # --- Alternative: DefaultAzureCredential only (uncomment to test) ---
    # Requires: brew install azure-cli && az login
    # search = AzureFoundryWebSearch(
    #     project_endpoint=project_endpoint,
    #     agent_name=agent_name,
    #     agent_version=agent_version,
    # )

    result = search.search(
        QUERY,
        recency_filter="month",
        language_filter=["de"],
    )

    nonfatal_notice = search.consume_nonfatal_notice()
    citations = result.citation_urls
    citations_text = "\n".join(f"- {url}" for url in citations) or "<no citations returned>"

    console = Console()
    if nonfatal_notice:
        console.print(
            Panel(
                nonfatal_notice,
                title="Provider notice",
                expand=False,
            )
        )
        if "content_filter" in nonfatal_notice:
            console.print(
                Panel(
                    "Azure hat den Prompt per Content Filter blockiert. "
                    "Das ist kein Auth- oder Endpoint-Fehler. "
                    "Setze fuer einen Smoke-Test z. B. "
                    "AZURE_WEB_SEARCH_TEST_QUERY='Tell me what you can help with.' "
                    "oder eine einfache aktuelle Nachrichtenfrage. "
                    "Wenn selbst harmlose Queries blockiert werden, pruefe auch die Agent-"
                    "Instruktionen im Foundry-Portal.",
                    title="Hint",
                    expand=False,
                )
            )
    console.print(
        Panel(
            result.answer or "<empty answer>",
            title="Azure Foundry Web Search answer",
            expand=False,
        )
    )
    console.print(
        Panel(
            citations_text,
            title=f"Citations ({len(citations)})",
            expand=False,
        )
    )
    console.print(
        Panel(
            "\n".join(
                [
                    f"Agent: {agent_name}" + (f" v{agent_version}" if agent_version else ""),
                    f"Prompt tokens: {result.prompt_tokens}",
                    f"Completion tokens: {result.completion_tokens}",
                ]
            ),
            title="Metadata",
            expand=False,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (
        RuntimeError,
        ValueError,
        AgentTimeout,
        AgentRateLimited,
        AzureFoundryWebSearchAPIError,
    ) as exc:
        raise SystemExit(str(exc)) from exc
