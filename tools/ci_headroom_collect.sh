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
# The regex also matches the capacity guard's own two audit messages, below
# the stage layer: 'instance placement denied' (shakenfist/instance.py) is
# the ledger refusing a write, and 'placement admitted over namespace
# capacity claim' is a placement admitted over an advisory claim. The three
# guard forms are added as top-level alternatives beside the scheduler's own
# group, since none of them is a substring of the stage phrases or of each
# other. Without them the query sees only the scheduler's per-candidate stage
# events, and the guard's own refusals -- which sit one layer below the stage
# check -- never appear at all. That produced a *Capacity guard census*
# section with nothing to count on every run since the stage-event filter
# was fixed (docs/plans/PLAN-ci-cloud-sizing-phase-02-baseline.md in
# shakenfist, decision D20, survey finding 4) even though the guard fired.
#
# The third guard message, 'placement recorded despite exceeding capacity
# guard' (also shakenfist/instance.py), is the P5 forced ground-truth write:
# a placement recorded even though the guard refused it. It matters more than
# its rarity suggests. Step 2f established that a cluster's first ~165 seconds
# admit every placement unguarded, because scheduler_node_capacity has no rows
# until the reconciler's first pass, and the reconciler then records the
# result as a node holding more than its own limit. That mechanism and the
# P5 forced write leave the *same* end state, and this event is the only
# thing which tells them apart -- so a census collecting the other two but
# not this one cannot distinguish the defect from its lookalike.
#
# The census cannot reuse the Loki dump that ansible/ci-gather-logs-loki.yml
# already puts in every bundle. That one is an unfiltered {job="shakenfist"}
# with limit 5000 and direction=forward over a six hour window, so it returns
# the first 5000 lines of the DEPLOY and never reaches the test window at all.
# Filtered to the scheduler's stage events and the guard's three audit
# messages, the same limit is still comfortable for a smoke run. The guard
# messages do not change that much: all three are emitted once per placement
# (shakenfist/instance.py), whereas the stage events are emitted once per
# candidate node considered, so a placement which evaluates many candidates
# logs many stage events and at most one guard event. A census holding
# exactly census_limit entries is reported as possibly truncated rather than
# as a complete count, which is the honest reading if this ever stops being
# true.
#
# Loki is installed only on the primary, in every topology
# (build-smoke-cluster/action.yml), and the census runs on the primary, so
# http://localhost:3100 is correct here -- exactly as the gather playbook does
# and explains.
#
# Usage:
#   tools/ci_headroom_collect.sh <primary> <ssh-user> [label]
#
# The label is free text describing the run, and smoke-cluster.yml passes the
# topology and the stestr config separated by a single space. It is written
# verbatim to /srv/ci/traces/headroom-label, followed by a newline. A harvest
# should read the whole file rather than its first line: the label is free
# text and nothing collapses a newline inside it, though no caller can
# produce one today. An absent label writes no file at all rather than an
# empty one, so the two cases stay distinguishable. It reaches the primary base64 encoded -- see the ssh call
# below, which explains why.
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
# max_entries_limit_per_query, whose default is 5000. It gives no signal when
# it cuts a response off at that limit, so the number is handed to the report
# as well: a census holding exactly this many entries is reported as possibly
# truncated rather than as a complete count of the run's refusals.
census_limit=5000

# There is deliberately no `|= "Added event"` line filter on the census query
# below. That is the message eventlog.add_event_multi logs under, but pylogrus'
# JsonFormatter merges the caller's fields over the record last and one of them
# is `message`, so the shipped JSON's `message` is the event's own message and
# the string `Added event` never appears in the line. Such a filter matches
# nothing at all, and the empty census which results is reported honestly as
# "no schedule stage events at all" -- which reads like an idle cluster rather
# than a broken query. See tools/queue-wait-report.py's docstring in the
# shakenfist repository, which learned this the same way.

if [ -z "${primary}" ]; then
    echo "usage: $0 <primary> <ssh-user> [label]"
    echo "SKIPPING: no headroom series or census collected."
    exit 0
fi

ssh_opts=(-i /srv/github/id_ci -o StrictHostKeyChecking=no
          -o UserKnownHostsFile=/dev/null)

echo "=== Stopping the headroom probe and taking the refusal census ==="

# ssh does not preserve argv boundaries: it joins its command arguments with
# spaces into a single string and the remote login shell re-parses that. The
# label smoke-cluster.yml passes contains a space, so sent as-is only the
# topology would bind to "label" on the far side, and a label carrying $(...),
# a backtick or a semicolon would run on the primary instead of being written
# down. base64 output has nothing the second parse can act on, and unlike
# printf '%q' it does not assume the remote login shell is bash. Any argument
# added to this call needs the same treatment.
label_b64=$(printf '%s' "${label}" | base64 -w0 2>/dev/null || true)
if [ -n "${label}" ] && [ -z "${label_b64}" ]; then
    echo "WARNING: could not base64 the label, so it will not reach the"
    echo "bundle. The summary below still has it."
fi

ssh "${ssh_opts[@]}" "${ssh_user}@${primary}" \
    bash -s -- "${census_limit}" "${label_b64}" <<'REMOTE_EOF' || true
census_limit="$1"
label=$(printf '%s' "${2:-}" | base64 -d 2>/dev/null || true)

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

# The scheduler's two stage forms, then the capacity guard's three audit
# messages, as top-level alternatives. See the header for why each is here.
census_match='schedule (at stage|has no candidates at stage)'
census_match="${census_match}|instance placement denied"
census_match="${census_match}|placement admitted over namespace capacity claim"
census_match="${census_match}|placement recorded despite exceeding capacity guard"

curl -sS -G http://localhost:3100/loki/api/v1/query_range \
    --data-urlencode "query={job=\"shakenfist\"} |~ \"${census_match}\"" \
    --data-urlencode "start=${start_ns}" \
    --data-urlencode "end=${end_ns}" \
    --data-urlencode "limit=${census_limit}" \
    --data-urlencode "direction=forward" \
    > /srv/ci/traces/headroom-census.json 2>/dev/null || true

# The label the runner passed us (topology plus stestr config) is not written
# anywhere on the primary today, so a later harvest over the bundle has to
# guess the topology from the artifact name -- and the "guests" bundle's name
# does not encode it, so the guess needs a lookup table that silently rots
# whenever the job matrix changes. Write it beside the series and census so it
# lands in the same "Gather logs" scp and the guess is no longer needed.
# Written only when there is something to write, so an absent file means
# unambiguously that no label was supplied rather than that one was empty.
if [ -n "${label}" ]; then
    printf '%s\n' "${label}" > /srv/ci/traces/headroom-label 2>/dev/null || true
fi

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

# The report is taken from the triggering component ref, which may predate
# --census-limit even where it is new enough to have the tool at all. The
# report treats an unknown argument as a usage error and exits 0 without
# printing anything, so an unconditional flag would silently cost the whole
# summary on those refs.
if grep -q -- '--census-limit' "${report}" 2>/dev/null; then
    report_args+=(--census-limit "${census_limit}")
fi
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
