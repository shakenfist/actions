#!/bin/bash

# Copyright 2019 Michael Still and contributors
#
# Run the headroom report, and decide whether its verdict may fail the job.
#
# Split out of ci_headroom_collect.sh so that the one part of the headroom
# instrument which CAN fail a build is a short script taking a report path and
# some arguments, with no ssh, no scp and no Loki in it. That is what makes it
# runnable by tests/test_ci_headroom_verdict.py. The collection around it
# cannot be tested that way, and does not need to be, because it cannot fail
# a job.
#
# The contract, in full:
#
#   * ci_headroom_report.py returns 3, and only 3, when the cluster-wide ratio
#     of committed vCPU to the schedulable ledger sits outside the band CI
#     sizing is held to. That is a statement about the cloud the suite just
#     ran on rather than about the instrument, and phase 5 of
#     PLAN-ci-cloud-sizing decided it may stop a merge.
#
#   * Every other non-zero status is the report being unhappy rather than the
#     cloud: a series it cannot parse, a python that will not run it, a flag
#     it was passed and does not have. Each of those is a reason to distrust
#     the measurement, not to fail the build, so they are reported in the log
#     and let go. That is D15 of phase 1, which still governs every path but
#     the one above.
#
# Two guards sit in front of the gate, and both fail towards not gating.
#
# The first is version skew, handled the way this instrument handles it
# everywhere else: ci_headroom_collect.sh greps the report's source before
# passing --census-limit or --waits, because a report from an older component
# ref does not have them. The same hazard applies here and matters more, since
# its failure mode is a red job on an innocent pull request rather than a
# missing paragraph in a log. So the report has to carry BAND_VIOLATION_EXIT
# in its source to be trusted with the status; for a report that does not, 3
# means nothing in particular and is treated as the report being unhappy. A
# rename on either side breaks the match and stops gating loudly, rather than
# gating on a coincidence.
#
# The second is an off switch. shakenfist's functional-tests.yml consumes
# smoke-cluster.yml @main with no pin, and the band itself is fitted and
# maintained in the shakenfist repository, so a band that turns out to be too
# tight -- or an under-cloud that drifts, which is the condition this
# instrument exists to detect -- can turn every functional job in the fleet
# red without anything landing here at all. CI_HEADROOM_GATE=false, plumbed
# as smoke-cluster.yml's headroom_gate input, downgrades a band violation to
# the same logged-and-swallowed path as everything else, so the remedy is one
# line in a caller rather than a cross-repository commit.

report="${1:-}"
if [ -z "${report}" ]; then
    echo "usage: $0 <path to ci_headroom_report.py> [report arguments...]" >&2
    exit 64
fi
shift

band_violation_status=3
band_violation_sentinel='BAND_VIOLATION_EXIT'

status=0
python3 "${report}" "$@" || status=$?

if [ "${status}" -eq 0 ]; then
    exit 0
fi

not_the_cloud() {
    echo
    echo "$1"
    echo "It is being read as the report failing rather than as a statement"
    echo "about the cloud, so it is not failing this job. The raw series and"
    echo "census are still in the bundle."
}

if [ "${status}" -ne "${band_violation_status}" ]; then
    not_the_cloud "The headroom report exited ${status}, which is not the band violation status (${band_violation_status})."
    exit 0
fi

if ! grep -q -- "${band_violation_sentinel}" "${report}" 2>/dev/null; then
    not_the_cloud "The headroom report exited ${band_violation_status}, but its source does not mention ${band_violation_sentinel}, so it does not implement the band violation contract."
    exit 0
fi

case "${CI_HEADROOM_GATE:-true}" in
    0|false|False|FALSE|no|No|NO|off|Off|OFF)
        echo
        echo "The headroom verdict above is a band violation, but the gate is"
        echo "switched off for this run (CI_HEADROOM_GATE=${CI_HEADROOM_GATE})."
        echo "The verdict stands and is worth acting on; it is simply not"
        echo "failing this job."
        exit 0
        ;;
esac

# The annotation renders at the top of the run summary. Without it a reader
# sees only a failed step whose name is entirely about collection, and has to
# open it and scroll past the series, the census and the whole report to find
# out that this is a sizing verdict -- which is the opposite of the point.
echo "::error title=Cluster headroom outside the CI sizing band::Not a test failure. The cluster this suite ran on was the wrong size. See the headroom summary in this step's log."
echo
echo "The headroom verdict above has failed this job. This is not a test"
echo "failure: the tests are whatever the log above says they are. It says"
echo "the cluster they ran on sat outside the band CI sizing is held to,"
echo "which is a sizing question rather than a change under review. The"
echo "verdict says which bound was crossed and by how much."
exit "${band_violation_status}"
