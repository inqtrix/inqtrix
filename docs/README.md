# Documentation

## Scope

Start here when you know what you want to do but not which page owns it. The root [`README.md`](../README.md) stays the short project front page; this file is the task-oriented map for the full `docs/` tree.

## Start by task

| I want to... | Go to |
|--------------|-------|
| Install the project and run the offline suite | [Installation](getting-started/installation.md) |
| Run the first live research question | [First research run](getting-started/first-research-run.md) |
| Run the full stack with one command (Docker) | [Stack quickstart](getting-started/stack-quickstart.md) |
| Deploy on Kubernetes (Helm chart) | [Kubernetes](deployment/kubernetes.md) |
| Upgrade a bundled or managed PostgreSQL database safely | [Database migrations](deployment/database-migrations.md) |
| Configure AWS S3, workload identity, or an S3-compatible store | [Object storage](deployment/object-storage.md) |
| Decide which way to run it | [Deployment modes](deployment/deployment-modes.md) |
| Decide which infrastructure I need (Postgres, S3, Qdrant, workers, OIDC) | [Platform components](getting-started/platform-components.md) |
| Operate the stack (start/stop/update/backup) | [Runbooks](deployment/runbooks.md) |
| Enable and operate live editor collaboration | [Deploy editor collaboration](deployment/editor-collaboration.md) |
| Get a cited answer from my own documents | [First knowledge answer](getting-started/first-knowledge-answer.md), [Knowledge engine](architecture/knowledge-engine.md), [Retrieval profiles](configuration/knowledge-profiles.md) |
| Choose or connect an authentication mode | [Auth modes](deployment/auth-modes.md) |
| Create or manage users (owner setup, roles, disabling, PATs) | [Create and manage users](deployment/create-and-manage-users.md) |
| Deploy to production (TLS, secrets, backups, hardening) | [Production checklist](deployment/production-checklist.md) |
| Embed Inqtrix in a Python script | [Library mode](deployment/library-mode.md) |
| Run the OpenAI-compatible HTTP server | [Web server mode](deployment/webserver-mode.md) |
| Build or deploy the React/browser UI | [React UI](deployment/react-ui.md), [Runs API](extending/runs-api.md), [Web server mode](deployment/webserver-mode.md) |
| Build my own UI or client on the native API | [Build a UI on Inqtrix](extending/ui.md) |
| Pick a provider stack | [Providers overview](configuration/providers.md) |
| Configure a provider combo via `.env` (Azure, Anthropic, Bedrock, ...) | [Provider recipes](getting-started/provider-recipes.md) |
| Find runnable example scripts | [Examples index](../examples/README.md) |
| Configure env vars or report profiles | [Settings and env](configuration/settings-and-env.md), [Report profiles](configuration/report-profiles.md) |
| Understand how the research loop works | [Research loop](architecture/websearch/loop.md) |
| Understand the knowledge/RAG retrieval pipeline (hybrid, RRF, gate loop) | [Knowledge engine](architecture/knowledge-engine.md) |
| Understand the workspace agent (plans, approvals, child runs, memo canvas) | [Agent platform](architecture/agent-platform.md) |
| Understand the live editor data and trust model | [Editor collaboration architecture](architecture/editor-collaboration.md) |
| Debug a bad or expensive run | [Debugging runs](observability/forensic-cookbook.md#debugging-runs), [Logging](observability/logging.md), [Forensic cookbook](observability/forensic-cookbook.md) |
| Scrape metrics / wire readiness probes | [Metrics](observability/metrics.md) |
| Learn why a run stopped | [Stopping](architecture/websearch/stopping.md), [Score ledger](observability/forensic-cookbook.md#score-ledger) |
| Add a custom provider | [Writing a custom provider](extending/provider.md) |
| Plug in a custom auth provider or storage backend | [Custom auth provider](extending/auth-provider.md), [Custom storage backend](extending/storage.md) |
| Work on tests, replay cassettes, or releases | [Running tests](development/running-tests.md), [Testing strategy](development/testing-strategy.md), [Release process](development/release-process.md) |
| See how AI-generated content is marked, and what that means for operators | [AI transparency](deployment/ai-transparency.md) |

## Paths by audience

**First-time user:** [Installation](getting-started/installation.md) -> [First research run](getting-started/first-research-run.md) -> [First knowledge answer](getting-started/first-knowledge-answer.md).

**Operator (self-hosting the product):** [Stack quickstart](getting-started/stack-quickstart.md) -> [Provider recipes](getting-started/provider-recipes.md) -> [Platform components](getting-started/platform-components.md) -> [Database migrations](deployment/database-migrations.md) / [Object storage](deployment/object-storage.md) -> [Create and manage users](deployment/create-and-manage-users.md) -> [Runbooks](deployment/runbooks.md) -> optional [Deploy editor collaboration](deployment/editor-collaboration.md) -> [Production checklist](deployment/production-checklist.md).

**Application integrator:** [Library mode](deployment/library-mode.md) -> [Build a UI on Inqtrix](extending/ui.md) -> [Custom auth provider](extending/auth-provider.md) / [storage backend](extending/storage.md) -> [Library API](architecture/library-api.md).

**HTTP operator:** [Web server mode](deployment/webserver-mode.md) -> [Auth modes](deployment/auth-modes.md) -> [Security model](deployment/security-model.md) -> [Logging](observability/logging.md) -> [Forensic cookbook](observability/forensic-cookbook.md#timeouts-and-errors).

**Contributor:** [Architecture overview](architecture/overview.md) -> [Research loop](architecture/websearch/loop.md) -> [Coding standards](development/coding-standards.md) -> [Running tests](development/running-tests.md).

## Folder map

| Folder | Use it for |
|--------|------------|
| `getting-started/` | Install it, get the first research answer and the first knowledge answer, bring up the full stack, and decide which infrastructure you actually need. |
| `deployment/` | Run it for real: Compose and Kubernetes, library and HTTP mode, auth modes and user management, the React UI, editor collaboration, database and object storage, the security model, version upgrades, AI transparency, and the production checklist. |
| `architecture/` | How each subsystem works, including `architecture/websearch/` for the research engine: the loop and its scoring pages, the knowledge engine, the workspace agent, editor collaboration, the data model, and the library API. |
| `extending/` | Build your own implementation against a seam: provider, auth provider, storage backend, your own UI or client, and the Runs API wire contract. |
| `configuration/` | `AgentConfig`, every environment variable, report and retrieval profiles, model cards, and the provider settings that back them. |
| `observability/` | Logs, run events, the iteration and score ledgers, metrics, and the forensic cookbook for reading a finished run back out. |
| `development/` | Contribution rules, docs maintenance, tests, parity tooling, releases. |

## Every page
The map above groups by task; this lists everything, so no page is
reachable only by knowing it exists.

**`getting-started/`** — [First knowledge answer](getting-started/first-knowledge-answer.md) · [First research run](getting-started/first-research-run.md) · [Installation](getting-started/installation.md) · [Overview](getting-started/overview.md) · [Platform components — do you need this?](getting-started/platform-components.md) · [Provider recipes (mix and match)](getting-started/provider-recipes.md) · [Stack quickstart — 5 minutes to Research Desk](getting-started/stack-quickstart.md)

**`deployment/`** — [AI transparency](deployment/ai-transparency.md) · [Authentication modes](deployment/auth-modes.md) · [Create and manage users](deployment/create-and-manage-users.md) · [Database migrations](deployment/database-migrations.md) · [Deploy editor collaboration](deployment/editor-collaboration.md) · [Deployment modes](deployment/deployment-modes.md) · [Kubernetes](deployment/kubernetes.md) · [Library mode](deployment/library-mode.md) · [Object storage](deployment/object-storage.md) · [Production checklist](deployment/production-checklist.md) · [React UI](deployment/react-ui.md) · [Runbooks — operate the Stack-mode deployment](deployment/runbooks.md) · [Security model](deployment/security-model.md) · [Web server mode](deployment/webserver-mode.md)

**`architecture/`** — [Agent platform (Agent Desk)](architecture/agent-platform.md) · [Architecture overview](architecture/overview.md) · [Data architecture](architecture/data-architecture.md) · [Editor collaboration](architecture/editor-collaboration.md) · [Knowledge engine](architecture/knowledge-engine.md) · [Library API](architecture/library-api.md)

**`architecture/websearch/`** — the web-research engine, one concern per page: [Answer composition](architecture/websearch/answer.md) · [Evidence pipeline](architecture/websearch/evidence.md) · [Nodes](architecture/websearch/nodes.md) · [Research loop](architecture/websearch/loop.md) · [Stopping](architecture/websearch/stopping.md)

**`extending/`** — build your own implementation against a seam: [Build a UI on Inqtrix](extending/ui.md) · [Runs API](extending/runs-api.md) · [Writing a custom auth provider](extending/auth-provider.md) · [Writing a custom provider](extending/provider.md) · [Writing a custom storage backend](extending/storage.md)

**`configuration/`** — [AgentConfig](configuration/agent-config.md) · [Model cards](configuration/model-cards.md) · [Providers](configuration/providers.md) · [Report profiles](configuration/report-profiles.md) · [Retrieval profiles (mode=knowledge)](configuration/knowledge-profiles.md) · [Settings and environment variables](configuration/settings-and-env.md)

**`observability/`** — [Tracing legend](observability/tracing-legend.md) · [Forensic cookbook](observability/forensic-cookbook.md) · [Iteration log](observability/iteration-log.md) · [Logging](observability/logging.md) · [Metrics (`/metrics`)](observability/metrics.md)

**`development/`** — [Archived GitHub Actions workflows](development/archived-github-actions/README.md) · [Coding standards](development/coding-standards.md) · [Contributing](development/contributing.md) · [Docs maintenance](development/docs-maintenance.md) · [Local infrastructure](development/local-infrastructure.md) · [Parity tooling](development/parity-tooling.md) · [Release process](development/release-process.md) · [Running tests](development/running-tests.md) · [Testing strategy](development/testing-strategy.md)

## Related docs

- [Examples index](../examples/README.md)
- [Docs maintenance](development/docs-maintenance.md)
- [Architecture overview](architecture/overview.md)
- [Evidence pipeline](architecture/websearch/evidence.md)
- [Knowledge engine](architecture/knowledge-engine.md)
- [Agent platform](architecture/agent-platform.md)
- [AI transparency](deployment/ai-transparency.md)
