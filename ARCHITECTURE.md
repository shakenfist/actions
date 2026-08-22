# Architecture

This repository is the shared CI toolkit for the Shaken Fist ecosystem.
It ships no runnable product of its own: everything here exists to be
consumed by another repository's workflows.

For how this repository's *own* CI works, and why so little of it can be
tested before merge, see [docs/ci.md](docs/ci.md).

## The consumption model

Everything published here is resolved at runtime, by reference, from
another repository:

```yaml
uses: shakenfist/actions/.github/workflows/smoke-cluster.yml@main
uses: shakenfist/actions/build-smoke-cluster@main
```

Two properties follow, and almost every other design decision in this
repository is downstream of them.

**Every consumer pins `@main`.** There are no version tags and no
release process. A merge to `main` is therefore a deploy: the next
downstream CI run in any repository picks it up, with no staging step
in between. The default branch is `main` rather than the fleet's usual
`develop` for exactly this reason -- renaming it would break every
consumer simultaneously -- and the `default-branch-naming` audit carries
an explicit exception for it.

**References cannot be parameterised.** GitHub does not allow
expressions in `uses:`, so there is no way to point a consumer at a
branch of this repository for testing. Relative references
(`uses: ./build-smoke-cluster`) do work, but only when the calling
workflow lives here; called from another repository they resolve
against the *caller's* checkout. This is why `smoke-cluster.yml` names
its composite actions at `@main` even though it lives beside them, and
why changes to those actions are only integration-tested after they
land.

## What gets published

Two kinds of thing, distinguished by how a consumer invokes them.

**Composite actions** are single steps dropped into a consumer's job.
They inherit that job's runner, environment and token.

| Action | Purpose |
|---|---|
| `setup-test-environment` | Checks out `actions`, `shakenfist`, `client-python` and `agent-python` side by side |
| `build-smoke-cluster` | Provisions test instances and deploys a Shaken Fist cluster onto them |
| `pr-bot-trigger` | Validates an `@shakenfist-bot` comment and the commenter's permissions |
| `review-pr-with-claude` | Runs the Claude Code reviewer and posts the result |
| `setup-kerbside-environment` | Checks out the Kerbside-side repositories |
| `deploy-kolla-ansible` | Bootstraps and deploys Kolla-Ansible on a test VM |
| `deploy-kerbside-on-shakenfist` | Deploys a Kerbside proxy onto a running cluster's primary |

**Reusable workflows** are invoked as whole jobs, and bring their own
runner and permissions.

| Workflow | Purpose |
|---|---|
| `smoke-cluster.yml` | The full deploy-and-test lane: cluster, test suite, log bundle |
| `pr-auto-review.yml` | The automated reviewer, gated on the caller's tests passing |
| `export-repo-config.yml` | Exports repository settings and rulesets, opens a PR on drift |
| `ci.yml` | This repository's own pull request checks |
| `canary.yml` | This repository's post-merge integration check |
| `pr-retest.yml` | Re-runs `ci.yml` on a bot comment |
| `pr-re-review.yml` | Re-runs the reviewer, with `force`, on a bot comment |
| `pr-address-comments.yml` | Works through the review's actionable items on a bot comment |
| `prune-reviews.yml` | Drops review marks made stale by a push to main and commits the regenerated state back |

The last six are the exception to "nothing here runs for itself" --
they exist only for this repository and are not consumed downstream. The
three bot-triggered ones are the shared templates from
`shakenfist/development`, deployed here late: this repository ships the
review automation the fleet runs and did not run it on itself until
after #20 and #21 had both merged with their review fixes unlooked-at.

`prune-reviews.yml` is unlike the rest and worth singling out. It is
the only workflow here that holds a `contents: write` token, commits to
`main` without review, and runs code cloned from another repository --
`shakenfist/development`, at whatever its default branch holds when the
run starts. That trust edge is deliberate and is described in
[docs/ci.md](docs/ci.md), but it is invisible from this side, which is
why it is named here rather than only there.

## How a smoke run flows

This is the load-bearing path, and the one worth understanding before
changing anything under `ansible/` or `build-smoke-cluster/`.

The central idea is that Shaken Fist's CI tests Shaken Fist by running
it inside itself. An existing, long-lived Shaken Fist cluster hosts the
test instances -- the "under-cloud" -- and a fresh cluster is then
deployed onto those instances and torn down with them.

```
caller repository (e.g. shakenfist/shakenfist)
  └── smoke-cluster.yml @main
        ├── setup-test-environment            checkouts, side by side
        ├── build-smoke-cluster
        │     ├── tools/install-collection.sh     collection onto the runner
        │     ├── ansible/ci-topology-<t>.yml     create under-cloud instances
        │     ├── MariaDB and Loki onto primary
        │     ├── tools/ci-make-inventory.py      topology facts -> inventory
        │     ├── collection site.yml             deploy Shaken Fist
        │     └── wait schedulable, import base image, export coordinates
        ├── stestr suite (or the ansible-modules suite)
        └── ansible/ci-gather-logs*.yml           bundle artifact
```

Component selection is worth being explicit about, because it is not
what the input names suggest. `setup-test-environment` checks out the
triggering repository at the triggering ref and every other repository
at its default branch. So a `client-python` pull request is tested
against a `develop` server, and vice versa; cross-repository changes
land in dependency order rather than being tested together. The
`component` and `component_ref` inputs to `smoke-cluster.yml` drive
concurrency grouping, logging and the artifact name -- **not** checkout
selection.

`ansible/ci-topology-*.yml` chooses the shape of the under-cloud:
`localhost` for the single-node smoke case, `slim-primary` and
`slim-tier` for multi-node. Each writes a JSON facts file which
`tools/ci-make-inventory.py` turns into the deploy inventory, so one
code path covers every node count.

## How review automation flows

Two entry points, sharing the machinery underneath.

```
caller CI (tests pass)                 human comment on a PR
  └── pr-auto-review.yml @main           └── pr-bot-trigger @main
        └── review-pr-with-claude @main        └── review-pr-with-claude @main
              ├── review-pr-with-claude.sh              (with force set)
              ├── render-review.py    JSON -> markdown
              └── create-review-issues.py   actionable items -> issues
```

A review is gated three ways: the caller's `needs:` list (tests passed),
a check that the last commit is not the bot's, and a check inside
`review-pr-with-claude` that the bot has not already reviewed this pull
request. Only the bot-triggered path sets `force`, so a human asking is
the sole way to get a second review -- which is why a repository without
`pr-re-review.yml` deployed reviews each pull request exactly once and
never sees the fixes.

The reviewer runs Claude Code with `--dangerously-skip-permissions`
while holding a write-capable token, and a pull request diff is
untrusted input, so `pr-auto-review.yml` restricts itself to
same-repository pull requests. Fork pull requests are reviewed only on
explicit human request.

The review data structure is shared, not ad hoc:
`review-pr-with-claude/review-schema.json` defines it, `render-review.py`
embeds it in a collapsed block in the posted comment, and the
address-comments automation reads it back out of that block. The round
trip is load-bearing -- if the embedded JSON stops parsing, that
automation silently has nothing to work from.

## Supporting material

`ansible/` holds the CI playbooks: topology provisioning, log gathering
(both filesystem and Loki), node health checks, and the Kerbside
instance lifecycle.

`tools/` holds helper scripts. Some run on the CI runner
(`ci-make-inventory.py`, `install-collection.sh`, `clone_with_depends.py`),
and some are copied to and executed on cluster nodes over ssh via
`tools/run_remote` (`ci_drain_check.sh`, `ci_log_checks*.sh`,
`ci_node_checks.sh`). A few are inherited from the `shakenfist`
repository and are not called from anywhere here.

`etc/` holds oVirt answer file templates and patches used by the
Kerbside interoperability lane.

`tests/` holds unit tests for the Python helpers, and `docs/` holds the
prose documentation this file indexes.

## Why it is shaped this way

The repository is a library rather than a service, so it has no
`pyproject.toml`, no packaging and no release workflow; the
`pyproject-usage` and `release-process` audits treat it as a non-Python
project deliberately. Its Python exists to be executed in place by a
workflow step, not imported.

Composite actions cannot carry `timeout-minutes` on their own steps, so
every consumer is expected to put a timeout on the step that uses them.
This is a GitHub limitation rather than a choice, and it is why the
README examples all carry timeouts.

The split between `build-smoke-cluster` (a composite action) and
`smoke-cluster.yml` (a reusable workflow wrapping it) exists so that a
repository with its own test content can get a live cluster inside its
own job, while repositories that just want the standard suite can call
the workflow and pass a few inputs.
