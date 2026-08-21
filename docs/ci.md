# CI for this repository

This repository ships the composite actions and reusable workflows the
rest of the Shaken Fist fleet runs its CI from. Until recently it had no
CI of its own: `export-repo-config.yml`, `pr-auto-review.yml` and
`smoke-cluster.yml` are all `workflow_call`-only, invoked by downstream
projects, and nothing ran on a pull request opened here.

That was not an oversight so much as an unresolved problem, and the
problem is worth stating before the solution.

## Why this repository is hard to test

Every consumer pins `@main`:

```yaml
uses: shakenfist/actions/.github/workflows/smoke-cluster.yml@main
uses: shakenfist/actions/build-smoke-cluster@main
uses: shakenfist/actions/review-pr-with-claude@main
```

So does this repository internally -- `smoke-cluster.yml` names
`setup-test-environment@main` and `build-smoke-cluster@main`. Two
consequences follow.

**A merge here is a deploy.** There is no staging step. The moment a
change lands on `main`, every downstream CI run resolves it.

**The obvious pre-merge test does not work.** Running shakenfist's
functional tests against a branch of this repository does not test that
branch's composite actions: the reusable workflow still pulls
`build-smoke-cluster@main`, the version already on main. The change
under review is not exercised.

This cannot be fixed by parameterising the ref. GitHub does not allow
expressions in `uses:`, so an `actions_ref` input pointing at a pull
request's ref is not expressible. Relative refs (`uses: ./...`) do work,
but only within this repository -- inside a reusable workflow called
from another repository they resolve against the *caller's* checkout, so
switching to them would break every consumer.

## What CI does about it

Three lanes, covering what is checkable without a cluster plus a
post-merge check on what is not.

### Pull request lane -- `ci.yml`

| Job | What it covers |
|-----|----------------|
| Check paths | Decides whether this pull request touches anything but documentation and review marks. Runs on a static runner; the three jobs below are each an ephemeral VM |
| Lint | `pre-commit run --all-files`: actionlint over the reusable workflows, shellcheck over `tools/`, flake8 over the Python, plus hygiene hooks |
| Unit tests | `python3 -m unittest discover -s tests -t .` |
| gitleaks | `tools/gitleaks-scan.sh` over all history reachable from `HEAD` |
| Automated reviewer | The shared Claude Code review, gated on lint, unit tests and gitleaks |

The reviewer is called by relative path rather than `@main`, so a pull
request which changes the reviewer workflow is reviewed by its own
version. That only reaches one level: the workflow's inner reference to
`review-pr-with-claude` is an action reference, so it stays `@main`.

**Only lint and unit tests consume the path filter.** `gitleaks`
deliberately does not, and that is the important half of the design. A
scanner exists to read the human-written text a filter skips;
documentation and review notes are prose, and prose is where a secret
or an invisible-unicode smuggle lands. `automated_reviewer` needs lint
and unit tests, so it skips when they do without a condition of its
own -- LLM credits are the other thing not worth spending on a typo.

Two consequences of that filter are worth stating rather than
discovering:

* **The filter fails open.** By default a job whose dependency did not
  succeed is skipped, and a skipped job satisfies branch protection --
  so a broken `check_paths` would read as "the required checks are
  green" on a repository where a merge to `main` deploys to ten others.
  The `!cancelled()` term in each lane's condition overrides that: only
  a filter that positively answered "no code changed" skips a lane.
* **A documentation-only pull request skips the hygiene hooks too.**
  `check-merge-conflict`, `trailing-whitespace` and `end-of-file-fixer`
  ride in the lint job, and their subject matter is precisely markdown,
  so a stray conflict marker in `docs/` would now merge unremarked.
  That is an accepted trade: running them on the static runner instead
  would mean executing the branch's pre-commit configuration there,
  which is the one thing `check_paths` is careful not to do.

**Fork pull requests do not run any of the checking lanes.** Each of
the three executes the pull request's own code -- pre-commit clones and
runs whatever repositories `.pre-commit-config.yaml` names, the unit
tests import the branch's test files, and `gitleaks-scan.sh` is the
branch's own shell script -- on a self-hosted runner that holds
`/srv/github/id_ci`, the key `smoke-cluster.yml` and `tools/run_remote`
use to reach every node in the CI mesh. Code execution in any of those
jobs therefore reaches the whole cluster estate. `pr-auto-review.yml`
has carried the same condition since it was written, for the same
reason; the three jobs in `ci.yml` now carry it too. GitHub's default
approval prompt is not a substitute: for a public repository it only
covers *first-time* contributors, so one merged trivial pull request
clears it thereafter.

`check_paths` is the exception, and carries no fork guard. It has no
checkout step: on a `pull_request` event `dorny/paths-filter` reads the
changed-file list from the `pulls.listFiles` API and never looks at the
working tree, so a fork's file content never reaches the static runner
at all. That API call is why the job grants itself
`pull-requests: read` -- specifying `permissions` sets every unlisted
scope to none, and this repository being public is not a reason to rely
on the call succeeding without it.

A skipped job still satisfies branch protection, so this does not wedge
a required check -- but it does mean a fork pull request arrives
unchecked. Review it by pushing the branch to this repository, where the
lane runs in full.

One operational note on the gitleaks job: it is the only thing in the
fleet asking for a `debian-13` runner, because gitleaks is not packaged
before trixie. That runner class exists and the job runs green on it,
but it is scarce -- the first run of this workflow sat queued for about
ninety minutes before starting, then finished in thirteen seconds. A job
with no matching runner does not fail, it queues, and `timeout-minutes`
does not cover queue time. So a gitleaks check that has been pending for
a long while is waiting for a runner rather than broken, and if this
ever becomes a required check that distinction matters.

### Bot-triggered lane

Three `issue_comment` workflows let a collaborator with write access
re-run something from a pull request comment, which is the only way to
get a second look without pushing a commit:

| Comment | Workflow | What it does |
|---------|----------|--------------|
| `@shakenfist-bot please retest` | `pr-retest.yml` | Dispatches `ci.yml` against the pull request branch |
| `@shakenfist-bot please re-review` | `pr-re-review.yml` | Runs the reviewer again, with `force` set |
| `@shakenfist-bot please address comments` | `pr-address-comments.yml` | Has Claude Code work through the review's `fix` and `document` items and pushes a commit per item |

All three match their phrase with `contains()` on the whole comment
body, so writing one of them inside a sentence about it -- or inside a
quote -- fires it. The two that push commits are the ones to be careful
of. They ignore comments posted by a bot, so the summaries
`pr-address-comments.yml` quotes back onto a pull request cannot
re-trigger the lane.

**Fork pull requests are refused**, by `pr-bot-trigger` rather than by
each workflow. Its `pr-ref` output is `.head.ref`, a branch name in the
*head* repository with nothing to say which repository that is; callers
check that name out and push to it here. A fork pull request opened from
the fork's default branch names `main`, so the checkout would succeed
against this repository's `main` and the push would land bot commits on
the branch the whole fleet pins. Putting the refusal in the action means
every repository consuming it at `@main` gets the guard without editing
anything.

The re-review one matters more than it looks. `review-pr-with-claude`
skips a pull request the bot has already reviewed unless `force` is set,
and the automatic review in `ci.yml` deliberately does not set it. So
without `pr-re-review.yml` a pull request is reviewed exactly once in its
life, normally on the first push, and every round of fixes after that
lands unlooked-at. This repository ran that way until these workflows
landed: pull requests #20 and #21 both merged with their review fixes
unreviewed.

Two deviations from the shared template in
`shakenfist/development/templates/ci-review-automation/`, both recorded
in the headers of the files themselves:

* `pr-retest.yml` dispatches `ci.yml` rather than `functional-tests.yml`,
  which does not exist here and cannot -- see above.
* `pr-address-comments.yml` points `TOOLS_DIR` at `review-pr-with-claude/`
  rather than copying `render-review.py` into `tools/`. This repository is
  where that script comes from, and it needs `review-schema.json` beside
  it. `SCHEMA_PATH` is `Path(__file__).parent / 'review-schema.json'`, and
  when that file is absent `load_schema()` returns `None` and
  `validate_review()` returns success **without checking anything at
  all** -- not weakened validation, none. (The structural fallback in
  that function is a different branch, taken only when `jsonschema` is
  not importable, and it runs whether or not the schema file is there. On
  a runner with `jsonschema` installed, which is the normal case, a
  missing schema means every review validates.) Keeping the canonical
  pair together avoids both the fork and the trap.

One convention is knowingly not met. AGENTS.md says not to write more
than about five lines of shell inline in a workflow step -- put it in a
script under `tools/` so it can be run and tested outside CI. These
three files carry several blocks well past that: the address invocation,
the push guard, the log scraping and the two comment steps. They are the
fleet's shared templates, and every line this repository rewrites is a
line that stops matching the nine other repositories running the same
workflows, which costs more than it saves while the templates are still
the source of truth. Recorded here rather than left as a silent conflict
between the convention and the files. If the log scraping or the push
guard grows any further, lift it into `tools/` and accept the
divergence.

### Post-merge lane -- `canary.yml`

`canary.yml` calls `smoke-cluster.yml` by relative path on every push to
`main`. Post-merge, every piece involved -- the reusable workflow, the
composite actions it names at `@main`, the scripts under `tools/` -- is
the just-merged code, deployed against the development branches of
`shakenfist`, `client-python` and `agent-python`. That is exactly the
combination the next downstream CI run will get.

This does not prevent a bad merge. It bounds how long one goes
unnoticed, from "until somebody opens a pull request downstream" to
"about as long as a smoke cluster takes". On failure it files or updates
a `canary`-labelled issue, because a broken `actions@main` is the whole
fleet's problem rather than the author's.

Documentation-only pushes are skipped via `paths-ignore`, and so are
pushes that only record a review: a merged review stamp writes
generated files under `.vscode/` which cannot change what any action
does. Those are the same paths `ci.yml`'s filter excludes, and
`tests/test_workflow_references.py` checks that the two lists stay in
step. Content scanners get no such exemption anywhere -- see the
`gitleaks` note above.

### Post-merge lane -- `prune-reviews.yml`

A review mark attests to exact file content, so any push to `main` can
make some of them stale. This workflow runs `tools/ci-prune-reviews.sh`
on every push to `main`, which drops the marks for files that have
changed, regenerates `REVIEWS.md`, and commits the result straight back
as `shakenfist-bot`.

That commit is unsigned and does not need to be: pruning only ever
*removes* marks, and the attestations themselves live in the signed
commits that recorded them. The push uses the built-in `GITHUB_TOKEN`
rather than a personal access token, which works here because this
repository's ruleset blocks force-pushes and deletion only. (Ryll's
copy of the same workflow needs `DEPENDENCIES_TOKEN`, because its
`develop` ruleset requires a pull request and the Actions app cannot be
a bypass actor.) A push made with `GITHUB_TOKEN` does not trigger
workflows, so the bot's commits fire neither `canary.yml` nor this
workflow again.

The `workflow_dispatch` trigger carries an `if: github.ref ==
'refs/heads/main'` guard. The script pushes to `main` whatever ref is
checked out, so dispatching it on a feature branch would push that
branch's unmerged commits to `main`, skipping review entirely.

### What is still not covered

Composite action changes are integration-tested only *after* they land.
Closing that gap needs either a self-test workflow which duplicates
`smoke-cluster.yml` using local `./` refs -- and can then drift from the
thing it is standing in for -- or an end to `@main` pinning across the
fleet. Neither is obviously worth it yet; the canary is the cheap 80%.

**The composite `action.yml` files are not linted.** actionlint checks
workflow files; the pre-commit hook restricts itself to
`.github/workflows/`, and actionlint invoked with no arguments globs the
same directory. So `pr-bot-trigger/`, `review-pr-with-claude/`,
`setup-test-environment/`, `build-smoke-cluster/`,
`deploy-kolla-ansible/`, `setup-kerbside-environment/` and
`deploy-kerbside-on-shakenfist/` are checked by nothing here. That is
the same set of files the section above says cannot be
integration-tested pre-merge either, so the repository's primary product
has both its weakest test story and its weakest lint story. It goes on
the backlog beside yamllint and ansible-lint.

Widening the hook's `files:` pattern is not the fix, though it looks
like one. actionlint has no notion of an action file: pointed at one
explicitly it parses it as a workflow and reports the differences as
errors --

```
review-pr-with-claude/action.yml:1:1: "jobs" section is missing in workflow
review-pr-with-claude/action.yml:1:1: "on" section is missing in workflow
review-pr-with-claude/action.yml:22:1: unexpected key "runs" for "workflow" section
```

-- so broadening the pattern fails the lint job on every composite
action at once rather than producing findings to work through. Covering
these files needs a different tool, not a wider glob.

**`tools/gitleaks-scan.sh`'s own argument handling and shallow-clone
guard are untested.** The positive control checks the scanner, not the
script wrapping it.

## Running the checks locally

```bash
pip install pre-commit
pre-commit install          # optional, to run on every commit
pre-commit run --all-files
```

The unit tests run standalone too, and need PyYAML:

```bash
sudo apt-get install -y python3-yaml
python3 -m unittest discover -s tests -t . --verbose
```

PyYAML is the suite's only dependency, and it is a hard requirement
rather than an optional one on purpose. The inventory test that parses
the generated YAML and checks its group structure used to skip itself
when the import failed; every other assertion in that file is substring
matching against hand-rendered text and would pass on malformed output.
A skip is not a failure, so the one test that validates the output could
have stopped running without anyone noticing.

The secret scan needs `gitleaks` (packaged from Debian 13 onward) and a
full clone, not a shallow one. Debian 13 ships gitleaks **8.16.0**,
which predates the 8.19 split into `gitleaks git` and `gitleaks dir`, so
the script drives the older `gitleaks detect` -- the newer subcommands
do not exist in the packaged build and switching to them would break the
scan outright. The Debian build also does not stamp a version (`gitleaks
version` prints "version is set by build process"), so pinning the apt
install to a known version is not practical either. If gitleaks is ever
fetched from upstream releases instead, revisit both.

```bash
tools/gitleaks-scan.sh
tools/gitleaks-scan.sh --gitleaks /path/to/gitleaks   # if not on PATH
```

It plants two credentials in a scratch directory and fails if the
scanner does not report both, before scanning for real. A detector that
reports nothing is otherwise indistinguishable from a broken one -- that
control caught a malformed `.gitleaks.toml` the first time it ran.

## Linters deliberately not enabled yet

Two are absent, and are tracked here rather than silently dropped:

| Linter | Findings on the tree today | Why not yet |
|--------|---------------------------|-------------|
| yamllint | 191 problems, even at a 120 column limit | Mostly `truthy` on ansible's idiomatic `yes`/`no` values |
| ansible-lint | 732 failures, 365 warnings | 334 are `fqcn`, asking for `ansible.builtin.` prefixes on every task |

Gating on either today would mean a large mechanical rewrite of the CI
playbooks, or a configuration that disables so much it asserts nothing.

shellcheck is enabled but gated at `error` severity rather than the
default. The tree carries 31 warning-level findings (`SC2046` unquoted
command substitution, `SC2034` unused variables, `SC2164` unchecked
`cd`) across seventeen scripts which run on remote CI nodes and cannot
be exercised locally. Raise the severity in `.pre-commit-config.yaml`
once those have been worked through.

## What the unit tests cover

The tests live in `tests/` and are plain `unittest`, loaded by path
because the scripts they cover are named with hyphens and there is no
package here to import. This repository is deliberately not a Python
project -- growing a `pyproject.toml` just to run tests would make it
one.

| Script | Why it is tested |
|--------|------------------|
| `tools/ci-make-inventory.py` | Writes the ansible inventory every CI cluster deploy is driven from. A mistake produces a valid inventory with a node in the wrong group, and the deploy then fails much further along |
| `review-pr-with-claude/render-review.py` | Renders the review comment posted on every fleet pull request, and the embedded JSON block that `@shakenfist-bot please address comments` reads back out |
| `review-pr-with-claude/create-review-issues.py` | Decides the labels every automated-review issue is triaged by, and builds the only context those issues carry once the pull request is gone |
| `tools/run_remote` | Its local branches word-split the command; quoting them silently kills the single-node path, which no CI run exercises |

The workflows get one test file too, `tests/test_workflow_references.py`.
It checks that everything a workflow names actually exists: dispatch
targets, relative `uses:` references, the scripts under `tools/` that
`run:` steps invoke (including their executable bit, since they are run
directly rather than through an interpreter), the agreement between
`pr-address-comments.yml`'s sparse checkout and the directories it then
reads tools from, and the agreement between the review-tracking
exclusions in `ci.yml`, `canary.yml` and `.pre-commit-config.yaml`.
Those are all cross-file references nothing else validates -- actionlint
checks a workflow's syntax, not whether the file it dispatches is there
-- and each one fails only when somebody is depending on it.

`tools/clone_with_depends.py` is not covered: it needs a real
`GitPython` repository and CI environment variables, and its
`Depends on` parsing is the only pure part. It is also no longer called
from the deploy path.

## Whole-file human review

Separately from pull request review, the files in this repository are
worked through one at a time and reviewed whole, looking for the
inconsistencies that accumulate where no single change is wrong.
`REVIEWS.md` reports the current coverage and `.vscode/review-scope.toml`
decides what is in scope -- close to everything here, because this
repository's product *is* the YAML and the shell, and there is no
release to stage a mistake behind. The scope file records why each
exclusion is an exclusion.

`REVIEWS.md` and the files under `.vscode/` are generated. Do not edit
them by hand; run the tooling instead:

```bash
tools/review-tracking.sh status   # coverage against HEAD, read only
tools/review-tracking.sh next     # open a random unreviewed file
tools/review-tracking.sh stamp    # record the files just reviewed
tools/review-tracking.sh prune    # drop marks made stale by a pull
```

In practice a contributor runs two of those: `prune` after a pull, and
`stamp` before committing review marks. The implementation is not in
this repository -- the wrapper execs
`scripts/review-tracking.py` from a `shakenfist/development` clone,
found next to this one, at `~/src/shakenfist/development`, or wherever
`SHAKENFIST_DEVELOPMENT` points. On `main` nobody runs `prune` by hand:
`prune-reviews.yml` does it after every push, as described above.

[shakenfist/development's `docs/code-review-tracking.md`](https://github.com/shakenfist/development/blob/main/docs/code-review-tracking.md)
is the canonical description, including how to verify the attestations.
