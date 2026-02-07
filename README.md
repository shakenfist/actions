# Shaken Fist Shared GitHub Actions

This repository contains reusable GitHub Actions used across Shaken Fist
projects.

## Available Actions

### pr-bot-trigger

Handles `@shakenfist-bot` trigger comments on pull requests. This action:

- Validates that the comment matches the specified trigger phrase
- Checks if the commenter has write/admin permissions
- Adds a reaction to the triggering comment
- Posts status messages (starting, unauthorized)
- Outputs PR details for downstream jobs

**Usage:**

```yaml
- uses: shakenfist/actions/pr-bot-trigger@main
  id: trigger
  with:
    trigger-phrase: 'please retest'
    reaction: 'rocket'
    starting-message: |
      Starting tests on branch `{pr_ref}`...
      [View workflow run]({run_url})

- name: Do something if authorized
  if: steps.trigger.outputs.authorized == 'true'
  run: |
    echo "User is authorized, PR branch is ${{ steps.trigger.outputs.pr-ref }}"
```

**Inputs:**

| Name | Required | Default | Description |
|------|----------|---------|-------------|
| `trigger-phrase` | Yes | - | Phrase to look for (without `@shakenfist-bot` prefix) |
| `reaction` | No | `rocket` | Emoji reaction to add (rocket, +1, eyes, etc.) |
| `starting-message` | No | - | Message to post when starting. Supports `{pr_ref}` and `{run_url}` placeholders |
| `unauthorized-message` | No | Default | Message to post when user is unauthorized. Supports `{username}` placeholder |

**Outputs:**

| Name | Description |
|------|-------------|
| `authorized` | `true` if user has write/admin permission, `false` otherwise |
| `triggered` | `true` if trigger phrase matched, `false` otherwise |
| `pr-number` | The PR number |
| `pr-ref` | The PR branch name |

### review-pr-with-claude

Runs an automated code review on a pull request using Claude Code.

**Usage:**

```yaml
- uses: shakenfist/actions/review-pr-with-claude@main
  with:
    pr-number: ${{ github.event.issue.number }}
    max-turns: '50'
```

**Inputs:**

| Name | Required | Default | Description |
|------|----------|---------|-------------|
| `pr-number` | Yes | - | The PR number to review |
| `max-turns` | No | `50` | Maximum Claude turns |
| `force` | No | `false` | Review even if bot has already reviewed |

### setup-test-environment

Sets up the test environment for Shaken Fist projects.

### setup-kerbside-environment

Sets up the Kerbside-specific test environment.

## Usage in Workflows

These actions are designed to be used in GitHub Actions workflows. Example:

```yaml
name: PR Retest

on:
  issue_comment:
    types: [created]

permissions:
  contents: read
  issues: write
  pull-requests: write
  actions: write

jobs:
  trigger-retest:
    if: |
      github.event.issue.pull_request &&
      contains(github.event.comment.body, '@shakenfist-bot please retest')
    runs-on: ubuntu-latest

    steps:
      - uses: shakenfist/actions/pr-bot-trigger@main
        id: trigger
        with:
          trigger-phrase: 'please retest'
          reaction: 'rocket'

      - name: Trigger functional tests
        if: steps.trigger.outputs.authorized == 'true'
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          gh workflow run functional-tests.yml \
            --repo ${{ github.repository }} \
            --ref "${{ steps.trigger.outputs.pr-ref }}"
```

## Contributing

When adding new actions:

1. Create a new directory with the action name
2. Add `action.yml` with the action definition
3. Add any supporting scripts
4. Update this README with documentation

## Projects Using These Actions

- [imago](https://github.com/shakenfist/imago) - Disk image management
- [occystrap](https://github.com/shakenfist/occystrap) - Container image tools
- [shakenfist](https://github.com/shakenfist/shakenfist) - Main project
