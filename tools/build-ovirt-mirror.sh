#!/bin/bash
# Generalized offline RPM + SRPM mirror builder for oVirt deployments.
#
# Builds a per-deployment mirror (binaries wholesale + dependency closure,
# plus matching SRPMs) on a well-connected host, so a deployment can later
# install from it. This is the generalization of build-ovirt-43-mirror.sh
# across releases.
#
#   build-ovirt-mirror.sh <profile> <mirror-dir>
#
# Profiles:
#   43-el7   oVirt 4.3 on CentOS 7      (delegates to build-ovirt-43-mirror.sh)
#   44-el8   oVirt 4.4 on Rocky 8 / el8
#   45-el8   oVirt 4.5 on Rocky 8 / el8
#   45-el9   oVirt 4.5 on Rocky 9 / el9
#
# WHAT IT CAPTURES (same model as the 4.3 builder)
#   rpms.cache/   the WHOLE upstream oVirt repo via "reposync --newest-only"
#                 (so optional packages no dependency pulls -- vdsm-hook-*,
#                 the non-default-language SDKs, auth extensions -- are
#                 captured), plus the dependency closure of ovirt-engine +
#                 ovirt-host + vdsm + a toolbelt, drawn from the much larger
#                 base/SIG/EPEL repos. createrepo_c'd into a usable dnf repo.
#   srpms.cache/  the matching oVirt sources wholesale (minus the multi-GB
#                 VM-image packages), so we can rebuild patched oVirt RPMs
#                 while the sources still exist. Base-OS (Rocky) sources are
#                 NOT captured: Rocky 8/9 is supported for years, unlike the
#                 EOL CentOS 7 under 4.3, so its sources are not at risk.
#
# el7 vs el8/el9: the el7 build (CentOS 7, EOL) needs vault redirects, the
# yum-utils reposync/repotrack toolchain, and an offline SDK build, so it is
# handled by the dedicated build-ovirt-43-mirror.sh. el8/el9 reuse the
# deployment's own repo setup (install the release RPM, apply the deployment
# repos patch) and the dnf reposync / dnf download toolchain, implemented
# here.
#
# Run on the Kasm host (needs Docker). Re-running is incremental: reposync /
# dnf download skip files already present.

set -xe
export PS4='=======================\n+ '

PROFILE="${1:?usage: build-ovirt-mirror.sh <profile> <mirror-dir>}"
MIRROR_DIR_ARG="${2:?usage: build-ovirt-mirror.sh <profile> <mirror-dir>}"
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"

# ----------------------------------------------------------------------
# Host side: pick the container image, then re-exec inside it. The el7
# profile delegates to the dedicated builder.
# ----------------------------------------------------------------------
if [ "${IN_CONTAINER:-}" != "1" ]; then
    case "${PROFILE}" in
        43-el7)
            exec "${SELF_DIR}/build-ovirt-43-mirror.sh" "${MIRROR_DIR_ARG}"
            ;;
        44-el8|45-el8) IMAGE=rockylinux:8 ;;
        45-el9)        IMAGE=rockylinux:9 ;;
        *) echo "unknown profile: ${PROFILE}" >&2; exit 1 ;;
    esac
    mkdir -p "${MIRROR_DIR_ARG}/rpms.cache" "${MIRROR_DIR_ARG}/srpms.cache"
    MIRROR_DIR="$(cd "${MIRROR_DIR_ARG}" && pwd)"
    exec docker run --rm \
        -v "${SELF_DIR}/$(basename "$0")":/build.sh:ro \
        -v "${SELF_DIR}/../etc":/etc-actions:ro \
        -v "${MIRROR_DIR}":/mirror \
        -e IN_CONTAINER=1 -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
        "${IMAGE}" bash /build.sh "${PROFILE}" /mirror
fi

# ======================================================================
# In container (el8/el9).
# ======================================================================
RPMS=/mirror/rpms.cache
SRPMS=/mirror/srpms.cache
ARCH=x86_64

# Engine + host + toolbelt closure roots (same set across releases).
CLOSURE_PKGS="ovirt-engine ovirt-host vdsm"
TOOLBELT="tcpdump strace lsof gdb bind-utils net-tools tmux wget \
          rsync vim-enhanced git nmap-ncat sysstat iotop"

# ----------------------------------------------------------------------
# Per-profile config.
#   RELEASE_RPM      installed to lay down the oVirt repo files
#   PATCH            deployment repos patch to apply (or "none")
#   MODULES          el8 dnf modules to enable (empty on el9)
#   WHOLESALE        repoid(s) whose binaries+sources we mirror wholesale
#   OVIRT_SRPMS_URLS oVirt SRPMS tree(s) (separate from the binary repo, so
#                    each needs its own source repo to reposync). 4.5 adds
#                    the CentOS virt SIG source tree (it ships vdsm etc.).
# ----------------------------------------------------------------------
case "${PROFILE}" in
    44-el8)
        RELEASE_RPM="https://resources.ovirt.org/pub/yum-repo/ovirt-release44.rpm"
        PATCH=ovirt-44-rocky-8-repos.patch
        MODULES="javapackages-tools postgresql:12 mod_auth_openidc:2.3 nodejs:14 pki-deps"
        WHOLESALE="ovirt-4.4"
        OVIRT_SRPMS_URLS="https://resources.ovirt.org/pub/ovirt-4.4/rpm/el8/SRPMS/"
        ;;
    45-el8)
        RELEASE_RPM="centos-release-ovirt45"
        PATCH=ovirt-45-rocky-8-repos.patch
        MODULES="javapackages-tools postgresql:12 mod_auth_openidc:2.3 nodejs:14 pki-deps"
        WHOLESALE="centos-ovirt45 ovirt-45-upstream"
        OVIRT_SRPMS_URLS="https://resources.ovirt.org/pub/ovirt-4.5/rpm/el8/SRPMS/ https://vault.centos.org/centos/8-stream/virt/Source/ovirt-45/"
        ;;
    45-el9)
        RELEASE_RPM="centos-release-ovirt45"
        PATCH=none
        MODULES=""
        WHOLESALE="centos-ovirt45 ovirt-45-upstream"
        OVIRT_SRPMS_URLS="https://resources.ovirt.org/pub/ovirt-4.5/rpm/el9/SRPMS/ https://mirror.stream.centos.org/SIGs/9-stream/virt/source/ovirt-45/"
        ;;
    *) echo "unknown profile: ${PROFILE}" >&2; exit 1 ;;
esac

# ----------------------------------------------------------------------
# Tooling + repo setup (mirror the deployment: release RPM + repos patch).
# ----------------------------------------------------------------------
# findutils: the minimal Rocky image ships no `find`, which prune_images
# and the summary need.
dnf install -y dnf-plugins-core createrepo_c patch findutils >/dev/null
dnf install -y "${RELEASE_RPM}"
if [ "${PATCH}" != "none" ]; then
    ( cd /etc/yum.repos.d && patch -p1 < "/etc-actions/${PATCH}" )
fi
# el8 needs the oVirt module streams (ovirt-engine requires postgresql >= 12,
# which is the postgresql:12 module rather than the default stream).
if [ -n "${MODULES}" ]; then
    dnf module -y enable ${MODULES}
fi

# Source repos: the oVirt SRPMS tree(s). 4.4 ships all its packages from a
# single upstream repo, so one tree. 4.5 splits its packages between the
# upstream resources.ovirt.org repo (the engine) and the CentOS virt SIG
# (vdsm, hooks, ...), so its sources are split too and we add the SIG source
# tree. We deliberately do NOT capture base-OS (Rocky) sources: unlike
# CentOS 7 under 4.3, Rocky 8/9 is supported for years, so its sources are
# not at risk -- and the full closure's worth is ~4GB of kernel/firmware we
# would never rebuild. The at-risk, rebuildable set is the oVirt sources.
: > /etc/yum.repos.d/zz-ovirt-mirror-sources.repo
i=0
SRPM_REPOIDS=""
for u in ${OVIRT_SRPMS_URLS}; do
    i=$((i + 1))
    rid="ovirt-srpms-${i}"
    SRPM_REPOIDS="${SRPM_REPOIDS} ${rid}"
    cat >> /etc/yum.repos.d/zz-ovirt-mirror-sources.repo <<EOF
[${rid}]
name=oVirt SRPMS ${i} (mirror build)
baseurl=${u}
enabled=0
gpgcheck=0

EOF
done

# Multi-GB VM-image packages the wholesale reposync would otherwise pull in
# (binary AND source): the hosted-engine appliance and the node-ng OS image.
# Our deployments are standalone engines on a full OS -- we never install or
# patch these, and the appliance source alone is ~3.3GB. Pruned after each
# wholesale step.
prune_images() {
    find "$1" \( -name 'ovirt-engine-appliance-*' \
                 -o -name 'ovirt-node-ng-image*' \
                 -o -name 'ovirt-node-ng-4*' \) -delete 2>/dev/null || true
}
# Exclude them at reposync time too, so they are never downloaded (the
# appliance source alone is ~3.3GB) -- prune_images is then just a backstop.
EXCLUDE_IMAGES='ovirt-engine-appliance*,ovirt-node-ng-image*,ovirt-node-ng-4*'

# ----------------------------------------------------------------------
# 1. oVirt repo(s), captured WHOLESALE (every optional package included).
# ----------------------------------------------------------------------
for r in ${WHOLESALE}; do
    dnf reposync --repoid="${r}" --newest-only --norepopath -p "${RPMS}" --setopt=exclude="${EXCLUDE_IMAGES}" || true
done
prune_images "${RPMS}"

# ----------------------------------------------------------------------
# 2. Dependency closure of engine + host + toolbelt. There is no
#    dnf repotrack here, so "dnf install --downloadonly" resolves the full
#    transitive set and downloads it. --nobest because ovirt-engine pins a
#    module-provided postgresql the default stream would otherwise shadow.
# ----------------------------------------------------------------------
dnf install -y --downloadonly --downloaddir="${RPMS}" --nobest \
    ${CLOSURE_PKGS} ${TOOLBELT} || true

createrepo_c "${RPMS}"

# ----------------------------------------------------------------------
# 3. SRPMs: the oVirt sources wholesale (minus the VM-image packages).
# ----------------------------------------------------------------------
for rid in ${SRPM_REPOIDS}; do
    dnf reposync --repoid="${rid}" --newest-only --norepopath --source \
        -p "${SRPMS}" --enablerepo="${rid}" --setopt=exclude="${EXCLUDE_IMAGES}" || true
done
prune_images "${SRPMS}"

createrepo_c "${SRPMS}"

# ----------------------------------------------------------------------
# Report and hand back to the invoking host user.
# ----------------------------------------------------------------------
echo "=== mirror summary (${PROFILE}) ==="
echo "binary RPMs : $(find "${RPMS}" -name '*.rpm' | wc -l)"
echo "source RPMs : $(find "${SRPMS}" -name '*.src.rpm' | wc -l)"
echo "vdsm-hook-nestedvt present: $(find "${RPMS}" -name 'vdsm-hook-nestedvt-*' | head -1 || echo NO)"
du -sh "${RPMS}" "${SRPMS}" || true

chown -R "${HOST_UID}:${HOST_GID}" /mirror
