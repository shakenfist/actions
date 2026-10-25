#!/bin/bash

# Run ansible-galaxy, retrying when galaxy.ansible.com fails transiently.
#
# Usage: ansible-galaxy-retry.sh <ansible-galaxy arguments...>
#
# Installing Shaken Fist's collection resolves its dependencies from
# galaxy.ansible.com -- shakenfist.kerbside among them -- even when the
# collection itself is a local tarball, so every smoke cluster deploy
# makes a network call to a service this fleet does not run. Galaxy
# answers HTTP 500 often enough to matter: two merge groups in
# shakenfist/shakenfist failed in "Build the smoke cluster" on
# 2026-10-24 with "Failed to download collection tar from 'default':
# HTTP Error 500", on a change that could not have caused it
# (shakenfist/shakenfist#4632).
#
# Only output that looks like the network or the server failing is
# retried. A bad requirements file, a missing tarball or an unknown
# collection fails at once, as ansible-galaxy alone would, because
# retrying those only delays the same answer.
#
# GALAXY_RETRY_ATTEMPTS (default 4) and GALAXY_RETRY_DELAY (default 15,
# doubled after each attempt) tune it; the tests set the delay to 0.

set -o pipefail

attempts="${GALAXY_RETRY_ATTEMPTS:-4}"
delay="${GALAXY_RETRY_DELAY:-15}"

# What a transient failure looks like in ansible-galaxy's output: a 5xx
# from Galaxy or the proxy in front of it, a timeout, a dropped
# connection, or a name that briefly would not resolve.
transient='HTTP Error 5[0-9][0-9]|returned error: 5[0-9][0-9]|timed out|[Tt]imeout|Connection reset|Connection refused|Remote end closed|Temporary failure in name resolution|Name or service not known'

output=$(mktemp)
trap 'rm -f "${output}"' EXIT

attempt=1
while true; do
    rc=0
    ansible-galaxy "$@" 2>&1 | tee "${output}" || rc=$?
    if [ "${rc}" -eq 0 ]; then
        exit 0
    fi
    if ! grep -qE "${transient}" "${output}"; then
        exit "${rc}"
    fi
    if [ "${attempt}" -ge "${attempts}" ]; then
        echo "::warning::ansible-galaxy $1 failed transiently ${attempts} times; giving up" >&2
        exit "${rc}"
    fi
    echo "ansible-galaxy $1 failed transiently (attempt ${attempt} of ${attempts}); retrying in ${delay}s" >&2
    sleep "${delay}"
    attempt=$((attempt + 1))
    delay=$((delay * 2))
done
