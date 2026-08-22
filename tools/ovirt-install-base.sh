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

# --nobest because a partially-synced mirror is not a reason to abort the
# whole install. Rocky 8 routinely pushes a platform-python/python3-libs
# pair to AppStream and BaseOS at different times, and regional mirrors lag
# the canonical one by varying amounts, so there is a window in which the
# best candidate for a package needs a dependency which has not landed yet.
# Under the default best-candidate resolution dnf calls that an error, and
# "set -e" above then takes the whole CI leg down over a skew which has
# nothing to do with anything under test. --nobest settles for the newest
# self-consistent set and moves on.
#
# This deliberately does not use --skip-broken, which would drop packages
# entirely rather than choosing an older candidate for them, and it is only
# applied to the bulk update: the packages this script actually needs are
# installed explicitly below, where a resolution failure is a real failure
# and should still be fatal.
sudo dnf update -y --nobest
sudo dnf install -y vim patch yum-utils rsync
