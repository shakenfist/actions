#!/bin/bash

# Review a PR using Claude Code.
#
# This script is designed to be called from the composite action
# defined in action.yml. It reads configuration from environment
# variables set by the action:
#
#   INPUT_PR_NUMBER   - PR number to review (required)
#   INPUT_MAX_TURNS   - Maximum Claude turns (default: 50)
#   INPUT_FORCE       - Review even if already reviewed (default: false)
#   GH_TOKEN          - GitHub token for API access
#
# The review output is structured JSON that is:
#   1. Validated against review-schema.json
#   2. Used to create GitHub issues for actionable items
#   3. Rendered to markdown with embedded JSON for automation
#
# Exit codes:
#   0 - Review posted successfully (or skipped)
#   1 - Error occurred

set -e

script_dir="$(cd "$(dirname "$0")" && pwd)"

# Read inputs from environment (set by the composite action)
pr_number="${INPUT_PR_NUMBER}"
max_turns="${INPUT_MAX_TURNS:-50}"
force="${INPUT_FORCE:-false}"

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

if ! command -v claude &> /dev/null; then
    echo "Error: Claude Code CLI not found"
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
gh pr diff "${pr_number}" > "${output_dir}/pr-diff.txt"

diff_lines=$(wc -l < "${output_dir}/pr-diff.txt")
echo "Diff size: ${diff_lines} lines"
echo

if [ "${diff_lines}" -gt 5000 ]; then
    echo "Warning: Large diff (${diff_lines} lines)," \
        "review may be limited"
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

## The PR Diff

PROMPT_EOF

# Substitute variables in the prompt using Python for safe handling of
# user-controlled input (PR titles can contain any characters including
# newlines, quotes, and shell metacharacters)
prompt_file="${output_dir}/claude-prompt.txt"
python3 - "${prompt_file}" "${pr_number}" "${pr_title}" "${pr_author}" \
    "${head_branch}" "${base_branch}" << 'PYSUBST'
import sys
from pathlib import Path

prompt_file = Path(sys.argv[1])
pr_number, pr_title, pr_author, head_branch, base_branch = sys.argv[2:7]

content = prompt_file.read_text()
content = content.replace('${pr_number}', pr_number)
content = content.replace('${pr_title}', pr_title)
content = content.replace('${pr_author}', pr_author)
content = content.replace('${head_branch}', head_branch)
content = content.replace('${base_branch}', base_branch)
prompt_file.write_text(content)
PYSUBST

# Append the diff
cat "${output_dir}/pr-diff.txt" >> "${prompt_file}"

# Run Claude Code to get JSON review
echo "Running Claude to generate review JSON..."
cat "${prompt_file}" | claude -p - \
    --dangerously-skip-permissions \
    --max-turns "${max_turns}" \
    --output-format json \
    > "${output_dir}/claude-output.json" || true

# Extract metadata for CI output
claude_output="${output_dir}/claude-output.json"
if [ -f "${claude_output}" ]; then
    num_turns=$(jq -r '.num_turns // "unknown"' \
        "${claude_output}")
    duration_ms=$(jq -r '.duration_ms // "unknown"' \
        "${claude_output}")
    cost_usd=$(jq -r '.total_cost_usd // "unknown"' \
        "${claude_output}")

    echo
    echo "Claude execution stats:"
    echo "  Turns: ${num_turns} / ${max_turns}"
    echo "  Duration: ${duration_ms}ms"
    echo "  Cost: \$${cost_usd}"

    ci_output "claude_turns" "${num_turns}"
    ci_output "claude_duration_ms" "${duration_ms}"
    ci_output "claude_cost_usd" "${cost_usd}"
fi

# Step 6: Extract and validate review JSON
echo
echo "Step 6: Extracting and validating review JSON..."

claude_result=$(jq -r '.result // empty' "${claude_output}")
if [ -z "${claude_result}" ]; then
    echo "Error: No result from Claude"
    ci_output "review_posted" "false"
    exit 1
fi

# Extract JSON from code block (between ```json and ```)
# Allow for whitespace variations in the markers
json_start='^[[:space:]]*```json[[:space:]]*$'
json_end='^[[:space:]]*```[[:space:]]*$'
review_json=$(echo "${claude_result}" | \
    sed -n "/${json_start}/,/${json_end}/p" | sed '1d;$d')

if [ -z "${review_json}" ]; then
    echo "Warning: No JSON code block found with standard markers"
    echo "Attempting fallback extraction..."

    # Fallback: use Python for portable JSON extraction
    review_json=$(echo "${claude_result}" | python3 -c '
import sys
import re
import json

content = sys.stdin.read()

# Try to find a JSON object with summary and items fields
match = re.search(
    r"\{[^{}]*\"summary\"[^{}]*\"items\".*\}",
    content,
    re.DOTALL
)
if match:
    try:
        candidate = match.group(0)
        json.loads(candidate)
        print(candidate)
    except json.JSONDecodeError:
        pass
' 2>/dev/null || true)

    if [ -z "${review_json}" ]; then
        echo "Error: Could not extract JSON from Claude's response"
        echo "Response was:"
        echo "${claude_result}" | head -50
        ci_output "review_posted" "false"
        exit 1
    fi
fi

# Save the extracted JSON
review_json_file="${output_dir}/review.json"
review_json_with_issues="${output_dir}/review-with-issues.json"
review_md_file="${output_dir}/review.md"
render_script="${script_dir}/render-review.py"
create_issues_script="${script_dir}/create-review-issues.py"

echo "${review_json}" > "${review_json_file}"
echo "Extracted review JSON to ${review_json_file}"

# Validate the JSON
echo "Validating JSON..."
if ! python3 "${render_script}" --validate "${review_json_file}"; then
    echo "Error: Review JSON failed validation"
    echo "JSON content:"
    cat "${review_json_file}"
    ci_output "review_posted" "false"
    exit 1
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

# Step 8: Render to markdown (with embedded JSON for address-comments
# automation)
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
