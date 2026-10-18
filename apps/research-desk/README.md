# Inqtrix Research Desk

React + Vite + shadcn-ready frontend shell for the native Inqtrix run API.

This app is intentionally isolated from the Python package. It consumes the
HTTP server through `/health`, `/v1/stacks`, and `/v1/runs*`; it never imports
`src/inqtrix` and never reads provider credentials.

## Prerequisites

- Node.js >= 22.12 (see root `package.json` `engines`).
- npm >= 10.9.0.

## Install

Use npm from the repository root. `package-lock.json` is the sole JavaScript
dependency lock:

```bash
npm ci
npm run ui:dev
```

Dependency patches outside npm's lock contract are prohibited. Fixes must live
in application code or an official dependency release.

## Development

Start the API server in one terminal. Choose either the uv or the standard
Python/pip path:

```bash
# uv
uv sync --extra dev
uv run python examples/webserver_stacks/multi_stack.py

# or standard Python/pip
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python examples/webserver_stacks/multi_stack.py
```

### Default dev server (Vite proxy to `http://localhost:5100`)

```bash
# From the repository root:
npm run ui:dev
# -> http://127.0.0.1:5173
```

The Vite dev server proxies `/health` and `/v1` to `http://localhost:5100`,
so browser fetches stay same-origin during local development while
preserving the production boundary between frontend and API.

### Dev server pointing at a non-default backend

`VITE_INQTRIX_API_BASE_URL` switches both the Vite dev-proxy target
(see `apps/research-desk/vite.config.ts`) and the browser-side default
base URL (`apps/research-desk/src/api/inqtrixClient.ts`).

```bash
VITE_INQTRIX_API_BASE_URL=http://127.0.0.1:6100 npm run ui:dev
```

The value must be a full browser origin (scheme + host + optional port).
Bare IP addresses without scheme are not accepted by the browser fetch
layer. Leave the variable unset during local proxy development.

If the server has `INQTRIX_SERVER_API_KEY` enabled, enter the matching
Bearer token in the app's Settings > Security view at runtime; do not put API keys
into `VITE_*` variables because they are exposed in the browser bundle.

### Available environment variables

| Variable | Stage | Default | Effect |
|---|---|---|---|
| `VITE_INQTRIX_API_BASE_URL` | dev + build | `""` (same-origin) | Backend origin baked into the bundle and used by the Vite dev-proxy. Leave unset for same-origin serving through the Python gateway or optional nginx adapter (see [`docs/deployment/react-ui.md`](../../docs/deployment/react-ui.md)). |

No other `VITE_*` variables are read by the React app today.

On first load the app creates a non-secret browser workspace id, stores it in
`localStorage`, sends it as `X-Inqtrix-Workspace-Id` on run and chat requests,
and writes it to `project.md` on export/save. Loading a project restores that
id, so `/v1/runs` hydration only brings back runs from the same workspace
namespace instead of every in-memory run on a shared API server.

## Live run integration

The composer creates native `/v1/runs` jobs. Web search enabled sends
`mode="research"`; web search disabled sends `mode="direct_llm"`. The app then
streams `events_url`, patches the selected job card from every snapshot event,
and fetches `result_url` after `inqtrix.run.completed` to populate the report,
evidence tab, and chat attachment data structure.

The visible live protocol is intentionally based on user-facing
`inqtrix.progress.message` events plus terminal/error events. Technical
snapshot, node-start/node-finish, and output-delta events remain part of the
internal state patching path but are not rendered as primary timeline steps.
Progress severities drive the compact info/warning icons in the selected card
and the warning treatment in the right-hand agent panel.

Completed runs keep the same visible event records in the project state and in
the exported Markdown frontmatter under `events`, so the report panel can show
the archived agent protocol after the live run has finished.

Completed reports remain attached to the in-memory project state while the app
is open and can be saved or exported with the project. Backend rehydration after
a full reload only works while the server still retains the terminal run under
`RUN_COMPLETED_TTL_SECONDS` (default 300 seconds). Increase that server setting
for longer review windows, or add durable backend run storage before relying on
reloads after that TTL.

## Prompt Library and mentions

The Prompt Library stores project-scoped prompt entries. Entries are client-side
Markdown snippets with a required slug label such as `executive-brief`; they
are still referenced in the composer as `@rules:executive-brief` for backward
compatibility and injected into the next `/v1/chat/completions` request as
bounded context. They are not a backend concept and do not change the selected
provider stack.

Prompt Library entries can be categorized as Instructions, Functions, or
Context Packs. Instructions describe roles, behavior, style, or skills.
Functions are action prompts such as translate, summarize, or rewrite; only
Function entries are used as prompt-chaining steps. Context Packs combine long
context text with optional references to existing Database files or file
groups. The Prompt Library page can link only existing Database entries, not
upload new files. Context Packs can place linked files with `{{context}}` inside
the prompt text; if the placeholder is omitted, the rendered context blocks are
appended at the end. The Database context picker is search-based and shows a
bounded result list, with selected files and groups kept separately above the
search results.

Visibility controls whether an entry appears in Chat autocomplete, Editor
autocomplete, both, or neither. Disabling autocomplete hides the entry from all
surfaces and disables the Chat/Editor checkboxes. `@rules:` remains the single
mention shortcut; autocomplete filters entries per surface and groups visible
results by category. The Prompt Library list is sorted by category, then title.
When a Context Pack is attached to a chat, the rendered context snapshot is
stored in chat history so older chats remain stable even if Database files later
change.

Completed research reports can be referenced the same way through
`@research:<label>`. The composer also exposes both groups through the plus
menu. Text pasted into a composer stays text: its `@files:`, `@filegroups:`
and `@research:` tokens never become references, not even when the composer
restores its draft or accepts an improved text. A reference copied inside a
composer pastes as that reference. Sending a message stores the visible user
text plus attachment snapshots in chat history, while the technical context
block is rebuilt only for the API request.

Project export writes completed research reports under `search-history/` and
chat threads under `chat-history/` with compact timestamp-plus-id filenames.
Long titles stay in frontmatter and in the readable `project.md` export index,
not in file names. Existing exports with older title-based file names still
load because import reads the canonical IDs from frontmatter.

The Chat history can group related conversations into project-scoped groups.
Groups are stored in `project.md` through `chat_groups`, `chat_group_order`,
and `chat_thread_group_memberships`; individual chat files remain unchanged
under `chat-history/`. Loading an older project without these fields treats all
threads as ungrouped.

Chat transcripts support lightweight message management inside the selected
conversation. The header selection mode marks individual messages for bulk
deletion, user messages can be edited inline from their hover actions, and
assistant messages expose a branch action that creates a new chat containing
the transcript up to that response. If deletion leaves a user message as the
last transcript item, its hover actions can start a new assistant response
without duplicating the user message.

Project export writes Prompt Library entries to `rules/<label>.md` with
frontmatter identifying `kind: "inqtrix.chat_rule"`. The frontmatter stores the
entry category, visibility, autocomplete status, and linked Context Pack
references additively; older rule files without those fields load as
Instruction entries that are visible in Chat and Editor autocomplete.
`project.md` stores `rule_order` so the library order survives a save/load
cycle. Older projects without rules still load with an empty Prompt Library.

Project export also stores UI preferences in `project.md`: locale, theme mode
(`light`, `dark`, or `system`), theme preset, and contrast mode. Loading a
project applies those preferences to the current browser session. It also
stores the non-secret `workspace_id` used for live API namespacing. Runtime-only
secrets such as Bearer tokens remain excluded.

High contrast mode strengthens surfaces and borders without disabling Shiki
syntax colors in chat and report Markdown code blocks.

## Build

### Same-origin build (recommended)

Leave `VITE_INQTRIX_API_BASE_URL` unset. The resulting bundle uses
relative URLs and works behind either the Python web gateway or the optional
nginx adapter
without rebuild.

```bash
npm run ui:typecheck
npm run ui:lint
npm run ui:build
# -> apps/research-desk/dist/
```

### Build for a fixed backend origin

```bash
VITE_INQTRIX_API_BASE_URL=https://inqtrix-api.example.com \
  npm run ui:build
```

Use this only when no reverse-proxy is available; otherwise prefer the
same-origin build because it deploys to every environment without
rebuild.

The production bundle is written to `apps/research-desk/dist/`. That
directory is build output and is intentionally not committed. See
[`docs/deployment/react-ui.md`](../../docs/deployment/react-ui.md)
for deployment options including the nginx pattern and the Python
gateway started with `python -m inqtrix_web_gateway`.
