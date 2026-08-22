# The CI playbooks

The `ansible/` directory contains playbooks used by CI workflows for
provisioning and configuring test infrastructure. They are invoked by
the composite actions rather than run directly; `build-smoke-cluster` is
the main caller.

- **ci-image.yml**: Builds CI base images with pre-installed packages.
- **ci-dependencies.yml**: Downloads and caches VM images.
- **ci-topology-\*.yml**: Provisions multi-node test clusters.
- **ci-gather-logs.yml**: Collects logs from test nodes after runs.

`ci-topology-*.yml` is where the shape of the under-cloud is chosen:
`localhost` for the single-node smoke case, `slim-primary` and
`slim-tier` for multi-node. Each writes a JSON facts file which
`tools/ci-make-inventory.py` turns into the deploy inventory, so one
code path covers every node count.

## Instance readiness

Every playbook that creates instances waits for them before using them,
and that wait is in two parts.

The cheap part is the familiar `wait_for` on port 22 with
`search_regex: OpenSSH`. It only proves sshd is listening and has emitted
a banner, which is enough to fail fast on a guest that never booted, and
nothing more than that. sshd answers early in boot; cloud-init then
regenerates the host keys and restarts sshd, so a connection landing in
that window is dropped with "connection refused". On a busy hypervisor
that window is wide enough to be hit regularly.

The real gate is `ansible/tasks/wait-for-cloud-init.yml`, imported by a
short play targeting the freshly created hosts:

```yaml
- name: Wait for instances to finish cloud-init
  hosts: allsf
  gather_facts: false
  tasks:
    - import_tasks: tasks/wait-for-cloud-init.yml
```

It establishes a real authenticated connection with
`wait_for_connection` (which retries through the sshd restart) and then
runs `cloud-init status --wait`, which blocks until cloud-init reaches a
terminal state. Both steps absorb a slow hypervisor without a magic
sleep. The exit code is deliberately ignored -- the gate exists to wait
out the cloud-init window, not to assert cloud-init succeeded, and a
genuinely broken guest produces a better error in the tasks that follow.
`--wait` has no timeout of its own, so it is capped with `timeout 600`
rather than being allowed to consume the whole workflow budget.

`wait_for_connection` returns on the first connection that
authenticates, which is usually the sshd that is about to be restarted,
so the `cloud-init status` command can itself land in the bounce. Both
attempts run with `ignore_unreachable`, and the first is retried once
behind a second `wait_for_connection`, so the gate cannot fail the play
with the flake it exists to remove. Because both attempts are failure
tolerant, the result may carry no return code at all; the log line that
reports it defaults every field it interpolates, and names the missing
return code rather than printing a bare "unknown" -- that case means the
gate returned without waiting for anything.

Budget for the slow path when reading a timeout. One gate can spend
about twenty minutes on one host -- 300s waiting for a connection, an
unreachable attempt, 300s waiting for the replacement sshd, then the
600s cap in the retry. That figure is per fork batch rather than per
play: nothing here sets `forks`, so Ansible's default of five applies,
and the largest topology puts six hosts in `allsf`, which is two
batches.

The topology playbooks are the ones that run under a GitHub timeout.
`build-smoke-cluster` invokes `ci-topology-<topology>.yml`, which
imports the gate once, under the 90 minute `timeout-minutes` on that
step in `smoke-cluster.yml`. `ci-image.yml` and `ci-image-desktop.yml`
import the gate twice each, but nothing in this repository invokes
them -- they are driven by conductor, with no GitHub timeout over them.
On the normal path, where cloud-init has already finished by the time
SSH answers, the gate costs seconds either way.

The gate matters even where nothing SSHes in directly afterwards: on the
image build and topology playbooks it stops Ansible's package tasks from
fighting cloud-init for the dpkg lock.

New playbooks that create instances should follow the same pattern. Put
the readiness play immediately after the provisioning play, before the
first play that does real work on the new hosts.
`tests/test_ansible_readiness.py` enforces this: it parses every YAML
file under `ansible/`, works out which plays create instances, and fails
if any of them is not followed by a readiness play that both imports the
gate and targets the hosts that play added. Matching on hosts rather
than on position is what makes a partially gated playbook fail: the two
image playbooks each build two independent instances, so a gate play
copied without changing its `hosts:` would otherwise pass. That
check exists because the invariant cannot be tested any other way before
merge -- the fabric is not available on a dev host -- and because it has
already been broken once, when only one of twelve provisioning paths
grew the gate.

## CI caching

The playbooks configure remote VMs to use local caches:

- **apt proxy**: Writes `/etc/apt/apt.conf.d/01proxy` pointing to
  `http://192.168.1.15:3128` (Squid).
- **pip mirror**: Writes `/etc/pip.conf` pointing to
  `https://devpi.home.stillhq.com/root/pypi/+simple/` (devpi).
- **collection deploy**: `deploy-collection.sh` (via the
  build-smoke-cluster action) exports `http_proxy`, `https_proxy` and
  `PIP_INDEX_URL` for package operations during deployment.

Plays targeting remote hosts also set `environment:` directives to
pass proxy settings to Ansible modules (apt, get_url, etc.).

## Linting

Neither `yamllint` nor `ansible-lint` is enabled against this directory
yet. Both report large backlogs -- 191 and 732 findings respectively --
that are mostly stylistic, and the reasoning for leaving them off is in
[ci.md](ci.md).
