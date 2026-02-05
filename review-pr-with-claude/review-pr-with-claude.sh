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
# Exit codes:
#   0 - Review posted successfully (or skipped)
#   1 - Error occurred

set -e

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

# Build the prompt
cat > "${output_dir}/claude-prompt.txt" << PROMPT_EOF
You are reviewing Pull Request #${pr_number} for a Shaken Fist project.

## PR Information

- **Title**: ${pr_title}
- **Author**: ${pr_author}
- **Branch**: ${head_branch} -> ${base_branch}

## Your Task

1. Read the PR diff below carefully
2. Analyze the changes for:
   - Code quality and readability
   - Potential bugs or logic errors
   - Security concerns (SQL injection, command injection, etc.)
   - Performance implications
   - Test coverage (are new features tested?)
   - Documentation (are changes documented?)
   - Style consistency with the codebase

3. Write a constructive review that:
   - Starts with a brief summary of what the PR does
   - Lists specific concerns with file:line references
   - Suggests improvements where relevant
   - Acknowledges good practices you observe
   - Is professional and helpful in tone

4. Post your review using this exact command:
   gh pr review ${pr_number} --comment --body "\$(cat <<'REVIEW_EOF'
   Your review content here...
   REVIEW_EOF
   )"

## Code Style Notes for Shaken Fist

- Python code uses single quotes for strings, double quotes for
  docstrings
- Line length limit is 80 chars (120 max)
- Type hints are encouraged but not required everywhere

## The PR Diff

PROMPT_EOF

# Append the diff
cat "${output_dir}/pr-diff.txt" \
    >> "${output_dir}/claude-prompt.txt"

# Run Claude Code
cat "${output_dir}/claude-prompt.txt" | claude -p - \
    --dangerously-skip-permissions \
    --max-turns "${max_turns}" \
    --output-format json \
    > "${output_dir}/claude-output.json" || true

# Extract and display the result
if [ -f "${output_dir}/claude-output.json" ]; then
    jq -r '.result // empty' \
        "${output_dir}/claude-output.json"

    # Extract metadata for CI output
    num_turns=$(jq -r '.num_turns // "unknown"' \
        "${output_dir}/claude-output.json")
    duration_ms=$(jq -r '.duration_ms // "unknown"' \
        "${output_dir}/claude-output.json")
    cost_usd=$(jq -r '.total_cost_usd // "unknown"' \
        "${output_dir}/claude-output.json")

    echo
    echo "Claude execution stats:"
    echo "  Turns: ${num_turns} / ${max_turns}"
    echo "  Duration: ${duration_ms}ms"
    echo "  Cost: \$${cost_usd}"

    ci_output "claude_turns" "${num_turns}"
    ci_output "claude_duration_ms" "${duration_ms}"
    ci_output "claude_cost_usd" "${cost_usd}"
fi

echo
echo "========================================"
echo "PR review complete!"
echo "========================================"
ci_output "review_posted" "true"
