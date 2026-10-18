# React UI

> Files: `apps/research-desk/`, `package.json`, `package-lock.json`,
> `src/inqtrix_web_gateway/`, `deploy/docker/Dockerfile.web`

## Scope

`apps/research-desk` is the React frontend for Inqtrix: a separate Vite
application in the repository. It uses HTTP for the FastAPI endpoints and
WebSockets for optional live collaboration. The full Compose stack serves the
bundle through the Python web gateway, which forwards both transports to the
API under the same browser origin.

## Shared Markdown rendering

The development fixture at `browser-tests/fixtures/markdown-rendering.html`
uses the same renderer, stylesheet and theme providers as the workspace.
`?sample=readme-architecture&variant=report&theme=dark` selects the exact Mermaid
fence from the repository README. The default sample and `extra=directive`
remain available for rendering and theme-boundary checks. Navigation tests parse
Markdown blocks before checking README anchors and local paths, so code examples
do not count as links or headings.

All Mermaid and table exports (PNG, CSV, source copy) are created locally in the
browser; no Markdown source or rendered content is sent to the server.

Tables preserve natural word widths when the browser sizes their columns;
surrounding prose may wrap anywhere without forcing table labels into individual
letters. Wide tables remain inside a named horizontal scroll region that can
receive keyboard focus. The same wrapping rule applies to chat and report tables.

One renderer serves every surface — chat messages, agent answers and canvas
documents, knowledge answers, the evidence panel, the document diff view and
the file preview. Callers choose only the variant: `chat` renders at the
compact reading scale, `report` at the document scale. The renderer sets the
reading-scale wrapper class itself on a box-less root, so no consumer can
render a different product by forgetting it; the Tiptap editor surface is the
one other place that carries the class because it renders its own document.

Math accepts `$…$` and `$$…$$` directly. A `\[…\]` block that stands on its
own lines and an inline `\(…\)` are normalised to those forms; nothing inside
fenced or inline code is touched, and a `\[` inside a sentence stays the
literal bracket Markdown escapes with — a reference title is never typeset as
an equation.

### Diagrams

A ```` ```mermaid ```` fence renders through Mermaid, but the figure belongs to
the page: node fill and hairline border come from the surface and border
tokens, connectors are body ink at 70 % and one pixel, series colours come from
the eight chart tokens — the ColorBrewer "Dark2" print palette, one palette in
every theme so a figure keeps its colours when copied out — the type scale
follows the surface (13 px chat, 14 px report), and the ground is transparent
in every theme. The promised grammars are `flowchart`, `sequenceDiagram`,
`classDiagram`, `stateDiagram-v2`, `erDiagram`, `gantt`, `pie` and
`xychart-beta`. Other grammars still render but are neither promised nor
styled on purpose.

Data plots follow the discipline of a typeset figure: a 4:3 drawing area of at
most 440 × 330 (like a one-column journal figure, so a lone plot sits in the
column instead of filling it), hairline axes and ticks, the title naming the
series (Mermaid plots have no legend). Figure type is one size, 13 px, a point
below the 14 px body text of both surfaces — and it never scales: plots and
gantt charts, the grammars Mermaid can draw at a given width, are redrawn at
the width the figure actually has (measured, in 40 px steps, never wider than
their natural size), so a label is 13 px alone, in a pair and on a phone. Two
plot fences written directly after each other share one row as paired
sub-figures and wrap under each other on a narrow column; only plots pair up,
diagrams stay stacked. Mermaid plots offer bars and lines only — no legend,
markers, error bars, log axes or second axes. Headings carry the structure of
an answer by size, weight and spacing; the renderer draws no rule beneath them.

Styling escape hatches in the source — `%%{init: …}%%` directives and the
`config`/`theme` keys of a frontmatter — are removed before rendering and the
figure says so; a `title` in the frontmatter stays. A diagram that opens with
its `accTitle` or `accDescr` before the grammar keyword, as answers written
under an earlier prompt do, reaches Mermaid with the keyword first; nothing is
lost, so the figure says nothing about it. A diagram never exceeds its
column: a diagram without a width knob (flowchart, sequence, class, state,
entity-relationship, pie) shrinks with the column down to 70 % of its natural
width and then scrolls horizontally inside the figure, a figure taller than
640 px scrolls vertically, and the enlarge dialog shows the whole picture.
Every rendered SVG
carries `role="img"` and an accessible name — the diagram's `accTitle`, else
the diagram type — and keeps Mermaid's `<desc>` from `accDescr`. The PNG export
paints the theme background because a transparent PNG is unreadable on a dark
viewer.

The stylesheet couples to a small set of Mermaid-internal SVG classes for the
connector ink; that list lives in `mermaidTheme.ts` and the browser sample-set
probe asserts every class still appears after a Mermaid upgrade. The demo
project ships a chat titled "Darstellungsstandard" whose answer exercises every
construct above; the browser fixture `markdown-rendering.html` renders that
same body under both variants, both themes, high contrast and a phone width.

## Editor workspace

The Editor rail view adds local Markdown documents, folders, live rendered
editing, a Markdown source mode, local inline comments, import from completed
research reports, and a persistent assistant composer. Its **Export to Word**
action converts the document Markdown to a styled `.docx` entirely client-side
-- no server round-trip; math degrades to its LaTeX source text and raw HTML is
skipped.

The editor assistant endpoints preserve Markdown as the editing format. Links,
citation labels, URLs, and LaTeX should remain Markdown-compatible across
suggestions. Document diff mode compares the active document against the saved
anchor as Markdown chunks rendered through the shared Markdown renderer; do not
flatten the document to plain text, because that drops headings, links, tables,
and LaTeX in the comparison view.

Editor state is part of the project model, so it survives export/import. Project
export writes editor files under `documents/*.md` with
`kind: inqtrix.editor_document` frontmatter. The Markdown body stays clean; local
comment threads are serialized as metadata so the same file remains usable as
normal Markdown outside Inqtrix.

The editor engine is the free Tiptap core stack: `@tiptap/react`, `@tiptap/core`,
and `@tiptap/pm` for the React and ProseMirror integration, `@tiptap/starter-kit`
plus selected MIT-compatible Tiptap extensions, and `@tiptap/markdown` as the
Markdown import/export bridge through `contentType: "markdown"`. Markdown is
written through `serializeEditorMarkdown` and `serializeEditorJson` (and a
selection quote through `markdownForRange`): they keep citation labels such as
`[K1]` raw, where the bare `editor.getMarkdown()` escapes them to `\[K1\]`,
which every citation reader takes for literal text. Stored and quoted Markdown
therefore goes through these helpers, never through `editor.getMarkdown()`.

Do not add Monaco, Tiptap Pro, Tiptap Cloud, private Tiptap registry packages,
or third-party editor starter kits for this workspace. CodeMirror 6 is the one
deliberate exception already in the tree: it backs the raw-Markdown Source mode
in `apps/research-desk/src/features/editor/MarkdownSourceEditor.tsx` and must
stay scoped to that surface. Tiptap examples may inform interaction patterns,
but Inqtrix owns the compact document toolbar, file tree, comment UI, and
assistant composer. Link handling must keep explicit safe-protocol checks when
`@tiptap/extension-link` is touched.

The live editor is intentionally WYSIWYG-first. Do not add an inline "show the
underlying Markdown tokens for the clicked rich text" interaction unless there is
a dedicated design and implementation pass; Tiptap/ProseMirror does not provide
that behavior natively. Use the Source mode for raw Markdown editing.

## Local production preview

The dev server (`ui:dev`) always runs the development React build, which wraps the
app in `React.StrictMode`. StrictMode double-invokes effects, reducers, and render
functions in development, so some defects are invisible under `ui:dev` and surface
only in the production bundle. Verify a change against the production build first.

```bash
npm run ui:prod
```

`ui:prod` builds the bundle and serves it through `vite preview` on `127.0.0.1:4173`.
`ui:preview` alone serves the existing `dist/` without rebuilding it, and a stale
`dist/` is the most common reason a fix appears to have no effect in the preview.
Neither command proxies `/v1` or `/health` -- only the dev server does -- so to
exercise the production bundle against a running backend on one origin, use the
standard packaged Python gateway under *Same-origin serving without Node*.

## Runtime API boundary

The React app uses the native run API rather than treating research runs as
chat messages: it creates a run, streams its structured events, and fetches the
result payload instead of posting chat turns.

The complete catalogue of routes the server exposes is in
[`webserver-mode.md`](webserver-mode.md); the run lifecycle a client has to
follow is in [`docs/extending/ui.md`](../extending/ui.md).

### Agent Desk composer and routing

The Agent Desk composer separates context and edit targets, the Source Dock,
the Execution Capsule, and the transparency row. Four of those separations are
deliberate and must survive a rebuild:

- Source availability is never a context chip; the chip row holds only
  selected collections, an edit target, and attached skills.
- The web switch has exactly two visible states (on: brand chip, off:
  struck-through glyph); `/web` sets the same stored session state instead
  of a per-message override. Project knowledge follows the same pattern:
  `/wissen` switches the session's knowledge on (`knowledge_filters.scope =
  "all"`, every collection the user may see), a knowledge glyph appears next
  to the web glyph and toggles it off and on, and the switch is stored with
  the session (`items_json.knowledge_switch`). An attached `@`-collection
  pins the scope to that chip and hides the glyph; a session that never
  asked shows no glyph and sends no knowledge scope. While the server reports
  the knowledge control unavailable (`source_controls[].reason_code`), the
  glyph is greyed out with the reason and `/wissen` is not offered.
- The run overview trigger stays an independent control; it is not moved into
  Run Setup on narrow layouts.
- Once a run exists, `snapshot.execution` is authoritative and replaces client
  inference, so “Web available” and “Web used: 1 search” stay distinct rows
  ([`agent-platform.md`](../architecture/agent-platform.md)).

Run Setup reads the server's permission and depth manifests
([`agent-platform.md`](../architecture/agent-platform.md)). If the kernel is
not registered, the composer states that only Mission is available instead of
silently relabelling Mission as Automatic. The effective depth is always
serialized, including an explicit `normal` selection when the server capability
default is `deep`, so display and submission consume the same value.

The slash menu sets a visible one-shot execution directive; the field
semantics behind `/web` and `/wissen` are in
[`agent-platform.md`](../architecture/agent-platform.md) and
[`webserver-mode.md`](webserver-mode.md). The client-state rule is local: the
UI clears the directive only after `POST /v1/runs` is admitted and a run
summary is returned, so validation, quota, or transport failure leaves it
visible for retry, and that reset never mutates the persistent session source
policy. `/web` is the deterministic one-search path, while Automatic remains
the ordinary front door that may choose instant web itself.

The demo manifest publishes the same `capabilities.agent` blocks as a live
server, so source states, slash routes, responsive model trigger, and run
overview can be reviewed without an agent backend; the feature-detection duty
itself is in [`agent-platform.md`](../architecture/agent-platform.md).

The Magic Stick text-improvement control is an explicit review flow, not an
auto-rewrite. The browser sends only the active field text plus its context
(`chat_input` or `prompt_template`) to `/v1/text/improvements`; the server uses
the configured LLM provider without creating a research run, saving a chat
session, or invoking search. The UI must show the returned candidate with
highlighted changes and require accept/reject before replacing the draft.
Editor assistant drafts and inline comment drafts reuse the `chat_input`
context; do not add a parallel endpoint for field-level polishing unless the
server-side rewrite contract itself changes.

Every finished turn ends in one status slot under the question: the live
activity row while the run works, the “Work completed” recap afterwards. The
recap's last line states the conditions the run actually ran under — engine,
permission, Stufe, model, effort, web and knowledge use with the searched
knowledge boundary — read from the accepted run's execution snapshot
(`runSettingsSummary`); nothing there is inferred, so a run without a
snapshot shows only permission and Stufe, and a failed or cancelled turn
carries the same line.

How the run's review ended comes from its outcome record
(`inqtrix.agent.outcome`) and nowhere else: the recap, a line under the chat
answer and a line under the document canvas show the record's `summary`,
with its `lines` behind a disclosure (“What the review left open”). The
desk builds no sentence of its own from the record's counts. Only a review
that ran or was owed and gave no usable verdict (`review` `completed` or
`unavailable`) is shown; a Stufe that waives the review and a run that ended
without one show nothing. A completed agent run whose stream holds no
record, or only a record without a `summary`, reads “No outcome record” in
the recap, and nothing under its answer. The line under the answer and the
line under the canvas describe the text the run delivered: once the user
edited that answer or document, its line disappears and the recap keeps the
summary. A delegated mission's folded card names its counts and duration
first and closes with the summary its projected record carries; a mission
that ended without a record claims no review there, and a completed one
reads “No outcome record”.

### Agent Desk execution control centre

The Agent Desk run canvas is the execution overview, not a raw tool log:

- The inline work plan is the model's last reported plan. Its marked task is
  static; the separate live activity line follows actual run and tool events.
  A cancelled run does not become complete because every reported todo is done.
- Consecutive retained-evidence reads share a disclosure row. Details use the
  registered source title or search question, with a generic selection label
  for historical id-only previews. Counts describe tool invocations, not source
  quality. Failed reads and unfinished reads remain distinguishable.
- A delegated mission keeps its compact card. Its details open the existing
  run canvas, where a research work unit exposes its own query events. Only the
  inspected child subscribes to a separate event channel, under caller scope;
  closing the view aborts that channel. Child hydration never creates sessions.
  Query start and finish events update one named row. The detail history keeps
  its latest 100 rows and explicitly counts older hidden messages. A research
  task's guiding questions are never used as a completed-search denominator;
  actual query totals come from the child's metrics.
- The UI never fabricates a percentage or an ETA. Instant search uses an
  indeterminate one-call state, knowledge work may show a known `n/N`, and a
  real research child uses its graph phases.
- Selecting a work unit changes the run canvas descriptor to
  `{view: "run", runId, taskId}` while retaining the same `run:{runId}` tab and
  a visible Back action; it does not create a second task tab.
- Errors, fallbacks, insufficient evidence, partial result summaries, and
  SSE-to-polling degradation stay visible and expand into sanitized details.
- Terminal run hydration first reads the durable plan/task rows and then
  replays the existing event page through the live reducer, so cancellation or
  reload does not erase the execution story.

Failed server discovery retries five seconds after the preceding attempt has
settled, stopping on success or unmount. A visible notice explains recovery;
expanding it shows the request error. An unsupported capability manifest (404)
keeps its compatibility meaning and does not trigger transport retries.
Terminal research overviews use the stored duration; an absent duration is
shown as unknown rather than a fabricated zero.

Agent memo citations follow the artifact/result reference contract in
[`agent-platform.md`](../architecture/agent-platform.md), and the report body
does not duplicate citation chips above the document. Legacy saved artifacts
are repaired by a small Agent-only display adapter; the shared Markdown
renderer remains unchanged.

The report panel derives one explicit mode from the selected run: empty,
queued, running, completed with report, or completed without report -- one
derived value rather than scattered conditions. Transitions between the live
protocol and the completed report keep the panel frame stable and cross-fade
the inner content, rather than remounting the whole right surface abruptly.
The hide/focus actions stay fixed in the top-right header row in all modes;
completed-run tabs (Preview, Evidence, Export, and `Agent steps`) sit below
that row. The step list is a height-bounded internal log that auto-follows the
newest active event, so the right-hand panel does not grow the whole page; the
archived view is static and readable, without live auto-follow or active-step
animation. The event semantics behind the live protocol are in
[Runs API](../extending/runs-api.md).

## Deployment options

The split-origin path serves the built frontend as static files separately from
the API. It is the exception for a static CDN that cannot proxy, not the
default -- the standard runtime is the packaged Python web gateway described
under *Same-origin serving without Node*. The build steps and the
`VITE_INQTRIX_API_BASE_URL` origin rule are in
[`apps/research-desk/README.md`](../../apps/research-desk/README.md).

## Same-origin serving without Node

The Python web gateway is the standard runtime for the built React bundle. It
keeps the browser on one origin and routes `/api/*`, `/v1/*`, `/health`, and
`/collaboration` to FastAPI.

| Situation | Selection |
|---|---|
| Default Compose, Kubernetes, bare-metal Python, or local production-build verification | Packaged Python gateway |
| Your own reverse proxy in front (nginx, Traefik, HAProxy) | Keep the Python gateway; set `INQTRIX_PUBLIC_BASE_URL`, `INQTRIX_EXTERNAL_SCHEME` and `INQTRIX_TRUSTED_PROXY_HOPS` |
| Static CDN that cannot proxy | Split-origin build with `VITE_INQTRIX_API_BASE_URL`; configure backend CORS |

### Standard: packaged Python web gateway

Pod topology:

```text
+----------------------+            +-----------------------+
|  Frontend pod        |            |  Backend pod          |
|  python:3.12-slim    |   /api/*   |  python:3.12-slim     |
|  inqtrix_web_gateway |   /v1/*    |  uvicorn :5100        |
|  dist/ contents      | ---------> |  python -m inqtrix    |
|  HTTP + WebSocket    |  /health   |                       |
|  proxy :8080         |  /collab-  |                       |
|                      |  oration   |                       |
+--------^-------------+            +-----------------------+
         |
       Browser
       (single origin)
```

[`deploy/docker/Dockerfile.web`](../../deploy/docker/Dockerfile.web) produces
one shared Node build stage and two explicit final targets. The default
`web-python` target contains the built SPA, the packaged
`src/inqtrix_web_gateway`, and dependencies projected from the repository's
single `uv.lock` through the `web-gateway` group. Its runtime contains no Node,
npm, uv, nginx, or API/agent dependency graph. It covers cookie login, SSE
streaming, uploads, the instance probe, the collaboration WebSocket, SPA
fallback, MIME handling, cache policy, forwarded-header trust, and sanitized
guest-link logs.

#### Container quickstart

Build and run the same image used by Compose and Helm:

```bash
docker build --target web-python \
  -f deploy/docker/Dockerfile.web -t inqtrix-web .
docker run --rm -p 8080:8080 --read-only --tmpfs /tmp \
  -e INQTRIX_BACKEND_URL=http://host.docker.internal:5100 \
  -e INQTRIX_PUBLIC_BASE_URL=http://localhost:8080 \
  inqtrix-web
```

For direct TLS in Compose, mount certificate material from outside the
repository through the supplied override; no second image or web service is
created:

```bash
INQTRIX_PUBLIC_BASE_URL=https://desk.example:8080 \
INQTRIX_WEB_TLS_CERTFILE=/absolute/path/tls.crt \
INQTRIX_WEB_TLS_KEYFILE=/absolute/path/tls.key \
podman compose -f deploy/compose/compose.stack.yaml \
  -f deploy/compose/compose.web-tls.yaml \
  --env-file deploy/.env.stack.secrets \
  --env-file deploy/.env.stack \
  up -d --build
```

The override applies that one required public origin to the web gateway, API,
and worker, so guest links, OIDC callbacks, source URLs, forwarded headers,
and WebSocket Origin checks cannot retain a different value from a paired
runtime file. It also disables the plain-HTTP account- and guest-cookie
escape hatches for the API; Direct TLS always restores `Secure` cookies and
the `__Host-` account-cookie contract.

The normal trusted-LAN stack remains HTTP-capable for signed-in users.
Account-less guest links remain disabled there; they require a real HTTPS/WSS
browser origin through direct TLS or a trusted edge terminator.

#### Direct host start

The same package runs directly on hosts where containers are not permitted. It is also the shortest way to verify a production `dist/` locally.

The browser observes the same contract in either packaging:

- Unknown paths outside `/assets/` serve `index.html` (SPA fallback);
  missing files under `/assets/` stay hard 404s.
- `index.html` (and every other non-asset path) is served with
  `Cache-Control: no-cache` so a redeploy is picked up immediately;
  hashed `/assets/` bundles cache immutably for a year.
- Proxied requests replace client-supplied forwarded Host/Proto metadata with
  the configured `INQTRIX_PUBLIC_BASE_URL`, the optional matching scheme
  override, or the gateway connection as the documented fallback.
  `X-Forwarded-For` remains append-only and `X-Real-IP` records the immediate
  peer, so per-client login rate limiting keeps distinct buckets. The raw
  request target is forwarded byte-identically (encoded path segments and
  repeated query keys survive).
- `/collaboration` preserves the public Host, Origin, cookies, raw query, binary
  frames, and upstream close behavior. It never starts or connects directly to
  the Node service.
- An unreachable backend answers `502 Bad Gateway`, with a
  60-second HTTP connect timeout; an unavailable collaboration gateway closes
  the socket visibly rather than degrading to polling or autosave.

The gateway requires a pre-built `dist/`. That build step and both supported
Python installation paths (uv and pip) are in
[Installation](../getting-started/installation.md#built-research-desk-with-the-python-gateway).

CLI arguments override environment variables; environment variables override
documented defaults. Startup validates `dist/index.html`, the backend origin,
and the TLS certificate/key pair before accepting traffic.

#### Gateway environment variables

| Variable | Default | Effect |
|---|---|---|
| `INQTRIX_WEB_ADAPTER` | `python` | Image/Compose integrity sentinel. The Python target accepts only `python`; another target accepts only `nginx`. Operators select the adapter through the documented image target or Compose override, not by changing this value in isolation. |
| `RESEARCH_DESK_HOST` | `127.0.0.1` | uvicorn bind host. Use `0.0.0.0` inside containers. |
| `RESEARCH_DESK_PORT` | `8080` | uvicorn bind port. |
| `RESEARCH_DESK_WORKERS` | `1` | Worker processes. Above 1 uses uvicorn's multiprocess supervisor sharing one listen socket; the upstream pool and body caps apply per worker. |
| `WEB_CONCURRENCY` | unset | Not a second worker setting. A conflicting value is ignored with a warning; use only `RESEARCH_DESK_WORKERS`. |
| `RESEARCH_DESK_SSL_CERTFILE` / `RESEARCH_DESK_SSL_KEYFILE` | unset | Optional TLS termination; both must be set together. `RESEARCH_DESK_SSL_KEYFILE_PASSWORD` decrypts an encrypted key file. |
| `INQTRIX_BACKEND_URL` | `http://localhost:5100` | Origin the gateway proxies `/v1/*`, `/api/*`, `/health`, and `/collaboration` to. |
| `INQTRIX_PUBLIC_BASE_URL` | unset | Explicit browser origin when a trusted reverse proxy terminates TLS before the gateway; pins the forwarded scheme AND host. |
| `INQTRIX_EXTERNAL_SCHEME` | unset | Optional scheme-only override: pins `X-Forwarded-Proto` while the forwarded host follows the request. If `INQTRIX_PUBLIC_BASE_URL` is also set, both schemes must match or startup fails. |
| `INQTRIX_MAX_UPSTREAM_CONNECTIONS` | `512` | Per-worker ceiling for pooled backend connections; each browser tab holds one long-lived SSE stream. Sized against the API's own admission caps: chat and native runs each admit up to 100, and every open event stream holds a connection for the run's whole duration on top of that. Exhaustion is a `503` with a warning naming this variable, never a silent wait. Connections open on demand, so the ceiling costs nothing until the load arrives.  Helm: set via `web.maxUpstreamConnections`; the web pod has no `envFrom`, so `config:` entries never reach it. |
| `INQTRIX_PROXY_MAX_BODY_BYTES` | derived | Explicit request-body cap in bytes. Unset derives `INQTRIX_MAX_FILE_BYTES` + 10 MiB; mirror the backend variable in split-container setups or the gateway warns and uses the packaged default. |
| `INQTRIX_DIST_DIR` | `<repo>/apps/research-desk/dist` | Override when serving a `dist/` from a release artifact path. |
| `INQTRIX_COLLABORATION_MAX_FRAME_BYTES` / `INQTRIX_COLLABORATION_MAX_QUEUED_FRAMES` | `2097152` / `32` | Collaboration relay frame-size and queue-depth limits; keep in sync with the collaboration service. |

If the resolved `dist/` directory or `index.html` does not exist, startup fails
loudly and names the resolved path; run `npm run ui:build` first or point
`INQTRIX_DIST_DIR` at an existing release artifact.

### Backend notes (apply to both paths)

- `INQTRIX_SERVER_CORS_ORIGINS` can stay unset. The browser sees a
  single origin, so cross-origin preflight never happens.
- `INQTRIX_SERVER_API_KEY` continues to work transparently because
  both proxies forward the `Authorization` header verbatim. It does not grant a
  collaboration lease; live editor sessions require cookie auth.

## Runtime connection settings

`VITE_INQTRIX_API_BASE_URL` is a public build variable: it may hold only the API
origin, never credentials; Vite embeds `VITE_*` into the browser bundle.

When `GET /health` reports `auth_required: true`, a full-screen unlock gate
takes a runtime Bearer token, validates it with `GET /v1/runs`, and then sends
it on protected `/v1/*` requests and the SSE stream. It lives in React memory
only, never in `localStorage`, `sessionStorage`, project files, exports, logs,
URLs, or `VITE_*`, so a reload asks again; Settings > Security holds it too.

Settings > Licensing shows the license tag, the version, the usage notices and
the repository, documentation, and license links; the unlock gate carries none
of them. The Settings workspace takes the project name, license and source URL
from the `legal` block of `/health` and falls back to bundled project metadata
when the block is absent.

`/health` also carries an `ai_disclosure` block beside `legal`. See
[AI transparency](ai-transparency.md) for its markers and for what
is deliberately left unmarked.

The unlock gate is a UX guard, not the security boundary; the backend Bearer
dependency stays authoritative. For TLS and the CORS origin allow-list see
[Security model](security-model.md); add a restrictive Content Security
Policy at the hosting/reverse-proxy layer to reduce XSS token-exfiltration risk.

The chat workspace is separate from the research-run lifecycle: it sends
OpenAI-compatible requests to `POST /v1/chat/completions` with
`mode="direct_llm"` and `include_progress=false`. It sends `stack` with the
selected stack whenever stack discovery succeeded, and omits it only when the
server reports no multi-stack support or discovery failed -- then the server's
default stack handles the request. A message sent from the incognito chat adds
`incognito: true`, so the server keeps the turn out of its traces; the thread
itself stays in the browser. An incognito Knowledge Desk ask sends
`incognito: true` with `POST /v1/runs` and no session: its trace carries no
content, and the server deletes the run shortly after it ended, also when the
page is reloaded before the browser could remove it.

`/v1/stacks` is optional for single-stack servers, and the app does not probe
it to find out. It reads `features.multi_stack` from `/v1/capabilities` and
calls `GET /v1/stacks` only when that flag is true. Otherwise stack discovery
is marked unsupported for the page session: stack selection is hidden and the
server default is labelled from `/health`. A 404 from `/v1/capabilities`
itself (an older backend) is swallowed, leaving the same single-stack
presentation.

If a streaming chat request fails before any content arrives, or if the server
answers the streaming path without `text/event-stream` (typical for a buffering
or rewriting proxy), the UI retries the same messages once with `stream=false`.
That fallback is visible: the Streaming toggle switches off and the composer
shows a compact warning.

Project export persists completed runs as Markdown files under `search-history/`.
The YAML frontmatter carries the canonical `events` array for the agent protocol
and the report `references` array for the Evidence tab; there is no parallel
`agent_steps` field. On project load, older files without `kind`, `severity`, or
`references` are normalized so they still render.

## Brand and appearance

New browser profiles start with **Inqtrix Petrol**. Settings → Appearance keeps
**Inqtrix Blue** (`standard`), Slate, Graphite, and Sage available. A saved preset
is kept, both in the browser and in the account preference row. The browser saves
the preset when one is picked or applied from an account or a project, so a
browser that never did either shows Petrol after an upgrade; this includes every
browser of a deployment without per-user sign-in. The account row stores the
preset together with every other preference, so an account that saved any
preference while Inqtrix Blue was the default keeps Blue. A saved account
preference wins on sign-in; the lock screen before sign-in follows the browser.
Light/dark mode, high contrast, and chat bubble colors remain independent.
The fixed Petrol mark uses the appropriate light/dark asset in every preset and
monochrome artwork in high contrast. The lock-screen background follows the
selected preset; high contrast removes its decorative fade.

Runtime marks live in `apps/research-desk/src/brand`, with browser icons in
`public`. Their source is `assets/inqtrix-png-logo-kit-v6`; see its README for
explicit export instructions. Normal Vite builds and both web-image targets
include these files. Docker and Helm need no asset-specific settings or mounts.

Petrol account preferences require migration `0093_petrol_theme`. Upgrade the
API/worker image and web image together through the existing migration workflow;
Helm's migration Job uses the API image and the existing maintenance requirement
for installed-schema upgrades still applies. Preserve operator-provided image
tags/digests. A downgrade maps only saved `petrol` values to `standard` and retains
all other preferences. The upgrade itself changes no saved preference.

For isolated visual checks, the frontend test page
`/browser-tests/fixtures/auth-brand.html` renders the real lock screen without a
backend. It accepts `theme=light|dark`, `themePreset=petrol|standard|slate|graphite|sage`,
`contrastMode=standard|high`, `locale=en|de`, `mode=local|sso|apikey`, and `error` or
`busy` flags. `view=setup` shows owner setup. It is a development fixture, not a production route.

## Related docs

- [Web server mode](webserver-mode.md)
- [Runs API](../extending/runs-api.md)
- [Deploy editor collaboration](editor-collaboration.md)
