# Examples

## Scope

Runnable examples for the three main ways to use Inqtrix: quickstart scripts, explicit provider stacks, and HTTP server stacks. These scripts are intentionally small entry points; provider credentials still come from your environment or a local `.env` file.

## Python installation and command parity

Choose either installation path once from the repository root:

```bash
# uv
uv sync --extra dev

# or standard Python/pip
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

The examples below show both launch forms. `uv run python path/to/script.py`
and plain `python path/to/script.py` execute the same script; the latter uses
the activated pip-installed environment.

## Observability (logs + forensic)

The `provider_stacks/` and `custom_providers/` scripts keep the terminal for their own output: they log only when `INQTRIX_LOG_TARGET` is set (`console`, `file` for `logs/inqtrix-script.log`, or `both`). The `webserver_stacks/` servers log like `python -m inqtrix`: `INFO` as JSON on stdout by default. `INQTRIX_LOG_LEVEL` and `INQTRIX_LOG_FORMAT` (`json` or `text`) work for both.

Your own log format and logging stack:

| Example | Shows |
|---------|-------|
| [`logging/organization_format.yaml`](logging/organization_format.yaml) | A `dictConfig` file for `INQTRIX_LOG_CONFIG` that replaces the built-in setup with your own format. |
| [`logging/stdlib_json_lines.py`](logging/stdlib_json_lines.py) | An embedding application with its own handler and JSON formatter and `InqtrixLogFilter()` on the handler. |
| [`logging/opentelemetry_logs.py`](logging/opentelemetry_logs.py) | `InqtrixLogFilter()` on the OpenTelemetry `LoggingHandler`, exported to the console. |
| [`logging/structlog_processor_formatter.py`](logging/structlog_processor_formatter.py) | structlog renders the standard records through `ProcessorFormatter` on a filtered handler (needs structlog). |
| [`logging/loguru_intercept.py`](logging/loguru_intercept.py) | loguru receives the standard records through a filtered intercept handler (needs loguru). |

The standard-library script runs as it is; the OpenTelemetry script needs the `observability` extra (also part of `dev`); structlog and loguru are not dependencies of Inqtrix. [Logging](../docs/observability/logging.md#use-your-organizations-log-format) explains each pattern.

Structured forensic lineage events (`query_record`, `source_record`, `answer_claim_binding`, and the rest) require:

- `OBSERVABILITY_PROFILE=forensic`
- a trace sink: `INQTRIX_TRACING=file` or `otlp`

They are recorded on the run's trace, never in the log. For operator recipes and ID walkthroughs, see [Forensic cookbook](../docs/observability/forensic-cookbook.md). All logging variables, the line fields and the `OBSERVABILITY_PROFILE` values `summary` / `debug` / `forensic` are documented in [Logging](../docs/observability/logging.md).

Forensic events also flow into `iteration_logs` when testing mode is on (env `TESTING_MODE`, `AgentSettings(testing_mode=True)`, or HTTP `POST /v1/test/run`); see [Iteration log](../docs/observability/iteration-log.md) and [Web server mode](../docs/deployment/webserver-mode.md). The [`webserver_stacks/README.md`](webserver_stacks/README.md) lists the server variables and logging recipes.

## Quickstart

| Script | Use it for |
|--------|------------|
| [`quickstart/basic_env.py`](quickstart/basic_env.py) | Minimal `ResearchAgent()` run from env-based configuration. |
| [`quickstart/streaming.py`](quickstart/streaming.py) | Iterating over `agent.stream(...)` chunks. |

Run with:

```bash
# uv
uv run python examples/quickstart/basic_env.py

# standard Python/pip
python examples/quickstart/basic_env.py
```

## Provider stacks

Provider stacks run one research question in-process through explicit Baukasten constructors:

| Script | LLM | Search |
|--------|-----|--------|
| [`provider_stacks/litellm_perplexity.py`](provider_stacks/litellm_perplexity.py) | LiteLLM | Perplexity |
| [`provider_stacks/anthropic_perplexity.py`](provider_stacks/anthropic_perplexity.py) | Anthropic | Perplexity |
| [`provider_stacks/anthropic_perplexity_chat.py`](provider_stacks/anthropic_perplexity_chat.py) | Anthropic | Perplexity |
| [`provider_stacks/bedrock_perplexity.py`](provider_stacks/bedrock_perplexity.py) | Bedrock | Perplexity |
| [`provider_stacks/azure_openai_perplexity.py`](provider_stacks/azure_openai_perplexity.py) | Azure OpenAI | Perplexity |
| [`provider_stacks/azure_foundry_web_search.py`](provider_stacks/azure_foundry_web_search.py) | Azure OpenAI | Azure Foundry Web Search |

The interactive Anthropic + Perplexity script opens a terminal REPL and keeps chat history in the process:

```bash
# uv
uv run python examples/provider_stacks/anthropic_perplexity_chat.py

# standard Python/pip
python examples/provider_stacks/anthropic_perplexity_chat.py
```

## Custom providers

| Script | Shows |
|--------|-------|
| [`custom_providers/anthropic_with_env_search.py`](custom_providers/anthropic_with_env_search.py) | Custom LLM with auto-created search provider. |

Use these when you want to copy the constructor-first pattern into your own script.

## Webserver stacks

`examples/webserver_stacks/` exposes the same provider combinations over the OpenAI-compatible HTTP API:

| Script / doc | Shows |
|--------|-------|
| [`webserver_stacks/litellm_perplexity.py`](webserver_stacks/litellm_perplexity.py) | LiteLLM + Perplexity behind the HTTP server. |
| [`webserver_stacks/anthropic_perplexity.py`](webserver_stacks/anthropic_perplexity.py) | Anthropic + Perplexity behind the HTTP server. |
| [`webserver_stacks/bedrock_perplexity.py`](webserver_stacks/bedrock_perplexity.py) | Bedrock + Perplexity behind the HTTP server. |
| [`webserver_stacks/azure_openai_perplexity.py`](webserver_stacks/azure_openai_perplexity.py) | Azure OpenAI + Perplexity behind the HTTP server. |
| [`webserver_stacks/azure_foundry_web_search.py`](webserver_stacks/azure_foundry_web_search.py) | Azure OpenAI + Azure Foundry Web Search behind the HTTP server. |
| [`webserver_stacks/azure_knowledge_quickstart.py`](webserver_stacks/azure_knowledge_quickstart.py) | Knowledge engine on the Azure stack (memory vector store, Azure embeddings); docstring walks the full curl sequence from collection to `mode=knowledge` run. Profiles: [Retrieval profiles](../docs/configuration/knowledge-profiles.md). |
| [`webserver_stacks/multi_stack.py`](webserver_stacks/multi_stack.py) | Every stack above in one process; per-request `"stack"` routing plus `GET /v1/stacks` discovery. |
| [`webserver_stacks/oidc_stack.md`](webserver_stacks/oidc_stack.md) | Walkthrough: browser SSO via `INQTRIX_AUTH_MODE=oidc` with the Dex reference IdP from the dev compose stack. |

Start one stack:

```bash
# uv
uv run python examples/webserver_stacks/anthropic_perplexity.py

# standard Python/pip
python examples/webserver_stacks/anthropic_perplexity.py
```

Or start the multi-stack server, which registers every stack whose required env vars are present:

```bash
# uv
uv run python examples/webserver_stacks/multi_stack.py

# standard Python/pip
python examples/webserver_stacks/multi_stack.py
```

The operational reference for env vars, logging, TLS, API keys, CORS, per-request overrides, multi-stack routing, and cancel behaviour is [`webserver_stacks/README.md`](webserver_stacks/README.md).

## Azure smoke tests

The scripts under [`provider_stacks/azure_smoke_tests/`](provider_stacks/azure_smoke_tests/) make isolated live calls against Azure providers. They are not part of the offline pytest suite and require real Azure configuration:

```bash
# uv
uv run python examples/provider_stacks/azure_smoke_tests/test_llm.py
uv run python examples/provider_stacks/azure_smoke_tests/test_foundry_web_search.py

# standard Python/pip
python examples/provider_stacks/azure_smoke_tests/test_llm.py
python examples/provider_stacks/azure_smoke_tests/test_foundry_web_search.py
```

## Related docs

- [Docs hub](../docs/README.md)
- [Library mode](../docs/deployment/library-mode.md)
- [Web server mode](../docs/deployment/webserver-mode.md)
- [Logging](../docs/observability/logging.md)
- [Forensic cookbook](../docs/observability/forensic-cookbook.md)
- [Iteration log](../docs/observability/iteration-log.md)
- [Writing a custom provider](../docs/extending/provider.md)
