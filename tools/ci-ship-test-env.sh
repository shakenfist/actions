#!/bin/bash
set -euo pipefail

# Copy a deploy profile's test environment to the node a suite runs on, and
# print the shell fragment that loads it there.
#
# Arguments:
#   $1  user  The login user on the node (the base_image_user input).
#   $2  host  The node's address (the primary, which runs the suite).
#
# Environment:
#   DEPLOY_PROFILE  The deploy_profile input; empty when there is none.
#
# tools/ci-apply-deploy-profile.py writes the profile's test_env as
# "export NAME='value'" lines to /srv/github/ci-deploy-profile-test-env.sh.
# When DEPLOY_PROFILE is set and that file is not empty, this copies it to
# the node's home directory, keeping its 0600 mode, and prints
#
#     . ~/ci-deploy-profile-test-env.sh;
#
# on stdout, for the caller to append to the remote command after sfrc, so
# the profile's variables win. Otherwise it prints nothing and copies
# nothing. /srv/github outlives a job on a static runner, so without a
# profile a file an earlier run left there is ignored rather than trusted.
#
# Only variable names are logged, on stderr; stdout is the fragment alone.

USER_NAME="${1:?user required}"
HOST="${2:?host required}"
DEPLOY_PROFILE="${DEPLOY_PROFILE:-}"

TEST_ENV_FILE=/srv/github/ci-deploy-profile-test-env.sh
REMOTE_NAME=ci-deploy-profile-test-env.sh

if [ -z "${DEPLOY_PROFILE}" ] || [ ! -s "${TEST_ENV_FILE}" ]; then
    exit 0
fi

names=$(sed -n 's/^export \([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "${TEST_ENV_FILE}" | tr '\n' ' ')
echo "Exporting the deploy profile's test environment on ${HOST}: ${names}" >&2

scp -p -i /srv/github/id_ci -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "${TEST_ENV_FILE}" "${USER_NAME}@${HOST}:${REMOTE_NAME}" >&2

# The tilde is for the remote shell to expand.
# shellcheck disable=SC2088
echo ". ~/${REMOTE_NAME};"
