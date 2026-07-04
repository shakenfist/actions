#!/bin/bash
set -e

# Install the shakenfist.shakenfist ansible collection from a shakenfist
# repository checkout, so that playbooks referencing the collection's
# modules (shakenfist.shakenfist.sf_instance and friends) resolve on this
# runner. The under-cloud topology and image-build playbooks create their
# test instances with these modules; they used to resolve unqualified
# sf_* names from the shims that shipped with shakenfist-client, but
# those shims were retired in favour of the collection.
#
# Arguments (positional):
#   $1  checkout   path to the shakenfist repository checkout
#                  (default: ${GITHUB_WORKSPACE}/shakenfist)
#
# The in-tree galaxy.yml carries a placeholder version (0.0.0), which is
# fine here -- we only need the module plugins resolvable. Deploy paths
# that ship the collection to real clusters build a properly versioned
# tarball with tools/build-collection.py instead.

CHECKOUT="${1:-${GITHUB_WORKSPACE}/shakenfist}"

ansible-galaxy collection install \
    "${CHECKOUT}/shakenfist/deploy/collection" --force
