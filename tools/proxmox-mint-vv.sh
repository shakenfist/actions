#!/bin/bash

# Mint a SPICE console .vv for one Proxmox VE guest, through the node's API
# with an API token, the way a console broker (kerbside's Proxmox driver)
# will.
#
#   proxmox-mint-vv.sh --api-url https://pve1.example:8006 --node pve1 \
#       --vmid 100 --token-id 'kerbside@pve!console' \
#       --token-file /path/to/token --ca-file /path/to/pve-root-ca.pem \
#       --out /path/to/console.vv [--resolve 10.0.2.2]
#
# On success it writes --out, mode 0600: a "[virt-viewer]" line, then one
# key=value line per field of the API's answer, and prints exactly one line
# to stdout -- the time the mint was requested, as epoch seconds with a
# fractional part. It is taken just BEFORE the request, so "now - that" is
# an upper bound on the ticket's age. Proxmox tickets are good for about 30
# seconds, so open the .vv straight away.
#
# THE SECRETS. This runs in public CI logs, and handles two credentials:
#
#   * The API token secret. It never reaches argv (where ps, /proc and any
#     xtrace would show it) or a shell variable: it is streamed from
#     --token-file into a 0600 header file inside a 0700 temporary
#     directory, and curl reads the header from there with -H @file. That
#     directory is removed on exit, whatever the exit.
#   * The ticket itself -- a SPICE password and a CONNECT pseudo-hostname
#     carrying a proxy ticket. It goes to --out, 0600, and is never printed,
#     including on failure.
#
# So this script must never run with xtrace, and it turns xtrace off before
# anything else in case it was inherited (SHELLOPTS=xtrace in the
# environment would otherwise switch it on here). Do not add set -x, and do
# not add curl --verbose or --trace: both print the Authorization header.
#
# --resolve ADDRESS pins the API URL's host to ADDRESS for this one request,
# as curl --resolve does. deploy-proxmox-on-shakenfist uses it once, to mint
# the ticket it then takes the node's FQDN from, before that FQDN resolves
# on the runner. Everything after that should leave it off, so that the name
# is resolved the way any other client on the runner will resolve it.
#
# The API host is never sent through an HTTP proxy, whatever http_proxy,
# https_proxy and no_proxy say: a Proxmox node deployed by this repository
# sits on a network the fleet's squid cannot route to, and the runner image
# exports https_proxy.
#
# Tickets are minted WITHOUT the API's optional "proxy" parameter. Proxmox
# then advertises "http://<hostname -f>:3128" (PVE::AccessControl::
# remote_viewer_config), which is what a real .vv carries and what a client
# under test ought to be dialling.

set +o xtrace
set -o errexit
set -o nounset
set -o pipefail

# EPOCHREALTIME's fractional part is formatted with the shell's own
# LC_NUMERIC decimal separator, not always ".". This script publishes that
# value as its stdout contract, and proxmox-self-check.sh parses it back
# with awk, so pin the locale here rather than let a runner's locale choose
# the separator silently.
export LC_ALL=C

usage() {
    sed -n '7,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

fail() {
    echo "proxmox-mint-vv.sh: $*" >&2
    exit 1
}

api_url=''
node=''
vmid=''
token_id=''
token_file=''
ca_file=''
out=''
resolve=''

while [ "$#" -gt 0 ]; do
    case "$1" in
        --api-url|--node|--vmid|--token-id|--token-file|--ca-file|--out|--resolve)
            [ "$#" -ge 2 ] || fail "$1 needs a value"
            case "$1" in
                --api-url) api_url="$2" ;;
                --node) node="$2" ;;
                --vmid) vmid="$2" ;;
                --token-id) token_id="$2" ;;
                --token-file) token_file="$2" ;;
                --ca-file) ca_file="$2" ;;
                --out) out="$2" ;;
                --resolve) resolve="$2" ;;
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

for required in api_url node vmid token_id token_file ca_file out; do
    [ -n "${!required}" ] || fail "--${required//_/-} is required"
done

# Everything below lands in a URL path, a header or curl's --resolve, so
# refuse anything that could step outside the value it is meant to be.
[[ "${node}" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || fail "--node is not a node name: ${node}"
[[ "${vmid}" =~ ^[0-9]+$ ]] || fail "--vmid is not a number: ${vmid}"
# user@realm!tokenname. PVE's own grammar is narrower; this only needs to
# keep the header line a single line with one '=' after the id.
[[ "${token_id}" =~ ^[^[:space:]=!]+@[^[:space:]=!]+![^[:space:]=!]+$ ]] \
    || fail "--token-id is not of the form user@realm!token"
[[ "${api_url}" =~ ^https://([A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\]):([0-9]+)/?$ ]] \
    || fail "--api-url must be https://<host>:<port>, got ${api_url}"
api_host="${BASH_REMATCH[1]}"
api_port="${BASH_REMATCH[2]}"
api_url="${api_url%/}"
if [ -n "${resolve}" ]; then
    [[ "${resolve}" =~ ^[0-9A-Fa-f:.]+$ ]] || fail "--resolve is not an address: ${resolve}"
fi

[ -r "${ca_file}" ] || fail "cannot read --ca-file ${ca_file}"
[ -s "${token_file}" ] || fail "--token-file ${token_file} is missing or empty"
[ -r "${token_file}" ] || fail "cannot read --token-file ${token_file}"
out_dir="$(dirname "${out}")"
[ -d "${out_dir}" ] || fail "the directory for --out does not exist: ${out_dir}"

# Everything this script creates is private to the runner's user.
umask 077
scratch="$(mktemp -d)"
trap 'rm -rf "${scratch}"' EXIT
header="${scratch}/header"
response="${scratch}/response"
headers="${scratch}/headers"

# printf is a builtin and tr reads the secret on stdin, so the secret is on
# no command line. Whitespace is stripped because the file an editor or an
# "echo >" wrote ends in a newline, and a newline would end the header.
{
    printf 'Authorization: PVEAPIToken=%s=' "${token_id}"
    tr -d '[:space:]' < "${token_file}"
    printf '\n'
} > "${header}"

curl_args=(
    --silent --show-error
    --max-time 20
    --noproxy "${api_host}"
    --cacert "${ca_file}"
    --header "@${header}"
    --request POST
    --data ''
    --output "${response}"
    --dump-header "${headers}"
    --write-out '%{http_code}'
)
if [ -n "${resolve}" ]; then
    curl_args+=(--resolve "${api_host}:${api_port}:${resolve}")
fi

minted="${EPOCHREALTIME}"
if ! status="$(curl "${curl_args[@]}" \
        "${api_url}/api2/json/nodes/${node}/qemu/${vmid}/spiceproxy")"; then
    fail "could not reach the Proxmox API at ${api_url} (curl failed; its error is above)"
fi

if [ "${status}" != '200' ]; then
    # PVE puts the reason in the status line's reason phrase -- "401
    # authentication failure", "403 Permission check failed (/vms/100,
    # VM.Console)" -- and sometimes a "message" in the body. Neither carries
    # a credential (an error answer holds no ticket, and PVE does not echo
    # the Authorization header), so both are worth showing.
    reason="$(head -n 1 "${headers}" 2>/dev/null | tr -d '\r' | cut -d ' ' -f 3- | head -c 300 || true)"
    message="$(jq -r '.message // empty' "${response}" 2>/dev/null | head -c 300 || true)"
    fail "the Proxmox API refused the console request with HTTP ${status}${reason:+ ${reason}}${message:+ (${message})}"
fi

# From here the response holds a live ticket, so nothing below may echo it:
# every check fails with a fixed message.
jq -e '.data | type == "object"' "${response}" > /dev/null 2>&1 \
    || fail 'the Proxmox API answered 200 but with no console object in "data"'
for key in proxy host tls-port password host-subject ca; do
    jq -e --arg k "${key}" '.data | has($k)' "${response}" > /dev/null \
        || fail "the console ticket has no \"${key}\" field"
done
# One field per line is the whole format, so a value with a raw newline in
# it would forge extra keys. The "ca" PEM arrives with its newlines already
# escaped as the two characters \n, which is what a .vv expects.
jq -e '[.data[] | tostring | test("[\r\n]")] | any | not' "${response}" > /dev/null \
    || fail 'a console ticket field contains a raw newline; refusing to write it as a .vv'

partial="${scratch}/console.vv"
{
    printf '[virt-viewer]\n'
    jq -r '.data | to_entries[] | "\(.key)=\(.value | tostring)"' "${response}"
} > "${partial}"
chmod 0600 "${partial}"
mv -f "${partial}" "${out}"

echo "${minted}"
