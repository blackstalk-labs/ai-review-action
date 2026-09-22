from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import ai_review


def test_parse_findings_handles_plain_json_array() -> None:
    findings = ai_review.parse_findings('[{"file": "a.py", "severity": "HIGH"}]')

    assert findings == [{"file": "a.py", "severity": "HIGH"}]


def test_parse_findings_handles_fenced_json_block() -> None:
    raw = '```json\n[{"file": "a.py", "severity": "CRITICAL"}]\n```'

    findings = ai_review.parse_findings(raw)

    assert findings == [{"file": "a.py", "severity": "CRITICAL"}]


def test_parse_findings_rejects_non_array_json() -> None:
    with pytest.raises(ValueError, match="JSON array"):
        ai_review.parse_findings('{"not": "a list"}')


def test_parse_findings_empty_array_means_no_findings() -> None:
    assert ai_review.parse_findings("[]") == []


def test_parse_exclude_paths_handles_commas_and_spaces() -> None:
    result = ai_review.parse_exclude_paths("vendor, node_modules examples")

    assert result == [":!vendor", ":!node_modules", ":!examples"]


def test_parse_exclude_paths_empty_string_yields_no_excludes() -> None:
    assert ai_review.parse_exclude_paths("") == []


def test_render_markdown_with_no_findings() -> None:
    md = ai_review.render_markdown([], {"CRITICAL", "HIGH"})

    assert "No findings" in md


def test_render_markdown_flags_blocking_findings() -> None:
    findings = [
        {
            "file": "app.py",
            "line": 10,
            "severity": "CRITICAL",
            "category": "security",
            "summary": "SQL injection",
            "explanation": "explanation text",
            "production_impact": "data breach",
            "remediation": "use parameterized queries",
        },
        {
            "file": "app.py",
            "line": 20,
            "severity": "LOW",
            "category": "performance",
            "summary": "minor inefficiency",
            "explanation": "explanation text",
            "production_impact": "negligible",
            "remediation": "optional cleanup",
        },
    ]

    md = ai_review.render_markdown(findings, {"CRITICAL", "HIGH"})

    assert "1 blocking finding" in md
    assert "SQL injection" in md
    assert "minor inefficiency" in md


def test_render_markdown_respects_custom_blocking_severities() -> None:
    findings = [{"file": "a.py", "line": 1, "severity": "MEDIUM", "summary": "s"}]

    md_default = ai_review.render_markdown(findings, {"CRITICAL", "HIGH"})
    md_strict = ai_review.render_markdown(findings, {"CRITICAL", "HIGH", "MEDIUM"})

    assert "No blocking findings" in md_default
    assert "1 blocking finding(s)" in md_strict


def test_build_reviewer_returns_none_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert ai_review.build_reviewer("anthropic") is None


def test_build_reviewer_returns_anthropic_reviewer_with_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    reviewer = ai_review.build_reviewer("anthropic")

    assert isinstance(reviewer, ai_review.AnthropicReviewer)


def test_build_reviewer_raises_for_unknown_provider() -> None:
    with pytest.raises(NotImplementedError, match="openai"):
        ai_review.build_reviewer("openai")


def test_write_github_output_appends_to_file(tmp_path: Path) -> None:
    output_file = tmp_path / "github_output"
    output_file.write_text("")
    os.environ["GITHUB_OUTPUT"] = str(output_file)
    try:
        ai_review.write_github_output("blocking-count", "3")
        ai_review.write_github_output("findings-file", "ai-review-comment.md")
    finally:
        del os.environ["GITHUB_OUTPUT"]

    content = output_file.read_text()
    assert "blocking-count=3" in content
    assert "findings-file=ai-review-comment.md" in content


def test_write_github_output_noop_without_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    # Should not raise even though there's nowhere to write.
    ai_review.write_github_output("blocking-count", "0")


def test_default_prompt_file_exists_and_is_nonempty() -> None:
    assert ai_review.DEFAULT_PROMPT_PATH.is_file()
    assert len(ai_review.DEFAULT_PROMPT_PATH.read_text()) > 100


def test_default_prompt_output_schema_matches_finding_fields() -> None:
    prompt_text = ai_review.DEFAULT_PROMPT_PATH.read_text()
    for field in ["file", "line", "severity", "category", "summary", "explanation"]:
        assert field in prompt_text


# --- AnthropicReviewer (network mocked) --------------------------------------


def test_anthropic_reviewer_returns_concatenated_text_blocks() -> None:
    fake_response = MagicMock()
    fake_response.read.return_value = json.dumps(
        {"content": [{"type": "text", "text": "[]"}]}
    ).encode("utf-8")
    fake_response.__enter__.return_value = fake_response
    fake_response.__exit__.return_value = False

    with patch("ai_review.urllib.request.urlopen", return_value=fake_response) as mock_open:
        reviewer = ai_review.AnthropicReviewer(api_key="test-key", model="claude-sonnet-5")
        result = reviewer.review(diff="diff --git a b", system_prompt="be a reviewer")

    assert result == "[]"
    mock_open.assert_called_once()


def test_anthropic_reviewer_raises_runtime_error_on_http_error() -> None:
    import urllib.error

    http_error = urllib.error.HTTPError(
        url="https://api.anthropic.com/v1/messages",
        code=401,
        msg="Unauthorized",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )
    http_error.read = lambda: b'{"error": "invalid api key"}'  # type: ignore[method-assign]

    with patch("ai_review.urllib.request.urlopen", side_effect=http_error):
        reviewer = ai_review.AnthropicReviewer(api_key="bad-key", model="claude-sonnet-5")
        with pytest.raises(RuntimeError, match="401"):
            reviewer.review(diff="diff", system_prompt="prompt")


# --- get_diff (real temporary git repo) --------------------------------------


def _init_git_repo(path: Path) -> str:
    run = lambda *args: subprocess.run(  # noqa: E731
        list(args), cwd=path, check=True, capture_output=True, text=True
    )
    run("git", "init", "-q")
    run("git", "config", "user.email", "test@example.com")
    run("git", "config", "user.name", "Test")
    (path / "file.txt").write_text("original\n")
    run("git", "add", ".")
    run("git", "commit", "-q", "-m", "base")
    return run("git", "rev-parse", "HEAD").stdout.strip()


def test_get_diff_returns_empty_string_when_nothing_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_sha = _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert ai_review.get_diff(base_sha, []) == ""


def test_get_diff_includes_changes_since_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_sha = _init_git_repo(tmp_path)
    (tmp_path / "file.txt").write_text("changed\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "change"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)

    diff = ai_review.get_diff(base_sha, [])

    assert "changed" in diff


def test_get_diff_respects_exclude_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base_sha = _init_git_repo(tmp_path)
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "lib.txt").write_text("vendored\n")
    (tmp_path / "file.txt").write_text("changed\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "change"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)

    diff = ai_review.get_diff(base_sha, [":!vendor"])

    assert "changed" in diff
    assert "vendored" not in diff


# --- main() end-to-end, reviewer stubbed --------------------------------------


class _StubReviewer:
    def __init__(self, response: str) -> None:
        self._response = response

    def review(self, diff: str, system_prompt: str) -> str:
        return self._response


def _run_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base_sha: str,
    argv_extra: list[str] | None = None,
) -> tuple[int, Path, Path]:
    output_path = tmp_path / "out.md"
    github_output = tmp_path / "github_output"
    github_output.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.chdir(tmp_path)
    argv = ["ai_review.py", "--base-ref", base_sha, "--output", str(output_path)]
    monkeypatch.setattr("sys.argv", argv + (argv_extra or []))
    exit_code = ai_review.main()
    return exit_code, output_path, github_output


def test_main_skips_gracefully_without_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    base_sha = _init_git_repo(tmp_path)

    exit_code, output_path, github_output = _run_main(tmp_path, monkeypatch, base_sha)

    assert exit_code == 0
    assert "Skipped" in output_path.read_text()
    assert "blocking-count=0" in github_output.read_text()


def test_main_returns_zero_on_empty_diff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base_sha = _init_git_repo(tmp_path)
    monkeypatch.setattr(ai_review, "build_reviewer", lambda provider: _StubReviewer("[]"))

    exit_code, output_path, _ = _run_main(tmp_path, monkeypatch, base_sha)

    assert exit_code == 0
    assert "No reviewable changes" in output_path.read_text()


def test_main_returns_one_when_blocking_findings_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_sha = _init_git_repo(tmp_path)
    (tmp_path / "file.txt").write_text("changed\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "change"], cwd=tmp_path, check=True)

    finding = json.dumps(
        [{"file": "file.txt", "line": 1, "severity": "CRITICAL", "summary": "bad"}]
    )
    monkeypatch.setattr(ai_review, "build_reviewer", lambda provider: _StubReviewer(finding))

    exit_code, output_path, github_output = _run_main(tmp_path, monkeypatch, base_sha)

    assert exit_code == 1
    assert "blocking finding" in output_path.read_text()
    assert "blocking-count=1" in github_output.read_text()


def test_main_returns_zero_when_only_low_severity_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_sha = _init_git_repo(tmp_path)
    (tmp_path / "file.txt").write_text("changed\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "change"], cwd=tmp_path, check=True)

    finding = json.dumps([{"file": "file.txt", "line": 1, "severity": "LOW", "summary": "minor"}])
    monkeypatch.setattr(ai_review, "build_reviewer", lambda provider: _StubReviewer(finding))

    exit_code, _, github_output = _run_main(tmp_path, monkeypatch, base_sha)

    assert exit_code == 0
    assert "blocking-count=0" in github_output.read_text()


def test_main_returns_zero_on_unparseable_model_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_sha = _init_git_repo(tmp_path)
    (tmp_path / "file.txt").write_text("changed\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "change"], cwd=tmp_path, check=True)

    monkeypatch.setattr(
        ai_review, "build_reviewer", lambda provider: _StubReviewer("not json at all")
    )

    exit_code, output_path, _ = _run_main(tmp_path, monkeypatch, base_sha)

    assert exit_code == 0
    assert "unparseable" in output_path.read_text()


# --- oversized diff and reviewer failures ---------------------------------


class _ExplodingReviewer:
    """Stands in for the Anthropic API rejecting an over-long prompt."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def review(self, diff: str, system_prompt: str) -> str:
        raise self._exc


def _commit_change(tmp_path: Path, content: str) -> None:
    (tmp_path / "file.txt").write_text(content)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "change"], cwd=tmp_path, check=True)


def test_main_skips_when_diff_exceeds_max_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_sha = _init_git_repo(tmp_path)
    _commit_change(tmp_path, "x\n" * 20000)
    monkeypatch.setenv("MAX_DIFF_BYTES", "1000")
    # Would raise if the guard failed to short-circuit before the API call.
    monkeypatch.setattr(
        ai_review,
        "build_reviewer",
        lambda provider: _ExplodingReviewer(AssertionError("reviewer must not be called")),
    )

    exit_code, output_path, github_output = _run_main(tmp_path, monkeypatch, base_sha)

    body = output_path.read_text()
    assert exit_code == 0, "an over-large diff is a tooling limit, not a merge blocker"
    assert "over this action's" in body
    assert "exclude-paths" in body, "message must say how to fix it"
    assert "blocking-count=0" in github_output.read_text()


def test_oversize_message_names_the_largest_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_sha = _init_git_repo(tmp_path)
    (tmp_path / "bundle.min.js").write_text("y\n" * 5000)
    _commit_change(tmp_path, "x\n" * 100)
    monkeypatch.setenv("MAX_DIFF_BYTES", "500")
    monkeypatch.setattr(ai_review, "build_reviewer", lambda provider: _StubReviewer("[]"))

    _, output_path, _ = _run_main(tmp_path, monkeypatch, base_sha)

    assert "bundle.min.js" in output_path.read_text()


def test_main_does_not_block_when_reviewer_call_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_sha = _init_git_repo(tmp_path)
    _commit_change(tmp_path, "changed\n")
    monkeypatch.setattr(
        ai_review,
        "build_reviewer",
        lambda provider: _ExplodingReviewer(RuntimeError("Anthropic API error 400: too long")),
    )

    exit_code, output_path, github_output = _run_main(tmp_path, monkeypatch, base_sha)

    assert exit_code == 0, "an unreachable reviewer must never block all merges"
    assert "tooling failure" in output_path.read_text()
    assert "blocking-count=0" in github_output.read_text()


def test_output_file_always_written_even_on_reviewer_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: the comment step reads this file unconditionally."""
    base_sha = _init_git_repo(tmp_path)
    _commit_change(tmp_path, "changed\n")
    monkeypatch.setattr(
        ai_review,
        "build_reviewer",
        lambda provider: _ExplodingReviewer(OSError("connection reset")),
    )

    _, output_path, _ = _run_main(tmp_path, monkeypatch, base_sha)

    assert output_path.is_file() and output_path.read_text().strip()


def test_largest_files_in_diff_orders_by_lines_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_sha = _init_git_repo(tmp_path)
    (tmp_path / "big.js").write_text("a\n" * 500)
    (tmp_path / "small.js").write_text("b\n" * 5)
    _commit_change(tmp_path, "changed\n")
    monkeypatch.chdir(tmp_path)

    listing = ai_review.largest_files_in_diff(base_sha, [])

    assert listing.index("big.js") < listing.index("small.js")


def _fake_api_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def test_reviewer_raises_when_thinking_consumed_the_token_budget() -> None:
    """Thinking blocks carry empty text by default, so a response can be
    non-empty yet contain nothing reviewable."""
    payload = {
        "content": [{"type": "thinking", "thinking": ""}],
        "stop_reason": "max_tokens",
    }

    with patch("ai_review.urllib.request.urlopen", return_value=_fake_api_response(payload)):
        reviewer = ai_review.AnthropicReviewer(api_key="k", model="claude-opus-5")
        with pytest.raises(RuntimeError, match="no text content") as excinfo:
            reviewer.review(diff="d", system_prompt="p")

    message = str(excinfo.value)
    assert "max_tokens" in message, "must name the stop_reason so the cause is actionable"
    assert "thinking" in message


def test_reviewer_accepts_text_alongside_thinking_blocks() -> None:
    payload = {
        "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "[]"}],
        "stop_reason": "end_turn",
    }

    with patch("ai_review.urllib.request.urlopen", return_value=_fake_api_response(payload)):
        reviewer = ai_review.AnthropicReviewer(api_key="k", model="claude-opus-5")
        assert reviewer.review(diff="d", system_prompt="p") == "[]"


def test_reviewer_failure_message_reaches_the_pr_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_sha = _init_git_repo(tmp_path)
    _commit_change(tmp_path, "changed\n")
    monkeypatch.setattr(
        ai_review,
        "build_reviewer",
        lambda provider: _ExplodingReviewer(RuntimeError("stop_reason='max_tokens'")),
    )

    _, output_path, _ = _run_main(tmp_path, monkeypatch, base_sha)

    assert "max_tokens" in output_path.read_text(), "diagnostic must not be logs-only"
