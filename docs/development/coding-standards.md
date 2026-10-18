# Coding standards

## Scope

The binding coding conventions for the Inqtrix repository. Deviations are called out in code review; new modules are expected to match the conventions out of the gate.

## Language and version

- **Python ≥ 3.11** for the library itself (matches `pyproject.toml`).
- **Type hints** on every new function, method, and class. Prefer `TypedDict`, `Literal`, and `Protocol` where they add meaningful constraints; avoid `Any`.
- **English** for all committed artefacts: code, comments, docstrings, variable names, README, `docs/**`, commit messages, test IDs, log messages, tool and API error texts, and every LLM prompt template (see "Language policy" below). The only non-English artefacts are user-facing UI strings (`translations.ts`), demo content, seed templates and fixtures that deliberately model a German user.

## Language policy

- **Prompts are written in English.** Every prompt template, tool description, system role and structured-output
  instruction in `src/inqtrix/**` is English. Prompts never hard-code an answer language.
- **The answer language is the language of the question.** Each writing path receives its language directive from
  `inqtrix.prompt_language` (one source of truth), phrased in the target language itself. The Research report writer
  opens its system message with the directive; mission and kernel writers open their user message with it.
  Providers preserve the placement chosen by the caller. Contractual headings (summary, open points, sources) come
  from the same module per language; code never searches for a German heading literal.
- **Real umlauts in German text.** User-visible German (UI strings, demo content, seed templates, fixtures, and the
  German answer directive) uses ä ö ü ß, never the ASCII substitutes ae/oe/ue/ss. The web client's
  `germanOrthography.test.ts` checks a reviewed word list in shipped copy, including visible demo metadata.
  Identifiers, URLs, code, math and template parameters retain their spelling. This is a regression guard for
  known misspellings, not a runtime rewrite of user text or model answers; new copy still needs editorial review.
- **Logs, tool errors and API diagnostics are English.** The web client maps known error codes to translated UI
  strings. Localized product explanations, such as a Knowledge grounding rejection in the answer's language,
  remain in their explicit language paths. Interpolated user and provider content is not translated.
  `tests/test_log_language.py` checks owned log format strings with a reviewed German vocabulary, including
  named loggers and inline `logging.getLogger(...)` calls. This is a regression guard, not a complete language
  detector; new prose still requires editorial review. Dynamic templates are admitted only at named producers
  with functional wording checks. Error codes, status codes, parameter names and format arguments stay stable.
- **Exception, stated once:** the knowledge contextualisation prompt writes its output in the language of the
  document it describes; its instructions are English like every other prompt.

## Docstrings

- **Google style** (`Args`, `Returns`, `Raises`, optional `Example`). The reference implementation is `AzureOpenAILLM.__init__` in `src/inqtrix/providers/azure.py`.
- **Semantic, not tautological.** A parameter description explains what the parameter steers, the value range, the default rationale, interactions with other parameters, and consequences of misuse. It does not repeat the type (type hints are the source of truth).
- **Class docstrings** additionally cover use-case (when to pick this class), lifecycle obligations (singleton vs per-run, thread/async safety), and known limitations.
- **Examples** are mandatory for non-trivial constructors and for methods with complex argument combinations. They never replace the `Args` section.

### Forbidden anti-patterns

- Tautological echoes (`foo: The foo.`).
- "See ``Settings`` for details." as the sole description.
- Code-only examples without a textual `Args` block.
- Listing the type in prose (`"str — a string"`); the type hint carries it.
- `See also` links used as a stand-in for an actual description.

## Pydantic models

Every field in a `BaseModel` / `BaseSettings` class has both:

- `Field(description="...")` so `model_json_schema()` exports carry the description. `tests/test_docstring_completeness.py` enforces it on the public settings and result models, and the agent pattern models hand that schema to the provider on every structured LLM call.
- An attribute docstring immediately after the field definition so Pylance / VS Code hover shows the same string.

```python
field: Type = Field(
    default=...,
    alias="ENV_NAME",
    description=(
        "Meaning. Value range. Default rationale. "
        "Interactions with other fields."
    ),
)
"""Meaning. Value range. Default rationale. Interactions with other fields."""
```

The two strings are **word-for-word identical**. Do not summarise, translate, or paraphrase one of them. `model_config` must not set `use_attribute_docstrings=True`.

## Inline comments

- Comments explain *why*, not *what*. The code itself tells the reader *what*.
- No narrative comments (`# increment counter`, `# loop over items`, `# return result`).
- Block comments above module-level constants name their source (provider docs link, spec section, audit log) when the value is not self-explanatory.
- Describe the current technical cause and invariant without referring to an
  internal plan, priority, review, incident, private decision id, branch,
  commit, or the date on which a change was made.
- Dates and versions remain appropriate when executable behaviour or external
  interoperability depends on them, such as protocol versions, schema
  revisions, retention calculations, source provenance, and deterministic
  temporal fixtures.
- No emojis — anywhere.

## Reuse and abstraction boundaries

Use the smallest abstraction that has a demonstrated shared responsibility:

- Search for an existing semantic token, motion contract, type, service, or UI
  primitive before introducing another owner for the same contract.
- Similar appearance alone does not justify sharing. Components with different
  behaviour, lifecycle, or ownership may remain separate.
- When the same semantic role or interaction contract appears independently in
  more than one feature, extend an existing primitive or extract one shared
  implementation instead of copying it.
- Do not create global abstractions for speculative reuse. New shared tokens
  and variants name their purpose and document their intended consumers.
- Prefer deliberate variants over feature-owned forks of a shared primitive.
  If consumers need different contracts, keep them separate rather than
  accumulating unrelated conditionals.

The Research Desk visual roles live in code: the `.t-*` typography roles,
`.icon-*` icon roles and colour tokens in
`apps/research-desk/src/styles/globals.css`, and the control primitives in
`apps/research-desk/src/components/ui/`. Feature code uses a role or a
primitive instead of an ad-hoc size; an ESLint rule in
`apps/research-desk/eslint.config.js` warns on arbitrary `text-[..px]` and
`leading-[..]` values under `src/features/`.

## Logging and secrets

- Use `logging.getLogger("inqtrix")`.
- Route every potentially sensitive message through `sanitize_log_message(...)` from `inqtrix.urls` before logging.
- Every fallback path emits all three of a `log.warning(...)`, a progress event on the user-visible queue, and an iteration-log marker, so no fallback is ever silent.
- Never log raw API keys, tokens, endpoints with embedded credentials, or full prompt bodies.

## Test discipline

- Every new branch in a node, strategy, or provider has a unit test.
- Replay tests use VCR cassettes or `botocore.Stubber`; see [Testing strategy](testing-strategy.md).
- Cassettes are scrubbed before commit (`tests/fixtures/sanitize.py` + protective scan in `tests/replay/test_sanitization.py`).
- When a test changes a logger or any other global state, add an `autouse=True` reset fixture to avoid test-order pollution.
- Do not use `time.sleep` as a blind wait for an asynchronous result; poll the condition against a deadline and fail the test when it is not reached. Short sleeps that pace such a poll loop, separate two timestamps, or let a deliberately shortened expiry elapse are the accepted use. Under `tests/replay/`, an autouse fixture in `tests/replay/conftest.py` replaces `time.sleep` with a no-op for the duration of each test so provider backoff does not slow the suite; it does not reach tests outside that directory.

## Linter / formatter

- `ruff` (rules in `[tool.ruff.lint]` of `pyproject.toml`) is the lint gate. It runs green on `src/` today: `uv run ruff check src/`.
- The enabled set is deliberately small and enforceable: `F` (pyflakes) and `E9` (syntax and IO errors). Both catch defects rather than style. `E9` in particular catches syntax that only parses on a Python newer than `requires-python` promises, which no test on the development interpreter can see.
- `target-version` is **not** set. Ruff derives it from `requires-python`, so the minimum supported Python is stated once.
- Rules beyond this set (`I`, `UP`, `B`, `SIM`, and the pydocstyle `D` family) are not enabled. They would flag a large volume of out-of-scope modules at once, which makes a risk-appropriate review impossible; enabling any of them is its own change with its own review.
- Do not commit code that fails the enabled ruff rules.

## Backwards compatibility

- Removing a public constructor signature, a Settings field, or a `ResearchAgent` method is a deliberate decision, not an automatic one. Redundancy is removed at the root rather than softened with a compatibility layer: no shim module, no alias, no `deprecated=True` flag. Call sites move to the surviving path in the same change, and the tests that pinned the removed behaviour move with them. The removal decision belongs to the operator: candidates are verified against the code and presented together with what breaks, and only then removed.
- Additive changes are always preferred. New fields on `AgentState` must be `NotRequired[...]` and underscore-prefixed when internal.
- Providers must honour the Constructor-First convention: no direct environment-variable reads inside provider modules.

## Git hygiene

- No destructive Git operations without explicit user request (force pushes, history rewrites, hard resets on `main`).
- Never skip pre-commit / hook validation (`--no-verify`, `--no-gpg-sign`) without request.
- One logical change per commit; rebase to squash noise before opening a PR.

## Related docs

- [Contributing](contributing.md)
- [Testing strategy](testing-strategy.md)
- [Docs maintenance](docs-maintenance.md)
