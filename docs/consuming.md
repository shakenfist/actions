# Consuming these actions

How to wire a repository up to the shared CI toolkit. For the input and
output surface of each action see [actions.md](actions.md); for how a
run flows through them see [ARCHITECTURE.md](https://github.com/shakenfist/actions/blob/main/ARCHITECTURE.md).

Everything here is resolved at `@main`. There are no version tags and no
release process, so whatever has landed in this repository is what your
next CI run gets.

## Adding Shaken Fist smoke CI to your repository

Two modes, depending on what "have I broken things?" means for your
repository. Both need a `[self-hosted, vm, debian-12]` runner.

**Mode 1 — your check is Shaken Fist's own smoke suite** (the component
you develop is deployed into the cluster and the standard suite
exercises it). This mode only tests YOUR change when your repository is
one of the components the deploy builds from a checkout — shakenfist,
client-python or agent-python. For any other repository it deploys pure
develop and your change is never exercised: use Mode 2 instead.

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

**Mode 2 — you want to run your own tests against a live cluster**
(nothing of yours is inside the cluster; you test your integration with
it). Add your own `actions/checkout` for your repository's test content
— setup-test-environment only checks out the Shaken Fist component
repositories:

```yaml
jobs:
  smoke:
    runs-on: [self-hosted, vm, debian-12]
    steps:
      - name: Setup test environment
        uses: shakenfist/actions/setup-test-environment@main

      - name: Build the smoke cluster
        id: cluster
        timeout-minutes: 90
        uses: shakenfist/actions/build-smoke-cluster@main

      - name: Run my tests against the cluster
        run: |
          # The cluster's API is on the primary; credentials are in
          # /etc/sf/sfrc on the cluster nodes. For example:
          ssh -i /srv/github/id_ci -o StrictHostKeyChecking=no \
              -o UserKnownHostsFile=/dev/null \
              debian@${{ steps.cluster.outputs.primary }} \
              '. /etc/sf/sfrc; /srv/shakenfist/venv/bin/sf-client node list'
```

The cluster's lifetime is the job: nothing tears it down explicitly, the
under-cloud reaper collects the test instances afterwards. The deploy
builds the shakenfist server and client wheels from the checkouts made
by setup-test-environment, so cross-repo changes must land in
dependency order.

## Adding a bot-triggered workflow

`pr-bot-trigger` turns an `@shakenfist-bot` pull request comment into a
gated, authorized trigger for anything you want to run. A complete
example:

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

Note the `if:` on the job as well as the check inside the action. The
job-level `contains()` is what stops every comment on every pull request
from starting a runner; the action is what decides whether the commenter
is allowed to do this and whether the pull request is a fork.

## Adding the automated reviewer

Call `pr-auto-review.yml` as a job, gated on your test jobs via
`needs:`, and grant it the token scope it needs from the caller:

```yaml
jobs:
  automated_reviewer:
    needs: [lint, unit-tests]
    permissions:
      contents: read
      pull-requests: write
      issues: write
    uses: shakenfist/actions/.github/workflows/pr-auto-review.yml@main
```

Do **not** add `secrets: inherit`. Nothing in the reviewer chain reads a
secret -- it authenticates with `github.token` from the `permissions:`
block above -- and inheriting hands every secret your repository holds
to a workflow in another repository for no benefit.

A pull request is reviewed exactly once this way. The reviewer skips a
pull request the bot has already looked at unless `force` is set, and
this path deliberately does not set it, so deploy `pr-re-review.yml`
alongside or the fixes made in response to a review are never seen. The
shared templates for that and the other bot workflows live in
`shakenfist/development/templates/ci-review-automation/`.
