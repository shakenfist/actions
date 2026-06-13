#!/bin/bash
# Patch the oVirt host-deploy OVN Ansible role (oVirt/ovirt-engine#949).
#
# This script runs on the oVirt target node. It fixes a bug introduced in
# oVirt 4.5 where the host-deploy OVN configuration task runs even when OVN
# is not configured on the cluster: the condition "ovn_central is defined"
# fires when ovn_central is None/empty. The fix adds None/length checks.
#
# Two things vary across releases, so we probe rather than assume:
#   * Path. 4.4/4.5 ship the role under the ansible-runner-service-project
#     layout; 4.3 ships it under /usr/share/ovirt-engine/playbooks.
#   * Whether the bug is even present. 4.3's configure.yml already guards
#     the condition (... and ovn_central | ipaddr), so it has no bare
#     "ovn_central is defined" line and needs no patch.
# So: find the configure.yml at whichever path exists, and only sed it if
# the buggy end-of-line pattern is actually there. Otherwise this is a
# clean no-op (e.g. on 4.3) rather than a hard failure.

set -xe
export PS4='=======================\n+ '

# Candidate locations across oVirt releases (newest layout first).
CANDIDATES=(
    /usr/share/ovirt-engine/ansible-runner-service-project/project/roles/ovirt-provider-ovn-driver/tasks/configure.yml
    /usr/share/ovirt-engine/playbooks/roles/ovirt-provider-ovn-driver/tasks/configure.yml
)

OVN_CFG=
for c in "${CANDIDATES[@]}"; do
    if [ -f "$c" ]; then
        OVN_CFG="$c"
        break
    fi
done

if [ -z "${OVN_CFG}" ]; then
    echo 'No ovirt-provider-ovn-driver configure.yml found; nothing to patch.'
    exit 0
fi

if ! grep -q 'ovn_central is defined$' "${OVN_CFG}"; then
    echo "OVN configure.yml (${OVN_CFG}) has no bare 'ovn_central is defined'"
    echo 'condition; this oVirt version is unaffected by #949. Skipping.'
    exit 0
fi

sudo sed -i \
    's/ovn_central is defined$/ovn_central is defined and ovn_central != None and ovn_central | length != 0/' \
    "${OVN_CFG}"

echo '--- Patched OVN configure.yml ---'
grep -n 'ovn_central' "${OVN_CFG}"
