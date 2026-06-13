#!/bin/bash
# Prepare a target to install oVirt purely from the seeded local mirror.
#
# Used by the ovirt_engine role when ovirt_local_mirror_only is true (the
# EOL-release path, e.g. oVirt 4.3 on CentOS 7). By the time this runs the
# role has already rsync'd the controller's mirror into /tmp/rpms.cache and
# dropped /etc/yum.repos.d/00-homelab-mirror.repo (a file:///tmp/rpms.cache
# repo at cost=1). This script makes that mirror the ONLY package source
# and installs the handful of base utilities the el8/el9 ovirt-install-base.sh
# would have, but which that script cannot provide on el7 (it enables
# powertools/crb and epel-release, none of which exist or resolve here).
#
# Why disable the other repos rather than just rely on cost=1: a cold
# CentOS 7 box's stock repos point at the dead mirror.centos.org, and even
# with a local mirror present dnf still tries to refresh every *enabled*
# repo's metadata and aborts on the dead ones. Moving them aside makes the
# install genuinely offline and deterministic.

set -xe
export PS4='=======================\n+ '

# 1. Make the local mirror the only enabled repo. Move every other repo
#    definition aside (including the vault-redirected CentOS-Base.repo the
#    bootstrap play wrote for its small dnf/python38 pull — the heavy oVirt
#    install must not depend on vault).
mkdir -p /etc/yum.repos.d/disabled
shopt -s nullglob
for f in /etc/yum.repos.d/*.repo; do
    if [ "$(basename "$f")" != "00-homelab-mirror.repo" ]; then
        mv -v "$f" /etc/yum.repos.d/disabled/
    fi
done

# 2. Base utilities, from the mirror. --setopt=strict=0 so a single
#    package that happens not to be in the mirror does not abort the batch.
dnf install -y --setopt=strict=0 vim patch yum-utils rsync

dnf --version
echo 'Local mirror is now the only enabled repo:'
dnf repolist
