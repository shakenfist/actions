#!/bin/bash
# Build an offline RPM + SRPM mirror for oVirt 4.3 on CentOS 7 (el7).
#
# WHY THIS EXISTS
# ---------------
# oVirt 4.3 and CentOS 7 are both end-of-life. Their packages still exist
# upstream today (vault.centos.org, resources.ovirt.org, the EPEL archive,
# fedorapeople, COPR) but that is exactly the kind of thing that vanishes
# without notice. For 4.4/4.5 we let a live deployment run seed the local
# mirror as a byproduct; for an EOL release that is too fragile — every
# 40-minute instance run would bet on a dead distro's mirrors still being
# up and complete. So instead we capture the mirror HERE, once, on a
# well-connected host, and the deployment then installs from it offline
# (see the ovirt_local_mirror_only path in the ovirt_engine role).
#
# WHAT IT CAPTURES
# ----------------
#   rpms.cache/   binary RPMs, createrepo_c'd into a usable dnf repo:
#                   * the ENTIRE oVirt 4.3 el7 repo via "reposync
#                     --newest-only" — NOT a dependency closure. Closure
#                     resolution (repotrack/yumdownloader --resolve) only
#                     pulls packages something depends on, so it silently
#                     drops every *optional* package — the canonical
#                     example being vdsm-hook-nestedvt (the nested-virt
#                     hook), which is in the repo but pulled by nothing.
#                     Mirroring the repo wholesale captures every optional
#                     hook/extension structurally. --newest-only keeps one
#                     build of each name instead of ~35 historical z-builds.
#                   * the dependency CLOSURE of the engine + host stack +
#                     a small operator toolbelt, drawn from the much larger
#                     CentOS 7 base/updates/extras + SIG + EPEL repos
#                     (full reposync of those is 40GB+ and not worth
#                     committing; base lives on the comparatively stable
#                     vault).
#   srpms.cache/  matching source RPMs, createrepo_c'd. We expect to
#                 rebuild patched RPMs for el7-era bugs, and sources vanish
#                 when upstream rotates. The oVirt SRPM tree is small so we
#                 take it wholesale; CentOS/SIG sources are fetched for the
#                 binary closure only.
#
# REBUILD LOOP (for when we patch a package later)
# ------------------------------------------------
# In a centos:7 container: install the .src.rpm from srpms.cache, add a
# patch to the spec, bump Release with a ".homelabN" suffix so the rebuilt
# NEVRA outranks upstream, rpmbuild -bb, drop the binary into rpms.cache/,
# then "createrepo_c --update rpms.cache". The instance's cost=1 local
# mirror serves the patched build automatically.
#
# USAGE (run on the Kasm host, not the target)
# --------------------------------------------
#   tools/build-ovirt-43-mirror.sh /path/to/mirror/ovirt-43-centos-7
#
# The single argument is the per-deployment mirror directory (normally
# homelab-deployments/mirror/ovirt-43-centos-7). rpms.cache/ and
# srpms.cache/ are created beneath it. Re-running is incremental: dnf /
# reposync skip files already present, so a second run only fetches what
# changed or what a widened package list newly requires.
#
# All baseurls below were reachable on 2026-06-13. If a fetch 404s, the
# source has finally rotated; check vault / the EPEL archive for the new
# path.

set -xe
export PS4='=======================\n+ '

# ----------------------------------------------------------------------
# Container re-exec. This script runs its real work inside a centos:7
# container (el7 dnf/yum semantics, createrepo_c, reposync), writing into
# a bind-mounted output directory. The host side just launches that
# container and re-execs this same file with --in-container.
# ----------------------------------------------------------------------
if [ "${1:-}" != "--in-container" ]; then
    MIRROR_DIR="${1:?usage: build-ovirt-43-mirror.sh <mirror-dir>}"
    mkdir -p "${MIRROR_DIR}/rpms.cache" "${MIRROR_DIR}/srpms.cache"
    MIRROR_DIR="$(cd "${MIRROR_DIR}" && pwd)"
    exec docker run --rm \
        -v "$(cd "$(dirname "$0")" && pwd)/$(basename "$0")":/build.sh:ro \
        -v "${MIRROR_DIR}":/mirror \
        -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
        centos:7 bash /build.sh --in-container
fi

# ======================================================================
# Everything below runs inside the centos:7 container.
# ======================================================================
RPMS=/mirror/rpms.cache
SRPMS=/mirror/srpms.cache
ARCH=x86_64
VAULT=https://vault.centos.org/7.9.2009
OVIRT=https://resources.ovirt.org/pub/ovirt-4.3/rpm/el7

# Engine + host package closure roots. ovirt-engine pulls the management
# server; ovirt-host is the metapackage that drags in vdsm and the whole
# hypervisor stack (qemu-kvm-ev, libvirt, ...). Together they cover both
# halves of the single-node deployment (the box is engine AND host).
CLOSURE_PKGS="ovirt-engine ovirt-host vdsm"

# Bootstrap packages. CentOS 7 ships yum (not dnf) and python3.6 (too old
# for Ansible 2.19's target-side modules, which want >= 3.7). The el7
# deployment installs dnf (so the dnf-based role works unchanged),
# rh-python38 (the 3.8 interpreter Ansible drives the host with), and
# rsync (the cloud image has none, and the mirror seed needs it). They are
# captured here so the box can (re)install them from the mirror offline.
CLOSURE_PKGS="${CLOSURE_PKGS} dnf dnf-plugins-core rh-python38 rsync"

# Build deps for pip-installing the python3 oVirt SDK into rh-python38 on
# the engine box. oVirt 4.3/el7 ships only the python2 SDK, but the smoke
# test (start-test-target.py) is python3, so we build ovirt-engine-sdk-python
# (and its pycurl dep) for python3.8 there. The SDK's C extension links
# libxml2; pycurl needs libcurl + openssl headers; rh-python38-python-devel
# provides the 3.8 headers; gcc is the compiler. (The SDK sdist itself comes
# from PyPI at smoke time, not the mirror.)
CLOSURE_PKGS="${CLOSURE_PKGS} rh-python38-python-devel gcc libxml2-devel libcurl-devel openssl-devel nss-devel"

# createrepo_c so the post-run artifact gather (ovirt-gather-artifacts.sh,
# MIRROR_RPMS=1) can re-index /tmp/rpms.cache. It exists for el7 but is not
# pulled by anything, and in mirror-only mode the box can only install from
# this mirror -- so capture it explicitly.
CLOSURE_PKGS="${CLOSURE_PKGS} createrepo_c"

# Operator toolbelt — packages we will want to dnf install on the box
# later for debugging. Post-EOL none of these are installable unless they
# are in the mirror now. Extend freely; each addition is one cheap re-run.
TOOLBELT="tcpdump strace lsof gdb bind-utils net-tools tmux screen wget \
          rsync vim-enhanced git nmap-ncat sysstat iotop"

# ----------------------------------------------------------------------
# Point the container at live, EOL-archived repositories. The baked-in
# CentOS 7 mirrorlists (mirror.centos.org) are dead; redirect to vault.
# Then lay down the oVirt 4.3 repo set with the mirror.centos.org SIG
# baseurls rewritten to vault and the off-CentOS sources (EPEL archive,
# virtio-win, COPR) pointed at their surviving homes. gpgcheck=0
# throughout: we only DOWNLOAD here, we never install, so key juggling
# buys nothing.
# ----------------------------------------------------------------------
sed -i -e 's|^mirrorlist=|#mirrorlist=|g' \
       -e "s|^#\?baseurl=http://mirror.centos.org/centos/\$releasever|baseurl=${VAULT}|g" \
       /etc/yum.repos.d/CentOS-Base.repo

cat > /etc/yum.repos.d/ovirt-43-build.repo <<EOF
[ovirt-4.3]
name=oVirt 4.3 (el7)
baseurl=${OVIRT}/
enabled=1
gpgcheck=0

[ovirt-4.3-deps-gluster6]
name=CentOS-7 Storage SIG - Gluster 6
baseurl=${VAULT}/storage/${ARCH}/gluster-6/
enabled=1
gpgcheck=0

[ovirt-4.3-deps-qemu-ev]
name=CentOS-7 Virt SIG - QEMU EV (kvm-common)
baseurl=${VAULT}/virt/${ARCH}/kvm-common/
enabled=1
gpgcheck=0

[ovirt-4.3-deps-ovirt43]
name=CentOS-7 Virt SIG - oVirt 4.3
baseurl=${VAULT}/virt/${ARCH}/ovirt-4.3/
enabled=1
gpgcheck=0

[ovirt-4.3-deps-ovirt-common]
name=CentOS-7 Virt SIG - oVirt common
baseurl=${VAULT}/virt/${ARCH}/ovirt-common/
enabled=1
gpgcheck=0

[ovirt-4.3-deps-opstools]
name=CentOS-7 OpsTools SIG
baseurl=${VAULT}/opstools/${ARCH}/
enabled=1
gpgcheck=0

[ovirt-4.3-deps-sclo-rh]
name=CentOS-7 SCLo rh (Software Collections)
baseurl=${VAULT}/sclo/${ARCH}/rh/
enabled=1
gpgcheck=0

[ovirt-4.3-deps-epel]
name=EPEL 7 (archive) - oVirt allowlist
baseurl=https://archives.fedoraproject.org/pub/archive/epel/7/${ARCH}/
enabled=1
gpgcheck=0
includepkgs=ansible,ansible-doc,epel-release,facter,golang,golang-bin,golang-src,hiera,libtomcrypt,libtommath,nbdkit,nbdkit-devel,nbdkit-plugin-python2,nbdkit-plugin-python-common,nbdkit-plugin-vddk,ovirt-guest-agent*,puppet,python2-crypto,python2-ecdsa,python-ordereddict,ruby-augeas,rubygem-rgen,ruby-shadow
EOF

# Two repos from the stock ovirt-release43 dependency set are deliberately
# omitted:
#   * sac/gluster-ansible COPR — copr-be.cloud.fedoraproject.org now
#     requires TLS >= 1.2, which CentOS 7's openssl 1.0.2 cannot negotiate
#     ("unsupported protocol version"), so the el7 container cannot reach
#     it at all. Its packages are only needed for hyperconverged Gluster,
#     which the single-node local-storage deployment does not use.
#   * virtio-win (fedorapeople) — Windows guest drivers, irrelevant to the
#     Linux smoke test, and its "latest" tree advertises zero packages to
#     el7 yum anyway.
# If a future deployment needs either, fetch the packages host-side (where
# modern TLS works) and drop them into rpms.cache before createrepo_c.

# CentOS 7 source repos (for the binary-closure SRPMs). These ship their
# own repodata under Source/, so "yumdownloader --source" can resolve
# against them.
cat > /etc/yum.repos.d/centos7-source.repo <<EOF
[base-source]
name=CentOS-7 os Source
baseurl=${VAULT}/os/Source/
enabled=0
gpgcheck=0

[updates-source]
name=CentOS-7 updates Source
baseurl=${VAULT}/updates/Source/
enabled=0
gpgcheck=0

[extras-source]
name=CentOS-7 extras Source
baseurl=${VAULT}/extras/Source/
enabled=0
gpgcheck=0
EOF

yum -y install yum-utils createrepo_c >/dev/null

# ----------------------------------------------------------------------
# 1. oVirt 4.3 repo, captured WHOLESALE (every optional package included).
#    --norepopath dumps straight into rpms.cache rather than a per-repo
#    subdir; --newest-only keeps one build per package name.
# ----------------------------------------------------------------------
reposync --repoid=ovirt-4.3 --newest-only --norepopath -p "${RPMS}"

# ----------------------------------------------------------------------
# 2. Dependency closure of the engine + host stack + toolbelt, drawn from
#    CentOS base/SIG/EPEL. repotrack walks the full transitive closure and
#    downloads every package (including the roots) into rpms.cache. oVirt
#    packages that reappear here are already present from step 1 and are
#    simply skipped — same filename.
# ----------------------------------------------------------------------
repotrack -a "${ARCH}" -p "${RPMS}" ${CLOSURE_PKGS} ${TOOLBELT}

createrepo_c "${RPMS}"

# ----------------------------------------------------------------------
# 3. SRPMs. oVirt's source tree is small and precious, so mirror it
#    wholesale (one build per name to match --newest-only). CentOS/SIG
#    sources are fetched only for the binary closure we actually captured.
# ----------------------------------------------------------------------
# oVirt SRPMs wholesale via its source repo if it carries repodata, else a
# recursive fetch of the flat SRPMS directory.
if curl -sfL "${OVIRT}/SRPMS/repodata/repomd.xml" >/dev/null 2>&1; then
    cat > /etc/yum.repos.d/ovirt43-source.repo <<EOF
[ovirt-4.3-source]
name=oVirt 4.3 SRPMS
baseurl=${OVIRT}/SRPMS/
enabled=1
gpgcheck=0
EOF
    reposync --repoid=ovirt-4.3-source --newest-only --norepopath -p "${SRPMS}"
else
    # No source repodata: pull the flat directory directly. Only one
    # version of each is on disk in practice, so this stays small.
    ( cd "${SRPMS}" && \
      wget -q -r -np -nd -A '*.src.rpm' "${OVIRT}/SRPMS/" )
fi

# CentOS / SIG sources for the captured binary closure. Derive the source
# package names from what landed in rpms.cache and fetch them with the
# source repos enabled.
INSTALLED_NAMES=$(rpm -qp --qf '%{NAME}\n' "${RPMS}"/*.rpm 2>/dev/null \
                  | sort -u | tr '\n' ' ')
yumdownloader --source --destdir="${SRPMS}" \
    --enablerepo='*-source' \
    ${INSTALLED_NAMES} || true

createrepo_c "${SRPMS}"

# ----------------------------------------------------------------------
# Report and hand the tree back to the invoking host user.
# ----------------------------------------------------------------------
echo "=== mirror summary ==="
echo "binary RPMs : $(find "${RPMS}" -name '*.rpm' | wc -l)"
echo "source RPMs : $(find "${SRPMS}" -name '*.src.rpm' | wc -l)"
# reposync preserves the oVirt repo's noarch/ + x86_64/ subdir layout, so
# look recursively — the optional packages land under those subdirs.
echo "vdsm-hook-nestedvt present: $(find "${RPMS}" -name 'vdsm-hook-nestedvt-*' | head -1 || echo NO)"
echo "rh-python38 present: $(find "${RPMS}" -name 'rh-python38-3*' | head -1 || echo NO)"
du -sh "${RPMS}" "${SRPMS}" || true

chown -R "${HOST_UID}:${HOST_GID}" /mirror
