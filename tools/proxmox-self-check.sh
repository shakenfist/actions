#!/bin/bash

# The last step of deploy-proxmox-on-shakenfist: before the action hands the
# node back, do what a consumer will do first -- mint a .vv through the API
# URL it was given, then open the tunnel that .vv names -- and report any
# failure as the substrate's, not the client's.
#
#   proxmox-self-check.sh --workdir DIR --api-url URL --node NAME \
#       --vmid VMID --token-id ID
#
# Unlike the mint in proxmox-publish-node.sh, this one reaches the API by
# name through the runner's own resolver, so it also proves the /etc/hosts
# entry that step wrote. The probe stops at the proxy's 200: TLS and SPICE
# belong to the client under test. See tools/proxmox-connect-probe.py.
#
# The .vv holds a live SPICE password, so it is removed on the way out; it
# is useless within a minute anyway.

set +o xtrace
set -o errexit
set -o nounset
set -o pipefail

# EPOCHREALTIME's fractional part follows the shell's own LC_NUMERIC, and
# this script both reads its own EPOCHREALTIME and awk-parses the mint
# script's, so a locale using a comma separator would otherwise make
# "elapsed" silently wrong.
export LC_ALL=C

fail() {
    echo "::error title=Proxmox substrate::$*"
    echo "proxmox-self-check.sh: $*" >&2
    exit 1
}

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

workdir=''
api_url=''
node=''
vmid=''
token_id=''

while [ "$#" -gt 0 ]; do
    case "$1" in
        --workdir|--api-url|--node|--vmid|--token-id)
            [ "$#" -ge 2 ] || fail "$1 needs a value"
            case "$1" in
                --workdir) workdir="$2" ;;
                --api-url) api_url="$2" ;;
                --node) node="$2" ;;
                --vmid) vmid="$2" ;;
                --token-id) token_id="$2" ;;
            esac
            shift 2
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done

for required in workdir api_url node vmid token_id; do
    [ -n "${!required}" ] || fail "--${required//_/-} is required"
done

vv="${workdir}/self-check.vv"
trap 'rm -f "${vv}"' EXIT

minted="$("${here}/tools/proxmox-mint-vv.sh" \
    --api-url "${api_url}" \
    --node "${node}" \
    --vmid "${vmid}" \
    --token-id "${token_id}" \
    --token-file "${workdir}/token" \
    --ca-file "${workdir}/pve-root-ca.pem" \
    --out "${vv}")" \
    || fail "could not mint a console ticket from ${api_url} (the reason is above)"

python3 "${here}/tools/proxmox-connect-probe.py" "${vv}" \
    || fail 'the node is deployed, but a freshly minted ticket did not open a tunnel from the runner (the probe says why above)'

elapsed="$(awk -v start="${minted}" -v now="${EPOCHREALTIME}" 'BEGIN {printf "%.1f", now - start}')"
echo "Self-check passed: minted through ${api_url} and tunnelled ${elapsed}s after the mint"
