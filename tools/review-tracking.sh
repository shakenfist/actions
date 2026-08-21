#!/bin/bash

# Run the shared code review tracking helper by hand. Subcommands:
#
#   stamp   record blob SHAs for newly reviewed files, regen REVIEWS.md
#   prune   drop review marks for files changed since review, regen
#   regen   regenerate REVIEWS.md from current state
#   next    pick a random unreviewed in-scope file and open it
#   status  report effective review coverage against HEAD (read-only)
#
# These used to run automatically from git hooks (pre-commit,
# post-merge, post-checkout, post-rewrite), which made them fire
# confusingly in the middle of other git operations; in a clone they
# only run when invoked explicitly. Typical session: "prune" after a
# pull, "stamp" before committing review marks. On main itself the
# prune-reviews workflow runs prune automatically after every push
# (via tools/ci-prune-reviews.sh). The implementation lives in the
# shakenfist/development repository; see
# https://github.com/shakenfist/development/blob/main/docs/code-review-tracking.md

set -e

repo_root="$(git rev-parse --show-toplevel)"

candidates=(
    "${SHAKENFIST_DEVELOPMENT:-}"
    "${repo_root}/../development"
    "${HOME}/src/shakenfist/development"
)

# Discovery tests for the file, not for the executable bit. A clone
# sitting exactly where expected but with the bit dropped -- a zip
# export, a copy across filesystems, a restrictive umask -- would
# otherwise fall through to "cannot find a clone", sending somebody
# who has already done both of the things that message asks for
# looking in the wrong place entirely.
searched=()
for candidate in "${candidates[@]}"; do
    if [ -z "${candidate}" ]; then
        continue
    fi
    script="${candidate}/scripts/review-tracking.py"
    searched+=("${script}")
    if [ -f "${script}" ]; then
        if [ ! -x "${script}" ]; then
            echo "Found ${script} but it is not executable." >&2
            echo "Run: chmod +x ${script}" >&2
            exit 1
        fi
        cd "${repo_root}"
        exec "${script}" "$@"
    fi
done

echo 'Cannot find a shakenfist/development clone providing' >&2
echo 'scripts/review-tracking.py. Clone it next to this repository or' >&2
echo 'set SHAKENFIST_DEVELOPMENT to the path of an existing clone.' >&2
echo 'Searched:' >&2
for path in "${searched[@]}"; do
    echo "  ${path}" >&2
done
exit 1
