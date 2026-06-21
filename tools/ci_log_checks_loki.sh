#!/bin/bash
set -euo pipefail

# ci_log_checks_loki.sh -- Loki-aware successor to ci_log_checks.sh.
#
# This script reproduces the INTENT of every non-etcd check in
# tools/ci_log_checks.sh, but instead of grepping the primary node's
# aggregated /var/log/syslog it queries the per-run Loki stood up in
# phase 3 of PLAN-remove-syslog-forwarding.
#
# Shaken Fist logs are now structured JSON (phase 1 field contract). The
# Loki stream labels are exactly:
#     {job="shakenfist", daemon=<daemon name>, host=<node name>}
# and the JSON line body carries (at least):
#     logger_name, ts, level, pid, thread_name, message,
#     exception_class, stack_trace, module, function
# plus any .with_fields(...) keys (instance_uuid, network_uuid,
# request_id, ...).
#
# Because the lines are JSON, we DO NOT raw-grep the line with `|=`
# (which is brittle against JSON-escaped quotes). Instead we parse with
# `| json` and match the structured fields, e.g.
#     {job="shakenfist"} | json | message =~ "(?i)<pattern>"
# Patterns that in the old flat "LEVEL message" text spanned the level
# and the message (e.g. "ERROR gunicorn", "ERROR sf") are RESTRUCTURED
# to match the parsed fields, e.g.
#     {job="shakenfist"} | json | level="ERROR" | message =~ "(?i)gunicorn"
#
# Args (same positional contract as ci_log_checks.sh):
#     $1  branch    -- gates the v0.7 vs v0.8+ forbidden set
#     $2  job_name  -- gates the non-upgrade-only forbidden set
#
# Environment:
#     LOKI_BASE_URL  -- default http://localhost:3100 (this script runs
#                       on the primary via tools/run_remote, where the
#                       CI Loki lives).
#
# DROPPED (deliberately, vs ci_log_checks.sh): the etcd checks. etcd was
# removed by the BYO-MariaDB plan, so the "Building new etcd connection"
# >5000 counted threshold and the "Cannot communicate with etcd, no
# configured server" forbidden pattern have NO successor here.
# (ci_event_checks.sh, which was entirely etcd-centric, likewise gets no
# Loki successor.)
#
# Full validation of this script is the phase-5 CI run against a live
# Loki. Here it is only bash -n / shellcheck / local-Loki smoke tested.

BRANCH="${1:-}"
JOB_NAME="${2:-}"

LOKI_BASE_URL="${LOKI_BASE_URL:-http://localhost:3100}"

# Wide query window. Phase 3 stands up a fresh Loki per CI run, so every
# line in it is from this run; we do not need a precise window. Six hours
# comfortably covers a CI run.
QUERY_WINDOW_SECONDS=$(( 6 * 60 * 60 ))
# Generous per-query line cap. Loki's query_range caps at this many
# entries; counts above it are reported as ">=LIMIT".
QUERY_LIMIT=5000

# Fixed-grace fallback (seconds) for the once-stable time anchor, used if
# the steady-state marker line is not found in Loki.
STABLE_GRACE_SECONDS=60

failures=0

echo
echo "Running Loki log checks for branch ${BRANCH} and job ${JOB_NAME}."
echo "Querying Loki at ${LOKI_BASE_URL}."
echo

# now_ns / start_ns helpers. Loki query_range wants RFC3339 or unix
# nanosecond timestamps; we use nanoseconds throughout.
now_ns() {
    # date %N gives nanoseconds-within-second; %s seconds.
    echo "$(date +%s)000000000"
}

# loki_query_range <logql> <start_ns> <end_ns> -> raw JSON response on stdout.
loki_query_range() {
    local query="${1}"
    local start_ns="${2}"
    local end_ns="${3}"

    # --data-urlencode handles all the LogQL metacharacters for us.
    curl -sS -G "${LOKI_BASE_URL}/loki/api/v1/query_range" \
        --data-urlencode "query=${query}" \
        --data-urlencode "start=${start_ns}" \
        --data-urlencode "end=${end_ns}" \
        --data-urlencode "limit=${QUERY_LIMIT}" \
        --data-urlencode "direction=forward"
}

# loki_count <logql> <start_ns> <end_ns> -> integer count of matched
# log entries (summed across all returned streams).
loki_count() {
    local query="${1}"
    local start_ns="${2}"
    local end_ns="${3}"

    local resp count
    resp=$(loki_query_range "${query}" "${start_ns}" "${end_ns}")
    # If Loki rejects the query or has a transient error it returns a plain
    # text body, not JSON; jq then fails. Guard so one hiccup does not abort
    # the whole gate under `set -euo pipefail` -- treat a non-JSON / non
    # numeric result as zero, but say so loudly on stderr.
    count=$(printf '%s' "${resp}" | jq '[.data.result[]?.values[]?] | length' 2>/dev/null || true)
    if ! [[ "${count}" =~ ^[0-9]+$ ]]; then
        echo "WARNING: Loki query did not return a JSON result; treating as 0." >&2
        echo "         query: ${query}" >&2
        echo "         response: ${resp}" >&2
        count=0
    fi
    printf '%s' "${count}"
}

# loki_show <logql> <start_ns> <end_ns> -- print up to the first 20
# matched lines (ts + line), mirroring the old "head -20" behaviour.
loki_show() {
    local query="${1}"
    local start_ns="${2}"
    local end_ns="${3}"

    loki_query_range "${query}" "${start_ns}" "${end_ns}" \
        | jq -r '[.data.result[]? | .stream as $s | .values[]?
                    | {ts: .[0], line: .[1], stream: $s}]
                 | sort_by(.ts) | .[:20][]
                 | "    [\(.stream.daemon // "?")@\(.stream.host // "?")] \(.line)"'
}

# check_forbidden <logql> <human description> <start_ns> <end_ns>
# -- fail (count>0) forbidden check; prints matches.
check_forbidden() {
    local query="${1}"
    local desc="${2}"
    local start_ns="${3}"
    local end_ns="${4}"

    echo "    Check for >>${desc}<< in logs."
    local count
    count=$(loki_count "${query}" "${start_ns}" "${end_ns}")
    if [ "${count}" -gt 0 ]; then
        echo "FAILURE: Forbidden condition found ${count} times: ${desc}"
        echo "         query: ${query}"
        loki_show "${query}" "${start_ns}" "${end_ns}"
        failures=$(( failures + 1 ))
    fi
}

# check_warning <logql> <human description> <start_ns> <end_ns>
# -- non-fatal; prints matches but only bumps a separate warning tally.
warnings=0
check_warning() {
    local query="${1}"
    local desc="${2}"
    local start_ns="${3}"
    local end_ns="${4}"

    echo "    Check for >>${desc}<< in logs."
    local count
    count=$(loki_count "${query}" "${start_ns}" "${end_ns}")
    if [ "${count}" -gt 0 ]; then
        echo "WARNING: Undesirable condition found in logs ${count} times: ${desc}"
        loki_show "${query}" "${start_ns}" "${end_ns}"
        warnings=$(( warnings + 1 ))
    fi
}

# regex_escape <string> -- escape RE2 regex metacharacters so a pattern is
# matched literally by a `=~` matcher. We escape the standard RE2 set:
# \ . + * ? ( ) [ ] { } ^ $ |. We do NOT escape for the LogQL string layer
# because the queries below wrap the regex in a LogQL BACKTICK (raw) string,
# which performs no escape processing -- so the RE2-escaped pattern reaches
# RE2 intact. (Double-quoted LogQL strings would swallow the backslashes,
# which is the bug this replaces.) Patterns must therefore not contain a
# backtick; none of ours do.
regex_escape() {
    # shellcheck disable=SC2001  # sed is clearest for a class of chars.
    printf '%s' "${1}" \
        | sed -e 's/[\\.[\*^$(){}?+|]/\\&/g' \
              -e 's/\]/\\]/g'
}

# msg_query <pattern> -- a JSON-structured LogQL query that matches the
# given (case-insensitive) substring against the parsed `message` field.
# The regex is delimited with LogQL backticks (raw string) so RE2 sees the
# escaping from regex_escape verbatim.
msg_query() {
    local pat
    pat=$(regex_escape "${1}")
    printf '%s' '{job="shakenfist"} | json | message =~ `(?i)'"${pat}"'`'
}

# level_msg_query <level> <pattern> -- restructured query for the old
# flat "LEVEL message" patterns: parsed level field equals <level> AND
# the parsed message contains <pattern>.
level_msg_query() {
    local level="${1}"
    local pat
    pat=$(regex_escape "${2}")
    printf '%s' '{job="shakenfist"} | json | level="'"${level}"'" | message =~ `(?i)'"${pat}"'`'
}

END_NS=$(now_ns)
START_NS=$(( END_NS - QUERY_WINDOW_SECONDS * 1000000000 ))

# ---------------------------------------------------------------------------
# Counted threshold: "Sent SIGTERM to " > 50.
#
# Old: grep -c across syslog + syslog.1, fail if total > 50. This message
# is emitted during shutdown; the message-substring match is exact.
# (The etcd "Building new etcd connection" >5000 counted threshold is
# DROPPED -- etcd is gone.)
# ---------------------------------------------------------------------------
echo
SIGTERM_QUERY=$(msg_query "Sent SIGTERM to ")
sigterms=$(loki_count "${SIGTERM_QUERY}" "${START_NS}" "${END_NS}")
echo "This CI run sent ${sigterms} SIGTERM signals while shutting down."
if [ "${sigterms}" -gt 50 ]; then
    echo "FAILURE: Too many SIGTERMs sent!"
    loki_show "${SIGTERM_QUERY}" "${START_NS}" "${END_NS}"
    failures=$(( failures + 1 ))
fi

# ---------------------------------------------------------------------------
# Always-forbidden set. Each fails if it appears even once.
#
# Most of these are message substrings -> msg_query. The one
# level+message-spanning entry is "ERROR gunicorn": in the old flat text
# "ERROR gunicorn ..." the "ERROR" was the level column, so we restructure
# to level="ERROR" + message =~ "gunicorn".
# ---------------------------------------------------------------------------
echo
echo "Always-forbidden checks."

# Message-substring forbidden patterns (always).
FORBIDDEN_MSG=(
    "Traceback (most recent call last):"
    "Extra vxlan present"
    "Fork support is only compatible with the epoll1 and poll polling strategies"
    "not using configured address"
    "Dumping thread traces"
    "because it is leased to"
    "Received a GOAWAY with error code ENHANCE_YOUR_CALM"
    "ConnectionFailedError"
    "invalid JWT in Authorization header"
    "Libvirt Error: XML error"
    "cluster wide cleanup daemon is deleting this IPAM as leaked"
    "Cleaning up leaked vxlan"
    "invalid salt"
    "unable to execute QEMU command"
    "bad argument type for built-in operation"
    "libvirt: QEMU Driver error : unsupported configuration"
    "unsupported configuration: disk type of 'vdc' does not support ejectable media"
    "architectural violation"
    "unhandled instance definition error"
)

# "ERROR gunicorn" spanned level+message in the flat format -> restructure.
check_forbidden "$(level_msg_query "ERROR" "gunicorn")" \
    "ERROR-level message containing 'gunicorn'" "${START_NS}" "${END_NS}"

# v0.8+-only forbidden patterns (skipped when branch matches v0.7).
if ! echo "${BRANCH}" | grep -q "v0.7"; then
    echo "INFO: Including forbidden strings for v0.8 onwards."
    FORBIDDEN_MSG+=(
        "Ignoring malformed cache entry"
        "WORKER TIMEOUT"
        "Repeated failures to add address to device"
        "Lock held by missing process on this node"
        "Cannot record event, no configured server"
        # NOTE: the old "Cannot communicate with etcd, no configured
        # server" pattern is DROPPED here -- etcd is gone.
        "Recreating not okay network on hypervisor"
        "Failed to change thread name"
        "Unhandled gRPC call failure"
    )
else
    echo "INFO: Branch matches v0.7; excluding v0.8+ forbidden strings."
fi

# non-upgrade-only forbidden pattern.
if ! echo "${JOB_NAME}" | grep -q "upgrade"; then
    echo "INFO: Including forbidden strings for non-upgrade jobs."
    FORBIDDEN_MSG+=("online upgrade")
else
    echo "INFO: Job is an upgrade job; excluding 'online upgrade' forbidden string."
fi

# NOTE: kernel/systemd/stderr-origin conditions -- apparmor="DENIED"
# (kernel audit), "segfault" (kernel), "*** Check failure stack trace:
# ***" (abseil/gRPC C++ fatal on stderr), and the systemd "State
# 'stop-sigterm' timed out" / "Main process exited, code=exited" /
# "Failed with result 'exit-code'" lines -- are intentionally NOT checked
# here. Loki carries only SF's Python application logs, so these never
# reach it. Their gating moves to a per-node systemctl/journald check
# (phase 5 of PLAN-remove-syslog-forwarding); they now live only in each
# node's journald.
for forbid in "${FORBIDDEN_MSG[@]}"; do
    check_forbidden "$(msg_query "${forbid}")" "${forbid}" "${START_NS}" "${END_NS}"
done

# ---------------------------------------------------------------------------
# Once-stable forbidden set: TIME ANCHOR replacement for the old "lines
# >= 1000" heuristic.
#
# Old: these patterns were only checked on syslog lines >= 1000, a proxy
# for "after the cluster finished its startup churn". Loki has no line
# numbers, so we anchor on the timestamp of a reliable steady-state
# marker.
#
# MARKER: the cluster-maintainer's "Running cluster maintenance" log line
# (shakenfist/daemons/cluster/main.py:79, emitted at the start of the
# first maintenance pass AFTER election succeeds, daemon="cluster"). This
# is a strong steady-state signal: it cannot fire until a node has won the
# cluster lock and entered its elected loop, i.e. the cluster has settled
# enough to elect a maintainer. We take its EARLIEST occurrence as the
# "stable from here" anchor.
#
# FALLBACK: if the marker is not found (e.g. the cluster never elected, or
# the message text changed), fall back to (earliest SF log ts + 60s).
#
# The level+message-spanning entry here is "ERROR sf": flat "ERROR sf..."
# was level=ERROR + a message starting with an "sf"-prefixed logger/text.
# We restructure to level="ERROR" + message =~ "sf" (case-insensitive),
# matching the original broad intent of "an ERROR from something named
# sf-* once the cluster is stable".
# ---------------------------------------------------------------------------
echo
echo "Determining steady-state time anchor for once-stable checks."

MARKER_QUERY='{job="shakenfist", daemon="cluster"} | json | message =~ "(?i)Running cluster maintenance"'
# Earliest matching entry's nanosecond timestamp (values[][0] is the ns ts
# string), or empty if none.
marker_ts=$(loki_query_range "${MARKER_QUERY}" "${START_NS}" "${END_NS}" \
    | jq -r '[.data.result[]?.values[]? | .[0] | tonumber] | min // empty')

if [ -n "${marker_ts}" ]; then
    STABLE_START_NS="${marker_ts}"
    echo "INFO: Using steady-state marker 'Running cluster maintenance'"
    echo "      earliest at ts ${STABLE_START_NS} ns as the once-stable anchor."
else
    # Fall back to earliest SF log ts + grace.
    earliest_ts=$(loki_query_range '{job="shakenfist"}' "${START_NS}" "${END_NS}" \
        | jq -r '[.data.result[]?.values[]? | .[0] | tonumber] | min // empty')
    if [ -n "${earliest_ts}" ]; then
        STABLE_START_NS=$(( earliest_ts + STABLE_GRACE_SECONDS * 1000000000 ))
        echo "WARNING: steady-state marker not found; falling back to"
        echo "         (earliest SF log ts + ${STABLE_GRACE_SECONDS}s) = ${STABLE_START_NS} ns."
    else
        STABLE_START_NS="${START_NS}"
        echo "WARNING: no SF logs found at all; once-stable checks will run"
        echo "         over the full window (anchor = window start)."
    fi
fi

echo
echo "Once-stable forbidden checks (from ts ${STABLE_START_NS} ns)."

# "ERROR sf" spanned level+message -> restructure to level=ERROR + msg "sf".
check_forbidden "$(level_msg_query "ERROR" "sf")" \
    "ERROR-level message containing 'sf' (once stable)" \
    "${STABLE_START_NS}" "${END_NS}"

FORBIDDEN_ONCE_STABLE_MSG=(
    "Failed to send event with gRPC"
    "Unknown server error while sending multi event with gRPC"
    "not committing online upgrade"
    "Cluster not yet stable"
    "StatusCode.UNAVAILABLE"
    # NOTE: the systemd "Failed with result 'exit-code'." line is NOT
    # checked here (systemd-origin, not in Loki); it moves to the
    # per-node systemctl/journald check in phase 5.
    "API query for node told node not ready"
    "Not processing queues as dependencies are unhealthy"
)
for forbid in "${FORBIDDEN_ONCE_STABLE_MSG[@]}"; do
    check_forbidden "$(msg_query "${forbid}")" \
        "${forbid} (once stable)" "${STABLE_START_NS}" "${END_NS}"
done

echo
if [ "${failures}" -gt 0 ]; then
    echo "...${failures} failures detected."
    exit 1
fi

# ---------------------------------------------------------------------------
# Non-fatal warnings. Printed but never fail the run (parity with the old
# script's trailing warning block).
# ---------------------------------------------------------------------------
echo
echo "Warning checks (non-fatal)."
WARNING_MSG=(
    "Waiting to acquire lock"
    "Transaction failure"
    "Lock refreshers should not be used under gunicorn"
)
for warn in "${WARNING_MSG[@]}"; do
    check_warning "$(msg_query "${warn}")" "${warn}" "${START_NS}" "${END_NS}"
done

echo
if [ "${warnings}" -gt 0 ]; then
    echo "...${warnings} warnings detected."
fi

exit 0
