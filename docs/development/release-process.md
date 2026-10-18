# Release process

## Scope

The release process is maintainer-owned. This page exists so contributors know what to expect and what they can prepare, not to prescribe a specific cadence.

## Today's state

The repository is marked experimental and is on release line `0.2.0` (dev designation `0.2.0.10`), a placeholder. The version is defined once as `__version__` in `src/inqtrix/__init__.py`; `pyproject.toml` derives the package version from it (Hatchling dynamic version). No version has been tagged or published yet. A formal release process — signing, PyPI publication, GitHub Releases, change categorisation — is a dedicated follow-up task.

## Automation status

Source-controlled GitHub Actions automation is intentionally disabled. There
are no active workflow YAML files under `.github/workflows`; the previous CI
and image-release definitions are retained byte-for-byte as non-executable
audit snapshots in
[`docs/development/archived-github-actions/`](archived-github-actions/README.md).

Those snapshots are audit evidence, not a restoration path: what reactivating
them requires, and what this source-tree status does not assert, is set out in
the [archived workflows README](archived-github-actions/README.md).

## Bumping the version

Two values live in [`src/inqtrix/__init__.py`](../../src/inqtrix/__init__.py), and the difference between them matters:

- **`__version__`** is the **release line**, always three segments (`major.minor.patch`). `pyproject.toml` declares `dynamic = ["version"]` with a `[tool.hatch.version]` source pointing at that line, so Hatchling derives the package version and the wheel metadata from it. Three other things are pinned to it and break if it moves: `apps/research-desk/package.json` (npm accepts only SemVer), the `_inqtrix_version` stamp in every `tests/eval/baselines/*.json` (enforced by `tests/verification/repository-hygiene.test.ts`, so moving it declares those measured baselines re-validated), and a literal in `tests/verification/orchestrator.test.ts`.
- **`__display_version__`** is the **dev designation inside that line** — a fourth segment naming the current dev milestone. It is what `/health` reports and what the app shows in Settings → Licensing. It moves freely every dev milestone, which is precisely why it is separate: the release line stays put and nothing downstream is disturbed. `tests/test_routes.py` pins that it starts with `__version__`.

Verify with:

```bash
# uv
uv run python -c "import inqtrix; print(inqtrix.__version__, inqtrix.__display_version__)"
uv run python -c "from importlib.metadata import version; print(version('inqtrix'))"

# or, after `python -m pip install -e .`
python -c "import inqtrix; print(inqtrix.__version__, inqtrix.__display_version__)"
python -c "from importlib.metadata import version; print(version('inqtrix'))"
```

**A dev milestone** moves only `__display_version__`. Nothing else needs to change: `/health` and the SPA read it directly (`apps/research-desk/vite.config.ts` parses it out of `src/inqtrix/__init__.py`, and `deploy/docker/Dockerfile.web` copies that file into the SPA build stage so the container build can too).

**A release-line bump** additionally moves `__version__`, and these surfaces cannot derive it, so they are changed by hand in the same commit:

1. **`src/inqtrix/__init__.py`**: `__version__` itself. Run `uv sync --all-extras --reinstall-package inqtrix` afterwards — a plain `uv sync` neither refreshes the editable install's metadata on a version-only edit (`importlib.metadata` keeps reporting the old number) nor keeps the extras, and it prunes `pytest` out of the environment so `uv run pytest` silently falls back to whatever `pytest` is on `PATH`. `uv.lock` never records the project's own version, so it shows no diff.
2. **`README.md`**: the static `Version X.Y.Z` shields.io badge (markdown, with no CI to derive it).
3. **`apps/research-desk/package.json`**: the React app's `version`, which mirrors `__version__` and must stay valid SemVer — `npm ci` fails on a four-segment workspace version, so `__display_version__` never goes here. The app does not read it either way.
4. **`tests/eval/baselines/*.json`**: the `_inqtrix_version` stamp on every measured baseline. Re-stamping asserts those numbers still hold for the new release line, so re-run the evals rather than editing the field on its own.
5. **`tests/verification/orchestrator.test.ts`**: the literal release line asserted on the verification report.
6. **Docs prose** that names the number: this page's *Today's state*, the version markers in [`docs/getting-started/overview.md`](../getting-started/overview.md) and [`docs/deployment/webserver-mode.md`](../deployment/webserver-mode.md), and the example image tags in [`docs/deployment/kubernetes.md`](../deployment/kubernetes.md) and [`docs/deployment/editor-collaboration.md`](../deployment/editor-collaboration.md). `git grep -n '0\.2\.0' -- docs README.md` finds them; the `since v0.2.0.8` markers stay, because they track the dev designation rather than the release line.
7. **`deploy/helm/inqtrix/Chart.yaml`**: `appVersion`, which the chart renders as the default first-party image tag whenever `image.<component>.tag` is left empty (`templates/_helpers.tpl`). Nothing compares it to `__version__`, so leaving it behind silently ships a chart whose default tag still points at the previous release line. Changing it is a chart edit, so bump the chart `version` in the same file and update `_CHART_CONTRACT_DIGEST`, the pin in `test_chart_version_tracks_chart_contract_changes`, and the rendered `0.2.0` image tags asserted in `tests/test_helm_chart.py`.

There is currently no active source-controlled CI or release pipeline, so the
version is bumped manually at milestones.

## What contributors can do

- Write PR descriptions so that a future changelog entry can be assembled quickly (motivation, user-facing change, migration notes if any).
- When a contract changes (provider method signature, Settings field name, HTTP body schema), call it out explicitly in the PR description so the future release note can flag it.

## What a maintainer-driven release step would look like (indicative)

1. Pick the new version number following SemVer (`0.x.y` until the API stabilises; `1.y.z` once semver applies strictly).
2. Bump the version (see [Bumping the version](#bumping-the-version)): edit `__version__` and `__display_version__` in `src/inqtrix/__init__.py`, run `uv sync --all-extras --reinstall-package inqtrix`, then work through the release-line list on that section — README badge, `apps/research-desk/package.json`, eval baselines, the orchestrator literal, and the docs prose.
3. Tag the commit with `vX.Y.Z`, push the tag.
4. Create a GitHub Release pointing at the tag and write the release notes from the commits since the previous tag.
5. Publish to PyPI if applicable (`uv build`, inspection, `uv publish`).
6. Refresh and verify third-party notices before packaging distributable assets:
   ```bash
   # Locked uv release environment
   uv sync --all-extras
   uv run python scripts/generate_third_party_notices.py
   uv run python scripts/generate_third_party_notices.py --check

   # The generator itself also runs in a standard pip environment:
   python -m pip install -e ".[dev]"
   python scripts/generate_third_party_notices.py
   python scripts/generate_third_party_notices.py --check
   ```

   `uv sync` is what the generator needs, because the Python notices read
   installed distribution metadata alongside `uv.lock`; no npm install is
   required, because `package-lock.json` and the four workspace
   `package.json` manifests are the sole JavaScript sources. The uv path
   remains authoritative for refreshing `uv.lock`; the plain Python
   commands document that the project scripts do not require uv as their
   runtime.

Automated release publication remains disabled and is explicitly out of scope
until the reactivation review described above has been completed.

## Related docs

- [Contributing](contributing.md)
