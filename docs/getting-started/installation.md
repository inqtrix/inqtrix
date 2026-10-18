# Installation

> Files: `pyproject.toml`, `package.json`, `apps/research-desk/package.json`

## Scope

How to install Inqtrix for local development and for consumer-style use, including the `src/` layout gotcha, the optional extras, and the frontend toolchain.

## Requirements

- Python 3.11 or newer.
- A package manager: [`uv`](https://github.com/astral-sh/uv) is recommended; `conda` and plain `pip` also work.
- Credentials for at least one LLM and one search provider if you intend to run real research.
- For the Research Desk UI only: Node.js 22.12+ with npm 10.9+.

## From a fresh clone

Create a dedicated Python 3.11 environment first:

```bash
git clone https://github.com/inqtrix/inqtrix.git
cd inqtrix

# Option A: uv (recommended)
uv sync --extra dev
source .venv/bin/activate

# Option B: standard Python and pip
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"

# Option C: conda with pip
conda create -n inqtrix python=3.11
conda activate inqtrix
python -m pip install -e ".[dev]"
```

Editable install is the recommended workflow for local development and testing:

- `python -m pip install -e .` — editable install for normal local use.
- `python -m pip install -e ".[dev]"` — editable install plus test dependencies.
- `uv sync --extra dev` — equivalent for `uv` users.

Code changes under `src/inqtrix/` are picked up immediately without re-installing after every edit.

## Optional extras

Two platform backends pull optional dependencies; install them only when you use the matching env switch (see [Platform components](platform-components.md)):

| Extra | Enables | Env switch |
|-------|---------|------------|
| `knowledge-qdrant` | Qdrant vector store with hybrid (dense + BM25) retrieval | `INQTRIX_VECTOR_BACKEND=qdrant` |
| `queue-valkey` | Valkey-Streams job queue for durable run dispatch | `INQTRIX_QUEUE_BACKEND=valkey` |

```bash
uv sync --extra knowledge-qdrant --extra queue-valkey
# pip equivalent:
python -m pip install -e ".[knowledge-qdrant,queue-valkey]"
```

## Frontend toolchain (optional)

The Research Desk UI under `apps/research-desk/` needs Node.js 22.12+. npm and
the root `package-lock.json` are the sole supported JavaScript install path:

```bash
npm ci                                      # from the repository root
```

See [`apps/research-desk/README.md`](../../apps/research-desk/README.md) for the
npm workspace build commands.

## Built Research Desk with the Python gateway

Build the frontend once, then serve the resulting `dist/` directory through
the default Python web gateway:

```bash
npm run ui:build

# uv
uv sync
uv run python -m inqtrix_web_gateway \
  --dist-dir apps/research-desk/dist \
  --backend-url http://127.0.0.1:5100

# or, after `python -m pip install -e .`
python -m inqtrix_web_gateway \
  --dist-dir apps/research-desk/dist \
  --backend-url http://127.0.0.1:5100
```

## Optional deployment CLI

`inqtrix-deploy` is a convenience frontend for the canonical Compose stack.
It does not replace raw `docker compose` / `podman compose`, infer a provider,
or maintain separate configuration.

```bash
# uv
uv run inqtrix-deploy --help

# Standard Python/pip, console entry point
python -m pip install -e .
inqtrix-deploy --help

# Same pip installation without relying on a scripts directory in PATH
python -m inqtrix.deploy --help
```

Raw Compose and CLI counterparts for start, stop, status, logs, maintenance,
and named environment pairs are documented together in
[Runbooks](../deployment/runbooks.md).

## The `src/` layout caveat

The repository uses a `src/` layout, so a plain clone is not importable by default. In other words: being inside the project directory is not enough on its own for `import inqtrix`, `python -m inqtrix`, or the `inqtrix-parity` entry point to work reliably. Installing the project in editable mode links the environment to your working tree, which is exactly what you want while developing.

If you only want a quick experiment without installing, you can set `PYTHONPATH=src` manually, but that is a temporary workaround rather than the recommended setup.

## First local check

This is the fastest offline regression check after cloning. It runs the local `pytest` suite only, does **not** call real model or search providers, and does not require API keys.

```bash
# uv
uv run pytest tests/ -v

# standard Python/pip environment
python -m pytest tests/ -v
```

Use `uv run pytest tests/ --collect-only -q`, or
`python -m pytest tests/ --collect-only -q` in the pip-installed environment,
when you need the exact current count; the suite grows as provider and server
coverage expands.

## Consumer-style install (non-editable)

For a consumer-style install outside active development, use a normal non-editable install instead:

```bash
python -m pip install .

# Editable install without test extras
python -m pip install -e .

# Editable install with test extras
python -m pip install -e ".[dev]"
```

## Next steps

- [First research run](first-research-run.md) — run a live question against your own providers.
- [Platform components](platform-components.md) — add Postgres, object store, Qdrant, workers, OIDC.
- [Library mode](../deployment/library-mode.md) — embed in your own script.
- [Web server mode](../deployment/webserver-mode.md) — run as an HTTP service.

## Related docs

- [Overview](overview.md)
- [Running tests](../development/running-tests.md)
- [Providers overview](../configuration/providers.md)
