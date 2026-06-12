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

# Record where every installed package came from, as a single batched,
# metadata-only query. This is pure provenance: a URL per package, kept
# as an artifact even when MIRROR_RPMS is off.
#
# The previous approach ran one yumdownloader per package, which reloaded
# repo metadata and re-resolved dependencies from scratch each time and
# dominated runtime on a full oVirt install (~1500 packages -> ~45 min,
# even on a warm cache). rpms.list already contains the complete
# installed set (the full dependency closure), so passing every name at
# once loads metadata exactly once. "dnf download --url" prints the URLs
# without downloading — it is what "yumdownloader --urls" wraps — so the
# rpms.urls file is identical to before.
#
# --setopt=strict=0 is essential here. EL9's dnf defaults to strict=1, so
# a single requested NVRA that is no longer in any enabled repo (a
# superseded build, a package whose exact version moved) aborts the WHOLE
# batch — "Exiting due to strict setting" — and prints nothing. With a
# full oVirt install that is almost guaranteed, so without strict=0 the
# entire rpms.urls (and the RPM cache below) comes out empty. strict=0
# downloads everything available and merely warns about the rest. EL8's
# older dnf was lenient by default, which is why this only bites on el9.
sudo dnf download --url --setopt=strict=0 $(cat /tmp/rpms.list) 2>/dev/null \
    | egrep -v '(metadata expiration|Waiting for)' \
    > /tmp/rpms.urls || true

# Optional RPM mirroring: fetch every installed package into a local
# cache directory and createrepo_c the result so it doubles as a usable
# local dnf repo. "dnf download" skips files already present in the
# download dir, so when a caller seeded /tmp/rpms.cache from a prior run
# this becomes near-instant — the warm-cache fast path. Fetching by
# package name (one batched call) rather than wget-ing a URL list keeps
# this to a single metadata load.
extra_zip_paths=()
if [ "${MIRROR_RPMS:-0}" = "1" ]; then
    sudo dnf -y install createrepo_c
    sudo mkdir -p /tmp/rpms.cache
    # --setopt=strict=0 for the same reason as the --url query above:
    # without it one un-downloadable installed package aborts the whole
    # batch on el9 and the cache silently comes out empty.
    sudo dnf download --downloaddir=/tmp/rpms.cache --setopt=strict=0 \
        $(cat /tmp/rpms.list) 2>/dev/null || true
    sudo createrepo_c /tmp/rpms.cache
    extra_zip_paths+=("/tmp/rpms.cache")
    # Fail loudly rather than shipping an empty mirror. An empty cache
    # here means every dnf download failed (the el9 strict-mode trap, a
    # missing download plugin, etc.) — exactly the silent failure that
    # produced an empty mirror before. Surface it instead of hiding it.
    rpm_count=$(find /tmp/rpms.cache -name '*.rpm' | wc -l)
    echo "Mirrored ${rpm_count} RPMs into /tmp/rpms.cache"
    if [ "${rpm_count}" -eq 0 ]; then
        echo 'ERROR: MIRROR_RPMS=1 but no RPMs were downloaded.' >&2
        exit 1
    fi
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
