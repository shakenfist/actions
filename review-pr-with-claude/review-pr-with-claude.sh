#!/bin/bash

# Review a PR using Claude Code.
#
# This script is designed to be called from the composite action
# defined in action.yml. It reads configuration from environment
# variables set by the action:
#
#   INPUT_PR_NUMBER   - PR number to review (required)
#   INPUT_MAX_TURNS   - Maximum Claude turns. Empty or "auto" scales
#                       the budget from the diff size (the default);
#                       an integer pins it and opts out of scaling.
#   INPUT_FORCE       - Review even if already reviewed (default: false)
#   GH_TOKEN          - GitHub token for API access
#
# The review output is structured JSON that is:
#   1. Validated against review-schema.json
#   2. Used to create GitHub issues for actionable items
#   3. Rendered to markdown with embedded JSON for automation
#
# Exit codes:
#   0 - Review posted successfully, or skipped for a reason the PR
#       cannot do anything about (already reviewed, diff too large for
#       the API, reviewer ran out of turns). Skips that happen after
#       the PR is known are explained in a comment on the PR itself.
#   1 - Something went wrong that a human should look at: the reviewer
#       errored, or produced output no review could be recovered from.
#       The reason is written to the job summary, not just the log.

set -e

script_dir="$(cd "$(dirname "$0")" && pwd)"

# Read inputs from environment (set by the composite action)
pr_number="${INPUT_PR_NUMBER}"
max_turns="${INPUT_MAX_TURNS:-}"
force="${INPUT_FORCE:-false}"

# A caller-pinned budget has to be a plain integer: it is handed to
# ``claude --max-turns`` and interpolated into a comment heredoc below,
# and neither wants a surprise. Anything else falls back to scaling
# from the diff rather than failing the run, since the reviewer being
# unavailable over a malformed input helps nobody.
if [ -n "${max_turns}" ] && [ "${max_turns}" != "auto" ] && \
        ! [[ "${max_turns}" =~ ^[0-9]+$ ]]; then
    echo "Warning: max-turns '${max_turns}' is not a number;" \
        "scaling from the diff size instead"
    max_turns=''
fi

# CI mode is always true when running as an action
ci_mode=true

# No colors in CI
RED=''
GREEN=''
YELLOW=''
BLUE=''
NC=''

# Create output directory
output_dir=$(mktemp -d)
cleanup() {
    rm -rf "${output_dir}"
}
trap cleanup EXIT

# CI mode output helper
ci_output() {
    local key="$1"
    local value="$2"
    echo "${key}=${value}"
}

# Write lines to the job's check summary, when running in Actions.
# Anything explaining why a run did not review needs to land here:
# reading it out of step logs costs a log dive per occurrence, which is
# the whole complaint in issue #39.
step_summary() {
    if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
        printf '%s\n' "$@" >> "${GITHUB_STEP_SUMMARY}"
    fi
}

# No review this time, and nothing the PR can do about it. Explain on
# the PR and in the job summary, then finish green.
#
# The reviewer is not a required check anywhere in the fleet, so a red
# job here buys nothing: it does not block a merge, it just leaves an X
# for a human to triage into the same answer every time. The money is
# spent either way, so spend it on an explanation people can read.
review_unavailable() {
    local reason="$1"
    local heading="$2"
    local body="$3"

    local comment_file="${output_dir}/unavailable-comment.md"
    {
        echo "## :robot: ${heading}"
        echo
        echo "${body}"
        echo
        echo "This message comes from the reviewer's \"no review to"
        echo "post\" handler; the workflow step exited successfully so"
        echo "it does not block the merge queue."
    } > "${comment_file}"

    # Has this same explanation already been posted? The diff-too-large
    # check runs before the already-reviewed gate, and the re-review
    # trigger sets force, so a second attempt reaches this handler and
    # would otherwise leave an identical comment each time. The job
    # summary is still written either way: that is per-run, and the
    # run it explains is this one.
    if gh pr view "${pr_number}" --json comments \
            --jq '.comments[] |
                select(
                    .author.login == "github-actions" or
                    .author.login == "shakenfist-bot"
                ) | .body' 2>/dev/null \
            | grep -qF "${heading}"; then
        echo "Note: the PR already carries this explanation, not" \
            "posting it again"
    else
        gh pr comment "${pr_number}" --body-file "${comment_file}" \
            || echo "Warning: failed to post ${reason} comment"
    fi

    step_summary "### :robot: ${heading}" '' "${body}"

    ci_output "review_skipped" "${reason}"
    ci_output "review_posted" "false"
    echo
    echo "========================================"
    echo "PR review skipped (${reason})"
    echo "========================================"
    exit 0
}

# The reviewer spent its whole turn budget without arriving at a
# review. Called from both shapes that takes: an envelope with no
# result text at all, and one carrying text that holds no review.
#
# The heredocs in here and in review_truncated_unavailable() are
# unquoted so they can interpolate the budget and the diff size. Only
# numeric variables set by this script may be named in them -- an
# unquoted heredoc also runs $(...) and backticks, and everything the
# reviewer touches after step 2 is pull request controlled.
review_out_of_turns() {
    review_unavailable "turn_budget_exhausted" \
        "Automated review ran out of turns" \
        "$(cat << COMMENT_EOF
The reviewer used its whole budget of ${max_turns} turns on this
${diff_lines} line diff without reaching a review, so there is
nothing to post. Nothing is wrong with the PR -- the reviewer ran
out of room before it finished looking.

Options:

* **Ask for another review.** The re-review trigger runs the
  reviewer again, and the budget is scaled from the diff size, so a
  rerun starts from the same place. Worth one attempt: the review
  ending here is partly luck of the run.
* **Split the PR.** A smaller diff reviews inside the budget and is
  easier for humans to read too.
* **Land without an automated review.** This job is not a required
  check, so it does not block the merge queue.
COMMENT_EOF
)"
}

# The response stopped mid-JSON early enough that nothing survived the
# salvage pass, or that what survived is not a whole review. Same
# family as running out of turns: the reviewer ran out of room, which
# is a fact about the size of this pull request rather than a broken
# tool, so it finishes green with an explanation.
review_truncated_unavailable() {
    review_unavailable "truncated_unusable" \
        "Automated review was cut off before it said anything" \
        "$(cat << COMMENT_EOF
The reviewer's response stopped part way through the review, on this
${diff_lines} line diff, before any complete finding had been
written. A partial review is salvaged and posted when there is one to
salvage; this response was cut too early for that, so there is
nothing to show. Nothing is wrong with the PR.

Options:

* **Ask for another review.** How much output a run gets through
  varies, so a rerun is worth one attempt.
* **Split the PR.** A smaller diff leaves the reviewer room to
  finish, and is easier for humans to read too.
* **Land without an automated review.** This job is not a required
  check, so it does not block the merge queue.
COMMENT_EOF
)"
}

# The reviewer broke rather than ran out of room. Say so where the
# check summary shows it, and fail.
review_failed() {
    local heading="$1"
    local body="$2"

    echo "Error: ${heading}"
    echo "${body}"
    step_summary "### :x: ${heading}" '' "${body}"
    ci_output "review_posted" "false"
    exit 1
}

echo "========================================"
echo "Shaken Fist PR Reviewer"
echo "========================================"
echo

# Step 1: Validate environment
echo "Step 1: Validating environment..."

if ! command -v gh &> /dev/null; then
    echo "Error: GitHub CLI (gh) not found"
    exit 1
fi

# Locate the claude binary. Honour CLAUDE_BIN if set, then look on PATH,
# then fall back to the default install location used by the official
# installer (~/.local/bin/claude is not always on PATH for non-login
# shells like the GitHub Actions runner).
if [ -n "${CLAUDE_BIN:-}" ]; then
    claude_bin="${CLAUDE_BIN}"
elif command -v claude &> /dev/null; then
    claude_bin="claude"
elif [ -x "${HOME}/.local/bin/claude" ]; then
    claude_bin="${HOME}/.local/bin/claude"
else
    claude_bin=""
fi
if [ -z "${claude_bin}" ] || \
        { ! command -v "${claude_bin}" &> /dev/null && \
          ! [ -x "${claude_bin}" ]; }; then
    echo "Error: Claude Code CLI not found (${claude_bin:-not set})"
    echo "Install with: npm install -g @anthropic-ai/claude-code"
    echo "Or set CLAUDE_BIN to the path of an existing install."
    exit 1
fi

if [ -z "${pr_number}" ]; then
    echo "Error: PR number not provided"
    exit 1
fi

echo "Reviewing PR #${pr_number}"
echo

# Step 2: Fetch PR information
echo "Step 2: Fetching PR information..."

gh pr view "${pr_number}" \
    --json title,body,author,baseRefName,headRefName \
    > "${output_dir}/pr-info.json"

pr_title=$(jq -r '.title' "${output_dir}/pr-info.json")
pr_author=$(jq -r '.author.login' \
    "${output_dir}/pr-info.json")
base_branch=$(jq -r '.baseRefName' \
    "${output_dir}/pr-info.json")
head_branch=$(jq -r '.headRefName' \
    "${output_dir}/pr-info.json")

echo "Title: ${pr_title}"
echo "Author: ${pr_author}"
echo "Branch: ${head_branch} -> ${base_branch}"
echo

# Step 3: Get the diff
echo "Step 3: Fetching PR diff..."

# GitHub's pulls API caps diffs at 20,000 lines and returns HTTP
# 406 above that. ``gh pr diff`` surfaces this as a non-zero exit
# with the failure printed to stderr. Capture both so we can
# distinguish "too large" from other errors (network, auth) and
# post a graceful skip comment in the too-large case rather than
# letting ``set -e`` kill the step with no PR-side trace.
diff_stderr_file="${output_dir}/pr-diff.stderr"
if gh pr diff "${pr_number}" > "${output_dir}/pr-diff.txt" \
        2> "${diff_stderr_file}"; then
    diff_fetch_ok=true
else
    diff_fetch_rc=$?
    diff_fetch_ok=false
fi

if [ "${diff_fetch_ok}" = "false" ]; then
    diff_err=$(cat "${diff_stderr_file}")
    echo "Diff fetch failed: ${diff_err}"

    if echo "${diff_err}" | \
            grep -qiE 'exceeded the maximum number of lines'; then
        # Diff too big for the API. Say so on the PR and exit
        # success. A green check here means "the reviewer ran to a
        # known endpoint," not "the PR is reviewed."
        review_unavailable "diff_too_large" \
            "Automated review skipped -- diff too large" \
            "$(cat << 'COMMENT_EOF'
The PR diff exceeds GitHub's 20,000-line cap on
``GET /repos/{owner}/{repo}/pulls/{number}`` and the reviewer
could not fetch it for analysis.

Options for getting an automated review:

* **Split the PR.** Smaller PRs review faster and are easier
  for humans to read too.
* **Run the reviewer manually.** A maintainer can invoke
  ``shakenfist/actions/review-pr-with-claude`` against the
  branch with a locally-generated diff (``git diff
  develop...HEAD``) and post the result.
* **Skip automated review.** Land relying on the merge-queue
  CI and human review.
COMMENT_EOF
)"
    fi

    # Any other failure (network, auth, gh itself) is a real
    # problem -- surface it as a workflow failure so it gets
    # noticed and fixed.
    step_summary "### :x: Automated review failed" '' \
        "The PR diff could not be fetched: ${diff_err}"
    echo "Error: PR diff fetch failed with an unexpected error"
    exit "${diff_fetch_rc}"
fi

diff_lines=$(wc -l < "${output_dir}/pr-diff.txt")
echo "Diff size: ${diff_lines} lines"

# A diff over this many lines gets the prioritisation instructions
# added to the prompt (see step 5). Chosen from the two observed
# failures in issue #39: a 1637 line diff exhausted 50 turns, and a
# 5440 line one produced a review too long to finish emitting.
large_diff_threshold=1000

# The turn budget scales with the diff unless the caller pinned it. A
# fixed 50 turns is generous for a 200 line diff and not enough for a
# 1600 line one, and running out costs the whole review: the model has
# spent the money by then and has nothing to show for it. Ten more
# turns per 500 lines of diff, capped so a pathological PR cannot run
# unbounded.
if [ -z "${max_turns}" ] || [ "${max_turns}" = "auto" ]; then
    max_turns=$(( 50 + (diff_lines / 500) * 10 ))
    if [ "${max_turns}" -gt 150 ]; then
        max_turns=150
    fi
    echo "Turn budget: ${max_turns} (scaled from diff size)"
else
    echo "Turn budget: ${max_turns} (set by the caller)"
    echo "Note: an explicit max-turns opts out of scaling with the" \
        "diff. Drop the input, or pass 'auto', to get it back."
fi
echo

if [ "${diff_lines}" -gt "${large_diff_threshold}" ]; then
    echo "Note: Large diff (${diff_lines} lines), asking the reviewer" \
        "to prioritise"
fi

# Step 4: Check for existing bot reviews
echo "Step 4: Checking for existing reviews..."

existing_review=$(gh pr view "${pr_number}" --json reviews \
    --jq '.reviews[] |
        select(
            .author.login == "github-actions" or
            .author.login == "shakenfist-bot"
        ) | .id' \
    2>/dev/null | head -1 || true)

if [ -n "${existing_review}" ]; then
    if [ "${force}" = "true" ]; then
        echo "Note: Bot has already reviewed this PR"
        echo "Proceeding with new review (force specified)..."
    else
        echo "Bot has already reviewed this PR"
        ci_output "review_skipped" "already_reviewed"
        exit 0
    fi
fi
echo

# Step 5: Run Claude Code for review
echo "Step 5: Running Claude Code for review..."
echo

# Build the prompt - request structured JSON output
cat > "${output_dir}/claude-prompt.txt" << 'PROMPT_EOF'
You are reviewing Pull Request #${pr_number} for a Shaken Fist project.

## PR Information

- **Title**: ${pr_title}
- **Author**: ${pr_author}
- **Branch**: ${head_branch} -> ${base_branch}

## Your Task

0. Read the contents of AGENTS.md, ARCHITECTURE.md, and README.md to
   gather context.

1. Read the PR diff below carefully

2. Analyze the changes for:
   - Code quality and readability
   - Potential bugs or logic errors
   - Security concerns (SQL injection, command injection, etc.)
   - Performance implications
   - Test coverage (are new features tested?)
   - Documentation (are changes documented?)
   - Style consistency with the codebase

3. Output your review as a JSON object with the following structure:

```json
{
  "summary": "Brief 1-3 sentence summary of what the PR does",
  "items": [
    {
      "id": 1,
      "title": "Short title for this item",
      "category": "security|bug|performance|documentation|style|testing|other",
      "severity": "critical|high|medium|low",
      "action": "fix|document|consider|none",
      "description": "Detailed description of the issue or observation",
      "location": "path/to/file.py:100-150",
      "suggestion": "Specific suggestion for how to address this",
      "rationale": "For action=none or consider, explain why"
    }
  ],
  "positive_feedback": [
    {
      "title": "What was done well",
      "description": "Why this is good"
    }
  ],
  "test_coverage": {
    "adequate": true,
    "missing": ["list of missing test scenarios"]
  }
}
```

## Action Types

- **fix**: This MUST be fixed before merging (security issues, bugs, etc.)
- **document**: Documentation should be added or updated
- **consider**: Optional improvement, reviewer's suggestion but not required
- **none**: Informational observation only, no action needed

## Important Rules

1. Every item MUST have: id, title, category, action
2. Items with action="fix" MUST have severity
3. Items with action="none" or "consider" SHOULD have rationale
4. Include location (file:lines) when referencing specific code
5. Be specific in suggestions - vague advice is not actionable

## CRITICAL: Output Format

Your response MUST contain a JSON code block with the review data.
Start the JSON block with ```json and end with ```.
Do NOT post the review to GitHub - just output the JSON.
The JSON will be validated and rendered to markdown by a separate script.

## Code Style Notes for Shaken Fist

- Python code uses single quotes for strings, double quotes for docstrings
- Line length limit is 80 chars (120 max)
- Type hints are encouraged but not required everywhere
${large_diff_guidance}

## The PR Diff

PROMPT_EOF

# Instructions added only for a large diff. A truncated review is
# worth nothing, so on a diff this size the model is told to converge:
# work in priority order, cap the item count, and finish the JSON.
large_diff_guidance=''
if [ "${diff_lines}" -gt "${large_diff_threshold}" ]; then
    large_diff_guidance=$(cat << GUIDANCE_EOF

## This Diff Is Large -- Prioritise

This diff is ${diff_lines} lines, which is large enough that a review
covering everything will not fit. Converge rather than running out:

- Work in priority order: correctness and security first, then test
  coverage and documentation, then style.
- Report at most 15 items, keeping the most important ones. A short
  complete review is worth far more than a long unfinished one.
- Keep each description and suggestion to a few sentences.
- Emit the closing brace. A response cut off mid-JSON is salvaged for
  whatever items completed, and everything after the cut is lost.
GUIDANCE_EOF
)
fi

# Substitute variables in the prompt using Python for safe handling of
# user-controlled input (PR titles can contain any characters including
# newlines, quotes, and shell metacharacters)
prompt_file="${output_dir}/claude-prompt.txt"
python3 - "${prompt_file}" "${pr_number}" "${pr_title}" "${pr_author}" \
    "${head_branch}" "${base_branch}" "${large_diff_guidance}" << 'PYSUBST'
import sys
from pathlib import Path

prompt_file = Path(sys.argv[1])
(pr_number, pr_title, pr_author, head_branch, base_branch,
 large_diff_guidance) = sys.argv[2:8]

content = prompt_file.read_text()
content = content.replace('${pr_number}', pr_number)
content = content.replace('${pr_title}', pr_title)
content = content.replace('${pr_author}', pr_author)
content = content.replace('${head_branch}', head_branch)
content = content.replace('${base_branch}', base_branch)
content = content.replace('${large_diff_guidance}', large_diff_guidance)
prompt_file.write_text(content)
PYSUBST

# Append the diff
cat "${output_dir}/pr-diff.txt" >> "${prompt_file}"

# Run Claude Code to get JSON review
echo "Running Claude to generate review JSON..."
cat "${prompt_file}" | "${claude_bin}" -p - \
    --dangerously-skip-permissions \
    --max-turns "${max_turns}" \
    --output-format json \
    > "${output_dir}/claude-output.json" || true

# Extract metadata for CI output. ``subtype`` is how the run ended --
# ``success``, ``error_max_turns`` when the budget ran out,
# ``error_during_execution`` when the SDK itself failed -- and step 6
# needs it to tell "ran out of room" apart from "broke", which look
# identical from the missing result alone.
claude_output="${output_dir}/claude-output.json"
result_subtype='unknown'
if [ -s "${claude_output}" ]; then
    num_turns=$(jq -r '.num_turns // "unknown"' \
        "${claude_output}")
    duration_ms=$(jq -r '.duration_ms // "unknown"' \
        "${claude_output}")
    cost_usd=$(jq -r '.total_cost_usd // "unknown"' \
        "${claude_output}")
    result_subtype=$(jq -r '.subtype // "unknown"' \
        "${claude_output}" 2>/dev/null || echo 'unparseable_envelope')

    echo
    echo "Claude execution stats:"
    echo "  Turns: ${num_turns} / ${max_turns}"
    echo "  Duration: ${duration_ms}ms"
    echo "  Cost: \$${cost_usd}"
    echo "  Outcome: ${result_subtype}"

    ci_output "claude_turns" "${num_turns}"
    ci_output "claude_duration_ms" "${duration_ms}"
    ci_output "claude_cost_usd" "${cost_usd}"
    ci_output "claude_subtype" "${result_subtype}"
fi

# Step 6: Extract and validate review JSON
echo
echo "Step 6: Extracting and validating review JSON..."

review_json_file="${output_dir}/review.json"
review_json_with_issues="${output_dir}/review-with-issues.json"
review_md_file="${output_dir}/review.md"
claude_result_file="${output_dir}/claude-result.txt"
render_script="${script_dir}/render-review.py"
create_issues_script="${script_dir}/create-review-issues.py"
extract_script="${script_dir}/extract-review-json.py"

jq -r '.result // empty' "${claude_output}" > "${claude_result_file}" \
    2>/dev/null || true

if [ ! -s "${claude_result_file}" ]; then
    # The envelope carries no result text at all. Which of the two
    # reasons that is matters: running out of turns is a budget
    # question about this PR, anything else is the reviewer breaking.
    if [ "${result_subtype}" = "error_max_turns" ]; then
        review_out_of_turns
    fi

    review_failed "Automated review produced no result" \
        "Claude returned no result text (outcome: ${result_subtype}, \
turns: ${num_turns:-unknown}, diff: ${diff_lines} lines). This is the \
reviewer failing rather than running out of budget -- check the step \
log for the SDK error."
fi

# Pull the review JSON out of the response. The extractor salvages a
# response that was cut off mid-JSON, marking what it recovers as a
# partial review, because a large diff runs out of output room often
# enough that discarding those is throwing away most of a review.
echo "Extracting review JSON..."
review_truncated=false
extract_rc=0
extract_status=$(python3 "${extract_script}" \
    "${claude_result_file}" "${review_json_file}") || extract_rc=$?

if [ "${extract_rc}" -eq 0 ]; then
    echo "Extraction: ${extract_status}"
    if [ "${extract_status}" = "status=salvaged" ]; then
        echo "Note: the response was truncated; the review is partial"
        review_truncated=true
        ci_output "review_truncated" "true"
    fi
else
    echo "Extraction failed: ${extract_status}"
    echo "Response was:"
    head -50 "${claude_result_file}"

    # Exit 2 says a review block was there and stopped before anything
    # usable arrived, which is the large-diff outcome this PR exists
    # to handle. So is a budget that ran out with prose in the result
    # rather than nothing at all. Either way the response is too small,
    # not the tooling too broken.
    if [ "${extract_rc}" -eq 2 ]; then
        ci_output "review_truncated" "true"
        review_truncated_unavailable
    fi
    if [ "${result_subtype}" = "error_max_turns" ]; then
        review_out_of_turns
    fi

    review_failed "Automated review output could not be parsed" \
        "No JSON review could be recovered from the reviewer's \
output, not even a partial one (${extract_status}; outcome: \
${result_subtype}, diff: ${diff_lines} lines)."
fi

# Validate the JSON
echo "Validating JSON..."
if ! python3 "${render_script}" --validate "${review_json_file}"; then
    echo "JSON content:"
    cat "${review_json_file}"

    # A salvaged review that will not validate is the response having
    # been cut off, not the schema and the prompt disagreeing. Saying
    # "tooling problem" there sends a human looking for a bug that is
    # not present, so route it where the truncation cases go.
    if [ "${review_truncated}" = "true" ]; then
        review_truncated_unavailable
    fi

    review_failed "Automated review failed schema validation" \
        "The reviewer returned JSON that does not match \
review-schema.json (outcome: ${result_subtype}). The prompt, the \
schema and the renderer have to agree, so this is a tooling problem \
rather than a problem with the PR."
fi
echo "JSON validation passed"

# Step 7: Create GitHub issues for actionable items
echo
echo "Step 7: Creating GitHub issues for action items..."
python3 "${create_issues_script}" \
    "${review_json_file}" \
    "${review_json_with_issues}" \
    --pr "${pr_number}" || {
    echo "Warning: Issue creation failed, continuing without issues"
    cp "${review_json_file}" "${review_json_with_issues}"
}

# Step 8: Render to markdown (with the review embedded as JSON, so it can
# be recovered from the posted comment)
echo
echo "Step 8: Rendering review to markdown..."
python3 "${render_script}" --embed-json \
    "${review_json_with_issues}" "${review_md_file}"
echo "Rendered review to ${review_md_file}"

# Step 9: Post the review
echo
echo "Step 9: Posting review to PR..."

review_size=$(wc -c < "${review_md_file}")
if [ "${review_size}" -gt 0 ]; then
    gh pr review "${pr_number}" --comment \
        --body-file "${review_md_file}"
    echo "Review posted successfully"
    ci_output "review_posted" "true"
else
    echo "Warning: Rendered review is empty"
    ci_output "review_posted" "false"
fi

echo
echo "========================================"
echo "PR review complete!"
echo "========================================"
