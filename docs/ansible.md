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

## Kolla node prerequisites

The kerbside playbooks add a second short play after the readiness gate,
importing `ansible/tasks/install-kolla-prerequisites.yml`:

```yaml
- name: Install kolla-ansible's node prerequisites
  hosts: allsf
  gather_facts: false
  tasks:
    - import_tasks: tasks/install-kolla-prerequisites.yml
```

It installs `python3-apt` on the Debian hosts. kolla-ansible's baremetal
role begins by gathering package facts, and ansible's `package_facts`
module needs the apt python bindings to select its apt backend. Without
them it does not fall back -- it reports that it could not detect a
package manager at all -- and the deploy dies there, ahead of every
install the role would have done, so the role cannot bootstrap its way
out of it. Rocky hosts take the rpm backend and need nothing.

The `debian:12` base image carried `python3-apt`, so this never had to be
asked for. `debian:13` does not. That difference was invisible on the
all-in-one topologies, where the kolla host is also the deploy host and
kerbside-patches' `_build/install-build-dependencies.sh` pulls the
package in -- `setup-kerbside-environment` runs that script over SSH on
the deploy host and on no other node. Multinode has no such coincidence,
and every node except the deploy host failed on the first run after the
base image moved.

The tasks use `apt-get` rather than the `apt` module on purpose. That
module needs `python3-apt` as well, and although it tries to install it
for itself when it is missing, this is the one host state where that
recovery path would be carrying the deploy rather than tidying up after
it.

Order matters as much as it does for the readiness gate, and for a
related reason: cloud-init holds the apt lock while it runs, so this play
must come after the gate rather than before it.

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

### The dependencies disk

`ci-dependencies.yml` builds a third cache, and unlike the proxy and the
mirror it is a disk rather than a service. The playbook creates a
builder instance with a 50GB second disk, fills it with the cloud images
CI boots -- cirros, the Ubuntu, Debian, CentOS Stream, Fedora and Rocky
minimal images, and the GitHub Actions runner tarball -- and adds the
imago-testdata clone and, when that label already exists, the
`debian-gnome-13` snapshot. The disk is then snapshotted and published
as the `dependencies` label, which every topology attaches as its second
disk and mounts at `/srv/ci`. The `get_url` loop in that playbook is
therefore the definition of what CI can boot without going to the
network; `debian:13` and `rocky:10` sit in it alongside the earlier
releases, and the images alone now come to roughly 10.2GB.

That snapshot lands on the disk at
`/srv/ci/cached/debian-gnome-agents`, and that name deliberately does
not say which Debian release it holds. The name is not private to this
repository: `shakenfist/kerbside` copies the file off the disk by
hardcoded path in its functional tests, so it is a cross-repository
interface. It also has no transition window, because the disk is
reformatted from scratch on every build -- the old name is gone the
moment the `dependencies` label is next republished, and a consumer
still asking for it fails on a branch nobody touched. Encoding the
release in that name therefore made every desktop bump a fleet change.
`gnome_release` in `ci-dependencies.yml` now governs only the label the
playbook looks up and the scratch filename on the runner; the published
name is stable, so bumping the desktop image is no longer a fleet
change at all.

While consumers migrate, the playbook also hardlinks the old
`/srv/ci/cached/debian-12-gnome-agents` to the same blob, which costs
no space and no second transfer. That task and its
`gnome_legacy_cached_name` var are transitional and should be deleted
once nothing reads the legacy name. `shakenfist/kerbside`'s
`functional-tests.yml` is the only consumer known to read it; the
`eol-distro` audit page in `shakenfist/shakenfist` mentions the name
but does not consume it.

**Rolling out a desktop release bump has a required order**, because a
missing gnome label is skipped here rather than being fatal: a
`dependencies` rebuild that runs too early publishes a disk with no
gnome snapshot under either name, and the disk's own verification
cannot catch it -- a missing cache entry passes on purpose. The order
is `shakenfist/images` publishes the base image, then
`ci-image-desktop.yml` publishes the `ci-images/debian-gnome-<release>`
label, then the conductor rebuilds `dependencies`, and only then do
consumers see the new contents. `GNOME_LABEL` in private-ci's conductor
names the same label a third time and has to move with `gnome_release`;
the conductor's gnome-less marker uses it to decide when a cluster
rebuilds its cache disk, so a constant naming a label this playbook
does not snapshot leaves the disk stale without anything reporting it.

Two things about that disk are load bearing and easy to undo by
accident.

The first is that the filesystem's feature set is written out in full --
`mkfs.ext4 -O none,<fifteen features>` rather than a bare `mkfs.ext4`.
One distribution builds this disk and a great many mount it, the oldest
of them on kernel 5.4, so its on-disk format wants to be a decision the
playbook makes rather than a side effect of whichever builder image it
ran on. It was the latter until recently: moving the builder from Debian
11 to Debian 13 changed the format on its own, because e2fsprogs 1.47
enables `orphan_file` by default. The `none,` prefix is the part doing
the work, and the part most likely to be dropped as redundant -- `-O`
adds to and subtracts from the `mke2fs.conf` set rather than replacing
it, so an explicit list without `none` still inherits whatever a future
e2fsprogs decides to turn on. The trade, which the comment on the task
argues at length, is that a desirable future default never reaches this
disk either, and a renamed or retired feature fails the build outright.
Both are preferable to the format drifting quietly under a disk the
whole fleet mounts.

The second is the pair of assertions that run on the builder
immediately before the unmount, which are covered with the other image
verification below.

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

## Image extras and the docker client

`ci-image.yml` takes an `extras` variable, a comma separated list of
feature tags; today the only tag is `docker`, and the private-ci
image builder passes it for the `debian-12-docker` and
`debian-13-docker` labels. Those are the images that back the
`*-docker` runner labels, which exist because static runners have no
docker daemon.

Installing the tag is not simply `docker.io`. Debian 13 split the
packaging: `docker.io` is now the daemon alone, and the `docker`
client binary moved to a separate `docker-cli` package which
`docker.io` only Recommends. The CI base images are built from cloud
images with recommends disabled, so naming `docker.io` on trixie
installs a daemon and no client. Bookworm and older ship the client
inside `docker.io` and have no `docker-cli` package at all, so the
name cannot simply be added everywhere either. The play therefore
asks apt whether `docker-cli` exists and installs it when it does,
rather than keying on a release number -- one playbook builds Debian,
Ubuntu and Rocky images, and Ubuntu has not made the split yet.

This failure mode is quiet, which is why the play ends with a `docker
version` check. An image missing its client builds, boots, snapshots
and publishes its label exactly like a healthy one; nothing notices
until a job on that label runs `docker` and gets `docker: command not
found`, by which time the failure is showing up in somebody's pull
request in another repository. The check moves the failure back to
the build that caused it.

## Verifying what an image provides

The `docker version` check above generalises, and every image build
playbook now ends the same way: assert that the artifact does the
thing it exists to do, and fail the build when it does not.

The reason is that the checks these playbooks already had are much
weaker than they look. Each one ends by booting the snapshot and
running a `dist-upgrade` or a `dnf update` on it, which establishes
that the image boots, can sudo and can manage packages. A docker
image with no docker client passes all three. So does a desktop image
with no desktop, and a cache disk whose downloads all returned a
proxy error page.

Where the checks run matters as much as what they assert. For the two
playbooks that build bootable images they run on the **test**
instance, the one booted from the snapshot, not on the builder. The
builder was booted from the base image and then modified in place, so
it can only answer for the machine the playbook built -- which is why
`debian-gnome:12` could be inspected on a builder for two years
without anybody noticing it was Debian 11. Only an instance booted
from the snapshot speaks for the artifact that will carry the label.

`ci-dependencies.yml` is the exception, and has to be. Its artifact is
a data disk rather than a bootable image, so there is no test instance
to boot; its check runs in the `allsf` play on the builder, against
the mounted filesystem, immediately before the unmount and snapshot.
That is a real limitation and not a preference: it speaks for the
filesystem as the builder saw it, not for the blob that gets labelled.

| Playbook | What the image exists to provide | How it is exercised | Where |
|----------|----------------------------------|---------------------|-------|
| `ci-image.yml` | A toolchain that can run tests | `tox --version` | test instance |
| `ci-image.yml` | The libvirt python bindings every smoke cluster needs | `python3 -c "import libvirt"` | test instance |
| `ci-image.yml` | Runner logs that reach Loki | `systemctl is-enabled alloy`, when the builder installed it | test instance |
| `ci-image.yml` | A working docker, for the `docker` extra | `docker version`, on the builder and again from a cold boot | both |
| `ci-image-desktop.yml` | A graphical session a console can see | `systemctl get-default`, `systemctl is-active display-manager`, and an active graphical session on `seat0` | test instance |
| `ci-dependencies.yml` | Cache entries CI jobs can read | No top level entry under a megabyte, and at least 2GB still free | builder, pre-unmount |

Three of those are deliberately weaker than they first appear, and all
three are worth knowing before you tighten them:

* **Alloy is checked for being enabled, not for running.** Its unit
  refuses to start until the hostname matches `sfcbr-*`, so that a
  host which is not a runner ships nothing rather than shipping
  mislabelled logs. The test instance is called `test`, so on a
  correctly built image Alloy is sitting in its `ExecStartPre` poll
  and `systemctl is-active` would fail on every image that is working
  properly.
* **libvirt is imported, not connected to.** `virsh version` or
  `libvirt.open()` would also prove the daemon is up, which is both a
  stronger claim and a riskier check: it races socket activation at
  boot, it cannot be exercised before merge, and a false failure here
  blocks every image the fleet builds. The import catches the
  packaging failure -- the one that actually differs between apt and
  dnf -- with no race at all.
* **The cache disk check is a size floor, not an inventory.** Every
  top level entry is a cloud image, a release tarball or a desktop
  snapshot, so anything under a megabyte is a failed download rather
  than a small file. A megabyte and not a kilobyte because a squid
  error page is two to four kilobytes, which a kilobyte floor would
  wave through. It does not check that a given entry is *present*,
  because the list of what should be there lives in the `get_url` loop
  and would have to be kept in step by hand. The free space assertion
  beside it covers the failure the floor is blind to: a download cut
  off by ENOSPC part way through a several hundred megabyte image is
  still tens of megabytes on disk and passes the floor easily, so the
  disk is also required to finish with at least 2GB free. That number
  is set from the healthy end rather than the failure end -- a full
  disk reports almost nothing free, so the only interesting question is
  how much headroom the cached set can grow into before the check
  starts failing builds for no reason.

The desktop check is the one to copy if you add a playbook, and the
reason is in what it does *not* ask. It asks **seat0** -- the physical
console -- for its active session and requires that session to be
`x11` or `wayland`. The obvious alternatives, asking whether the
desktop user is logged in, are both satisfied by ansible's own SSH
connection. Measured on the published `debian-gnome:13` image booted
under KVM, with gdm3 running and then stopped:

| Check | gdm3 up | gdm3 stopped |
|---|---|---|
| `loginctl show-user <u> --property=State` | `active` | `active` |
| `loginctl show-user <u> --property=Display` | `9` | `9` |
| `loginctl show-seat seat0 --property=ActiveSession` | `c1` | *(empty)* |

`pam_systemd` registers a session for the SSH login, a session with no
seat is unconditionally active, and logind will nominate that session
as the user's `Display` when there is no graphical one. Either
user-scoped form passes on an image with no desktop at all.

Asking the seat also sidesteps autologin, which does not fire on these
images: the `gnome-desktop` element creates the desktop account with
no password, so it is locked, and gdm3 stops at the greeter. The
greeter is a graphical session on `seat0`, and it proves the stack
works -- which is the claim worth making here.

One trap is worth carrying away from that poll, because it is not what
the documentation implies. **`failed_when: false` on a task with
`until` also suppresses the failure when the retries run out.** It is
easy to assume the retry loop gets the last word; measured, the poll
went its full thirty rounds against a broken image, reported `ok`, and
let the play continue. Neither `loginctl` call here exits non-zero
when there is nothing to report, so there was never a return code to
suppress and the flag is simply absent. If you add a poll that does
need it, put a real assertion after the loop.

## When a check fails

The label is not updated. `sf-client label update` runs in a later
play, so an image that fails verification leaves the previous label
pointing at the last blob that passed; conductor sees the playbook
exit non-zero, files a build-failure issue and moves on. Yesterday's
image beats a broken one.

The builder instance, the test instance and the intermediate snapshot
are all left behind, because the cleanup lives in that same later
play. That is deliberate rather than overlooked: they are the only
thing left to inspect when a build fails, and conductor's
`cleanup_stale_builders()` and `cleanup_stale_snapshots()` run before
every build, so the capacity comes back at the next attempt rather
than being held indefinitely.

## CI runner log shipping

`ci-image.yml` can bake a Grafana Alloy log shipper into the image, via
`ansible/tasks/install-ci-log-shipper.yml` and the config in
`ansible/files/ci-runner-alloy.alloy`. It is gated on `ci_log_shipper`,
which **defaults to false**, so a local run of the playbook installs
nothing.

Conductor turns it on, and only for the images runners boot: it passes
`ci_log_shipper` derived from `provisioner.CI_IMAGES`, which is a subset
of its own `IMAGE_BUILDS`. The ubuntu and desktop labels exist for
nested CI clusters to consume rather than for runners to boot, and a
nested cluster shipping as `job="ci-runner"` would be noise on a stream
whose value is that every line in it came from a runner.

This exists because an ephemeral runner is deleted seconds after its job
ends -- private-ci's cloud-init runs `run.sh` in the foreground and then
`sleep 30; sf-client instance poweroff` -- so nothing it wrote survives.
Shipping is therefore continuous; there is no teardown hook to hang it
off.

### What ships

| Source | Shipped | Why |
|---|---|---|
| `_diag/Runner_*.log` | yes | the listener log, which records the agent's own disconnects and retries |
| `_diag/Worker_*.log` | no | the job's output, which GitHub already keeps; 93% of `_diag` by volume |
| journal | yes | cloud-init, apt, resolved, kernel, docker |
| journal, `actions.runner.*` unit | no | job output again, at 60 MB/hour -- 90% of a runner's journal |

Dropping that one unit is what keeps this feed at roughly 150 MB/day
across the fleet instead of roughly 10 GB/day. If the
`mach33labs/33fl` ingest ratchet starts reporting `sfcbr/ci-runner` over
budget, suspect that the drop rule has stopped matching before
concluding that CI got busier.

### Reading it back

Logs land in the **`sfcbr`** Loki tenant as `job="ci-runner"`, with
`stream` set to `listener` or `journal` and `host` set to the Shaken
Fist instance name -- the short name, `sfcbr-XXXXXXXX`, never an FQDN.
The rocky images pick up a domain from the DHCP lease and report
`sfcbr-XXXXXXXX.local` from `hostname`, so the config strips everything
from the first dot on; see below.

Correlating a runner with the conductor's own view of it is a
**two-query job**, because conductor logs to the `home` tenant and Loki
cannot join across tenants. Join them on `host`, which is the instance
name at both ends:

```
# home tenant -- what the conductor saw from outside
{job="conductor"} |= "is offline but still holds job"

# sfcbr tenant -- what that runner saw from inside
{job="ci-runner", host="sfcbr-XXXXXXXX"}
```

### Pinning

`ci_alloy_version` and `ci_alloy_sha256` are a pair and must be bumped
together; the checksum is cross-checked against Grafana's published
`SHA256SUMS` for that tag. Alloy comes from the upstream release zip
rather than `apt.grafana.com` and `rpm.grafana.com` because one artifact
covers both package managers here, and because a repository key expiry
would fail the nightly image rebuild for every label at once.

Three things about the config resist casual editing, and all of them
fail silently rather than loudly:

- The `job` label is set through `relabel_rules`, not through `labels`.
  `loki.source.journal` overrides any `job` in `labels` with its own
  component ID.
- `loki.source.file` needs its `file_match` block to expand the glob.
  Without it the path is stat'd as a literal filename and nothing ships.
- `host` is `string.split(constants.hostname, ".")[0]`, not
  `constants.hostname`. Using the latter ships every rocky runner under
  an FQDN that no conductor log line will ever match, breaking the only
  join between the two tenants -- for rocky runners only, so a spot
  check on a debian runner looks perfectly healthy.

`alloy validate` returns zero for all three mistakes, and the build-time
validate task in the install file will not catch any of them. Prove
changes by running the built image and reading the labels back out of
Loki.

## Linting

Neither `yamllint` nor `ansible-lint` is enabled against this directory
yet. Both report large backlogs -- 191 and 732 findings respectively --
that are mostly stylistic, and the reasoning for leaving them off is in
[ci.md](ci.md).

What does run is `check-yaml`, in pre-commit. That is only a parse, but
until it was added these files had no gate whatsoever: nothing in this
repository calls them, conductor runs them out of band, so the first
execution of a change was a nightly image build on the CI cluster.

The fuller check is `tools/ansible-syntax-check.sh`, which runs
`ansible-playbook --syntax-check` over every file in `ansible/` that
contains a play. It is not wired into pre-commit, and the reason is
worth recording: modern `ansible-core` resolves module names during a
syntax check, so passing needs both `ansible.posix` and the
`shakenfist.shakenfist` collection present. The latter is built from a
deploy mirror rather than installed from PyPI, so a pre-commit
environment cannot assemble it, and a hook depending on whatever the
runner happens to have installed would be a lint that reds the build
for reasons unrelated to the change. Run it by hand on a machine with
the collections:

```
tools/ansible-syntax-check.sh
```
