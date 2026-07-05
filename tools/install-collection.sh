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
# We build a tarball and install that, rather than installing from the
# source directory, because the source-directory install form is not
# supported by the older ansible-galaxy on some runner images (it fails
# with "Invalid collection name"). Building and installing tarballs has
# been supported since ansible 2.9. The in-tree galaxy.yml carries a
# placeholder version (0.0.0), which is fine here -- we only need the
# module plugins resolvable. Deploy paths that ship the collection to
# real clusters build a properly versioned tarball with
# tools/build-collection.py instead.

CHECKOUT="${1:-${GITHUB_WORKSPACE}/shakenfist}"

builddir=$(mktemp -d)
trap 'rm -rf ${builddir}' EXIT

ansible-galaxy collection build \
    "${CHECKOUT}/shakenfist/deploy/collection" \
    --output-path "${builddir}"
ansible-galaxy collection install \
    "${builddir}"/shakenfist-shakenfist-*.tar.gz --force
