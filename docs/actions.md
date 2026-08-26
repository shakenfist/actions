# Composite action reference

The inputs, outputs and usage of every composite action published from
this repository. For what the components are and how a CI run flows
through them, see [ARCHITECTURE.md](https://github.com/shakenfist/actions/blob/main/ARCHITECTURE.md); for how to wire
a repository up to them, see [consuming.md](consuming.md).

Composite action steps cannot carry `timeout-minutes` -- that is a
GitHub limitation, not a choice -- so the caller must put a timeout on
the step that uses the action. The examples below do.

## pr-bot-trigger

Handles `@shakenfist-bot` trigger comments on pull requests. This action:

- Validates that the comment matches the specified trigger phrase
- Checks if the commenter has write/admin permissions
- Refuses pull requests from forks
- Adds a reaction to the triggering comment
- Posts status messages (starting, unauthorized, fork-not-supported)
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
| `authorized` | `true` if the request may proceed: write/admin commenter **and** a non-fork pull request |
| `triggered` | `true` if trigger phrase matched, `false` otherwise |
| `same-repo` | `true` if the PR head is a branch in this repository |
| `head-repo` | Full name of the repository the PR head lives in, empty if the fork was deleted |
| `pr-number` | The PR number |
| `pr-ref` | The PR branch name, in the head repository |

`pr-ref` is `.head.ref`, which for a fork pull request is a branch name
in the *fork* and carries no indication of that. Callers hand it to
`actions/checkout` and `git push` against their own repository, so a fork
PR opened from the fork's default branch would name `main` and act on the
wrong branch entirely. That is why fork pull requests are refused here
rather than in each caller: the guard cannot be lost when a project edits
its workflows, and every consumer inherits it at `@main` without changing
anything, because it is folded into `authorized`.

## review-pr-with-claude

Runs an automated code review on a pull request using Claude Code.

**Usage:**

```yaml
- uses: shakenfist/actions/review-pr-with-claude@main
  with:
    pr-number: ${{ github.event.issue.number }}
```

**Inputs:**

| Name | Required | Default | Description |
|------|----------|---------|-------------|
| `pr-number` | Yes | - | The PR number to review |
| `max-turns` | No | scaled from the diff | Maximum Claude turns |
| `force` | No | `false` | Review even if bot has already reviewed |

Leave `max-turns` alone unless you are pinning it for a test. The
budget is 50 turns plus 10 for every 500 lines of diff, capped at 150,
because a fixed budget is either generous on a 200 line diff or too
small on a 1600 line one -- and running out costs the entire review,
after it has been paid for. Over 1000 lines the prompt also asks the
reviewer to work in priority order and cap itself at fifteen items, so
that it converges instead of being cut off mid-sentence.

### When the reviewer cannot produce a review

The reviewer is not a required check anywhere in the fleet, so a red
job buys nothing on its own: it does not block a merge, it leaves an X
for a human to triage. What the exit code means is therefore split by
whose problem the outcome is, and every case says which one it was in
the job summary rather than only in the step log.

| Outcome | Job | What happens |
|---|---|---|
| Bot has already reviewed, and `force` is unset | Green | Skipped silently, as before |
| Diff over GitHub's 20,000-line API cap | Green | A comment on the PR explaining the options |
| Turn budget exhausted with no review produced | Green | A comment on the PR saying so, and suggesting a re-review or a smaller PR |
| Response truncated mid-JSON | Green | The findings that completed are posted, headed by a warning that the review is partial |
| Response held no recoverable review | Red | The reviewer or the prompt is at fault, not the PR |
| The SDK errored, or the JSON failed schema validation | Red | Same -- a tooling problem worth a human's attention |

The first four are ordinary outcomes of reviewing a large change, and
the money is spent by the time they are reached, so they buy an
explanation on the pull request instead of a red X. The last two mean
this repository is broken.

A green job means the reviewer reached a known endpoint. It does not
mean the pull request was reviewed -- read the comment.

## setup-test-environment

Sets up the test environment for Shaken Fist projects: checks out the
actions, shakenfist, client-python and agent-python repositories. The
checkout of the repository that triggered the workflow is at the
triggering ref (for a pull request, the PR merge ref -- the change as
merged into its base); the others are at their default branches.

## build-smoke-cluster

Provisions under-cloud test instances and deploys a Shaken Fist cluster
onto them via the shakenfist.shakenfist collection, leaving the cluster
usable by later steps in the same job. Requires setup-test-environment
to have run first. Outputs the cluster coordinates (`primary`,
`upload_target`, `namespace`, `inventory`).

## setup-kerbside-environment

Sets up the Kerbside-specific test environment: checks out kerbside-patches,
assembles patched source, provisions a test VM, installs build dependencies,
and configures the CI registry.

## deploy-kolla-ansible

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
| `use_ci_registry` | No | `false` | Pull from CI registry; pass `--use-ci-registry` to bootstrap and post-install. When `false`, CI registry settings are stripped from `globals.yml` so Kolla-Ansible uses local images. |

**Steps performed:**
1. Bootstrap Kolla-Ansible (with conditional registry/kerbside/ci-registry flags)
2. Run pre-checks
3. Pull images (only when `use_ci_registry` is `true`)
4. Deploy
5. Install patched OpenStack clients
6. Post install Kolla-Ansible setup

## deploy-kerbside-on-shakenfist

Provisions the Kerbside integration in a running single-node Shaken Fist
cluster (the `build-smoke-cluster` primary) and deploys a kerbside proxy
co-located on that primary, pointed at the cluster via a `type: shakenfist`
source. Used by kerbside's `sf-e2e-functional.yml` end-to-end lane. Mirrors
`deploy-kolla-ansible`'s shape (SSH into the primary, run a sequence of
steps, fail fast). The caller stages a kerbside checkout and a built proxy
wheel on the runner first.

**Inputs:**

| Name | Required | Default | Description |
|------|----------|---------|-------------|
| `base_user` | No | `debian` | SSH user on the SF primary |
| `primary` | Yes | - | Egress address of the SF primary node |
| `system_key` | Yes | - | The SF system namespace key |
| `kerbside_public_fqdn` | No | `http://127.0.0.1:13002` | `KERBSIDE_URL` set in SF; also the token audience and exchange-URL base (must equal kerbside's `SF_CONSOLE_TOKEN_AUDIENCE`) |
| `token_duration` | No | `300` | `KERBSIDE_TOKEN_DURATION` (seconds) set in SF |
| `kerbside_src` | Yes | - | Runner path to the kerbside checkout to deploy |
| `proxy_wheel` | Yes | - | Runner path/glob to the staged kerbside-proxy wheel |
