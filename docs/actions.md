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
| `max-turns` | No | `''` (scaled from the diff; `auto` means the same) | Maximum Claude turns |
| `force` | No | `false` | Review even if bot has already reviewed |

Leave `max-turns` alone unless you are pinning it for a test. The
budget is 50 turns plus 10 for every 500 lines of diff, capped at 150,
because a fixed budget is either generous on a 200 line diff or too
small on a 1600 line one -- and running out costs the entire review,
after it has been paid for. Over 1000 lines the prompt also asks the
reviewer to work in priority order and cap itself at fifteen items, so
that it converges instead of being cut off mid-sentence.

Passing an integer pins the budget and opts out of that scaling, which
matters for consumers copied from an older version of the example
above: a workflow still passing `max-turns: '50'` gets 50 turns on
every diff, exactly the behaviour this scaling replaced. Remove the
line to get scaling back, or pass `auto` if something in the workflow
needs the input to be there. A value that is not a positive integer of
at most four digits -- including `0`, which the CLI rejects -- is
ignored, with a warning, in favour of scaling.

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
| A complete review whose JSON was malformed by an unescaped `"` or a raw newline inside a string | Green | The review is repaired and posted as normal |
| Response truncated mid-JSON, with at least one complete finding | Green | The findings that completed are posted, headed by a warning that the review is partial |
| Response truncated before any finding completed | Green | A comment on the PR saying so; there is nothing to salvage |
| Response held no JSON review, and the turn budget was exhausted | Green | As the turn-budget row above: a comment on the PR, since the reviewer ran out of room rather than going wrong |
| Response held no JSON review, with turns to spare | Red | The reviewer or the prompt is at fault, not the PR. The response is posted to the PR as it came, marked as unparsed, so its findings are not lost |
| The CLI wrote something that is not a JSON envelope | Red | The CLI failed; nothing can be read out of it |
| The SDK errored | Red | Same -- a tooling problem worth a human's attention |
| A review that was not truncated failed schema validation | Red | A tooling problem too, and like an unparseable one its response is posted to the PR as it came |

The first seven are ordinary outcomes of reviewing a large change, and
the money is spent by the time they are reached, so they buy an
explanation on the pull request instead of a red X. The last four mean
this repository, or the tooling under it, is broken.

Truncation is told apart from the other failures by the fences. A
response with no ```json fence at all was never writing a review, and a
fence that closed says the response finished writing what is inside it
-- so JSON in there that will not parse is the reviewer emitting
something invalid rather than running out of room. Only a fence left
open, or an unfenced object running to the end of the response, is
treated as having been cut off.

Invalid JSON in a closed fence is usually a model quoting a literal --
a description discussing `packages = ["x"]` with the inner quotes left
unescaped. Before giving up on it, the extractor escapes every quote
that cannot be ending its string, judged by whether what follows it is
JSON structure or prose, and accepts raw newlines inside strings. The
result is posted only if it parses into a review with at least one
valid finding; anything that repair does not fix is a tooling problem
and goes red.

A truncated response can carry the same stray quote, and there it does
more damage: the salvage walk-back tracks strings to find where it can
safely cut, and an unescaped quote puts every cut after it in the wrong
place. So a truncated block that will not salvage as it came is
repaired and salvaged again, and reported as partial like any other
salvage.

A red outcome with a response in hand still posts that response.
The model call usually completed normally, and the findings in it are
real; left in the job log they reach nobody, because a red reviewer job
is the last place anyone looks. The comment puts the response in a code
fence longer than any backtick run inside it, breaks any mention of the
bot so a quoted trigger phrase cannot fire, and cuts it to fit GitHub's
comment size limit, with the full text still in the step log. It is an
ordinary comment rather than a review, so it does not satisfy the
already-reviewed check and the next review runs as normal.

The same explanation is not posted twice: each of these comments
carries an HTML marker naming its reason, and a handler that finds its
own marker already on the pull request writes the job summary and skips
the comment, so re-review rounds on an oversized diff do not stack up
identical notes. That check reads a bounded page of the pull request's
comments, so on a very long thread an older explanation can scroll out
of view and be posted again.

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

The calling job must request at least an `s` runner
(`runs-on: [self-hosted, vm, <image>, s]`). A composite action runs on
whatever runner its caller asked for, so the size is the consumer's to
get right, and the wheel builds and the ansible deploy both run on the
runner itself -- measured demand is roughly 2.7 GB, against the 2048 MB
a sizeless runs-on silently falls back to.

Two things here are still Debian 12 on purpose, although the runners
that host them are not. `base_image` defaults to
`sf://label/ci-images/debian-12`, the image the under-cloud instances
boot: a trixie under-cloud deploys a cluster perfectly well and then
leaves every instance on it reporting its agent as "not ready (no
contact)", which is tracked as
[shakenfist/shakenfist#4280](https://github.com/shakenfist/shakenfist/issues/4280).
And the cached disk this action uploads into the nested cluster is
uploaded under the artifact name `debian-12`, which is load bearing:
`shakenfist_ci`'s `CLUSTER_CI_IMAGE` and a long tail of individual tests
name that string literally, so it is a fleet-wide rename rather than a
default to change. Bookworm is in LTS rather than unsupported, so both
are defensible positions to hold while #4280 is open -- but neither is
an oversight, and the `eol-distro` consistency audit does not judge
guest images, so nothing will remind you of them.

## setup-kerbside-environment

Sets up the Kerbside-specific test environment: checks out kerbside-patches,
assembles patched source, provisions a test VM, installs build dependencies,
and configures the CI registry.

**Inputs:**

| Name | Required | Default | Description |
|------|----------|---------|-------------|
| `base` | Yes | `debian:13` | Base cloud image for the test VM |
| `base_user` | Yes | `debian` | SSH user on the test VM |
| `openstack_release` | Yes | `master` | OpenStack release to target |
| `topology` | Yes | `all-in-one` | Deployment topology (`all-in-one`, `multinode` or `multinode-2`), which selects the playbook |
| `vip_address` | No | `10.0.2.3` | The address the deployment brings up as its own VIP, reserved in Shaken Fist before anything else is allocated. Must match `kolla_internal_vip_address` in the globals file being deployed -- see [The deployment VIP](ansible.md#the-deployment-vip) |

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

## deploy-proxmox-on-shakenfist

Stands up a single-node Proxmox VE 9 hypervisor as an instance in the
runner's own Shaken Fist namespace: a booted SPICE smoke guest, a
least-privilege API token, and a runner that can resolve and reach the
node by its FQDN. Everything happens inside the calling job, because a
Proxmox console ticket lasts only about 30 seconds and a client under
test has to mint and connect within that window. The action's own
files -- the playbook, its task files and the two `tools/proxmox-*`
helpers -- are resolved through `github.action_path`, so a caller
always gets them from the same ref as the `action.yml` it resolved; see
[ansible.md](ansible.md#proxmox-node) for what the playbook does and
[ci.md](ci.md) for the lane this makes possible.

**Usage:**

```yaml
- name: Deploy a Proxmox VE node
  id: proxmox
  timeout-minutes: 90
  uses: shakenfist/actions/deploy-proxmox-on-shakenfist@main

- name: Mint a console ticket and connect, immediately
  env:
    MINT_SCRIPT: ${{ steps.proxmox.outputs.mint_script }}
    API_URL: ${{ steps.proxmox.outputs.api_url }}
    NODE: ${{ steps.proxmox.outputs.node_name }}
    VMID: ${{ steps.proxmox.outputs.vmid }}
    TOKEN_ID: ${{ steps.proxmox.outputs.token_id }}
    TOKEN_FILE: ${{ steps.proxmox.outputs.token_file }}
    CA_FILE: ${{ steps.proxmox.outputs.ca_file }}
  run: |
    "${MINT_SCRIPT}" --api-url "${API_URL}" --node "${NODE}" --vmid "${VMID}" \
        --token-id "${TOKEN_ID}" --token-file "${TOKEN_FILE}" --ca-file "${CA_FILE}" \
        --out "${RUNNER_TEMP}/console.vv"
    # Dial the .vv here, straight away: a Proxmox ticket is good for
    # about 30 seconds from the mint.
```

The calling job needs at least an `s` runner
(`runs-on: [self-hosted, vm, <image>, s]`): the deploy drives an
Ansible run from the runner itself, the same reason `build-smoke-cluster`
gives for its own minimum, and a sizeless `vm` job silently falls back
to `xs` (see `AGENTS.md`, *A `vm` runs-on must also name a size*).

**Inputs:**

| Name | Required | Default | Description |
|------|----------|---------|-------------|
| `base_user` | No | `debian` | The user the Debian base image logs in as |
| `node_address` | No | `10.0.2.2` | The node's address on the `proxmox` network the playbook creates (`10.0.2.0/24`); must sit inside that block |
| `smoke_vmid` | No | `100` | The VM id of the SPICE smoke guest. PVE requires 100 or more |
| `workdir` | No | `''` | Runner directory, created `0700`, for the token secret, the node CA, the deployment facts and a shakenfist checkout. Empty means `$RUNNER_TEMP/proxmox` |

**Outputs:**

| Name | Description |
|------|-------------|
| `node_name` | The PVE node name, used in API paths (`/nodes/<node_name>/...`) |
| `node_address` | The node address the runner reaches it on |
| `node_fqdn` | The node FQDN: the host of a ticket's proxy URL, and the CN of its certificate. Resolvable on the runner through `/etc/hosts` |
| `api_url` | `https://<node_fqdn>:8006`, the PVE API |
| `token_id` | The API token id, `user@realm!token` |
| `token_file` | Path to a `0600` file holding the API token secret. **A live credential**: never print it, pass it on a command line, or upload it. It is masked in the job log |
| `ca_file` | Path to the node's root CA (PEM), for verifying the API and SPICE TLS |
| `vmid` | The VM id of the running SPICE smoke guest |
| `kvm` | `"true"` if the node had `/dev/kvm`, `"false"` if the guest runs under TCG |
| `pve_version` | The `pve-manager` version the node reports |
| `mint_script` | Absolute path to `proxmox-mint-vv.sh`, which mints a `.vv` through the API token: `--api-url --node --vmid --token-id --token-file --ca-file --out` |

**Side effects on the runner, for the rest of the job:** the node's
FQDN is added to `/etc/hosts`, mapped to its address, and both are
appended to `no_proxy` and `NO_PROXY` -- the runner image exports
`http_proxy`/`https_proxy` for a squid cache that cannot route to the
node's network. Neither is undone at the end of the job; the runner is
single-use.

**Mint immediately before connecting.** A Proxmox console ticket is
good for about 30 seconds from the mint, so a consumer should call
`mint_script` as the last thing before dialling, not earlier in the
job -- and never re-use a ticket a previous step already minted.
