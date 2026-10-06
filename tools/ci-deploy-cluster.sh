#!/bin/bash
set -euo pipefail

# Deploy Shaken Fist onto a CI cluster with the collection, and, when the
# caller's deploy profile asks for it, deploy a second time and require that
# no checked systemd unit restarted. Run by build-smoke-cluster/action.yml's
# "Deploy Shaken Fist via the collection" step.
#
# Arguments:
#   $1  topology  The build-smoke-cluster topology input. localhost reaches
#                 MariaDB and Loki on 127.0.0.1; every other topology reaches
#                 them on the primary's mesh address.
#
# Environment (the step passes its inputs this way rather than interpolating
# them into a script, so values with shell metacharacters cannot inject):
#   MARIADB_PASSWORD, AUTH_SECRET, SYSTEM_KEY  passed to deploy-collection.sh.
#   DEPLOY_PROFILE  the deploy_profile input; empty when there is none.
#
# Both deploys run one deploy-collection.sh command line, built once below.
# Without a profile it is exactly the command the action ran before
# profiles existed, and there is no second deploy.
#
# The second deploy runs only when the profile asked for it, which
# tools/ci-apply-deploy-profile.py records by writing the redeploy units
# file (and removing a stale one). /srv/github outlives a job on a static
# runner, so a run without a profile must not trust a file an earlier run
# left behind: the file counts only when DEPLOY_PROFILE is set, because only
# then did this job's apply step decide whether it should exist.
#
# The check has to run here, straight after the first deploy: smoke-cluster.yml
# later restarts sf-api on purpose (the JWKS CA and drain steps), and a second
# deploy after those would legitimately restart it.
#
# Never trace this script: the deploy command line carries credentials.

TOPOLOGY="${1:?topology required}"
DEPLOY_PROFILE="${DEPLOY_PROFILE:-}"

STATE_DIR=/srv/github
INVENTORY="${STATE_DIR}/ci-inventory.yaml"
FACTS="${STATE_DIR}/ci-topology-facts.json"
EXTRA_VARS_FILE="${STATE_DIR}/ci-deploy-profile-extra-vars.json"
REDEPLOY_UNITS_FILE="${STATE_DIR}/ci-deploy-profile-redeploy-units"
REDEPLOY_STATE_FILE="${STATE_DIR}/ci-redeploy-check-state"
TOOLS="${GITHUB_WORKSPACE}/actions/tools"

# MariaDB is installed only on the primary, and the daemons ship logs to the
# Loki on the primary. Single-node smoke reaches both on 127.0.0.1;
# multi-node clusters must use the primary's mesh IP (read from the topology
# facts), since 127.0.0.1 would point every node at its own (absent) MariaDB /
# Loki -- in particular a second database node's sf-database would have no
# MariaDB to reach.
if [ "${TOPOLOGY}" == "localhost" ]; then
    mariadb_host="127.0.0.1"
    loki_url="http://127.0.0.1:3100"
else
    primary_mesh=$(python3 -c "import json, sys; d = json.load(open(sys.argv[1]));
print([n['mesh_ip'] for n in d['nodes'] if n['name'] == 'primary'][0])" "${FACTS}")
    mariadb_host="${primary_mesh}"
    loki_url="http://${primary_mesh}:3100"
fi

# Without a profile the array is empty and expands to no argument at all, so
# the deploy command is exactly what it was before profiles.
profile_args=()
if [ -n "${DEPLOY_PROFILE}" ]; then
    profile_args=("${EXTRA_VARS_FILE}")
fi

# deploy <label>: run the one deploy command, logging how long it took
# whether or not it succeeded, so a run near its timeout shows where the
# time went.
deploy() {
    local label="${1}"
    local start status=0
    start=$(date +%s)
    "${TOOLS}/deploy-collection.sh" \
        "${INVENTORY}" \
        "${MARIADB_PASSWORD}" \
        "${AUTH_SECRET}" \
        "${SYSTEM_KEY}" \
        "${loki_url}" \
        "${mariadb_host}" \
        "${profile_args[@]}" || status=$?
    echo "The ${label} deploy took $(( $(date +%s) - start )) seconds (exit status ${status})."
    return "${status}"
}

deploy first

if [ -z "${DEPLOY_PROFILE}" ] || [ ! -f "${REDEPLOY_UNITS_FILE}" ]; then
    exit 0
fi

mapfile -t units < "${REDEPLOY_UNITS_FILE}"
echo
echo "The deploy profile asks for a second deploy that restarts none of: ${units[*]}"
python3 "${TOOLS}/ci-redeploy-check.py" snapshot "${INVENTORY}" "${REDEPLOY_STATE_FILE}" "${units[@]}"
deploy second
python3 "${TOOLS}/ci-redeploy-check.py" compare "${INVENTORY}" "${REDEPLOY_STATE_FILE}" "${units[@]}"
