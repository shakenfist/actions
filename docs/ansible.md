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

The gate matters even where nothing SSHes in directly afterwards: on the
image build and topology playbooks it stops Ansible's package tasks from
fighting cloud-init for the dpkg lock.

New playbooks that create instances should follow the same pattern. Put
the readiness play immediately after the provisioning play, before the
first play that does real work on the new hosts.

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
