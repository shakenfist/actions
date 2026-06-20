#!/bin/bash
set -euo pipefail

# ci_node_checks.sh -- per-node, GATING system-level health check.
#
# Part of phase 5 of the Shaken Fist "ship logs to Loki" plan
# (PLAN-remove-syslog-forwarding). Once rsyslog forwarding is removed there
# is no central /var/log/syslog; Shaken Fist's structured application logs go
# to Loki and are gated by tools/ci_log_checks_loki.sh.
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
#     $2  job_name  -- accepted but UNUSED here (as above).
#
# The journald patterns checked below mirror EXACTLY the kernel/systemd/
# stderr-origin set that ci_log_checks_loki.sh excludes:
#     apparmor="DENIED"                          (kernel audit)
#     segfault                                   (kernel)
#     *** Check failure stack trace: ***         (abseil/gRPC C++ fatal)
#     State 'stop-sigterm' timed out. Killing.   (systemd)
#     Main process exited, code=exited           (systemd)
#     Failed with result 'exit-code'.            (systemd)

# shellcheck disable=SC2034  # BRANCH/JOB_NAME are intentionally unused; see
# the header -- they exist only to match ci_log_checks.sh's argument shape.
BRANCH="${1:-}"
# shellcheck disable=SC2034
JOB_NAME="${2:-}"

# Maximum matching lines to print per pattern, mirroring ci_log_checks.sh's
# "head -20" behaviour.
MAX_MATCHES=20

failures=0

echo
echo "Running per-node system checks on $(hostname)."
echo

# ---------------------------------------------------------------------------
# (a) Failed systemd units. `systemctl list-units --failed` lists any unit
#     systemd considers failed; in a healthy CI run there should be none.
#     --no-legend drops the header/footer, --plain drops the tree glyphs, so
#     a non-empty output means at least one failed unit.
# ---------------------------------------------------------------------------
echo "    Check for failed systemd units."
# systemctl exits non-zero when there are failed units; we want to inspect
# the output ourselves, so guard against set -e with `|| true`.
failed_units=$(systemctl list-units --failed --no-legend --plain 2>/dev/null || true)
if [ -n "${failed_units}" ]; then
    echo "FAILURE: systemd reports failed units on $(hostname):"
    echo "${failed_units}" | head -n "${MAX_MATCHES}"
    failures=$(( failures + 1 ))
fi

# ---------------------------------------------------------------------------
# (b) journald (current boot) for kernel/systemd/stderr-origin patterns that
#     Loki cannot see. We dump the current boot's journal ONCE and grep -F
#     (fixed strings) for each pattern -- fixed-strings sidesteps having to
#     escape the regex metacharacters in patterns like
#     `*** Check failure stack trace: ***` and `apparmor="DENIED"`.
#
#     `journalctl -b` can exit non-zero in odd states; capture it under
#     `|| true` so set -e does not abort before we have inspected it.
# ---------------------------------------------------------------------------
boot_journal=$(journalctl --no-pager -b 2>/dev/null || true)

# FORBIDDEN: kernel/systemd/stderr-origin substrings. These mirror EXACTLY
# the set excluded from ci_log_checks_loki.sh.
FORBIDDEN=(
    'apparmor="DENIED"'
    'segfault'
    '*** Check failure stack trace: ***'
    "State 'stop-sigterm' timed out. Killing."
    'Main process exited, code=exited'
    "Failed with result 'exit-code'."
)

for forbid in "${FORBIDDEN[@]}"; do
    echo "    Check for >>${forbid}<< in this boot's journal."
    # grep -F: fixed strings (no regex). -c: count. The whole pipeline is
    # guarded with `|| true` because grep exits 1 on no match, which would
    # trip set -e and -o pipefail.
    count=$(printf '%s\n' "${boot_journal}" | grep -F -c -- "${forbid}" || true)
    if [ "${count}" -gt 0 ]; then
        echo "FAILURE: Forbidden journald condition found ${count} times: ${forbid}"
        printf '%s\n' "${boot_journal}" | grep -F -- "${forbid}" | head -n "${MAX_MATCHES}"
        failures=$(( failures + 1 ))
    fi
done

echo
if [ "${failures}" -gt 0 ]; then
    echo "...${failures} system-level failures detected on $(hostname)."
    exit 1
fi

echo "No system-level failures detected on $(hostname)."
exit 0
