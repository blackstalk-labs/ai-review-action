#!/usr/bin/env python3
"""AI-assisted PR review — the engine behind the `ai-review-action`
GitHub Action.

Reads a unified diff from the consuming repo's git history, sends it to
an LLM alongside a reviewer system prompt, and writes:

  1. A markdown summary suitable for posting as a PR comment.
  2. GitHub Actions outputs (findings-file, blocking-count) so callers
     can branch on the result.

Exits 0 unless one or more findings meet the configured blocking
severity threshold, in which case it exits 1 to fail the calling
workflow step.

Design notes
------------
The `Reviewer` protocol below is the extension point: swapping LLM
providers (Claude, OpenAI, an in-house model) means implementing one
method, not touching action.yml or this file's control flow.
`AnthropicReviewer` is the only implementation shipped here; it depends
on nothing beyond the standard library so this action has zero install
step and works identically on any GitHub-hosted runner.

No API key, no crash
---------------------
If ANTHROPIC_API_KEY is unset, `main()` exits 0 with a clear message
instead of failing the workflow — an AI reviewer being unavailable must
never become a way to block all merges in a consuming repo. Whatever
deterministic checks and human review that repo already has remain the
required gates regardless.

Stack-agnostic by design
-------------------------
This script makes no assumption about the consuming repo's language or
toolchain. It operates purely on `git diff` output and free-form
findings; a PHP repo, a Python repo, and a Go repo all work identically.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Protocol

ACTION_DIR = Path(__file__).resolve().parent
DEFAULT_PROMPT_PATH = ACTION_DIR / "prompts" / "code-review.md"

SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFORMATIONAL"]
DEFAULT_BLOCKING_SEVERITIES = {"CRITICAL", "HIGH"}

# Roughly 150k tokens of diff — comfortably inside current context windows
# while leaving room for the system prompt and the response.
DEFAULT_MAX_DIFF_BYTES = 600_000

# Budget for the full contents of changed files, sent alongside the diff so
# the model can resolve identifiers declared outside the changed hunks.
# Roughly 100k tokens; set to 0 to send the diff alone.
DEFAULT_MAX_CONTEXT_BYTES = 400_000


class Reviewer(Protocol):
    def review(self, diff: str, system_prompt: str, context: str = "") -> str:
        """Return the raw model response (expected to be a JSON array).

        `context` carries the full post-change contents of the changed files.
        A diff alone shows only changed hunks, so anything declared elsewhere
        in the file looks undefined — the reviewer would report a variable as
        nonexistent when its declaration simply sat outside the hunk.
        """
        ...


class AnthropicReviewer:
    """Calls the Anthropic Messages API directly over HTTPS.

    Uses raw urllib rather than the `anthropic` SDK so this action has
    zero third-party dependencies and needs no install step in any
    consuming repo's CI.
    """

    API_URL = "https://api.anthropic.com/v1/messages"
    API_VERSION = "2023-06-01"

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    def review(self, diff: str, system_prompt: str, context: str = "") -> str:
        # File context goes before the diff so the model reads the definitions
        # first and has them available when it reaches the changed hunks.
        parts = []
        if context:
            parts.append(f"{context}\n")
        parts.append(
            "Review this pull request diff. Respond with the JSON array "
            f"described in your instructions, nothing else.\n\n```diff\n{diff}\n```"
        )

        body = {
            "model": self._model,
            # Current models think by default, and that thinking is billed
            # against max_tokens. At 4096 the budget was exhausted before any
            # text block was produced, so the response parsed as empty.
            "max_tokens": 16000,
            "system": system_prompt,
            "messages": [{"role": "user", "content": "\n".join(parts)}],
        }
        request = urllib.request.Request(
            self.API_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": self._api_key,
                "anthropic-version": self.API_VERSION,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Anthropic API error {exc.code}: {detail}") from exc

        text = "".join(
            block.get("text", "")
            for block in payload.get("content", [])
            if block.get("type") == "text"
        )
        if not text.strip():
            # Thinking blocks come back with empty text by default on current
            # models, so a response can be non-empty yet carry no reviewable
            # text. Say which case this is instead of surfacing it downstream
            # as an unexplained JSON parse failure.
            block_types = sorted({b.get("type", "?") for b in payload.get("content", [])})
            raise RuntimeError(
                f"Model returned no text content "
                f"(stop_reason={payload.get('stop_reason')!r}, "
                f"blocks={block_types or 'none'}). "
                "If stop_reason is 'max_tokens', raise max_tokens — thinking is "
                "billed against it."
            )
        return text


def parse_exclude_paths(raw: str) -> list[str]:
    parts = [p.strip() for p in raw.replace(",", " ").split() if p.strip()]
    return [f":!{p}" for p in parts]


def get_diff(base_ref: str, exclude_paths: list[str]) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", "diff", f"{base_ref}...HEAD", "--", ".", *exclude_paths],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def changed_files(base_ref: str, exclude_paths: list[str]) -> list[str]:
    """Paths changed in the diff, excluding ones deleted by it."""
    try:
        result = subprocess.run(  # noqa: S603
            [
                "git",
                "diff",
                "--name-only",
                "--diff-filter=d",  # skip deletions: nothing left to read
                f"{base_ref}...HEAD",
                "--",
                ".",
                *exclude_paths,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return []

    return [line for line in result.stdout.splitlines() if line.strip()]


def build_file_context(base_ref: str, exclude_paths: list[str], max_bytes: int) -> str:
    """Full post-change contents of the changed files, within a byte budget.

    Without this the model sees only the changed hunks and cannot tell a
    genuinely undefined identifier from one declared elsewhere in the same
    file — a false-positive class that does not self-correct on re-review,
    because re-running produces the same truncated view.

    Files are added largest-budget-first in path order and the listing stops
    once the budget is spent, so the model is told which files it did not
    receive rather than silently reasoning from a partial set.
    """
    if max_bytes <= 0:
        return ""

    sections: list[str] = []
    omitted: list[str] = []
    used = 0

    for path in changed_files(base_ref, exclude_paths):
        try:
            content = Path(path).read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue

        block = f"\n--- {path} ---\n{content}\n"
        if used + len(block.encode("utf-8")) > max_bytes:
            omitted.append(path)
            continue

        sections.append(block)
        used += len(block.encode("utf-8"))

    if not sections:
        return ""

    header = (
        "Full current contents of the files this diff touches. Use these to "
        "resolve identifiers: a name declared here but outside the diff's "
        "hunks is defined, not missing."
    )
    if omitted:
        header += (
            "\n\nNot included (context budget): "
            + ", ".join(omitted)
            + ". Do not assert that anything in these files is undefined."
        )

    return f"{header}\n{''.join(sections)}"


def largest_files_in_diff(base_ref: str, exclude_paths: list[str], limit: int = 5) -> str:
    """Markdown list of the files contributing most to the diff.

    Used only to make an over-limit diff actionable — naming the files
    turns "too big" into "exclude these".
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "diff", "--numstat", f"{base_ref}...HEAD", "--", ".", *exclude_paths],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return ""

    rows: list[tuple[int, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        if added == "-" or removed == "-":  # binary
            continue
        rows.append((int(added) + int(removed), path))

    rows.sort(reverse=True)
    return "\n".join(f"- `{path}` ({changed:,} changed lines)" for changed, path in rows[:limit])


def parse_findings(raw_response: str) -> list[dict[str, Any]]:
    text = raw_response.strip()
    # Models sometimes wrap JSON in a fenced code block despite instructions.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[len("json") :]
    findings = json.loads(text)
    if not isinstance(findings, list):
        raise ValueError("Expected the model to return a JSON array of findings.")
    return findings


def render_markdown(findings: list[dict[str, Any]], blocking_severities: set[str]) -> str:
    if not findings:
        return (
            "## AI Review\n\nNo findings. This is one signal among several — "
            "it is not the sole merge gate."
        )

    findings_sorted = sorted(
        findings, key=lambda f: SEVERITY_ORDER.index(f.get("severity", "INFORMATIONAL"))
    )
    lines = ["## AI Review", ""]
    blocking = [f for f in findings_sorted if f.get("severity") in blocking_severities]
    if blocking:
        lines.append(
            f"**{len(blocking)} blocking finding(s)** "
            f"({'/'.join(sorted(blocking_severities))}) — "
            "this check will fail until resolved or explicitly overridden by a human reviewer."
        )
    else:
        lines.append("No blocking findings. Lower-severity items below are informational.")
    lines.append("")

    for finding in findings_sorted:
        severity = finding.get("severity", "INFORMATIONAL")
        severity_emoji = {
            "CRITICAL": "🔴",
            "HIGH": "🟠",
            "MEDIUM": "🟡",
            "LOW": "🔵",
            "INFORMATIONAL": "⚪",
        }
        emoji = severity_emoji.get(severity, "⚪")
        lines.append(
            f"### {emoji} {severity} — {finding.get('category', 'uncategorized')} — "
            f"`{finding.get('file', 'unknown')}:{finding.get('line', '?')}`"
        )
        lines.append(f"**{finding.get('summary', '')}**")
        lines.append("")
        lines.append(finding.get("explanation", ""))
        lines.append("")
        lines.append(f"- **Production impact:** {finding.get('production_impact', 'n/a')}")
        lines.append(f"- **Suggested remediation:** {finding.get('remediation', 'n/a')}")
        lines.append("")

    return "\n".join(lines)


def build_reviewer(provider: str) -> Reviewer | None:
    provider = provider.lower()
    if provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            return None
        model = os.getenv("AI_REVIEW_MODEL", "claude-opus-5")
        return AnthropicReviewer(api_key=api_key, model=model)

    raise NotImplementedError(
        f"provider={provider!r} is not implemented. Implement the Reviewer protocol "
        "(see AnthropicReviewer in ai_review.py) to add OpenAI, CodeRabbit, or another "
        "provider without changing action.yml or the merge-gate logic."
    )


def write_github_output(name: str, value: str) -> None:
    output_file = os.getenv("GITHUB_OUTPUT")
    if not output_file:
        return
    with open(output_file, "a") as f:
        f.write(f"{name}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", default=os.getenv("BASE_REF", "origin/main"))
    parser.add_argument("--output", default="ai-review-comment.md")
    args = parser.parse_args()

    provider = os.getenv("AI_REVIEW_PROVIDER", "anthropic")
    blocking_severities = {
        s.strip().upper()
        for s in os.getenv("FAIL_ON_SEVERITY", "CRITICAL,HIGH").split(",")
        if s.strip()
    } or DEFAULT_BLOCKING_SEVERITIES
    exclude_paths = parse_exclude_paths(os.getenv("EXCLUDE_PATHS", ""))
    prompt_path = Path(os.getenv("SYSTEM_PROMPT_PATH") or DEFAULT_PROMPT_PATH)

    reviewer = build_reviewer(provider)
    if reviewer is None:
        print(
            "ANTHROPIC_API_KEY is not set — skipping AI review. This check is "
            "advisory-only and never the sole merge gate."
        )
        Path(args.output).write_text(
            "## AI Review\n\nSkipped: no API key configured for this run. "
            "Deterministic checks and human review remain required.\n"
        )
        write_github_output("findings-file", args.output)
        write_github_output("blocking-count", "0")
        return 0

    diff = get_diff(args.base_ref, exclude_paths)
    if not diff.strip():
        print("Empty diff — nothing to review.")
        Path(args.output).write_text("## AI Review\n\nNo reviewable changes in this diff.\n")
        write_github_output("findings-file", args.output)
        write_github_output("blocking-count", "0")
        return 0

    # A single generated file (a minified bundle, a lockfile, a vendored
    # dependency) can push the diff past the model's context window. Catch
    # that here with an actionable message instead of letting the API
    # reject it as an opaque 400 mid-run.
    max_diff_bytes = int(os.getenv("MAX_DIFF_BYTES", str(DEFAULT_MAX_DIFF_BYTES)))
    if len(diff.encode("utf-8")) > max_diff_bytes:
        largest = largest_files_in_diff(args.base_ref, exclude_paths)
        print(
            f"Diff is {len(diff.encode('utf-8')):,} bytes, over the "
            f"{max_diff_bytes:,} byte limit — skipping review.",
            file=sys.stderr,
        )
        Path(args.output).write_text(
            "## AI Review\n\n"
            f"Skipped: the diff is {len(diff.encode('utf-8')):,} bytes, over this "
            f"action's {max_diff_bytes:,} byte limit.\n\n"
            "This usually means generated files are reaching the reviewer. Add them "
            "to `exclude-paths` — note that a bare name like `dist` only matches a "
            "**top-level** directory, so nested build output needs `*/dist/*`.\n\n"
            + (f"Largest files in this diff:\n\n{largest}\n\n" if largest else "")
            + "Raise `max-diff-bytes` if the diff is legitimately this large.\n"
        )
        write_github_output("findings-file", args.output)
        write_github_output("blocking-count", "0")
        # Too large to review is a tooling limit, not a signal about the code.
        return 0

    max_context_bytes = int(os.getenv("MAX_CONTEXT_BYTES", str(DEFAULT_MAX_CONTEXT_BYTES)))
    file_context = build_file_context(args.base_ref, exclude_paths, max_context_bytes)
    if file_context:
        print(f"Including {len(file_context.encode('utf-8')):,} bytes of changed-file context.")

    system_prompt = prompt_path.read_text()
    try:
        raw_response = reviewer.review(diff=diff, system_prompt=system_prompt, context=file_context)
    except (RuntimeError, OSError) as exc:
        print(f"Reviewer call failed: {exc}", file=sys.stderr)
        Path(args.output).write_text(
            "## AI Review\n\nThe reviewer failed this run. Treat this as a "
            "tooling failure, not a signal about the code — deterministic checks "
            "and human review still apply.\n\n"
            f"```\n{str(exc)[:1500]}\n```\n"
        )
        write_github_output("findings-file", args.output)
        write_github_output("blocking-count", "0")
        # An unreachable reviewer must never become a way to block all merges.
        return 0

    try:
        findings = parse_findings(raw_response)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"Could not parse AI reviewer output as JSON: {exc}", file=sys.stderr)
        print(f"Raw response:\n{raw_response}", file=sys.stderr)
        Path(args.output).write_text(
            "## AI Review\n\nThe reviewer returned an unparseable response this run. "
            "Treat this as a tooling failure, not a signal about the code — "
            "deterministic checks and human review still apply.\n"
        )
        write_github_output("findings-file", args.output)
        write_github_output("blocking-count", "0")
        # A malformed model response should not block merges by itself.
        return 0

    Path(args.output).write_text(render_markdown(findings, blocking_severities))
    write_github_output("findings-file", args.output)

    blocking = [f for f in findings if f.get("severity") in blocking_severities]
    write_github_output("blocking-count", str(len(blocking)))

    for finding in blocking:
        print(
            f"::error file={finding.get('file')},line={finding.get('line')}::"
            f"[{finding.get('severity')}] {finding.get('summary')}"
        )

    if blocking:
        print(f"{len(blocking)} blocking finding(s). Failing this check.")
        return 1

    print("No blocking findings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
