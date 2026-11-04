#!/bin/bash

# Read one Proxmox VE guest's status through the node's API with an API
# token, the way a console broker reads a guest before it mints a ticket.
#
#   proxmox-audit-probe.sh --api-url https://pve1.example:8006 --node pve1 \
#       --vmid 100 --token-id 'kerbside@pve!console' \
#       --token-file /path/to/token --ca-file /path/to/pve-root-ca.pem
#
# WHY THIS EXISTS. The console account this repository creates holds
# exactly VM.Audit and VM.Console on /vms (see ansible/vars/proxmox.yml).
# A mint proves VM.Console: without it PVE answers "403 Permission check
# failed". Nothing proved VM.Audit, because the mint was the only
# token-authenticated call here, so dropping VM.Audit from the role would
# have left this repository's own lane green and broken the consumer's
# scrape instead -- in another repository, as a 403 on a call this one
# never makes ([#239](https://github.com/shakenfist/actions/issues/239)).
#
# GET .../status/current needs VM.Audit on /vms/<vmid> and nothing else, so
# it 403s when that half of the pair is missing and succeeds when it is
# there. It is read-only, it changes nothing about the guest, and its answer
# holds no credential -- unlike a VM's config, which can carry a cloud-init
# password -- so this script may print what it got.
#
# THE SECRET. The API token secret never reaches argv or a shell variable:
# it is streamed from --token-file into a 0600 header file inside a 0700
# temporary directory which is removed on exit, and curl reads the header
# from there. So this script must never run with xtrace, and it turns
# xtrace off before anything else in case it was inherited; do not add
# set -x, and do not add curl --verbose or --trace, which print the
# Authorization header. See proxmox-mint-vv.sh, which handles the same
# credential and a ticket besides.
#
# No --resolve, unlike the mint: this is called from the self-check, after
# proxmox-publish-node.sh has put the node's FQDN in the runner's
# /etc/hosts, so it reaches the API by name through the runner's own
# resolver -- which is the point, since so will a consumer.

set +o xtrace
set -o errexit
set -o nounset
set -o pipefail

usage() {
    # The usage example in the header, found by content so that editing the
    # comment above it cannot shift what --help prints.
    sed -n '/^#   proxmox-audit-probe.sh /,/^#$/{/^#$/d;p}' "${BASH_SOURCE[0]}" |
        sed 's/^# \{0,1\}//' >&2
    exit 2
}

fail() {
    echo "proxmox-audit-probe.sh: $*" >&2
    exit 1
}

# The input guards, the Authorization header and the common curl arguments
# are shared with proxmox-mint-vv.sh. Sourced after fail(), because that is
# how the library reports.
# shellcheck source=tools/proxmox-api-lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/proxmox-api-lib.sh"

api_url=''
node=''
vmid=''
token_id=''
token_file=''
ca_file=''
# The shared guards and curl arguments read this; see the header for why it
# is always empty here.
resolve=''

while [ "$#" -gt 0 ]; do
    case "$1" in
        --api-url|--node|--vmid|--token-id|--token-file|--ca-file)
            [ "$#" -ge 2 ] || fail "$1 needs a value"
            case "$1" in
                --api-url) api_url="$2" ;;
                --node) node="$2" ;;
                --vmid) vmid="$2" ;;
                --token-id) token_id="$2" ;;
                --token-file) token_file="$2" ;;
                --ca-file) ca_file="$2" ;;
            esac
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done

for required in api_url node vmid token_id token_file ca_file; do
    [ -n "${!required}" ] || fail "--${required//_/-} is required"
done

# Everything that identifies the API target lands in a URL path or an HTTP
# header, so refuse anything that could step outside the value it is meant
# to be. The guards are shared with the mint, which calls the same API with
# the same credential; this normalises api_url and sets api_host and
# api_port.
pve_validate_api_target

# Everything this script creates is private to the runner's user.
umask 077
scratch="$(mktemp -d)"
trap 'rm -rf "${scratch}"' EXIT
header="${scratch}/header"
response="${scratch}/response"
headers="${scratch}/headers"

pve_write_auth_header "${token_id}" "${token_file}" "${header}"

# No --request: a GET is curl's default, and the answer is read-only.
pve_common_curl_args "${header}" "${response}" "${headers}"

if ! status="$(curl "${pve_curl_args[@]}" \
        "${api_url}/api2/json/nodes/${node}/qemu/${vmid}/status/current")"; then
    fail "could not reach the Proxmox API at ${api_url} (curl failed; its error is above)"
fi

refusal=''
if [ "${status}" != '200' ]; then
    refusal="$(pve_describe_refusal "${status}" "${headers}" "${response}")"
fi
if [ "${status}" = '403' ]; then
    # The failure this probe is here to catch, so say what it means rather
    # than only what PVE said. PVE names the path and the privilege it
    # wanted in the reason phrase.
    fail "the Proxmox API refused a read of guest ${vmid} with ${refusal}." \
         "The token is missing VM.Audit on /vms, which anything brokering this node's consoles" \
         "needs to find a guest before it can mint a ticket for it"
fi
if [ -n "${refusal}" ]; then
    fail "the Proxmox API refused a read of guest ${vmid} with ${refusal}"
fi

jq -e '.data | type == "object"' "${response}" > /dev/null 2>&1 \
    || fail 'the Proxmox API answered 200 but with no guest object in "data"'
jq -e '.data | has("status")' "${response}" > /dev/null \
    || fail 'the guest status read has no "status" field'
guest_status="$(jq -r '.data.status' "${response}")"
# PVE's own return schema makes "vmid" optional here, so this checks it
# only when it is sent: an answer about a different guest would mean the
# path was built wrong, which the guards above are meant to prevent.
answered_vmid="$(jq -r '.data.vmid // empty' "${response}")"
if [ -n "${answered_vmid}" ] && [ "${answered_vmid}" != "${vmid}" ]; then
    fail "asked the Proxmox API about guest ${vmid} and it answered about ${answered_vmid}"
fi

# One line, no credential: the status is the guest's run state.
echo "Read guest ${vmid} on ${node} through the API token: status ${guest_status}"
