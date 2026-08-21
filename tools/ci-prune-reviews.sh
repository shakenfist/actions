#!/bin/bash -e

# Prune review marks made stale by pushes to main and commit the
# regenerated review state back. Run by the prune-reviews workflow;
# mirrors the development repository's scripts/commit-audit-docs.sh
# landing pattern. Prune only ever removes marks, so this unsigned
# bot commit does not weaken the review attestations -- those live in
# the signed commits that introduced the stamps. See the steady state
# section of
# https://github.com/shakenfist/development/blob/main/docs/code-review-tracking.md

./tools/review-tracking.sh prune

# git status --porcelain rather than git diff --quiet: the latter
# compares the working tree against the index for tracked paths only,
# so a file the helper newly creates under .vscode/ would be invisible
# to it and silently discarded when the workspace is next cleaned.
# Prune as it behaves today only removes marks and rewrites existing
# files, so this is not reachable now -- but this script cannot see
# that helper's implementation, and the failure would be silent.
if [ -z "$(git status --porcelain -- .vscode/ REVIEWS.md)" ]; then
    echo "No stale review marks to prune."
    exit 0
fi

git config user.name 'shakenfist-bot'
git config user.email 'bot@shakenfist.com'

git add .vscode/ REVIEWS.md
git commit -m 'Prune stale review marks.

Automated commit by the prune-reviews workflow.'

# Another push may have landed while we ran; rebase our commit on top
# rather than failing the workflow. The concurrency group serialises
# prune runs against each other but not against human merges, so a
# merge landing between the rebase and the push still gets a
# non-fast-forward rejection. The retry is what actually closes that
# window: without it the run goes red for a reason unrelated to
# correctness, which is how a workflow trains people to stop reading
# it.
for attempt in 1 2 3; do
    if git pull --rebase origin main && git push origin main; then
        exit 0
    fi
    # A conflicting merge leaves a rebase in progress, and the next
    # attempt would fail on that rather than on the race we are
    # retrying for.
    git rebase --abort 2> /dev/null || true
    echo "Landing attempt ${attempt} was rejected; retrying."
    sleep 5
done

echo "Could not land the pruned review state after three attempts." >&2
exit 1
