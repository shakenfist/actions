# Working in this repository

Shared CI actions and workflows for the Shaken Fist ecosystem. Read
[ARCHITECTURE.md](ARCHITECTURE.md) for what the components are and how a
CI run flows through them; this file is what you cannot infer by reading
the code.

## The one thing to understand first

**A merge to `main` is a deploy to every repository in the fleet.**

Consumers pin `@main` -- there are no tags and no release process -- so
the next CI run in any Shaken Fist repository picks up whatever landed
here, immediately, with nothing in between. There is no staging step and
no gradual rollout.

Two consequences for how you should work:

* **Prefer additive changes.** Removing or renaming an input, an output
  or a group name breaks consumers that have not been updated yet, and
  they will fail on a branch nobody touched. `ci-make-inventory.py`
  emitting both `database_node` and the legacy `etcd_master` group is
  the pattern: add the new thing, keep the old one for a release cycle,
  remove it once every consumer branch has moved.
* **Assume you cannot test it before merge.** See below.

## What you can and cannot verify before merging

You cannot integration-test a composite action change on a pull request.
`uses:` does not accept an expression, so nothing can be pointed at your
branch; a downstream run against your branch still pulls
`build-smoke-cluster@main`, the version already on main. This is a
GitHub limitation, not an oversight, and it is written up in
[docs/ci.md](docs/ci.md).

What you can do:

* `pre-commit run --all-files` -- actionlint, shellcheck, flake8,
  skillsaw and hygiene hooks. Run this before proposing a commit.
* `python3 -m unittest discover -s tests -t .` -- the Python helpers.
* `tools/gitleaks-scan.sh` -- needs `gitleaks` and a full clone.

What catches the rest: `canary.yml` runs a real smoke cluster on every
push to `main` and files a `canary`-labelled issue when it fails. If you
land something that breaks the deploy, that is where it surfaces, and it
is the whole fleet's problem until it is fixed or reverted.

## Traps

**Never write a bot trigger phrase in a comment you are posting.**
`pr-retest.yml`, `pr-re-review.yml` and `pr-address-comments.yml` all
match with `contains()` over the whole comment body, so the phrase fires
from inside backticks, inside a quote, or inside a sentence explaining
what not to do. Two of the three push commits to the pull request
branch, which is how they collide with work you are about to push
yourself. Describe them or break them up instead. The phrases are listed
in [docs/ci.md](docs/ci.md).

**A pull request is reviewed once unless somebody asks again.** The
automatic review in `ci.yml` does not set `force`, and
`review-pr-with-claude` skips a pull request the bot has already
reviewed. After you push a round of fixes, comment asking the bot to
re-review, or nothing looks at them.

**Do not "fix" the `@main` pins inside `smoke-cluster.yml`.** They look
wrong -- the composite actions are right there in the same repository --
but a relative reference inside a reusable workflow resolves against the
*caller's* checkout, so it would break every consumer. The pins are
correct and deliberate.

**Relative references are correct in `ci.yml` and `canary.yml`.** Those
run only for this repository, so `./` resolves here. That is the point:
it lets this repository's own CI test the pull request's version.

**Composite action steps cannot carry `timeout-minutes`.** Put the
timeout on the consumer's step that uses the action, as the README
examples do.

**Every runner label must be listed in `.github/actionlint.yaml`,** or
actionlint fails the lint job. Static runners are `[self-hosted, static]`
*exactly* -- adding a size or an OS label there asks for a runner that
does not exist, and the job waits forever without being scheduled.

**Do not add `secrets: inherit` when calling `pr-auto-review.yml`.**
Nothing in the reviewer chain reads a secret; both it and
`review-pr-with-claude` authenticate with `github.token` from the
caller's `permissions:` block. Inheriting hands every secret the calling
repository holds to a workflow in another repository, for no benefit.
Keep the `permissions:` block -- removing *that* does break the reviewer.

**Some `tools/` scripts only ever run on a remote cluster node,** copied
there and executed over ssh by `tools/run_remote`. They cannot be
exercised locally or in CI, so changes to them are effectively untested
until the canary runs. Be correspondingly careful.

**A script with no caller in this repository is usually still live.**
Consumers check this repository out as `actions/` and run scripts
straight out of it -- `shakenfist`'s `code-formatting.yml` calls
`${GITHUB_WORKSPACE}/actions/tools/ci_code_formatting.sh`, its
`sync-external-docs.yml` calls `ci_sync_docs.sh`, and kerbside's
functional tests copy the `ovirt-*.sh` scripts to a node and run them
there. Grepping this repository alone will tell you those are dead.
Grep the fleet before deleting or changing the interface of anything
under `tools/`.

## Conventions

**This is not a Python project.** There is no `pyproject.toml`, no tox
and no packaging, and the consistency audits are configured to expect
none. Do not add them. The Python here is executed in place by workflow
steps.

Tests are plain `unittest`, in `tests/`, loaded by path via
`tests/helpers.py` because the scripts they cover have hyphens in their
names and cannot be imported normally. Run them from the repository
root.

**Python style:** single quotes except in docstrings, which use triple
double quotes; never triple single quotes; wrap at 120 columns; no
trailing whitespace.

**Shell:** shellcheck is gated at `error` severity, not because warnings
are acceptable but because 31 pre-existing ones have not been worked
through. Do not add new findings at any level. Where an unquoted
expansion is genuinely intended, say so with a targeted
`# shellcheck disable=` and a comment explaining why.

**Workflows:** every workflow needs a top-level `permissions:` block,
set to the minimum it actually uses. Do not write more than about five
lines of shell inline in a workflow step -- put it in a script under
`tools/` and call it, so it can be run and tested outside CI.

**Documentation:** user-visible changes are documented in `docs/`.
`README.md` is a pitch, not a manual -- add new detail to `docs/` and
link to it. Update this file only when a *convention* changes, and
`ARCHITECTURE.md` only when the *shape of the system* changes.

**Review marks:** `REVIEWS.md` and everything under `.vscode/` except
`review-scope.toml` is generated by `tools/review-tracking.sh`. Never
edit them by hand -- the next regeneration discards the edit.

## Where to look

| Question | File |
|---|---|
| How does this repository's own CI work, and what is deliberately not linted? | [docs/ci.md](docs/ci.md) |
| What is `REVIEWS.md`, and how do I record a review? | [docs/ci.md](docs/ci.md) |
| What are the components and how does a run flow? | [ARCHITECTURE.md](ARCHITECTURE.md) |
| How do I consume these actions from my repository? | `README.md` |
| What shape must a review JSON take? | `review-pr-with-claude/review-schema.json` |

Fleet-wide conventions, and the consistency audits that check them, live
in the `shakenfist/development` repository. If an audit issue filed
against this repository looks wrong, the check itself may be at fault --
fix it there rather than working around it here.
