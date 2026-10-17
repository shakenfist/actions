#!/bin/bash

# Decide which commit a requested re-review checks out, and afterwards
# confirm that the checkout is that commit. Run by the pr-review-ref
# action, twice in one job: once before actions/checkout and once after.
#
#   pr-review-ref.sh resolve    needs GH_TOKEN, REPO and PR_NUMBER
#   pr-review-ref.sh confirm    needs HEAD_SHA and MERGED, and runs in
#                               the checkout
#
# Both write their results to GITHUB_OUTPUT.
#
# WHY RESOLVE RATHER THAN NAME THE MERGE REF. A named refs/pull/N/merge
# is right only when GitHub's copy is both present and current, and it
# can be neither.
#
# Present: the merge ref exists only while GitHub can compute a merge
# commit, so naming it can fail the checkout with a bare "couldn't find
# remote ref" after the requester has been acknowledged. A conflicted
# pull request is not reliably that case: measured against
# shakenfist/shakenfist on 2026-09-07, every open pull request had a
# merge ref, including the one GitHub reported as CONFLICTING. The ref
# survives a conflict; it is simply not guaranteed to.
#
# Current is the case which actually bites, and it is silent. GitHub
# recomputes the merge commit asynchronously after a push, so for a
# window afterwards the published ref still merges the *previous* head.
# Reviewing it then produces a real review of the wrong commit. Measured
# on the same day, shakenfist/shakenfist#4107 was serving a merge ref
# one push behind its head.
#
# So resolve confirms the merge commit's second parent is the head this
# review is for (a merge commit's parents are base then head), gives the
# recomputation a bounded moment to catch up, and only then falls back
# to the head ref. The fallback is a review of the right code seen
# without the base, which is worth having; a review of superseded code
# is not.
#
# WHY CONFIRM AS WELL. Resolve hands actions/checkout a ref *name*, and
# a name is not the sha it validated: between the two a push can move
# the head and GitHub can republish the ref. That is as true of
# refs/pull/N/head as of the merge ref, so confirm runs on both paths.
# The two races are not the same failure -- on the merge path the tree
# merges superseded code, on the head path it is a newer head that
# agrees with the diff the review fetches -- but confirm fails on both,
# because the property it holds is "the tree is what was validated",
# and one rule is easier to trust than two.

set -o errexit
set -o nounset
set -o pipefail

# Six attempts five seconds apart. The waits run on the scarce
# claude-code runner, which is why there are not more.
ATTEMPTS=6
DELAY=5

output() {
    echo "$1" >> "${GITHUB_OUTPUT}"
}

# A full 40-character sha, which is all GitHub serves. Lowercase hex
# alone is not enough: a truncated value never equals the full sha it is
# compared against, so it would surface later as a race that never
# happened rather than as the defect it is.
is_sha() {
    case "$1" in
        *[!0-9a-f]*)
            return 1
            ;;
    esac
    [ "${#1}" -eq 40 ]
}

resolve() {
    # The pull request number and repository go into API paths.
    case "${PR_NUMBER:-}" in
        '' | *[!0-9]*)
            echo "The pull request number is '${PR_NUMBER:-}', which is not a number."
            exit 1
            ;;
    esac
    if [ -z "${REPO:-}" ]; then
        echo "No repository was given to read the pull request from."
        exit 1
    fi

    # gh api rather than git ls-remote: this runs before the checkout,
    # and ls-remote against a private repository would need the token
    # in the URL, where it lands in the log.
    local head_sha
    if ! head_sha=$(gh api "repos/${REPO}/pulls/${PR_NUMBER}" --jq '.head.sha'); then
        echo "Cannot read the pull request, so there is no head to"
        echo "check a merge ref against. Failing rather than"
        echo "reviewing whatever a fallback would reach."
        exit 1
    fi

    # Everything below decides what to review by comparing this value
    # against a merge commit's parent, so it has to be a sha rather than
    # merely a string gh printed. --jq on an absent field yields the
    # literal "null", which would compare equal to the "null" a
    # single-parent commit yields for .parents[1].sha -- two absent
    # values agreeing is not a match.
    if ! is_sha "${head_sha}"; then
        echo "The pull request reports its head as '${head_sha}',"
        echo "which is not a sha. Failing rather than comparing"
        echo "it against a merge commit's parent."
        exit 1
    fi
    echo "Pull request head is ${head_sha}"
    output "head_sha=${head_sha}"

    # gh api writes the error body to *stdout* on a 404 and exits
    # non-zero, so the exit status is the only thing which says whether
    # a ref is there. Testing the output for emptiness would read the
    # "Not Found" document as a sha, and log "merge ref is behind" for a
    # ref which does not exist.
    #
    # reason carries which of the three ways this can fail was the last
    # one seen, so the notice posted on the pull request can say what
    # actually happened rather than guessing at the most quotable cause.
    # It is only read when merged is false.
    local merged=false reason=absent attempt merge_sha parent
    for attempt in $(seq 1 "${ATTEMPTS}"); do
        if ! merge_sha=$(gh api "repos/${REPO}/git/ref/pull/${PR_NUMBER}/merge" \
                --jq '.object.sha' 2>/dev/null); then
            reason=absent
            echo "Attempt ${attempt}: no merge ref published"
        elif ! is_sha "${merge_sha}"; then
            # --jq prints "null" for an absent field and exits 0, and
            # reading commits/null would log a 404 for a commit nobody
            # named.
            reason=unreadable
            echo "Attempt ${attempt}: merge ref names '${merge_sha}', not a commit"
        elif ! parent=$(gh api "repos/${REPO}/commits/${merge_sha}" \
                --jq '.parents[1].sha' 2>/dev/null); then
            reason=unreadable
            echo "Attempt ${attempt}: cannot read merge commit ${merge_sha}"
        elif ! is_sha "${parent}"; then
            # Not stale: a stale merge commit merges an earlier head,
            # and this one merges nothing.
            reason=unreadable
            echo "Attempt ${attempt}: merge ref names ${merge_sha}, which is not a merge"
        elif [ "${parent}" = "${head_sha}" ]; then
            merged=true
            break
        else
            reason=stale
            echo "Attempt ${attempt}: merge ref merges ${parent}, not the head"
        fi
        # Only worth waiting if there is another attempt to wait for.
        [ "${attempt}" = "${ATTEMPTS}" ] || sleep "${DELAY}"
    done

    if [ "${merged}" = "true" ]; then
        output "ref=refs/pull/${PR_NUMBER}/merge"
        output "merged=true"
        echo "Reviewing the merge of ${head_sha} into the base"
    else
        output "ref=refs/pull/${PR_NUMBER}/head"
        output "merged=false"
        output "reason=${reason}"
        echo "Falling back to the pull request head ${head_sha}"
    fi
}

confirm() {
    local reviewed what
    case "${MERGED:-}" in
        true)
            # Explicit rather than left to errexit, which would abort on
            # a non-merge HEAD with git's own message and nothing about
            # why this job cared.
            if ! reviewed=$(git rev-parse --verify --quiet 'HEAD^2'); then
                echo "The tree checked out is not a merge commit, so it is"
                echo "not the merge of ${HEAD_SHA:-} the resolve step validated."
                exit 1
            fi
            what="merge commit checked out merges"
            ;;
        false)
            if ! reviewed=$(git rev-parse --verify --quiet HEAD); then
                echo "The tree checked out has no HEAD, so it is not"
                echo "the ${HEAD_SHA:-} the resolve step validated."
                exit 1
            fi
            what="head checked out is"
            ;;
        *)
            echo "The resolve step reported merged as '${MERGED:-}', so"
            echo "there is no way to say what this checkout should be."
            exit 1
            ;;
    esac
    # A HEAD_SHA which is not a sha -- empty from a misspelt output,
    # say -- is a defect in the caller, not a race, so it must not reach
    # the comparison below and be reported as one.
    if ! is_sha "${HEAD_SHA:-}"; then
        echo "The validated head sha given is '${HEAD_SHA:-}', which is"
        echo "not a sha, so there is nothing to confirm the checkout"
        echo "against."
        exit 1
    fi
    if [ "${reviewed}" != "${HEAD_SHA}" ]; then
        # Only this branch is a race. The exits above are defects in the
        # workflow, and the requester's notice must not tell them to
        # comment again for those, so it reads cause rather than this
        # step's outcome.
        output "cause=moved"
        echo "The ${what} ${reviewed}, but the resolve step validated"
        echo "${HEAD_SHA}. The head moved between the two, so this tree"
        echo "is not the code the review was asked for. Comment again"
        echo "to review the new head."
        exit 1
    fi
    echo "Checkout confirmed as ${HEAD_SHA} (merged=${MERGED})"
}

if [ -z "${GITHUB_OUTPUT:-}" ]; then
    echo "GITHUB_OUTPUT is not set, so there is nowhere to write the result."
    exit 1
fi

case "${1:-}" in
    resolve)
        resolve
        ;;
    confirm)
        confirm
        ;;
    *)
        echo "usage: $0 resolve|confirm (got '${1:-}')"
        exit 2
        ;;
esac
