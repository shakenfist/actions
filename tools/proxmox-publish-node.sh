#!/bin/bash

# Hand a freshly deployed Proxmox node to the rest of the job:
#
#   1. mask the API token secret in the job's log;
#   2. mint one console ticket and take the node's FQDN from its "proxy" URL;
#   3. map that FQDN to the node's address in the runner's /etc/hosts, and
#      add both to no_proxy and NO_PROXY for the steps that follow;
#   4. write the action's outputs.
#
#   proxmox-publish-node.sh --workdir DIR
#
# DIR is proxmox-deploy.sh's --workdir, holding token, pve-root-ca.pem and
# facts.json. Run as a step of deploy-proxmox-on-shakenfist, which provides
# GITHUB_OUTPUT and GITHUB_ENV.
#
# WHY THE FQDN COMES FROM A TICKET. A .vv client dials the host in the
# ticket's proxy URL, by name, and pins the certificate against the ticket's
# host-subject. The playbook has already asserted that hostname -f, the node
# certificate and a ticket minted on the node with pvesh all name one FQDN.
# What it could not check is the runner's side: this mints through the API
# token over HTTPS, exactly as a consumer will, and maps the name that
# ticket advertises -- so if anything about minting remotely changed the
# answer, the runner is set up for what clients will really be told, and
# the mismatch with the facts is reported here rather than as a client's
# TLS error.
#
# THE SECRET. The ::add-mask:: below is the first thing in the job that
# reads the token file, and it runs before anything else can print it. From
# then on the runner replaces the secret with *** anywhere in the job's log,
# including in later steps of the consumer. The value is read with a bash
# builtin and echoed with one, so it is never on a command line, and xtrace
# is forced off in case it was inherited: under xtrace the echo line itself
# would print the secret before the runner saw the mask.

set +o xtrace
set -o errexit
set -o nounset
set -o pipefail

fail() {
    echo "::error title=Proxmox substrate::$*"
    echo "proxmox-publish-node.sh: $*" >&2
    exit 1
}

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mint_script="${here}/tools/proxmox-mint-vv.sh"

workdir=''
while [ "$#" -gt 0 ]; do
    case "$1" in
        --workdir)
            [ "$#" -ge 2 ] || fail "$1 needs a value"
            workdir="$2"
            shift 2
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done
[ -n "${workdir}" ] || fail '--workdir is required'
: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is not set; this runs as an action step}"
: "${GITHUB_ENV:?GITHUB_ENV is not set; this runs as an action step}"

token_file="${workdir}/token"
ca_file="${workdir}/pve-root-ca.pem"
facts_file="${workdir}/facts.json"

# 1. Mask. Whitespace is stripped because the runner matches the mask
# literally, and the value curl sends is stripped the same way.
[ -s "${token_file}" ] || fail "no token secret at ${token_file}"
secret="$(< "${token_file}")"
secret="${secret//[[:space:]]/}"
[ -n "${secret}" ] || fail "the token file ${token_file} holds only whitespace"
echo "::add-mask::${secret}"
unset secret

# 2. The deployment's facts. None of them is secret.
[ -s "${facts_file}" ] || fail "no deployment facts at ${facts_file}"
# Not "// empty": that also swallows false, and kvm is a boolean.
fact() {
    jq -r --arg k "$1" \
        'if .[$k] == null then empty else .[$k] | tostring end' "${facts_file}"
}
node_name="$(fact node_name)"
node_address="$(fact node_address)"
facts_fqdn="$(fact node_fqdn)"
token_id="$(fact token_id)"
vmid="$(fact vmid)"
kvm="$(fact kvm)"
pve_version="$(fact pve_version)"

for required in node_name node_address facts_fqdn token_id pve_version; do
    [ -n "${!required}" ] || fail "${facts_file} has no ${required/facts_fqdn/node_fqdn}"
done
# Each of these becomes a line of GITHUB_OUTPUT, so one with a newline in it
# could write an output of its own choosing.
for value in node_name node_address facts_fqdn token_id vmid kvm pve_version; do
    [[ "${!value}" != *$'\n'* ]] || fail "${facts_file} has a newline in ${value}"
done
# The action needs the smoke guest: it is what the ticket below is minted
# for, and what a consumer connects to.
[ -n "${vmid}" ] || fail "${facts_file} has no vmid; the playbook ran with the smoke guest disabled"
[[ "${node_address}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] \
    || fail "the node address in ${facts_file} is not an IPv4 address: ${node_address}"
[[ "${facts_fqdn}" =~ ^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$ ]] \
    || fail "the node FQDN in ${facts_file} is not a fully qualified name: ${facts_fqdn}"

# 3. Mint, reaching the API by the facts' name pinned to the node's address,
# because nothing on the runner resolves that name yet.
bootstrap_vv="${workdir}/bootstrap.vv"
"${mint_script}" \
    --api-url "https://${facts_fqdn}:8006" \
    --resolve "${node_address}" \
    --node "${node_name}" \
    --vmid "${vmid}" \
    --token-id "${token_id}" \
    --token-file "${token_file}" \
    --ca-file "${ca_file}" \
    --out "${bootstrap_vv}" > /dev/null \
    || fail 'could not mint a console ticket with the API token (the reason is above)'

# Only the proxy line is read, and only its host is kept: the rest of the
# file is a live credential.
proxy_url="$(sed -n 's/^proxy=//p' "${bootstrap_vv}" | head -n 1)"
rm -f "${bootstrap_vv}"
[[ "${proxy_url}" =~ ^http://([A-Za-z0-9.-]+):([0-9]+)$ ]] \
    || fail "the ticket's proxy URL is not http://<name>:<port>: ${proxy_url}"
fqdn="${BASH_REMATCH[1]}"

echo "The ticket advertises its proxy as ${proxy_url}"
[ "${fqdn}" = "${facts_fqdn}" ] \
    || fail "a ticket minted through the API names the node ${fqdn}, but the node calls itself ${facts_fqdn}"

# 4. The runner's /etc/hosts. The runner is a single-use VM, discarded after
# the job, so there is no teardown. A stale mapping for the same name would
# win or lose depending on its position, so check what actually resolves.
if ! grep -qxF "${node_address} ${fqdn}" /etc/hosts; then
    echo "${node_address} ${fqdn}" | sudo tee -a /etc/hosts > /dev/null
fi
resolved="$(getent ahostsv4 "${fqdn}" | awk 'NR == 1 {print $1}')"
[ "${resolved}" = "${node_address}" ] \
    || fail "${fqdn} resolves to '${resolved}' on the runner, not ${node_address}"
echo "Mapped ${fqdn} to ${node_address} in /etc/hosts"

# no_proxy and NO_PROXY, for every later step of the job. The runner image
# exports http_proxy and https_proxy for a squid that cannot route to the
# node's network. Both spellings are set to the same list, because tools
# disagree about which one they read and curl prefers the lower case one --
# so setting only one could hide entries the other held.
current="${no_proxy:-${NO_PROXY:-}}"
additions=''
for entry in "${fqdn}" "${node_address}"; do
    case ",${current}," in
        *",${entry},"*) ;;
        *) additions="${additions:+${additions},}${entry}" ;;
    esac
done
if [ -n "${additions}" ]; then
    updated="${current:+${current},}${additions}"
    {
        echo "no_proxy=${updated}"
        echo "NO_PROXY=${updated}"
    } >> "${GITHUB_ENV}"
    echo "Added ${additions} to no_proxy and NO_PROXY for the rest of the job"
fi

{
    echo "node_name=${node_name}"
    echo "node_address=${node_address}"
    echo "node_fqdn=${fqdn}"
    echo "api_url=https://${fqdn}:8006"
    echo "token_id=${token_id}"
    echo "token_file=${token_file}"
    echo "ca_file=${ca_file}"
    echo "vmid=${vmid}"
    echo "kvm=${kvm}"
    echo "pve_version=${pve_version}"
    echo "mint_script=${mint_script}"
} >> "${GITHUB_OUTPUT}"

echo "Proxmox VE ${pve_version} node ${node_name} (${fqdn}, ${node_address});" \
    "smoke guest ${vmid}, KVM: ${kvm:-unknown}"
