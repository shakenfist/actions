#!/bin/bash
set -e

# Deploy Shaken Fist via the shakenfist.shakenfist ansible collection.
#
# Replaces the getsf-based CI deploy. Builds the collection tarball from the
# checked-out shakenfist repo, installs it with ansible-galaxy, then runs the
# examples/_shared/site.yml playbook against the CI-generated inventory. Play 0
# of that playbook builds the server and client wheels once on this controller
# (the runner), and play 1 ships them to every node in the inventory.
#
# Arguments (positional):
#   $1  inventory     path to the generated ansible inventory
#   $2  mariadb_pw    MariaDB password (matches the BYO MariaDB install step)
#   $3  auth_secret   AUTH_SECRET_SEED value for the cluster
#   $4  system_key    system namespace key for the cluster
#
# The caller (workflow step) is responsible for exporting the proxy / pip
# environment (http_proxy / https_proxy / PIP_INDEX_URL) before invoking this,
# exactly as the other actions-repo scripts expect. We do NOT hardcode those
# here.

INVENTORY="${1:?inventory path required}"
MARIADB_PASSWORD="${2:?mariadb password required}"
AUTH_SECRET="${3:?auth secret required}"
SYSTEM_KEY="${4:?system key required}"

cd "${GITHUB_WORKSPACE}/shakenfist"

# Build the collection tarball into dist-collection/ using a dedicated venv so
# we control the ansible-core / setuptools_scm / packaging versions.
python3 -mvenv /tmp/collection-venv
. /tmp/collection-venv/bin/activate
pip install -U pip
pip install ansible-core setuptools_scm packaging build

python3 tools/build-collection.py

# Install the freshly built collection tarball (the same artifact release.yml
# publishes), forcing over any previously installed copy.
ansible-galaxy collection install dist-collection/*.tar.gz --force

# Run the deploy from this controller against the generated inventory. Play 0
# builds both wheels here (sf_build_local_wheels=true), play 1 ships them to
# every node. The cluster-config seeds mirror examples/single-node/group_vars.
ansible-playbook -i "${INVENTORY}" examples/_shared/site.yml \
    --extra-vars "sf_build_local_wheels=true \
        repo_path=${GITHUB_WORKSPACE}/shakenfist \
        client_repo_path=${GITHUB_WORKSPACE}/client-python \
        mariadb_host=127.0.0.1 \
        mariadb_port=3306 \
        mariadb_user=shakenfist \
        mariadb_password=${MARIADB_PASSWORD} \
        mariadb_database=shakenfist \
        auth_secret=${AUTH_SECRET} \
        system_key=${SYSTEM_KEY} \
        dns_server=8.8.8.8 \
        floating_network_ipblock=192.168.230.0/24 \
        http_proxy= \
        extra_config=[]"
