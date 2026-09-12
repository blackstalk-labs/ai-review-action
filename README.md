# AI Review Action

AI-powered pull request review focused on production risk — business logic,
security, reliability, and performance, not style or formatting. A drop-in
GitHub Action for any language or stack: point it at a PR diff and a
GitHub secret, get severity-classified findings back as a PR comment.

This action is the reusable review layer extracted from
[`ai-and-system-production-pipeline`](https://github.com/blackstalk/ai-and-system-production-pipeline),
a reference architecture for AI-assisted production delivery. That repo
documents the full pipeline this action is one layer of — worth reading if
you're deciding how to wire this into a merge gate, not just how to call it.

## Why this exists

Deterministic tools (linters, type checkers, dependency scanners) can't
reason about intent: whether an authorization check actually verifies
ownership, whether an exception handler is hiding a payment failure,
whether a "clever" state transition is actually broken. An LLM can — but
only if it's told exactly what to look for and, just as importantly, what
to ignore. This action wraps a prompt tuned to skip style/formatting noise
and focus entirely on the categories deterministic tooling can't cover,
and returns structured, severity-classified findings instead of a wall of
prose.

**It is deliberately not the sole merge gate.** It's one signal among
several — see [Where this fits](#where-this-fits) below.

## Quickstart

```yaml
# .github/workflows/ai-review.yml
name: AI Review
on:
  pull_request:

permissions:
  contents: read
  pull-requests: write

jobs:
  ai-review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0   # required — the action diffs against base-ref

      - uses: blackstalk/ai-review-action@v1
        with:
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          base-ref: origin/${{ github.event.pull_request.base.ref }}
```

Add `ANTHROPIC_API_KEY` as a repo secret (Settings → Secrets and variables →
Actions). Without it, the action skips gracefully and exits 0 — it never
becomes a reason all merges are blocked.

## Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `anthropic-api-key` | No | — | API key. Unset → skip gracefully. |
| `provider` | No | `anthropic` | Reviewer backend. Only `anthropic` is implemented today — see [Adding a provider](#adding-a-provider). |
| `model` | No | `claude-sonnet-5` | Model name. |
| `base-ref` | **Yes** | — | Ref to diff `HEAD` against, e.g. `origin/main`. Requires `fetch-depth: 0` on checkout. |
| `exclude-paths` | No | `''` | Comma/space-separated pathspecs to exclude, e.g. `vendor node_modules`. |
| `fail-on-severity` | No | `CRITICAL,HIGH` | Severities that fail this check. Lower severities are still posted, just non-blocking. |
| `system-prompt-path` | No | bundled `prompts/code-review.md` | Path (in the consuming repo) to a custom reviewer prompt. |
| `post-comment` | No | `true` | Whether to post/update a PR comment with findings. |
| `github-token` | No | workflow's `GITHUB_TOKEN` | Token used to post the comment. |

## Outputs

| Output | Description |
|---|---|
| `findings-file` | Path to the markdown findings file this run wrote. |
| `blocking-count` | Number of findings at or above `fail-on-severity`. |

## What it looks for

Full detail in [`prompts/code-review.md`](prompts/code-review.md) — the
actual system prompt sent to the model, kept in version control so changes
to reviewer behavior are reviewable like any other change.

- **Business logic** — incorrect assumptions, bad state transitions, missing
  validation, race conditions, broken workflows
- **Security** — injection, auth bypass, IDOR, unsafe deserialization,
  secrets exposure, insecure defaults, SSRF, path traversal
- **Reliability** — unhandled exceptions, missing timeouts/retries, resource
  leaks, non-idempotent operations, swallowed failures
- **Performance** — N+1 queries, unbounded loops/workloads, unnecessary
  network calls

It explicitly ignores formatting, naming preference, and anything a linter
or type checker already owns — see "What to ignore" in the prompt. Changes
touching auth, payments, personal data, deletions, migrations, or secrets
are treated as higher severity by default.

Findings are CRITICAL / HIGH / MEDIUM / LOW / INFORMATIONAL, each with file,
line, category, explanation, production impact, and a concrete remediation.

## Examples by stack

This action is language-agnostic — it only ever sees a `git diff`. The
`exclude-paths` input is where stack-specific noise (vendored dependencies,
build output, generated code) gets filtered out before the diff is sent to
the model.

### Python

```yaml
- uses: blackstalk/ai-review-action@v1
  with:
    anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    base-ref: origin/${{ github.event.pull_request.base.ref }}
    exclude-paths: "tests fixtures"
```

### PHP

```yaml
- uses: blackstalk/ai-review-action@v1
  with:
    anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    base-ref: origin/${{ github.event.pull_request.base.ref }}
    exclude-paths: "vendor storage/framework bootstrap/cache"
```

The prompt's security/reliability/business-logic categories (SQL injection,
auth bypass, swallowed exceptions, N+1 queries) apply identically regardless
of language — nothing about the review logic changes per stack, only which
paths get excluded from the diff.

## Where this fits

Deterministic tooling (linters, type checkers, security scanners) and human
approval remain required regardless of what this action finds — see
`docs/adr/001-ai-review-is-not-authoritative.md` in the
[reference architecture repo](https://github.com/blackstalk/ai-and-system-production-pipeline)
for the reasoning. In short: LLM output is probabilistic, not
deterministic — treating it as an unbypassable gate would make production
safety depend on a non-reproducible check. This action fails its own status
check on CRITICAL/HIGH findings, but a human reviewer retains the authority
to merge over a finding they judge to be wrong, the same as with any other
CI check.

## Adding a provider

```python
class Reviewer(Protocol):
    def review(self, diff: str, system_prompt: str) -> str: ...
```

`AnthropicReviewer` in [`ai_review.py`](ai_review.py) is the only
implementation shipped today. To add OpenAI, CodeRabbit, or an in-house
model: implement this one method, wire it into `build_reviewer()`, and add
the provider name to the `provider` input's accepted values — `action.yml`
and the merge-gate logic don't change.

## Local development

```bash
make install   # create a venv, install dev tooling
make test      # run the test suite
make ci        # lint + typecheck + test — what CI runs
```

`ai_review.py` has zero runtime dependencies (stdlib only), so consumers of
this action never need an install step — `dev` extras are for working on
the action itself.

## Versioning

Tagged with semver (`v1.0.0`), with a moving major tag (`v1`) kept in sync
— pin `@v1` to get non-breaking updates automatically, or a full version
for exact reproducibility.

## License

[MIT](LICENSE)
