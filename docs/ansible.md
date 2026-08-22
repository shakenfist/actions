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

## Linting

Neither `yamllint` nor `ansible-lint` is enabled against this directory
yet. Both report large backlogs -- 191 and 732 findings respectively --
that are mostly stylistic, and the reasoning for leaving them off is in
[ci.md](ci.md).
