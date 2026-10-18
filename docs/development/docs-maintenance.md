# Docs maintenance

## Scope

How to keep `docs/**` honest over time. The repository uses plain GitHub-rendered Markdown today (no MkDocs, no Sphinx); the conventions below keep structure and cross-links consistent and make a future migration to MkDocs-Material a mechanical move.

## Conventions per page

Every page under `docs/**`:

1. Starts with an H1 title in **sentence case**, derived from the filename unless a reader-facing title is clearer for a short slug (`extending/provider.md` → `Writing a custom provider`, `architecture/websearch/answer.md` → `Answer composition`). Acronyms and product names keep their capitals: `Library API`, `React UI`, `Runs API`, `AI transparency`, `AgentConfig`, `GitHub Actions`.
2. Has a `## Scope` section as the first sub-heading explaining what the page does and does not cover.
3. Uses relative links for internal cross-refs (`[Agent config](../configuration/agent-config.md)`), never absolute URLs to GitHub.
4. Ends with a `## Related docs` block listing the peer pages a reader needs next, each with a 0–1 sentence teaser. Two to four is the norm; hub pages and pages with many neighbours run longer (`deployment/webserver-mode.md` lists ten).
5. Mermaid diagrams use only the subset that renders on GitHub: `flowchart`, `stateDiagram`, `sequenceDiagram`. Avoid `timeline`, `mindmap`, `C4Context` until confirmed.
6. Code examples use triple backticks with an explicit language tag (`python`, `bash`, `yaml`, `json`, `dotenv`, `sql`, or `text` for output, trees and formulas).
7. English-only prose. German strings only inside quoted demo questions or prompt snippets, matching the repository convention.
8. No Mermaid diagram stands alone. The paragraph before it states the reader question it answers; the paragraph after it names the important transition(s) and clarifies which blocks are code vs data.
9. New terms are defined on first use. Avoid shorthand such as "ledger", "bundle", "contract", or "strategy" unless the local page says what object it refers to and where it lives.
10. Technical identifiers in prose include their data kind and type on first use within a section when the kind is not obvious from the sentence.

## Current-state versioned content

Versioned documentation describes the supported present contract. Do not cite
private plans, priority labels, internal decision ids, internal issue numbers,
task logs, branches, commits, incidents, or the dates on which internal
decisions and fixes were made. State the current rationale and observable
behaviour directly.

Functional protocol versions, schema revisions, dependency or image versions,
source provenance, retention examples, and deterministic temporal fixtures
remain valid when the described behaviour depends on them. Runtime verification
reports and their timestamps are local artifacts; public evidence identifies
the Inqtrix version under test.

`README.md`, `docs/**`, and the Markdown under `examples/**` are the public
documentation hierarchy. Private agent memory and editor-specific rule files are never a
public source of truth.

## Reader depth (configuration, APIs, and heuristics)

Structure alone (Scope, Related docs, tables) is not enough when a reader must predict runtime behaviour.

For pages under `docs/configuration/`, provider setup pages, HTTP deployment pages, and the web-research pages under `docs/architecture/websearch/`:

1. **Prefer an explicit behaviour column** in tables (for example **Effect** or **Behaviour**) that states what changes for the user or for a single run when the value is set, not only the literal meaning of the name.
2. **Add at least one copy-paste example** (Python, `dotenv`, `yaml`, or `bash`) when the interaction is non-obvious: precedence chains, per-request overrides, provider auto-creation, or anything that touches more than one subsystem.
3. **Link to runnable scripts** under `examples/` when they already demonstrate the stack; one sentence of context is enough.
4. **Use small diagrams or log excerpts** when control flow or iteration-log fields matter (see [`../architecture/websearch/stopping.md`](../architecture/websearch/stopping.md) as a reference).

Agent-facing coding rules for docstrings and Pydantic fields remain in [`coding-standards.md`](coding-standards.md); this section is the bar for **user-facing Markdown** in `docs/**`.

## Diagrams, schemas, and LLM-call documentation

Architecture diagrams should be readable by someone who has not opened the
source code yet. Use the legend from
[`../architecture/overview.md`](../architecture/overview.md#how-to-read-the-diagrams).

When documenting a new or changed LLM call, include:

- node/stage that makes the call,
- provider method (`complete`, `complete_with_metadata`, `complete_structured`, or strategy-owned call),
- model role (`classify`, `claim extraction`, `evaluate`, `reasoning`),
- prompt origin (`nodes.py`, `prompts.py`, strategy prompt, provider helper),
- expected output shape and parser,
- fallback marker or visible degradation path.

When documenting a new state/evidence structure, include either a compact schema
or a small example. Also state whether the object is primary truth, derived
view, prompt-facing view, audit view, or public projection.

## Identifier kind and type notation

Use normal Python/Pydantic vocabulary rather than inventing a custom mini-syntax.
The goal is to remove ambiguity without making prose noisy.

Preferred first-use forms:

| Identifier shape | First-use annotation |
|---|---|
| `state["iteration_logs"]` | `state["iteration_logs"]` (`AgentState` key, `list[dict[str, Any]]`) |
| `ResearchResult` | `ResearchResult` (`Pydantic BaseModel`) |
| `ProviderContext` | `ProviderContext` (`@dataclass`) |
| `LLMProvider` | `LLMProvider` (`ABC`) |
| `AgentSettings.skip_search` | `AgentSettings.skip_search` (`bool`, env alias `SKIP_SEARCH`) |
| `body["agent_overrides"]` | `body["agent_overrides"]` (`HTTP JSON object`) |

Do not annotate every repeated mention. For data-heavy sections, prefer a table
with `Identifier`, `Kind`, `Type`, `Source of truth`, and `Role` columns instead
of long parenthetical prose.

## Adding a new page

1. Pick the closest existing subdirectory (`architecture/` with its `architecture/websearch/` sub-folder, `configuration/`, `deployment/`, `development/`, `extending/`, `getting-started/`, `observability/`).
2. Write the page following the conventions above.
3. Add the new page to the `Related docs` blocks of the pages that should link to it.
4. Add the page to the `Every page` list in [`../README.md`](../README.md). This step is unconditional -- that list promises no page is reachable only by knowing it exists.
5. Add a `Start by task` row to [`../README.md`](../README.md) when the page answers a reader task, and update the root [`../../README.md`](../../README.md) when the page changes a major entry path.
6. Run a local link check (see below).

## Link check

Use [`lychee`](https://github.com/lycheeverse/lychee) for a fast offline link check. There is no committed CI workflow for link-checking today; the repository's CI configuration is maintainer-owned. Adding one is a maintainer decision under the review requirements in [`archived-github-actions/README.md`](archived-github-actions/README.md).

```bash
# Local check
lychee --no-progress --include-fragments \
    README.md 'docs/**/*.md' 'examples/**/README.md'
```

Alternative tools: `markdown-link-check` (Node, widely used, deprecated community action has forks), `linkinator` (Node, dual CLI/module). `lychee` is recommended for speed on CI.

## Mermaid syntax constraints

Stick to the following subset; each rule was verified to render on GitHub at the time of writing:

- Node IDs: camelCase or snake_case. No spaces (`UserService` OK, `User Service` not).
- Edge labels with special characters: wrap in quotes (`A -->|"O(1) lookup"| B`).
- Node labels with `(`, `)`, `:`: wrap in double quotes (`A["Process (main)"]`).
- Avoid reserved keywords as node IDs (`end`, `graph`, `subgraph`, `flowchart`).
- Subgraph headers: either an ID plus a bracketed label (`subgraph gb [Group B]`), or a quoted label on its own (`subgraph "Public API"`). A bare unquoted word works only when the label contains no spaces.
- Do not use explicit colours or `classDef fill:#...` — they render badly in dark mode.
- Do not use `click` events; GitHub strips them.

## Keeping docs in sync with code

Whenever a code change alters public behaviour, update the matching `docs/**` page in the same PR:

| Code area | Matching docs page(s) |
|-----------|-----------------------|
| `src/inqtrix/providers/<x>.py` | the matching `##` section in `docs/configuration/providers.md`; for `embeddings.py` / `rerankers.py` instead `docs/architecture/knowledge-engine.md` and `docs/configuration/knowledge-profiles.md`; `SearchProvider` / `LLMProvider` contract changes additionally `docs/extending/provider.md` |
| `src/inqtrix/providers/__init__.py` (env provider selector), `ProviderSettings` in `settings.py` | `docs/getting-started/provider-recipes.md`, `docs/configuration/providers.md`, `docs/configuration/settings-and-env.md` |
| `src/inqtrix/strategies/<x>.py` | the page under `docs/architecture/websearch/` that owns the concern |
| `src/inqtrix/settings.py` (any `Field(alias=...)` / `validation_alias`, or a new `*Settings` class) | `docs/configuration/settings-and-env.md` (every variable; see the completeness rule below), plus any affected `docs/configuration/*.md` |
| `os.getenv` / `os.environ` reads outside `Settings` (`src/inqtrix/__main__.py`, `src/inqtrix/worker/__main__.py`, `scripts/*.py`, `tests/**`) | `docs/configuration/settings-and-env.md` (the process-level and development/test-only sections) |
| `src/inqtrix/server/<x>.py` | `docs/deployment/webserver-mode.md` and/or `docs/deployment/security-model.md` |
| `src/inqtrix/auth/<x>.py`, `src/inqtrix/server/routers/auth.py`, `src/inqtrix/server/routers/admin.py` | `docs/deployment/auth-modes.md`, `docs/deployment/security-model.md`, `docs/deployment/create-and-manage-users.md` |
| `create_app`/`register_routes`/`build_container` injection seams (`auth_provider=`, `object_store_impl=`, `run_store=`, `permissions=`, `knowledge=`) | `docs/extending/auth-provider.md`, `docs/extending/storage.md` |
| `src/inqtrix/knowledge/<x>.py`, `src/inqtrix/knowledge/stores/<x>.py`, `src/inqtrix/storage/knowledge_orm.py`, `src/inqtrix/services/knowledge_service.py` | `docs/architecture/knowledge-engine.md`; profile semantics additionally `docs/configuration/knowledge-profiles.md` |
| `src/inqtrix/storage/<x>.py`, `src/inqtrix/runs/<x>.py`, `src/inqtrix/worker/<x>.py` | `docs/getting-started/platform-components.md`, `docs/configuration/settings-and-env.md`; migration/RLS changes additionally `docs/deployment/database-migrations.md`, object-store/S3 changes additionally `docs/deployment/object-storage.md` |
| project-persistence tier (`src/inqtrix/storage/{chat,editor,asset_records,knowledge_sessions,vector_index,account}_orm.py` + migrations, `src/inqtrix/project/*`, `src/inqtrix/services/{chat_history,editor_persistence,asset_records,knowledge_sessions,vector_index,account_preferences}_service.py`, and the `apps/research-desk` sync hooks) | `docs/architecture/data-architecture.md` (what lives where, the storage matrix, scoping, load-on-use, server-owned upload/index/delete lifecycles, revision/generation publication, account-wins, and capability-gated tier switch) |
| editor collaboration (`packages/editor-schema/**`, `apps/collaboration-server/**`, `src/inqtrix/{project,services,storage,server}/**collaboration**`, editor share/patch changes, and `apps/research-desk/src/features/editor/**`) | `docs/architecture/editor-collaboration.md` (truth, flow, contracts), `docs/deployment/editor-collaboration.md` (topology and operations), `docs/configuration/settings-and-env.md` (every variable), and `docs/development/testing-strategy.md` (cross-runtime gates) |
| `deploy/compose/compose.dev-ports.yaml` | `docs/development/local-infrastructure.md`, `docs/getting-started/platform-components.md` (manual/host section), and, for Dex/OIDC ports, `docs/deployment/auth-modes.md` |
| `deploy/compose/compose.stack.yaml`, `deploy/compose/compose.web-nginx.yaml`, `deploy/docker/**`, `src/inqtrix_web_gateway/**`, `deploy/.env.stack.example`, `deploy/.env.stack.secrets.example`, `deploy/.env.migrate.secrets.example` | `docs/getting-started/stack-quickstart.md`, `docs/getting-started/platform-components.md`, `docs/deployment/runbooks.md`, `docs/deployment/deployment-modes.md`, `docs/deployment/react-ui.md`, `docs/deployment/kubernetes.md` (image hardening / Python web proxy); migration-env changes additionally `docs/deployment/database-migrations.md`, S3 changes `docs/deployment/object-storage.md`, collaboration profile or proxy changes `docs/deployment/editor-collaboration.md` |
| `deploy/helm/inqtrix/**` (Helm chart, values, templates) | `docs/deployment/kubernetes.md`, `docs/deployment/deployment-modes.md`; migration role/Secret changes additionally `docs/deployment/database-migrations.md`, object-store identity/CA changes `docs/deployment/object-storage.md`, collaboration values/templates `docs/deployment/editor-collaboration.md` |
| `src/inqtrix/agent.py` public API | `docs/architecture/library-api.md`, `docs/architecture/overview.md` |
| `src/inqtrix/nodes.py` | `docs/architecture/websearch/nodes.md` |
| `src/inqtrix/agents/**`, `src/inqtrix/capabilities/**`, `src/inqtrix/services/agent_control_service.py`, `src/inqtrix/server/routers/agent_runs.py` | `docs/architecture/agent-platform.md`; event-catalog or waiting-status changes additionally `docs/extending/runs-api.md`; `INQTRIX_AGENT_*` changes additionally `docs/configuration/settings-and-env.md` |
| `src/inqtrix/evidence.py` | `docs/architecture/websearch/evidence.md` |
| `src/inqtrix/prompts.py` answer behaviour | `docs/architecture/websearch/answer.md` (the owning page: per-section prompt structure and write-last sections), plus `docs/architecture/websearch/evidence.md` and `docs/architecture/websearch/nodes.md` where they quote those prompts |
| `src/inqtrix/runtime_logging.py` forensic schemas / log tooling | `docs/observability/logging.md`, `docs/observability/forensic-cookbook.md` |
| `apps/research-desk/**`, `package.json`, `package-lock.json` | `docs/deployment/react-ui.md` and, when the run-event contract changes, `docs/extending/runs-api.md`; when a frontend dependency is added or changed, also regenerate `THIRD_PARTY_NOTICES.md`/`.json` per [`release-process.md`](release-process.md) (the third-party notices refresh step) |
| `apps/research-desk/src/styles/globals.css`, `apps/research-desk/src/motion/**`, `apps/research-desk/src/components/ui/**`, visual role or primitive changes | The role definition comments in `globals.css` and the primitive's own header comment; public user-visible behaviour additionally updates the relevant page under `docs/**` |
| `tests/verification/**`, the `verify:*` scripts in `package.json` | `docs/development/testing-strategy.md` (the profile table) and `docs/development/running-tests.md` (the profile list). Adding or renaming a profile changes both; `npm run verify:list` prints the current inventory and is the authority for the count. |
| `tests/eval/**` and its committed baselines | `docs/development/testing-strategy.md`, the retrieval- and agent-kernel evaluation sections. A stage that exists in the tree must not be described as planned. |
| `pyproject.toml` `[tool.pytest.ini_options]` markers, `tests/conftest.py` integration gating | `docs/development/testing-strategy.md` ("Markers and local gates") and `docs/development/running-tests.md`. A new marker needs its environment variable documented with it, or the documented command fails at collection. |

**Env-var completeness rule.** [`settings-and-env.md`](../configuration/settings-and-env.md) is the single source of truth for environment variables and must list **every** variable the code reads: every `Field(alias=...)` / `validation_alias` in `settings.py` (a new `*Settings` class gets its own block), and every `os.getenv` / `os.environ` read in the server/worker bootstrap, scripts, and tests (the last under the development/test-only section). A variable may appear in a table row or in a "Further tuning" prose line, but it must appear under its exact name so the page stays greppable. The committed `.env.example` / `deploy/.env.stack.example` templates are curated starters, not the reference; keep them free of variables that no longer exist in code. Deep-dive pages (provider recipes, auth modes, logging) may show usage but link here for the definition.

Public docs must stand on their own; what must not be cited in them is listed
under [Current-state versioned content](#current-state-versioned-content) above.

When a pull request changes public behaviour (`settings.py`, `AgentConfig`, HTTP routes, provider constructors, stop heuristics), update the matching `docs/**` page **in the same PR** and apply the **Reader depth** rules above for any new or materially changed knob.

## Legacy docs migration

When a long-form legacy document is split into the current tree, keep a small migration matrix in this section with these columns:

| Legacy section | Current home | Status |
|----------------|--------------|--------|
| `Quick Start / explicit providers` | `deployment/library-mode.md` | Updated to current imports and constructor signatures. |
| `HTTP Streaming` | `deployment/webserver-mode.md` | Updated to current SSE body and overrides. |
| `Custom Search Provider` | `extending/provider.md` | Updated with `search_model` and capability metadata. |

Before copying a legacy snippet, check imports, constructor signatures, enum names, env vars, and whether the feature still exists in code. Code wins over old prose.

## Related docs

- [Contributing](contributing.md)
- [Coding standards](coding-standards.md)
- [Testing strategy](testing-strategy.md)
