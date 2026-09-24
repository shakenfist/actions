#!/bin/bash

# File or update a tracking issue for a failed proxmox-substrate.yml run.
#
# Only the weekly schedule calls this: a pull request or a manual dispatch
# failure is the author's problem and shows up as a red check on their own
# run, but a scheduled failure has no author to notify, and a broken
# deploy-proxmox-on-shakenfist would otherwise first be noticed as a red
# lane in a client repository (ryll's proxmox-functional.yml, for one).
# Modelled on canary.yml's report-failure job: one issue per
# outage, not one per run, so a lane that stays red for a week does not
# file seven issues.
#
# Expects GH_TOKEN, RUN_URL, SHA and GITHUB_REPOSITORY in the environment.

set -o errexit
set -o nounset
set -o pipefail

LABEL='proxmox-substrate'

# gh issue create fails outright if the label does not exist, so the
# report would be lost at exactly the moment it is wanted. --force makes
# this idempotent.
gh label create "${LABEL}" \
    --repo "${GITHUB_REPOSITORY}" \
    --description 'Weekly proxmox-substrate.yml drift-detection failure' \
    --color d73a4a --force

existing=$(gh issue list --repo "${GITHUB_REPOSITORY}" \
    --label "${LABEL}" --state open --limit 1 --json number \
    --jq '.[0].number // empty')

# printf rather than a multi-line double-quoted string: a quoted string
# here would carry this file's indentation into every continuation line,
# and leading spaces make GitHub-flavoured Markdown render the whole body
# as a code block, which turns the run URL from a link into plain text.
body=$(printf '%s\n' \
    "The weekly proxmox-substrate.yml run failed for ${SHA}." \
    "" \
    "Run: ${RUN_URL}" \
    "" \
    "This lane exists to catch upstream Proxmox VE drift -- a rotated" \
    "keyring checksum, a moved pve-no-subscription package, a changed" \
    "API shape -- before it first shows up as a red lane in a client" \
    "repository's own proxmox lane. Diagnose from the run log; a" \
    "failure here is usually a substrate problem, not a client one." \
    "" \
    "One thing to rule out first: proxmox-deploy.sh clones" \
    "shakenfist/shakenfist unpinned, deliberately, so this lane also" \
    "runs against whatever shakenfist's default branch holds at mint" \
    "time. Check the run log's \"shakenfist checkout: <sha>\" line" \
    "before assuming Proxmox drifted.")

if [ -n "${existing}" ]; then
    echo "Commenting on existing proxmox-substrate issue #${existing}."
    gh issue comment "${existing}" --repo "${GITHUB_REPOSITORY}" \
        --body "${body}"
else
    echo "Filing a new proxmox-substrate issue."
    gh issue create --repo "${GITHUB_REPOSITORY}" \
        --title 'Weekly proxmox-substrate.yml failing' \
        --label "${LABEL}" \
        --body "${body}"
fi
