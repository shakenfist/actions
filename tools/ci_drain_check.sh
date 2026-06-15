#!/bin/bash

# Copyright 2019 Michael Still and contributors
#
# Prove the sf-api SIGTERM drain works on a live cluster node: when sf-api is
# stopped via systemd it must flip /readyz to 503 ("draining") WHILE the worker
# is still serving (/livez stays 200 and the gunicorn master is still alive),
# and only then exit cleanly. A load balancer watching /readyz can therefore
# pull the node out of rotation before any in-flight request is lost.
#
# The drain HANDLER has unit coverage in
# shakenfist/tests/external_api/test_gunicorn_drain.py; this script is the live
# proof on a real node. The cluster_ci python harness is API-only and cannot
# restart daemons, which is why this is a tools/ shell script run on the node.
#
# Intended to be run as root on a cluster node via run_remote. A clean drain
# exits with status 143 (SIGTERM), which the sf-api.service unit treats as
# success (SuccessExitStatus=SIGTERM), so this should NOT emit any of the
# systemd failure log lines the CI log checks grep for.

set -euo pipefail

# A trap that ALWAYS tries to bring sf-api back, on every exit path, so a
# failed assertion never leaves the node out of rotation.
trap 'systemctl start sf-api 2>/dev/null || true' EXIT

# GET a path on the local sf-api and print only the HTTP status code. On a
# connection failure curl prints "000" and exits non-zero; the trailing
# `|| true` keeps that from tripping `set -e` (a refused connection during the
# drain race is an expected, informative result, not a script error).
code() {
    curl -s -o /dev/null -w '%{http_code}' --max-time 2 "http://localhost:13000$1" || true
}

# Is the gunicorn master still alive? Either the pid file names a live process,
# or systemd still considers the unit to be (de)activating/active.
api_alive() {
    local pid
    if [ -e /run/sf/gunicorn.pid ]; then
        pid=$(cat /run/sf/gunicorn.pid 2>/dev/null || true)
        if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
            return 0
        fi
    fi
    case "$(systemctl is-active sf-api 2>/dev/null || true)" in
        active|activating|deactivating) return 0 ;;
        *) return 1 ;;
    esac
}

failures=0

echo
echo "=== sf-api SIGTERM drain check ==="
echo

# Step 1: baseline. The node must be healthy before we test the drain.
echo "Step 1: assert baseline health."
readyz=$(code /readyz)
livez=$(code /livez)
echo "    baseline /readyz=${readyz} /livez=${livez}"
if [ "${readyz}" != "200" ]; then
    echo "FAILURE: baseline /readyz is ${readyz}, expected 200. Node not healthy, aborting."
    exit 1
fi
if [ "${livez}" != "200" ]; then
    echo "FAILURE: baseline /livez is ${livez}, expected 200. Node not healthy, aborting."
    exit 1
fi
echo "    baseline healthy."
echo

# Step 2: send SIGTERM via systemd, in the background. The stop blocks for up to
# the drain grace plus the gunicorn graceful timeout, so we must not wait on it
# here -- we need to observe /readyz flip while the stop is in progress.
echo "Step 2: stopping sf-api (SIGTERM) in the background."
systemctl stop sf-api &
stop_pid=$!
echo

# Step 3: the key assertion. Within a bounded window, observe /readyz == 503
# WHILE /livez == 200 and the gunicorn master is still alive. That proves
# readiness flips before the process is gone.
echo "Step 3: waiting for /readyz to drain to 503 while still serving."
drained=0
# Up to ~15s at 0.5s per iteration.
for _ in $(seq 1 30); do
    if ! api_alive; then
        echo "    sf-api exited before /readyz was observed at 503."
        break
    fi
    readyz=$(code /readyz)
    livez=$(code /livez)
    echo "    /readyz=${readyz} /livez=${livez}"
    if [ "${readyz}" == "503" ] && [ "${livez}" == "200" ] && api_alive; then
        echo "    OBSERVED: /readyz=503 while /livez=200 and master still alive."
        drained=1
        break
    fi
    sleep 0.5
done

if [ "${drained}" != "1" ]; then
    echo "FAILURE: never observed /readyz=503 while sf-api was still serving."
    failures=$(( failures + 1 ))
fi
echo

# Step 4: let the stop complete (bounded). The unit's TimeoutStopSec is 70s, so
# allow a little more than that before giving up on the wait.
echo "Step 4: waiting for the stop to complete."
waited=0
while kill -0 "${stop_pid}" 2>/dev/null; do
    if [ "${waited}" -ge 80 ]; then
        echo "    stop did not complete within 80s; continuing to recovery."
        break
    fi
    sleep 1
    waited=$(( waited + 1 ))
done
# Reap if it finished.
wait "${stop_pid}" 2>/dev/null || true
echo "    stop finished (or wait bounded out) after ~${waited}s."
echo

# Step 5: bring sf-api back and wait for it to be ready, so the node is left
# healthy and back in rotation.
echo "Step 5: starting sf-api and waiting for /readyz=200."
systemctl start sf-api
recovered=0
# Up to ~60s at 1s per iteration.
for _ in $(seq 1 60); do
    readyz=$(code /readyz || true)
    if [ "${readyz}" == "200" ]; then
        echo "    /readyz=200, node recovered."
        recovered=1
        break
    fi
    sleep 1
done

if [ "${recovered}" != "1" ]; then
    echo "FAILURE: sf-api did not return /readyz=200 within 60s after restart."
    failures=$(( failures + 1 ))
fi
echo

if [ "${failures}" -gt 0 ]; then
    echo "DRAIN CHECK FAILED: ${failures} failure(s) detected."
    exit 1
fi

echo "DRAIN CHECK PASSED: readiness flipped to 503 before shutdown and the node recovered."
exit 0
