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

## Package resolution policy

The bulk `"*"` updates in `ci-image.yml` run with `nobest: true` and a
retry loop. A Red Hat derived mirror which is mid-sync can offer a best
candidate whose dependencies have not landed yet, which the default
best-candidate resolution reports as an error and which would otherwise
fail an image build over upstream timing rather than anything under
test. `nobest` degrades that to the newest self-consistent package set,
and the retries cover the transport half of the same problem. Targeted
`dnf` installs are left strict, because a package the playbook names by
hand failing to resolve is a real failure. For the same reason the
package list installed straight after the bulk update asks for
`present` rather than `latest`: an upgrade there would resolve strictly
and undo the decision `nobest` had just made.

The trade this makes is worth knowing when a CI image looks wrong. Before
`nobest`, mirror skew failed the build and the `sfci-image` label kept
pointing at the last good snapshot; now the build succeeds and
publishes an image assembled from whatever the mirror could satisfy,
which every subsequent CI run uses until the next rebuild. Nothing in
the task output distinguishes the newest self-consistent set from the
newest set, and the retry loop hides the transport transient too, so a
package unexpectedly behind on a CI image is a symptom worth checking
against the dnf output of the build that produced it.

`nobest` requires ansible-core >= 2.11 on the controller. `ci-image.yml`
has no caller in this repository and is run by hand, so that is the
version the operator happens to have: a 2.10 controller pointed at a
Red Hat derived base image now fails with `Unsupported parameters for
(dnf) module: nobest` rather than with a mirror error. Debian targets
are unaffected, because the dnf tasks are skipped there entirely. The
same reasoning, and the same flag, appears in
`tools/ovirt-install-base.sh`.

## Linting

Neither `yamllint` nor `ansible-lint` is enabled against this directory
yet. Both report large backlogs -- 191 and 732 findings respectively --
that are mostly stylistic, and the reasoning for leaving them off is in
[ci.md](ci.md).
