#!/bin/bash
# Gather diagnostic artifacts from an oVirt host for CI.
#
# This script runs on the oVirt target node. It collects RPM lists,
# download URLs, engine logs, VDSM logs, and SSH config into a zip
# bundle at /tmp/bundle.zip for upload as a CI artifact.
#
# Environment variables:
#   MIRROR_RPMS=1   Also download every RPM listed in /tmp/rpms.urls
#                   into /tmp/rpms.cache/ and bundle the files. Off by
#                   default; turning this on bloats the bundle from
#                   tens of MB to 1-2 GB on a typical oVirt install, so
#                   it is unsuitable for CI artifact storage but useful
#                   when reproducing ancient releases whose upstream
#                   mirrors may disappear.

set -xe
export PS4='=======================\n+ '

sudo rpm -qa --qf "%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n" \
    | grep -v '^gpg-pubkey' > /tmp/rpms.list

for rpm in $(cat /tmp/rpms.list); do
    echo "Caching URLs for ${rpm}"
    sudo yumdownloader --resolve --urls "${rpm}" \
        | egrep -v '(metadata expiration|Waiting for)' \
        >> /tmp/rpms.urls 2>/dev/null || true
done

# Optional RPM mirroring: fetch every URL into a local cache directory
# and createrepo_c the result so it doubles as a usable local dnf
# repo. -nc on wget skips RPMs that are already cached, which matters
# when a caller seeded /tmp/rpms.cache from a prior run before the
# build started.
extra_zip_paths=()
if [ "${MIRROR_RPMS:-0}" = "1" ]; then
    sudo dnf -y install wget createrepo_c
    sudo mkdir -p /tmp/rpms.cache
    sudo wget -nv -nc -P /tmp/rpms.cache -i /tmp/rpms.urls || true
    sudo createrepo_c /tmp/rpms.cache
    extra_zip_paths+=("/tmp/rpms.cache")
fi

sudo zip -r /tmp/bundle.zip \
    /etc/yum.repos.d \
    /tmp/rpms.list \
    /tmp/rpms.urls \
    /var/lib/ovirt-engine/setup \
    /var/log/ovirt-engine/ \
    /var/log/vdsm/ \
    /etc/ssh/sshd_config.d/ \
    "${extra_zip_paths[@]}" \
    || true
sudo chmod ugo+r /tmp/bundle.zip
ls -lrth /tmp/bundle.zip
