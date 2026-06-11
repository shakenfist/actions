#!/bin/bash
# Install base packages on an oVirt target node for CI.
#
# This script runs on the oVirt target node. It installs EPEL,
# enables powertools/CRB, and installs basic utilities.
#
# Usage: ovirt-install-base.sh

set -xe
export PS4='=======================\n+ '

# Enable the extra repositories oVirt needs (EPEL + PowerTools/CRB)
# before updating, so the single "dnf update" below fetches metadata for
# every enabled repo exactly once. dnf pulls metadata for newly-enabled
# repos on demand, so there is no need to "dnf clean all" between steps —
# doing so just discards freshly-downloaded metadata and forces a full
# re-fetch. On a fresh node the metadata is either absent (fetched on
# first use) or already warmed by the python3.9 bootstrap, so we skip the
# clean entirely.
sudo dnf install -y epel-release
sudo dnf config-manager --set-enabled powertools 2>/dev/null \
    || sudo crb enable

sudo dnf update -y
sudo dnf install -y vim patch yum-utils rsync
