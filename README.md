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

Sets up the Kerbside-specific test environment: checks out kerbside-patches,
assembles patched source, provisions a test VM, installs build dependencies,
and configures the CI registry.

### deploy-kolla-ansible

Bootstraps, validates, and deploys Kolla-Ansible on a test VM. This action
is shared between kerbside and kerbside-patches CI to avoid duplication.

**Usage:**

```yaml
# Local build (no registry) - used by kerbside CI
- uses: shakenfist/actions/deploy-kolla-ansible@main
  with:
    base_user: debian
    image_tag: local
    build_targets: master
    topology: all-in-one

# CI registry build - used by kerbside-patches CI
- uses: shakenfist/actions/deploy-kolla-ansible@main
  with:
    base_user: debian
    image_tag: master-debian-trixie-abc123
    build_targets: master
    topology: all-in-one
    registry_token: ${{ secrets.CI_REGISTRY_TOKEN }}
    enable_kerbside: 'true'
    use_ci_registry: 'true'
```

**Inputs:**

| Name | Required | Default | Description |
|------|----------|---------|-------------|
| `base_user` | Yes | `debian` | SSH user on target VM |
| `image_tag` | Yes | - | Container image tag (`local` or registry hash) |
| `build_targets` | Yes | - | OpenStack release (master, 2025.1, etc.) |
| `topology` | Yes | `all-in-one` | Deployment topology |
| `registry_token` | No | `''` | CI registry token (omit for local builds) |
| `enable_kerbside` | No | `true` | Enable kerbside in deployment |
| `use_ci_registry` | No | `false` | Pull from CI registry; pass `--use-ci-registry` to post-install |

**Steps performed:**
1. Bootstrap Kolla-Ansible (with conditional registry/kerbside flags)
2. Run pre-checks
3. Pull images (only when `use_ci_registry` is `true`)
4. Deploy
5. Install patched OpenStack clients
6. Post install Kolla-Ansible setup

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

## Ansible Playbooks

The `ansible/` directory contains playbooks used by CI workflows for
provisioning and configuring test infrastructure:

- **ci-image.yml**: Builds CI base images with pre-installed packages.
- **ci-dependencies.yml**: Downloads and caches VM images.
- **ci-topology-*.yml**: Provisions multi-node test clusters.
- **ci-gather-logs.yml**: Collects logs from test nodes after runs.

### CI Caching

The playbooks configure remote VMs to use local caches:

- **apt proxy**: Writes `/etc/apt/apt.conf.d/01proxy` pointing to
  `http://192.168.1.15:3128` (Squid).
- **pip mirror**: Writes `/etc/pip.conf` pointing to
  `https://devpi.home.stillhq.com/root/pypi/+simple/` (devpi).
- **getsf-wrapper**: Exports `http_proxy`, `https_proxy`, and
  `PIP_INDEX_URL` for package operations during deployment.

Plays targeting remote hosts also set `environment:` directives to
pass proxy settings to Ansible modules (apt, get_url, etc.).

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
