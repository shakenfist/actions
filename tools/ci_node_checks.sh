#!/bin/bash
set -euo pipefail

# ci_node_checks.sh -- per-node, GATING system-level health check.
#
# There is no central /var/log/syslog to grep: Shaken Fist ships its
# structured application logs to Loki, where tools/ci_log_checks_loki.sh
# gates them.
#
# However a class of failure conditions originate from the KERNEL, SYSTEMD,
# or a process's STDERR rather than from SF's Python logger, so they never
# reach Loki. ci_log_checks_loki.sh therefore deliberately does NOT check
# them (see the NOTE blocks in that script). This script restores that
# coverage by inspecting the things that DO see those conditions on each
# node directly: systemd's failed-unit list and the local journald.
#
# It is intended to be run ON a single node, as root (sudo), via
# tools/run_remote, once per node across the inventory (see
# ansible/ci-node-checks.yml). It exits NON-ZERO if this node has any
# system-level failure, after printing every finding.
#
# Args (same positional contract as ci_log_checks.sh, for consistency):
#     $1  branch    -- accepted but UNUSED here (kept so callers can invoke
#                      this script with the same argument shape they use for
#                      ci_log_checks.sh / ci_log_checks_loki.sh).
#     $2  job_name  -- the CI job. When it identifies the node-lifecycle job
#                      (matches *lifecycle*), the systemd-exit / failed-unit
#                      checks are SKIPPED: that job deliberately kills and
#                      restarts nodes, so sf-* daemons exiting with a failure
#                      is the expected behaviour under test, not a fault. The
#                      kernel/abseil checks (apparmor, segfault, C++ fatal)
#                      still run, since those remain real faults even there.
#
# The journald patterns checked below mirror EXACTLY the kernel/systemd/
# stderr-origin set that ci_log_checks_loki.sh excludes:
#     apparmor="DENIED"                          (kernel audit)
#     segfault                                   (kernel)
#     *** Check failure stack trace: ***         (abseil/gRPC C++ fatal)
#     State 'stop-sigterm' timed out. Killing.   (systemd)
#     Main process exited, code=exited           (systemd)
#     Failed with result 'exit-code'.            (systemd)
#
# Two scopes are used (see below). The kernel-origin patterns are matched
# against the WHOLE boot journal. The systemd/process patterns are matched
# only against the journal for `sf-*.service` units, because non-SF units
# (notably dnsmasq, which Shaken Fist restarts as networks come and go)
# routinely log transient, self-recovered "Failed with result 'exit-code'"
# / "Main process exited" lines that are normal churn and must not fail the
# run. systemd records those unit-state messages under the unit's journal,
# so `journalctl -u sf-*.service` still catches a genuine sf-database crash
# while ignoring dnsmasq and friends.
#
# `kerbside-*.service` (the Kerbside SPICE proxy, deployed by the "kerbside"
# deploy profile) is checked alongside `sf-*.service` in all three places: the
# failed-unit list, the journal patterns, and the restart count below. Kerbside
# logs through Python logging to the journal, and its normal startup lines
# contain none of the patterns above, so the same patterns apply unchanged. A
# host with no Kerbside units has nothing for those globs to match and is
# unaffected.

# shellcheck disable=SC2034  # BRANCH is intentionally unused; see the header
# -- it exists only to match ci_log_checks.sh's argument shape.
BRANCH="${1:-}"
JOB_NAME="${2:-}"

# The node-lifecycle job intentionally kills and restarts nodes, so sf-*
# daemons exiting with a failure (and units transiently failed) is the
# behaviour under test, not a fault. Skip those checks for it.
relax_unit_exits=0
case "${JOB_NAME}" in
    *lifecycle*) relax_unit_exits=1 ;;
esac

# Maximum matching lines to print per pattern, mirroring ci_log_checks.sh's
# "head -20" behaviour.
MAX_MATCHES=20

failures=0

echo
echo "Running per-node system checks on $(hostname)."
if [ "${relax_unit_exits}" -eq 1 ]; then
    echo "(job '${JOB_NAME}' kills nodes by design: skipping the"
    echo " systemd-exit / failed-unit checks; kernel/abseil checks still run.)"
fi
echo

# ---------------------------------------------------------------------------
# (a) Failed Shaken Fist systemd units. We scope to `sf-*.service` rather than
#     all units: the distro `dnsmasq.service` is left in a failed state on SF
#     nodes (SF runs its own per-network dnsmasq via managed executables, not
#     the system service), and other non-SF units may churn -- neither is a
#     Shaken Fist failure. A failed sf-* unit, however, is.
#     --no-legend drops the header/footer, --plain drops the tree glyphs, so
#     a non-empty output means at least one failed sf-* unit.
# ---------------------------------------------------------------------------
if [ "${relax_unit_exits}" -eq 1 ]; then
    echo "    Skipping failed sf-*.service unit check (node-killing job)."
else
    echo "    Check for failed sf-*.service and kerbside-*.service systemd units."
    # systemctl exits non-zero when there are failed units; we want to inspect
    # the output ourselves, so guard against set -e with `|| true`.
    failed_units=$(systemctl list-units --failed 'sf-*.service' 'kerbside-*.service' \
        --no-legend --plain 2>/dev/null || true)
    if [ -n "${failed_units}" ]; then
        echo "FAILURE: systemd reports failed sf-*/kerbside-* service units on $(hostname):"
        echo "${failed_units}" | head -n "${MAX_MATCHES}"
        failures=$(( failures + 1 ))
    fi
fi

# ---------------------------------------------------------------------------
# (a2) Kerbside restart counts. kerbside-api.service has Restart=always, so a
#      crash loop never shows up as a FAILED unit (systemd keeps restarting
#      it), and Kerbside exits 0 on a malformed config (kerbside#313), which
#      defeats Restart=on-failure as well. A healthy CI deploy never restarts
#      a Kerbside unit, so any non-zero NRestarts on a loaded kerbside-*
#      unit is a failure. Units that are not loaded (not-found) are ignored,
#      and a host with no Kerbside units has nothing to check.
# ---------------------------------------------------------------------------
if [ "${relax_unit_exits}" -eq 1 ]; then
    echo "    Skipping kerbside-*.service restart count check (node-killing job)."
else
    echo "    Check for restarted kerbside-*.service systemd units."
    kerbside_units=$(systemctl list-units --all 'kerbside-*.service' --no-legend --plain 2>/dev/null \
        | awk '$2 == "loaded" { print $1 }' || true)
    for unit in ${kerbside_units}; do
        restarts=$(systemctl show -p NRestarts --value "${unit}" 2>/dev/null || true)
        if [ -z "${restarts}" ]; then
            restarts=0
        fi
        if [ "${restarts}" -gt 0 ]; then
            echo "FAILURE: ${unit} has been restarted ${restarts} times on $(hostname) (NRestarts=${restarts})."
            failures=$(( failures + 1 ))
        fi
    done
fi

# ---------------------------------------------------------------------------
# (b) journald (current boot) for the patterns Loki cannot see. We dump two
#     journals ONCE each and grep -F (fixed strings) for each pattern --
#     fixed-strings sidesteps having to escape the regex metacharacters in
#     patterns like `*** Check failure stack trace: ***` and
#     `apparmor="DENIED"`.
#
#     `journalctl` can exit non-zero in odd states; capture it under
#     `|| true` so set -e does not abort before we have inspected it.
# ---------------------------------------------------------------------------
boot_journal=$(journalctl --no-pager -b 2>/dev/null || true)
sf_journal=$(journalctl --no-pager -b -u 'sf-*.service' -u 'kerbside-*.service' 2>/dev/null || true)

# check_patterns <journal-text> <scope-label> <pattern>...
# Greps the given journal text for each fixed-string pattern, printing and
# counting matches as failures.
check_patterns() {
    local journal="${1}"; shift
    local scope="${1}"; shift
    local pat count
    for pat in "$@"; do
        echo "    Check for >>${pat}<< in ${scope}."
        # grep -F: fixed strings. The pipeline is guarded with `|| true`
        # because grep exits 1 on no match, which would trip set -e/pipefail.
        count=$(printf '%s\n' "${journal}" | grep -F -c -- "${pat}" || true)
        if [ "${count}" -gt 0 ]; then
            echo "FAILURE: Forbidden journald condition found ${count} times: ${pat}"
            printf '%s\n' "${journal}" | grep -F -- "${pat}" | head -n "${MAX_MATCHES}"
            failures=$(( failures + 1 ))
        fi
    done
}

# Kernel-origin patterns: matched against the whole boot journal (they have
# no associated systemd unit). Always checked, including on node-lifecycle --
# a segfault or apparmor denial is a real fault even when nodes are being
# killed and restarted.
check_patterns "${boot_journal}" "this boot's journal" \
    'apparmor="DENIED"' \
    'segfault'

# Process-fatal pattern (abseil/gRPC C++ fatal on a daemon's stderr). Always
# checked: a crash like this is a real fault regardless of the job.
check_patterns "${sf_journal}" "the sf-*/kerbside-* service journal" \
    '*** Check failure stack trace: ***'

# systemd unit-exit patterns: matched only against the sf-*.service journal,
# so transient non-SF unit churn (e.g. dnsmasq restarts) is not flagged while
# a real sf-* daemon crash is. Skipped entirely on node-killing jobs, where
# these exits are the behaviour under test.
if [ "${relax_unit_exits}" -eq 1 ]; then
    echo "    Skipping systemd unit-exit checks (node-killing job)."
else
    check_patterns "${sf_journal}" "the sf-*/kerbside-* service journal" \
        "State 'stop-sigterm' timed out. Killing." \
        'Main process exited, code=exited' \
        "Failed with result 'exit-code'."
fi

echo
if [ "${failures}" -gt 0 ]; then
    echo "...${failures} system-level failures detected on $(hostname)."
    exit 1
fi

echo "No system-level failures detected on $(hostname)."
exit 0
