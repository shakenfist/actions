#!/bin/bash

# Shared pieces for the scripts that call a Proxmox VE API with an API
# token: proxmox-mint-vv.sh (POST .../spiceproxy) and
# proxmox-audit-probe.sh (GET .../status/current). Sourced, not executed.
#
# These two call the same API, on the same node, with the same credential,
# and every value they pass lands in a URL path, an HTTP header or curl's
# --resolve. The guards below are the only thing standing between a hostile
# value and one of those places, and tests/test_proxmox_shell_validation.py
# exists because the substrate lane only ever passes good values and so
# would never notice a guard that had stopped rejecting. One copy, used by
# both, is one copy to keep tested: a second copy is a guard that can be
# fixed in one script and left broken in the other.
#
# A caller must define fail() before sourcing this -- the functions here
# report through it, so each script's message keeps its own prefix -- and
# must not enable xtrace. Not for this file's sake: the secret reaches tr
# on a redirect, so xtrace prints "tr -d '[:space:]'" and no token. It is
# the caller that holds values xtrace would print. See the SECRETS note in
# proxmox-mint-vv.sh.
#
# This file must travel with any script that sources it: the callers find
# it beside themselves, so a script copied out of tools/ on its own will
# not run, and one copied to a directory others can write to would source
# whatever they put there.

# The guards' bracket ranges ([A-Za-z0-9.-], [0-9A-Fa-f:.]) and the
# [:print:] class below are read in the shell's locale, where a range is
# collation-ordered and can match more than ASCII. Pin it here, at source
# time, so that every caller of the guards gets the C locale whether or not
# it remembered to export it itself.
export LC_ALL=C

# Validate everything that identifies the API target, in the caller's
# variables: node, vmid, token_id, api_url, ca_file, token_file and the
# optional resolve. On return api_url has any trailing slash removed, and
# api_host and api_port hold its host and port for curl --noproxy and
# --resolve.
#
# shellcheck disable=SC2154  # node, vmid, token_id, api_url, ca_file,
# token_file and resolve are the caller's variables by contract, as the
# comment above says. Taking them as seven positional arguments instead
# would make a transposed pair of values silent, and this function would
# still have to hand api_url, api_host and api_port back out.
pve_validate_api_target() {
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
}

# Write an Authorization header file for curl --header @file.
#
# printf is a builtin and tr reads the secret on stdin, so the secret is on
# no command line. Whitespace is stripped because the file an editor or an
# "echo >" wrote ends in a newline, and a newline would end the header.
#
# The mode is set here rather than left to the caller's umask: both callers
# do set umask 077 first, but this function is the shared piece, and a
# third caller that forgot would write a credential a group could read.
pve_write_auth_header() {
    local token_id="$1" token_file="$2" path="$3"
    : > "${path}"
    chmod 0600 "${path}"
    {
        printf 'Authorization: PVEAPIToken=%s=' "${token_id}"
        tr -d '[:space:]' < "${token_file}"
        printf '\n'
    } > "${path}"
}

# Fill the pve_curl_args array with the arguments common to every call:
# quiet but not silent about errors, the node's CA, the token header, the
# body and headers to files, and the status code on stdout. The API host is
# never sent through an HTTP proxy, whatever http_proxy, https_proxy and
# no_proxy say: a Proxmox node deployed by this repository sits on a network
# the fleet's squid cannot route to, and the runner image exports
# https_proxy. The caller appends its own method, and the URL.
#
# Takes the header, response and headers file paths; reads api_host,
# api_port, ca_file and resolve from the caller.
pve_common_curl_args() {
    local header="$1" response="$2" headers="$3"
    pve_curl_args=(
        --silent --show-error
        --max-time 20
        --noproxy "${api_host}"
        --cacert "${ca_file}"
        --header "@${header}"
        --output "${response}"
        --dump-header "${headers}"
        --write-out '%{http_code}'
    )
    if [ -n "${resolve}" ]; then
        pve_curl_args+=(--resolve "${api_host}:${api_port}:${resolve}")
    fi
}

# Describe a non-200 answer, as "HTTP 403 Permission check failed ...".
#
# PVE puts the reason in the status line's reason phrase -- "401
# authentication failure", "403 Permission check failed (/vms/100,
# VM.Console)" -- and sometimes a "message" in the body. Neither carries a
# credential (an error answer holds no ticket, and PVE does not echo the
# Authorization header), so both are worth showing. Both are truncated: the
# body is whatever the far end chose to send.
#
# Both also go through pve_one_log_line, because this description is
# printed into a job log where a line beginning with "::" is a command to
# the runner. jq -r turns a "message" of "x\n::error ..." into real
# newlines, and the far end chooses that string.
pve_describe_refusal() {
    local status="$1" headers="$2" response="$3" reason message
    reason="$(head -n 1 "${headers}" 2>/dev/null | tr -d '\r' | cut -d ' ' -f 3- | head -c 300 || true)"
    reason="$(pve_one_log_line "${reason}")"
    message="$(jq -r '.message // empty' "${response}" 2>/dev/null | head -c 300 || true)"
    message="$(pve_one_log_line "${message}")"
    printf 'HTTP %s%s%s' "${status}" "${reason:+ ${reason}}" "${message:+ (${message})}"
}

# Reduce a value the far end chose to something that can only ever be part
# of one log line: no newline to start a line of its own, and no control
# byte to rewrite what is already there.
#
# A GitHub Actions runner reads a line beginning with "::" as a workflow
# command, so a newline in a value printed to stdout or stderr is the
# output-side half of the injection the input guards above prevent -- it
# can forge an ::error, mask output with ::add-mask::, or print a
# convincing "Self-check passed".
#
# The result can still begin with "::" itself, so it is safe only after a
# prefix on its line -- "proxmox-audit-probe.sh: ...", "HTTP 500 ..." --
# never as the first thing a caller prints.
pve_one_log_line() {
    printf '%s' "$1" | tr '\n\r\t' '   ' | tr -cd '[:print:]'
}
