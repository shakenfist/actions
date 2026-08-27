#!/bin/bash

# Copyright 2019 Michael Still and contributors
#
# Start the CI headroom probe on the cluster primary, in the background, so it
# samples cluster resources for the whole of the functional test step.
#
# The probe itself (shakenfist's tools/ci_headroom_probe.py) and the tool that
# summarises what it writes both live in the shakenfist repository, so that the
# JSONL format contract has both of its halves in one place and is covered by
# unit tests that run on an ordinary pull request. This script carries no
# analysis logic at all: it copies the probe over and starts it. See
# docs/plans/PLAN-ci-cloud-sizing-phase-01-headroom-probe.md in shakenfist,
# decisions D13 and D14.
#
# Usage:
#   tools/ci_headroom_launch.sh <primary> <ssh-user> <max-seconds> [interval]
#
# max-seconds is required and is derived by the caller from the workflow's
# test_timeout_minutes input, never hardcoded: callers pass anything from 45 to
# 70 minutes. The probe caps itself because smoke-cluster.yml sets
# cancel-in-progress, and a cancelled job runs no further steps -- so the step
# that stops this poller is not guaranteed to run, and nothing else tears the
# cluster down either.
#
# NOTHING in this script may fail the job. Every remote command tolerates a
# missing file, a missing venv and an unreachable node, and the script always
# exits 0. It runs on the CI runner, not on a cluster node.

primary="${1:-}"
ssh_user="${2:-debian}"
max_seconds="${3:-}"
interval="${4:-15}"

if [ -z "${primary}" ] || [ -z "${max_seconds}" ]; then
    echo "usage: $0 <primary> <ssh-user> <max-seconds> [interval]"
    echo "SKIPPING: the headroom probe was not started."
    exit 0
fi

probe="${GITHUB_WORKSPACE:-}/shakenfist/tools/ci_headroom_probe.py"
if [ ! -f "${probe}" ]; then
    echo "${probe} is not in this checkout, so there is no probe to start."
    echo "That is expected on a component ref predating the headroom probe."
    echo "SKIPPING: the headroom probe was not started."
    exit 0
fi

ssh_opts=(-i /srv/github/id_ci -o StrictHostKeyChecking=no
          -o UserKnownHostsFile=/dev/null)

echo "Copying ${probe} to ${primary}."
if ! scp "${ssh_opts[@]}" "${probe}" \
        "${ssh_user}@${primary}:/tmp/ci_headroom_probe.py"; then
    echo "SKIPPING: could not copy the probe to ${primary}."
    exit 0
fi

# bash -s with a quoted heredoc rather than a locally assembled command string:
# the remote block needs $! and ${interval} to be expanded on the far side and
# on this side respectively, and hand-escaping that in a "script=..." string is
# exactly the kind of quoting bug this change cannot test before it merges.
# Values that must come from here are passed as positional arguments instead.
ssh "${ssh_opts[@]}" "${ssh_user}@${primary}" \
    bash -s -- "${max_seconds}" "${interval}" <<'REMOTE_EOF' || true
max_seconds="$1"
interval="$2"

# Already created and chowned by the workflow's "Make the traces directory"
# step; this is belt and braces for a caller that skipped it.
mkdir -p /srv/ci/traces 2>/dev/null || true

# The census in ci_headroom_collect.sh reads this to bound its Loki query to
# the test window, rather than dredging up the whole deploy.
date +%s > /srv/ci/traces/headroom-start 2>/dev/null || true

if [ ! -x /srv/shakenfist/venv/bin/python3 ]; then
    echo "No Shaken Fist venv python on this node; not starting the probe."
    exit 0
fi

# sfrc supplies SHAKENFIST_NAMESPACE and SHAKENFIST_KEY. It is made
# world-readable by build-smoke-cluster, so no sudo is needed.
if [ -f /etc/sf/sfrc ]; then
    . /etc/sf/sfrc
fi
export SHAKENFIST_API_URL=http://localhost:13000

# ALL THREE file descriptors are redirected. Without stdin from /dev/null and
# both output streams into a file, the backgrounded process keeps the ssh
# session's pipes open and the ssh that started it hangs waiting for EOF --
# which would stall the workflow step rather than merely losing the probe.
nohup /srv/shakenfist/venv/bin/python3 /tmp/ci_headroom_probe.py \
    /srv/ci/traces/headroom.jsonl \
    --interval "${interval}" --max-seconds "${max_seconds}" \
    </dev/null >/srv/ci/traces/headroom-probe.log 2>&1 &
echo $! > /srv/ci/traces/headroom-probe.pid

echo "Headroom probe started as pid $(cat /srv/ci/traces/headroom-probe.pid)."
echo "    interval ${interval}s, cap ${max_seconds}s"
echo "    series /srv/ci/traces/headroom.jsonl"
REMOTE_EOF

exit 0
