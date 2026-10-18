# Contributing

## Scope

How to set up a development environment, propose a change, and keep it aligned with the project's coding standards. Detailed testing guidance lives in [Testing strategy](testing-strategy.md) and [Running tests](running-tests.md); release mechanics live in [Release process](release-process.md).

## Environment setup

Inqtrix targets Python ≥ 3.11 and uses a `src/` layout, so an editable install with the `[dev]` extra is the recommended workflow — the test suite needs that extra.

See [Installation](../getting-started/installation.md) for the full bootstrap.

## Change workflow

1. **Discuss before coding** when the change touches a provider interface, strategy contract, or HTTP surface. Small behavioural bugs can go straight to a PR.
2. **Start from a clean branch.** Never commit to `main` directly.
3. **Follow the coding standards.** See [Coding standards](coding-standards.md) for Python version, docstring format, logging rules, and type hints.
4. **Write tests.** Every new branch in a node, strategy, or provider gets a unit test. Mocks are preferred over real network calls; see [Testing strategy](testing-strategy.md) for the four-layer pyramid.
5. **Keep the docs honest.** Change docstrings and `docs/**` pages together with the code. Link-check locally before pushing.
6. **Do not commit secrets.** `.env`, credentials, cassettes with real keys — never. The sanitization helpers in `tests/fixtures/sanitize.py` and the protective scan in `tests/replay/test_sanitization.py` exist to catch mistakes.
7. **Sign off every commit.** Use `Signed-off-by: Your Name <your.email@example.com>` so the contribution has a clear Developer Certificate of Origin trail.

## Commit and PR conventions

- Commit subject line in English, imperative mood, ≤ 72 characters.
- Body paragraph when the diff is non-trivial — explain motivation, not mechanics.
- One focused change per PR; do not bundle refactors with feature additions unless the refactor is required by the feature.
- If the change touches an architectural contract, document the public behaviour in `docs/**` and keep the maintainer memory in sync locally.
- No emojis in code, logs, docs, or commit messages. [Coding standards](coding-standards.md) states this rule without an exception.

## Security and legal

- Never commit real API keys, cassette files with live data, or customer-identifying information.
- The project is explicitly experimental; do not add marketing claims to documentation.
- The Apache License, Version 2.0 (`Apache-2.0`) applies to all contributions; by opening a PR you confirm you have the right to license the content under it.

## Related docs

- [Root contribution notice](../../CONTRIBUTING.md)
- [Coding standards](coding-standards.md)
- [Testing strategy](testing-strategy.md)
- [Docs maintenance](docs-maintenance.md)
