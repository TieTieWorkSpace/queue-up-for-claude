---
abstract: "Test: pytest tests/. Lint: pyflakes src/queue_worker/. Install:
           pip install -e \".[dev]\". Smoke: queue-worker --help. No browser
           or playwright needed."
---

# How to do things

## Activate venv

```bash
source .venv/bin/activate
```

Every command below assumes the venv is activated.

## Install / re-install

```bash
pip install -e ".[dev]"
```

No `playwright install chromium` — the project no longer uses Playwright.
First-time install also has no Chrome dependency.

## Run tests

```bash
python -m pytest tests/
# or with concise output:
python -m pytest tests/ --tb=line
```

132 tests as of the most recent commit. Single-file run:

```bash
python -m pytest tests/test_usage_check_http.py -v
```

## Lint

```bash
python -m pyflakes src/queue_worker/
```

Should produce zero output. If it doesn't, fix every warning before committing —
unused imports and pointless f-strings are common ones.

## Smoke test the CLI

```bash
queue-worker --help
queue-worker run --help
TMP=$(mktemp -d) && queue-worker init "$TMP" && queue-worker ls && rm -rf "$TMP" queue/ logs/ state/
```

## Run the dashboard

```bash
queue-worker-web
# open http://localhost:51002
```

Includes the embedded background runner thread. Don't also run `queue-worker run`
against the same queue dir.

## Trigger a manual usage check

```bash
curl -X POST http://localhost:51002/api/check-usage
```

Or click "Check usage now" in the dashboard.

## Codex code review (focused, no skill ceremony)

```bash
codex exec "<prompt>" \
  -C "$(pwd)" \
  -s read-only \
  -c 'model_reasoning_effort="medium"' < /dev/null
```

Use `medium` for consult-style reviews; `high` for diff-bounded reviews where
thoroughness matters. Pass `--enable web_search_cached` if the prompt benefits
from doc lookups. The bare `codex exec` call is way faster than going through
the `/codex` Skill (which fires interactive prompts).

## Secrets scan

```bash
gitleaks detect --source . --no-git --verbose
```

Run before any push if you touched config / docs / templates. Test fixtures
intentionally use fake `sk-ant-test-fake-...` strings; gitleaks correctly
skips them by length/entropy.

## Git workflow

```bash
git status --short
git diff HEAD --stat
# stage by name (not -A) when there's any chance of incidental files
git add path/to/file ...
git commit -m "..."
# the harness blocks pushes to main; confirm with the user before pushing
git push
```

The remote uses SSH (`git@github.com:TieTieWorkSpace/queue-up-for-claude.git`),
not HTTPS — the previous HTTPS gh-cli token expired.

## Sanity-check after a usage_check refactor

```bash
# uninstall playwright to confirm the codebase truly doesn't need it:
pip uninstall playwright -y
python -c "import queue_worker.usage_check, queue_worker.usage_check_http; print('ok')"
python -m pytest tests/
```
