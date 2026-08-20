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
| Lint | `pre-commit run --all-files`: actionlint over the workflows and actions, shellcheck over `tools/`, flake8 over the Python, plus hygiene hooks |
| Unit tests | `python3 -m unittest discover -s tests -t .` |
| gitleaks | `tools/gitleaks-scan.sh` over all history reachable from `HEAD` |
| Automated reviewer | The shared Claude Code review, gated on the three above |

The reviewer is called by relative path rather than `@main`, so a pull
request which changes the reviewer workflow is reviewed by its own
version. That only reaches one level: the workflow's inner reference to
`review-pr-with-claude` is an action reference, so it stays `@main`.

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

Documentation-only pushes are skipped via `paths-ignore`.

### What is still not covered

Composite action changes are integration-tested only *after* they land.
Closing that gap needs either a self-test workflow which duplicates
`smoke-cluster.yml` using local `./` refs -- and can then drift from the
thing it is standing in for -- or an end to `@main` pinning across the
fleet. Neither is obviously worth it yet; the canary is the cheap 80%.

## Running the checks locally

```bash
pip install pre-commit
pre-commit install          # optional, to run on every commit
pre-commit run --all-files
```

The unit tests run standalone too:

```bash
python3 -m unittest discover -s tests -t . --verbose
```

The secret scan needs `gitleaks` (packaged from Debian 13 onward) and a
full clone, not a shallow one:

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

`tools/clone_with_depends.py` is not covered: it needs a real
`GitPython` repository and CI environment variables, and its
`Depends on` parsing is the only pure part. It is also no longer called
from the deploy path.
