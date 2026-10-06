# Consuming these actions

How to wire a repository up to the shared CI toolkit. For the input and
output surface of each action see [actions.md](actions.md); for how a
run flows through them see [ARCHITECTURE.md](https://github.com/shakenfist/actions/blob/main/ARCHITECTURE.md).

Everything here is resolved at `@main`. There are no version tags and no
release process, so whatever has landed in this repository is what your
next CI run gets.

## Adding Shaken Fist smoke CI to your repository

Two modes, depending on what "have I broken things?" means for your
repository. Both need a `[self-hosted, vm, debian-13]` runner.

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
    with:
      component: your-repo-name
      component_ref: ${{ github.sha }}
      tier: smoke
```

`test_kind` chooses the suite: `functional` (the default) or
`ansible-modules`. Any other value fails the job in its first steps,
before the deploy, with an "Unknown test_kind" annotation. Before that
check existed, a typo here deployed a full cluster, skipped every test,
and still passed.

Do **not** add `secrets: inherit`, here or when calling any other
reusable workflow in this repository. `smoke-cluster.yml` reads no
secret -- it reaches cluster nodes with the runner's on-disk key -- and
inheriting hands every secret your repository holds to a workflow called
at a moving `@main`. A reusable workflow here that needs a secret
declares it under `on.workflow_call.secrets`, and you pass that one by
name. The fleet
[reusable-workflow-secrets audit](https://github.com/shakenfist/development/blob/main/docs/audits/reusable-workflow-secrets.md)
reports a caller that inherits.

`smoke-cluster.yml` carries its own concurrency group, so callers do not
need one on the calling job. That group is merge-group aware: the
workflow inherits your event, and on `merge_group` your `github.ref` is
the per-attempt queue branch `gh-readonly-queue/<base>/pr-<N>-<SHA>`,
whose SHA GitHub mints afresh on every rebuild of the group. Keyed on
that alone, superseded merge groups were never cancelled and ran whole
clusters against the shared under-cloud
([shakenfist/kerbside#284](https://github.com/shakenfist/kerbside/issues/284)),
so the key now uses `github.event.merge_group.base_ref` in the queue.
That relies on your merge queue being serial — `max_entries_to_build: 1`,
which the fleet
[merge-queue-config audit](https://github.com/shakenfist/development/blob/main/audits/merge-queue-config.md)
requires. If you raise it, several merge groups are live at once and
this key would cancel one the queue is still waiting on.

In Mode 2 the concurrency group is yours to declare, and the same rule
applies to it; see the
[merge-group-cancellation audit](https://github.com/shakenfist/development/blob/main/audits/merge-group-cancellation.md)
for the pattern.

**Mode 2 — you want to run your own tests against a live cluster**
(nothing of yours is inside the cluster; you test your integration with
it). Add your own `actions/checkout` for your repository's test content
— setup-test-environment only checks out the Shaken Fist component
repositories:

```yaml
jobs:
  smoke:
    runs-on: [self-hosted, vm, debian-13, s]
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

The size element of that `runs-on` is not optional. The conductor takes
the runner size from the labels, and a `vm` runs-on which names none
falls back to the first entry in its size table -- `xs`, one vCPU and
2048 MB -- which is too small to drive the deploy. Mode 1 consumers
inherit the size from the reusable workflow, but in Mode 2 the job is
yours and so is the size.

The cluster's lifetime is the job: nothing tears it down explicitly, the
under-cloud reaper collects the test instances afterwards. The deploy
builds the shakenfist server and client wheels from the checkouts made
by setup-test-environment, so cross-repo changes must land in
dependency order.

### The headroom gate can fail a Mode 1 job

`smoke-cluster.yml` samples the cluster's spare capacity while the test
suite runs -- both `test_kind: functional` and `test_kind: ansible-modules`
are probed -- and prints a summary afterwards. That instrument
is deliberately unable to fail a build -- a probe which can fail the
thing it measures is measuring itself -- with one exception.

The summary ends in a verdict on the cluster's headroom: the ratio of
committed vCPU to the schedulable ledger, judged against a band fitted
to a distribution of past CI runs. When that verdict says the cluster
was outside the band and the job has opted in to the gate (see below),
the collection step exits non-zero and the job fails, with a message
saying so in as many words. Nothing else in the probe can do that:
every other failure it meets is logged and swallowed.

The distinction worth knowing when you see one is that this is not a
test failure and usually not about your change. It says the cluster the
suite ran on was the wrong size -- too tightly packed to schedule
reliably, or so empty that CI is paying for capacity it never uses. The
fix is a topology change in the fleet, not a change to your pull
request, so the useful response is to say so rather than to retry.

You will see it before you open the log: a band violation annotates the
run with "Cluster headroom outside the CI sizing band", which renders at
the top of the run summary.

The opposite case is annotated too. When the instrument itself fails --
no series was collected, the report or the verdict script is missing,
or the report exited for a reason other than the band -- the run carries
a "Headroom verdict withheld" warning, and a missing refusal census
carries "Refusal census not collected". Neither can fail your job. They
are there so that a run which measured nothing does not look like a
healthy one, and the step log says which failure it was. On a run that
failed before its tests -- a deploy that never finished, say -- the probe
never wrote a series, so the withheld warning appears beside the real
failure; it is a consequence of that failure, not a second one. A
component ref predating the headroom probe has neither probe nor report,
and is not annotated at all.

The gate is off unless you opt in, and it is ignored entirely for
`test_kind: ansible-modules`: that shape is probed, but no warn window has
measured it yet, so `smoke-cluster.yml` ands your `headroom_gate` with the
test kind rather than trusting a band that was never fitted to it. A
violation there is printed and annotated, never fatal.

Without the gate the verdict is still computed and printed, it just cannot
fail your job. Opt in only for a job shape a warn window has measured: the band is fitted in the
shakenfist repository against the shapes it has harvested, and this
workflow is consumed at `@main`, so there is no version of it you can
pin to instead. Opting in is one input:

```yaml
    with:
      component: your-repo-name
      component_ref: ${{ github.sha }}
      tier: full
      headroom_gate: true
```

An armed caller that wants to be able to switch the gate off without a
commit can pass an expression over a repository variable instead, as
shakenfist's merge matrix does with
`${{ vars.CI_HEADROOM_GATE != 'false' }}`: armed while the variable is
unset, disarmed by setting it to `false`. The expression has to evaluate
to a boolean -- `${{ vars.X }}` on its own is a string, and fails the
input's `type: boolean` validation before the job starts.

Only Mode 1 is affected. Mode 2 builds its own cluster and never runs
the collection step.

The band, the numbers behind it and the evidence they were fitted to
live in
[PLAN-ci-cloud-sizing.md](https://github.com/shakenfist/shakenfist/blob/develop/docs/plans/PLAN-ci-cloud-sizing.md)
in the shakenfist repository, along with the report tool itself.

### Deploy profiles

A deploy profile changes what the cluster deploys without any of the
change living here. It is a Jinja2 template in your checkout, named by
the `deploy_profile` input of `smoke-cluster.yml` (or
`build-smoke-cluster`, in Mode 2) as a path relative to
`GITHUB_WORKSPACE`:

```yaml
    with:
      component: shakenfist
      component_ref: ${{ github.sha }}
      topology: slim-tier
      deploy_profile: shakenfist/tools/ci-deploy-profiles/kerbside-slim-tier.yml.j2
```

The input is empty by default, and an empty input changes nothing: the
deploy command is the one it was before profiles existed.

The template renders against the topology facts the run wrote, never
against ansible, and an undefined name is an error rather than an empty
string. `nodes` maps each topology host name to its `name`,
`egress_ip`, `mesh_ip`, `is_hypervisor`, `is_network_node` and
`is_database_node`, so the primary's mesh address is
`{{ nodes.primary.mesh_ip }}`; `workspace` is the absolute
`GITHUB_WORKSPACE`. Host names come from the topology: `slim-tier` has
`primary`, `sf1` and `sf2`.

It must render to YAML with only these keys, all optional:

```yaml
groups:              # new inventory groups, by host name, with group vars
  kerbside:
    hosts: [sf2]
    vars:
      api_url: http://{{ nodes.primary.mesh_ip }}:13000
extra_vars:          # passed to site.yml after the fixed extra vars, so they win
  kerbside_url: http://{{ nodes.sf2.mesh_ip }}:13002
mariadb_sql: |       # run once with sudo mariadb on the primary, before the deploy
  CREATE DATABASE IF NOT EXISTS kerbside;
redeploy_check:      # deploy a second time; no matching unit may restart
  units: ['sf-*.service', 'kerbside-*.service']
test_env:            # exported into the test run; values are strings
  SF_CI_EXPECT_VDI_CONSOLE_PROXY: '1'
```

`redeploy_check` runs the deploy a second time, with the same command
line, inside `build-smoke-cluster`'s deploy step, and fails that step if
any matching unit restarted. Before the second deploy,
`tools/ci-redeploy-check.py` records the systemd `InvocationID` of every
loaded unit matching a glob on every inventory host; afterwards it reads
them again and prints each unit as unchanged, new, restarted or
vanished. A restarted or vanished unit fails the step with an annotation
naming the unit and its host, and a new one is reported but allowed.
`InvocationID` changes on every start, including one systemd's own
`Restart=` made, so a crash-looping daemon cannot pass. A glob that
matches no unit on any host fails the check before the second deploy
rather than passing vacuously. A host that does not answer the first
time is skipped with a warning (slim-primary deliberately lists one that
never exists); one that answered then and not afterwards has vanished
units. The check runs straight after the first
deploy because later steps restart `sf-api` on purpose (the JWKS CA and
drain steps). The log shows each deploy's elapsed seconds; the second
costs roughly as long as the first, so check the build step's
`timeout-minutes` (90 in `smoke-cluster.yml`) has room.

`test_env` is copied to the primary as a `0600` file and sourced after
`/etc/sf/sfrc` in the remote command that runs the suite, for both the
`functional` and `ansible-modules` test kinds, so a variable there wins
over one sfrc sets. Without a profile, or with no `test_env`, the remote
command is unchanged.

Anything else fails the deploy before it starts, with a "Deploy profile
rejected" annotation: an unknown key at any level, a host the topology
does not have, a group that already exists, or a name the facts do not
define. A profile may add groups but not change the ones the inventory
already has.

Extra vars and SQL usually carry credentials, even throwaway CI ones, so
they are written to `0600` files on the runner, the SQL reaches MariaDB
on stdin, and the log names variables but never shows their values.
`tools/ci-apply-deploy-profile.py`'s docstring is the full reference,
including the files it writes for later steps;
`tools/ci-redeploy-check.py`'s documents the restart check.

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
    runs-on: [self-hosted, static]

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

Keep the `permissions:` block: the reviewer authenticates with
`github.token`, and that block is what gives the token its scope. As
with [every reusable workflow here](#adding-shaken-fist-smoke-ci-to-your-repository),
do not add `secrets: inherit`.

A pull request is reviewed exactly once this way. The reviewer skips a
pull request the bot has already looked at unless `force` is set, and
this path deliberately does not set it, so deploy `pr-re-review.yml`
alongside or the fixes made in response to a review are never seen. The
shared templates for that and the other bot workflows live in
`shakenfist/development/templates/ci-review-automation/`.
