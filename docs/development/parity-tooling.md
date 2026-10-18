# Parity tooling

> Files: `src/inqtrix/parity/*`, `scripts/parity_runner.py`

## Scope

The `inqtrix-parity` CLI compares a live agent run against a pre-recorded baseline. It runs the canonical question set against a local HTTP server, stores structured artefacts, and produces deterministic report files (JSON + Markdown + CSV) plus an optional LLM-driven diagnostic pass.

## Canonical questions and baselines

- Canonical questions live in `tests/integration/questions.json`. The whole `tests/integration/` tree is git-ignored and has never been versioned, so a fresh clone contains neither the question set nor any baselines: put your own parity assets in place first, otherwise `inqtrix-parity contract` aborts with a `FileNotFoundError` on the missing questions file.
- Baseline artefacts live in `tests/integration/baselines/`: this is the default target of `import-baselines --output-dir` and the default `--baseline-dir` of `compare`. Run artefacts are written to `tests/integration/runs/` and compare reports to `tests/integration/reports/`; both directories are created on first use.
- The parity CLI does not invent baselines; you import them from another repository or re-record them intentionally.

## Commands

```bash
# Validate local parity assets
inqtrix-parity contract

# Import the reference baselines from the litellm repo into Inqtrix
inqtrix-parity import-baselines /path/to/litellm/research-agent/tests/baselines

# Run the canonical suite against a local server in TESTING_MODE
inqtrix-parity run --endpoint http://127.0.0.1:5100

# Compare a saved run against baseline artefacts
inqtrix-parity compare tests/integration/runs/run_YYYY-MM-DD_HH-MM-SS.json \
    --baseline-dir /path/to/baselines

# Optional: add an LLM-driven diagnostic pass
inqtrix-parity compare tests/integration/runs/run_YYYY-MM-DD_HH-MM-SS.json \
    --llm-analysis
```

The `inqtrix-parity` entry point exists only after the project has been installed (editable or not). From an uninstalled clone, run the same commands as `python scripts/parity_runner.py <command>`: the wrapper adds `src/` to `sys.path` itself, so only the third-party dependencies still have to be installed.

## What `compare` produces

Each `compare` run writes three deterministic artefacts into the report directory:

- `report_*.json` — full machine-readable compare result with per-question issues, structured metric checks, and exported top-claim snapshots.
- `report_*.md` — Markdown summary with overview and flagged-check tables, plus claim snapshot sections for quick audits.
- `report_*.csv` — flat `question × metric × status` export for spreadsheets, diffing, and cross-run analysis, including compact baseline/current claim snapshots.

`compare` always performs the deterministic baseline-vs-run check first. `--llm-analysis` adds a second, optional diagnostic layer that uses the configured analysis model to inspect iteration logs, query trajectories, stop logic, and answer quality.

## Configuring the analysis model

`--llm-analysis` auto-creates the default LLM provider from `Settings()`.
By default it uses `REASONING_MODEL`; pass `--analysis-model <model>` to
override the model name for the diagnostic pass. The analysis timeout uses the
effective `REASONING_TIMEOUT` (600 seconds by default) for the single optional
analysis call that summarizes the complete comparison. If that call fails or
times out, the deterministic comparison remains available. The HTTP run client
defaults to the active research-run budget plus the shared 30-second client
margin (3,630 seconds by default), so a valid slow run is not abandoned before
the server's own deadline.

## Minimal local parity run

Start the server in `TESTING_MODE` and fire the canonical suite from a second shell: steps 4 and 5 of the [recommended validation flow](running-tests.md#recommended-validation-flow) carry both commands.

Because the parity run talks to the real configured providers, it is slower and more failure-prone than `pytest` — but it gives a much better end-to-end signal.

## When to re-import baselines

Re-import a baseline when:

- The upstream reference repository publishes a new baseline for the same canonical questions.
- A change in Inqtrix legitimately alters the expected baseline (update the baseline, note the change in the commit, expect reviewer scrutiny).

Never edit baselines in place to "make the test pass" — either the behaviour is an intentional change (document it) or the behaviour is a regression (fix the code).

## Related docs

- [Testing strategy](testing-strategy.md)
- [Running tests](running-tests.md)
- [Iteration log](../observability/iteration-log.md)
- [Library API](../architecture/library-api.md)
