# Profession-agnostic local development

Base: upstream `v0.1.7`, commit `31c9dba72f0edbb2e31888ed60dac03180794ef1`.
Branch: `profession-agnostic`.

## Behavior and compatibility

- Scoring (single and batch), application answers and tailored CV prompts derive
  the candidate's profession and seniority exclusively from `profile.yaml`.
  Job descriptions, CV templates and bullet pools do not establish that identity.
- Scoring also receives `location` and `education`. Existing profiles remain
  valid: no new required fields, renamed keys or schema migrations.
- Regional fit and engagement preferences come from the profile. Missing
  information is unknown, not a default city, occupation or preference for
  permanent employment. Only explicit `criteria.hard_filters` trigger the
  hard-filter score cap; `criteria.soft_filters` guide ranking.
- The legacy two-argument `classify_location` API and its enum remain available,
  but now return `AMBIGUOUS`: a job's location alone cannot establish eligibility.
  The processing service no longer archives jobs using a fixed home city.
  Existing archived jobs and historical scores are not changed automatically.
- Profiles that relied on the original author's implicit location or contract
  preferences must state those preferences in their own criteria. This is an
  intentional behavioral correction, not a data-format incompatibility.
- Gupy receives the supplied keywords unchanged (empty means no occupation is
  injected). Remotive uses its unfiltered jobs endpoint. Both remain opt-in.
  Enabling these sources without keywords can return more jobs than before.
- CV output keys, including `technical_expertise` and `open_source`, are kept.
  Open-source contributions are optional; an empty pool does not introduce a
  corresponding heading into the prompt. There is no new portfolio schema.
- Logs now honor `MOONLIGHTER_HOME`, including in isolated tests. No existing
  personal configuration needs to be edited for this fix.

## Local environment on Windows

Use Python 3.14 and the checked-in `uv.lock`. From the repository directory:

```powershell
$env:UV_CACHE_DIR = Join-Path (Split-Path $PWD) '.uv-cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path (Split-Path $PWD) '.python'
uv sync --all-packages --all-extras --locked --python 3.14 --managed-python
$env:PYTHONUTF8 = '1'
.\.venv\Scripts\python.exe -m pytest -m 'not e2e' --cov-report=term-missing
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\mypy.exe --package moonlighter.core --package moonlighter.discovery --package moonlighter.application --package moonlighter.tracking --package moonlighter.server --package moonlighter.startup --package moonlighter.views --package moonlighter._tool_logging --package moonlighter.init
.\.venv\Scripts\mypy.exe scripts/
.\.venv\Scripts\python.exe scripts\check_version_lockstep.py
```

All five Moonlighter packages are editable workspace installations in `.venv`.
Do not use `uvx moonlighter` for this checkout: that resolves the published package.
The local server entry point is `.venv\Scripts\moonlighter.exe`; invoking it can
initialize application state. No existing MCP registration is changed by setup.
Switch an actual client to this absolute executable path only when ready to use
the local version, preserving its previous registration for rollback.

The test suite redirects `MOONLIGHTER_HOME` before collecting tests and uses
mock services. Real browser E2E is excluded. UTF-8 mode is needed for the upstream
fixtures on Windows. Permission tests verify requested POSIX modes everywhere
and actual mode bits only on POSIX: this does not certify Windows ACL protection.
Snapshot tests pin terminal rendering and normalize only their synthetic paths.

## Validation limits

Regression tests inspect the actual prompts sent to mock LLM callers and exercise
processing/persistence with synthetic jobs. They verify data flow and instructions,
not the compliance of a live language model. No scan, live LLM evaluation, Gmail
connection, LinkedIn connection or browser session is required or performed.

Dependency versions and package versions remain at the upstream lockfile values;
the Git base and branch identify this local variant. No push or PR is required
to review the working-tree diff.
