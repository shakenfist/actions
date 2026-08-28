#!/bin/bash

# Copyright 2019 Michael Still and contributors
#
# Stop the CI headroom probe, take the refusal census, bring both back to the
# runner and print the summary.
#
# Two separate instruments, deliberately not merged into one number. The
# headroom SERIES is what tools/ci_headroom_launch.sh started: a poll of
# /admin/resources every few seconds, which shows a cloud sitting half empty
# but cannot see a scheduler refusal, since a refusal begins and ends between
# samples. The refusal CENSUS is the converse: a filtered Loki query counting
# every candidate node the scheduler dropped, per stage, including on runs that
# pass. See docs/plans/PLAN-ci-cloud-sizing-phase-01-headroom-probe.md in
# shakenfist, decisions D9 and D11.
#
# The filter is a regex, not a substring, because the scheduler emits TWO
# message forms and the important one is the second: 'schedule at stage X'
# when candidates survived, and 'schedule has no candidates at stage X,
# aborting' when the stage exhausted them. The latter does not contain the
# substring 'schedule at stage', so a plain |= filter on that phrase would
# capture refusals on healthy runs and silently drop every event behind a
# 507 -- exactly backwards.
#
# The census cannot reuse the Loki dump that ansible/ci-gather-logs-loki.yml
# already puts in every bundle. That one is an unfiltered {job="shakenfist"}
# with limit 5000 and direction=forward over a six hour window, so it returns
# the first 5000 lines of the DEPLOY and never reaches the test window at all.
# Filtered to the scheduler's stage events, the same limit is generous.
#
# Loki is installed only on the primary, in every topology
# (build-smoke-cluster/action.yml), and the census runs on the primary, so
# http://localhost:3100 is correct here -- exactly as the gather playbook does
# and explains.
#
# Usage:
#   tools/ci_headroom_collect.sh <primary> <ssh-user> [label]
#
# NOTHING in this script may fail the job: this phase exists to observe CI's
# failure surface, and an instrument that can fail the job changes the thing
# being measured. Every step tolerates a dead poller, a missing file and an
# unreachable Loki, and the script always exits 0. It runs on the CI runner,
# not on a cluster node.

primary="${1:-}"
ssh_user="${2:-debian}"
label="${3:-}"

# Loki refuses a query_range asking for more entries than its
# max_entries_limit_per_query, whose default is 5000.
census_limit=5000

if [ -z "${primary}" ]; then
    echo "usage: $0 <primary> <ssh-user> [label]"
    echo "SKIPPING: no headroom series or census collected."
    exit 0
fi

ssh_opts=(-i /srv/github/id_ci -o StrictHostKeyChecking=no
          -o UserKnownHostsFile=/dev/null)

echo "=== Stopping the headroom probe and taking the refusal census ==="
ssh "${ssh_opts[@]}" "${ssh_user}@${primary}" \
    bash -s -- "${census_limit}" <<'REMOTE_EOF' || true
census_limit="$1"

# Stop the poller. It may have already exited on its own --max-seconds cap, or
# never have started at all; both are fine and neither is an error here.
pid=$(cat /srv/ci/traces/headroom-probe.pid 2>/dev/null || true)
if [ -n "${pid}" ]; then
    kill "${pid}" 2>/dev/null || true
fi
pkill -f ci_headroom_probe.py 2>/dev/null || true

# Bound the census to the test window. headroom-start is written by
# ci_headroom_launch.sh; if it is missing or not a number, fall back to the
# same six hour window the bundle dump uses, which comfortably covers a run.
start=$(cat /srv/ci/traces/headroom-start 2>/dev/null || true)
case "${start}" in
    ''|*[!0-9]*) start=$(( $(date +%s) - 21600 )) ;;
esac
start_ns=$(( start * 1000000000 ))
end_ns=$(( $(date +%s) * 1000000000 ))

curl -sS -G http://localhost:3100/loki/api/v1/query_range \
    --data-urlencode 'query={job="shakenfist"} |= "Added event" |~ "schedule (at stage|has no candidates at stage)"' \
    --data-urlencode "start=${start_ns}" \
    --data-urlencode "end=${end_ns}" \
    --data-urlencode "limit=${census_limit}" \
    --data-urlencode "direction=forward" \
    > /srv/ci/traces/headroom-census.json 2>/dev/null || true

echo "Contents of /srv/ci/traces:"
ls -l /srv/ci/traces 2>/dev/null || true
if [ -f /srv/ci/traces/headroom.jsonl ]; then
    echo "Samples in the series: $(wc -l < /srv/ci/traces/headroom.jsonl)"
fi
if [ -s /srv/ci/traces/headroom-probe.log ]; then
    echo "Last lines of the probe log:"
    tail -n 20 /srv/ci/traces/headroom-probe.log 2>/dev/null || true
fi
REMOTE_EOF

# Both files stay in /srv/ci/traces on the primary as well, because the
# workflow's "Gather logs" step already scp's that whole directory into the
# 90 day artifact bundle. These local copies exist only so the report can run
# here, on the runner, under stock python3.
workdir="${TMPDIR:-/tmp}/ci-headroom"
mkdir -p "${workdir}" || true
series="${workdir}/headroom.jsonl"
census="${workdir}/headroom-census.json"
rm -f "${series}" "${census}" || true

scp "${ssh_opts[@]}" \
    "${ssh_user}@${primary}:/srv/ci/traces/headroom.jsonl" "${series}" || true
scp "${ssh_opts[@]}" \
    "${ssh_user}@${primary}:/srv/ci/traces/headroom-census.json" "${census}" \
    || true

report="${GITHUB_WORKSPACE:-}/shakenfist/tools/ci_headroom_report.py"
if [ ! -f "${report}" ]; then
    echo "${report} is not in this checkout, so there is nothing to report"
    echo "with. That is expected on a component ref predating the headroom"
    echo "probe. The raw series and census are still in the bundle."
    exit 0
fi

if [ ! -s "${series}" ]; then
    echo "No headroom series was collected from ${primary}, so there is"
    echo "nothing to summarise."
    exit 0
fi

report_args=(--series "${series}")
if [ -s "${census}" ]; then
    report_args+=(--census "${census}")
else
    # Deliberately not passed as an empty census: a report that printed zero
    # refusals when log shipping was simply broken is the dangerous reading.
    echo "No refusal census was collected; the summary will say so."
fi
if [ -n "${label}" ]; then
    report_args+=(--label "${label}")
fi

echo
echo "=== Headroom summary ==="
python3 "${report}" "${report_args[@]}" || true

exit 0
