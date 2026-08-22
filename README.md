# Shaken Fist Shared GitHub Actions

The shared CI toolkit for the [Shaken Fist](https://github.com/shakenfist)
ecosystem: composite actions and reusable workflows that stand up a real
Shaken Fist cluster, run a test suite against it, gather the logs, and
run the fleet's automated code reviewer. It ships no product of its own
— everything here exists to be consumed by another repository's
workflows.

Shaken Fist's CI tests Shaken Fist by running it inside itself. A
long-lived cluster hosts the test instances, and a fresh cluster is
deployed onto those instances and torn down with them. That is what
`build-smoke-cluster` does, and it is why this repository exists rather
than each project growing its own copy.

**Every consumer pins `@main`, so a merge here is a deploy.** There are
no version tags and no release process: the next downstream CI run in
any repository picks up whatever just landed, with no staging step in
between. If you are about to change something, read
[docs/ci.md](https://github.com/shakenfist/actions/blob/main/docs/ci.md)
first — it explains what that means for testing, what this repository's
own CI does and does not cover, and how to run the checks locally.

## What is published

**Composite actions**, dropped into a consumer's job as a single step:
`setup-test-environment`, `build-smoke-cluster`, `pr-bot-trigger`,
`review-pr-with-claude`, `setup-kerbside-environment`,
`deploy-kolla-ansible` and `deploy-kerbside-on-shakenfist`.

**Reusable workflows**, invoked as whole jobs: `smoke-cluster.yml` (the
full deploy-and-test lane), `pr-auto-review.yml` (the automated
reviewer) and `export-repo-config.yml` (repository settings drift).

## Getting a cluster in your CI

```yaml
jobs:
  smoke:
    uses: shakenfist/actions/.github/workflows/smoke-cluster.yml@main
    secrets: inherit
    with:
      component: your-repo-name
      component_ref: ${{ github.sha }}
      tier: smoke
```

That runs Shaken Fist's own smoke suite. If you want to run *your* tests
against a live cluster instead, or wire up the reviewer or a bot-trigger
comment workflow, see
[docs/consuming.md](https://github.com/shakenfist/actions/blob/main/docs/consuming.md).

## Documentation

| Document | What is in it |
|---|---|
| [docs/consuming.md](https://github.com/shakenfist/actions/blob/main/docs/consuming.md) | How to wire your repository up to these actions |
| [docs/actions.md](https://github.com/shakenfist/actions/blob/main/docs/actions.md) | Inputs, outputs and usage for every composite action |
| [docs/ci.md](https://github.com/shakenfist/actions/blob/main/docs/ci.md) | This repository's own CI, and why so little of it can be tested before merge |
| [docs/ansible.md](https://github.com/shakenfist/actions/blob/main/docs/ansible.md) | The CI playbooks and the local package caches they configure |
| [ARCHITECTURE.md](https://github.com/shakenfist/actions/blob/main/ARCHITECTURE.md) | What the components are and how a run flows through them |
| [AGENTS.md](https://github.com/shakenfist/actions/blob/main/AGENTS.md) | Conventions and traps, for humans and coding agents alike |

## Contributing

When adding a new action: create a directory named for it, add an
`action.yml`, add any supporting scripts, and document it in
[docs/actions.md](https://github.com/shakenfist/actions/blob/main/docs/actions.md).

Before opening a pull request, run the checks CI will run:

```bash
pip install pre-commit
pre-commit run --all-files
python3 -m unittest discover -s tests -t . --verbose
```

## Projects using these actions

- [imago](https://github.com/shakenfist/imago) — disk image management
- [occystrap](https://github.com/shakenfist/occystrap) — container image tools
- [shakenfist](https://github.com/shakenfist/shakenfist) — the main project
