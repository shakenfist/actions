#!/bin/bash

# Stand up a Proxmox VE node in the runner's own ShakenFist namespace: fetch
# the shakenfist.shakenfist collection and run
# ansible/proxmox-single-node.yml from the same tree as this script.
#
#   proxmox-deploy.sh --workdir DIR --base-user USER \
#       --node-address ADDRESS --smoke-vmid VMID
#
# Run on a ShakenFist CI runner by deploy-proxmox-on-shakenfist; the runner
# is the instance named $(hostname), which the playbook adds the node's
# network to. It leaves three files in --workdir for the steps after it:
#
#   token           the API token secret, 0600, written by the playbook
#   pve-root-ca.pem the node's root CA
#   facts.json      node name, FQDN, address, PVE version, token id, vmid,
#                   whether /dev/kvm existed
#
# The playbook and task files are taken from this script's own checkout, not
# from a fresh clone of shakenfist/actions: that way the action at a given
# ref runs that ref's playbook, which is what lets this repository's CI test
# a pull request's version of it.
#
# Nothing here reads the token secret; the playbook writes it with no_log.
# xtrace is turned off regardless, in case it was inherited, because this
# runs in public CI logs and a later edit might.

set +o xtrace
set -o errexit
set -o nounset
set -o pipefail

fail() {
    echo "proxmox-deploy.sh: $*" >&2
    exit 1
}

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

workdir=''
base_user=''
node_address=''
smoke_vmid=''

while [ "$#" -gt 0 ]; do
    case "$1" in
        --workdir|--base-user|--node-address|--smoke-vmid)
            [ "$#" -ge 2 ] || fail "$1 needs a value"
            case "$1" in
                --workdir) workdir="$2" ;;
                --base-user) base_user="$2" ;;
                --node-address) node_address="$2" ;;
                --smoke-vmid) smoke_vmid="$2" ;;
            esac
            shift 2
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done

for required in workdir base_user node_address smoke_vmid; do
    [ -n "${!required}" ] || fail "--${required//_/-} is required"
done

# These end up in a networkspec string and in commands on the node, so
# refuse anything that is not plainly what it says it is.
[[ "${base_user}" =~ ^[a-z_][a-z0-9_-]*$ ]] || fail "--base-user is not a user name: ${base_user}"
[[ "${node_address}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] \
    || fail "--node-address is not an IPv4 address: ${node_address}"
[[ "${smoke_vmid}" =~ ^[0-9]+$ ]] || fail "--smoke-vmid is not a number: ${smoke_vmid}"
[ "${smoke_vmid}" -ge 100 ] || fail "--smoke-vmid must be 100 or more, as PVE requires"
[[ "${workdir}" = /* ]] || fail "--workdir must be an absolute path: ${workdir}"

mkdir -p "${workdir}"
chmod 0700 "${workdir}"

# The collection is built from a shakenfist checkout, as every other
# instance-creating path here does it. This is a plain clone rather than
# actions/checkout because actions/checkout refuses to write anywhere
# outside GITHUB_WORKSPACE, and the consumer's workspace is not ours to put
# a checkout in. Deliberately unpinned: this lane wants to run against
# current shakenfist develop, not a stale ref. The SHA is logged so that a
# weekly drift failure can be told apart from a shakenfist regression --
# the substrate's own report-proxmox-substrate-failure.sh points a reader
# here before blaming Proxmox.
rm -rf "${workdir}/shakenfist"
git clone --quiet --depth 1 https://github.com/shakenfist/shakenfist "${workdir}/shakenfist"
echo "shakenfist checkout: $(git -C "${workdir}/shakenfist" rev-parse HEAD)"
"${here}/tools/install-collection.sh" "${workdir}/shakenfist"

# As JSON, built by jq, rather than as a "k=v k=v" string: ansible splits
# the latter on whitespace and parses quotes in it, so a value is only ever
# a value this way.
extra_vars="${workdir}/extra-vars.json"
jq -n \
    --arg identifier "$(hostname)" \
    --arg base_user "${base_user}" \
    --arg node_address "${node_address}" \
    --argjson smoke_vmid "${smoke_vmid}" \
    --arg token_file "${workdir}/token" \
    --arg ca_file "${workdir}/pve-root-ca.pem" \
    --arg facts_file "${workdir}/facts.json" \
    '{identifier: $identifier,
      base_user: $base_user,
      proxmox_node_address: $node_address,
      proxmox_smoke_vmid: $smoke_vmid,
      proxmox_token_file: $token_file,
      proxmox_ca_file: $ca_file,
      proxmox_facts_file: $facts_file}' > "${extra_vars}"

ansible-playbook -i /home/debian/ansible-hosts \
    --extra-vars "@${extra_vars}" \
    "${here}/ansible/proxmox-single-node.yml"

for produced in token pve-root-ca.pem facts.json; do
    [ -s "${workdir}/${produced}" ] \
        || fail "the playbook finished but did not leave ${workdir}/${produced}"
done
