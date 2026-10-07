Thanks for your work on this. I appreciate it. Some final
checks before I push.

## How to use this runbook

This repository is not a service and not a package. It is the
composite actions, reusable workflows and helper scripts that
every Shaken Fist repository runs in CI, and every one of them
resolves it at `@main`, unpinned. A merge here is a deploy to the
whole fleet on its next CI run, with nothing in between, and a
pull request cannot integration-test most of what it changes:
`uses:` takes no expression, so nothing can be pointed at the
branch (`docs/ci.md` explains why, and names the one action that
is the exception). `canary.yml` bounds how long a bad merge goes
unnoticed after it lands; it does not stop one landing. This
checklist is the gate that comes before the push instead, and
the briefs below are written for that blast radius.

The audit splits into two waves:

**Wave 1 -- mechanical.** Lint, the unit tests, the secret scan,
then the grep-level checks on the diff. Always run wave 1 first;
wave 2 is only worth spending on if wave 1 passes.

**Wave 2 -- judgment.** Four independent sub-agents that read
code and apply judgment. They can be spawned in parallel.

The management session reviews all findings, fixes any issues,
and confirms the push.

Every diff command below reads a range held in one variable,
`AUDIT_RANGE`. Run `git fetch origin` first: `origin/main` is
itself a cached ref that only advances on fetch, so a clone
left alone for a while would otherwise widen the audit
silently, even under the range below.

The default, for work not yet merged, is `origin/main...HEAD`.
Work that has already landed sets `AUDIT_RANGE` explicitly
instead: a phase that landed as one merge commit sets
`AUDIT_RANGE=<sha>^1..<sha>`, and a plan whose phases landed as
several merges runs every command once per merge and pools the
findings. A phase that landed on `main` directly, with no merge
commit, sets `AUDIT_RANGE=<first>^..<last>` -- with the caret.
`first..last` is the label the `Merged` column records, but
`A..B` excludes `A`, so the diff that actually covers that label
is one commit wider than the bare range; drop the caret and the
first commit of the phase silently falls out of the diff, which
is where a version bump or a column addition tends to live.

That matters more here than in most repositories, because plans
in other repositories land phases here. A plan whose push-audit
phase reaches a phase that merged in this repository audits that
merge with this runbook, against this repository's conventions,
and cites the result -- it does not audit it with its own.

Every command below is written
`git diff "${AUDIT_RANGE:-origin/main...HEAD}"`, with the
default in the shell expansion rather than left for the reader
to export: an unset bare `$AUDIT_RANGE` diffs the working tree
instead, which is empty on a clean checkout, and every check
then passes over nothing.

One consequence matters for wave 2, because it is silent. A
sub-agent runs in its own shell and inherits nothing, so a brief
handed over with the expansion in it expands to the default --
which against already-landed work is the empty diff this rule
exists to prevent, and an empty diff reads as a clean review. So
the management session substitutes the concrete range into each
wave 2 brief rather than passing the expansion, tells each agent
which range it is reading, and requires every report to state
the insertion and deletion totals it measured. A report whose
totals do not match the range is re-run rather than triaged: it
is the one check on this that does not depend on the agent
noticing its own mistake.

## Wave 1: Mechanical checks

There is no `tox` and no `pyproject.toml` here, deliberately --
see `AGENTS.md` -- so wave 1 is four commands rather than one:

```
# actionlint over .github/workflows/, shellcheck at error severity,
# flake8, skillsaw, check-yaml and the hygiene hooks, plus the unit
# tests. ci.yml runs the same command on every pull request
pre-commit run --all-files

# The unit tests on their own, verbosely. pre-commit runs them too,
# but only prints them on failure; this is the run whose count of
# tests you can read. Needs PyYAML and Jinja2 (python3-yaml,
# python3-jinja2)
python3 -m unittest discover -s tests -t . --verbose

# Secret scan over history reachable from HEAD, with a positive
# control. Needs gitleaks and a full clone, not a shallow one
tools/gitleaks-scan.sh

# ansible's own --syntax-check over the playbooks in ansible/.
# pre-commit's check-yaml only parses them; this resolves module
# names, so it needs ansible.posix and the shakenfist.shakenfist
# collection installed. Run it whenever the diff touches ansible/,
# on a machine that has them, and say so if you could not
tools/ansible-syntax-check.sh
```

Then the grep-level checks on the diff:

```
# origin/main is a cached ref: without this a stale clone widens
# the audit silently, even under the range rule above
git fetch origin

# Lines over 120 characters in new Python
git diff "${AUDIT_RANGE:-origin/main...HEAD}" -- '*.py' | grep -nE '^\+[^+].{120,}'

# Touched source files long enough for `source-file-size` in 2a to
# be worth raising -- 800 lines to ask the question, 1500 to want an
# answer. The block is advisory and judging it is wave 2's job; this
# only produces the number. YAML is listed because here the workflow
# is the executable artifact, and smoke-cluster.yml is the file most
# often touched. --diff-filter=d keeps a deleted path out of wc's
# argv, and the awk drops wc's own total line
git diff --name-only -z --diff-filter=d \
    "${AUDIT_RANGE:-origin/main...HEAD}" -- '*.py' '*.sh' '*.yml' | \
    xargs -0r wc -l | sort -rn | awk '$1 > 800 && $2 != "total"'

# shellcheck at its default severity over the touched shell.
# pre-commit gates at error only because of a pre-existing backlog;
# a change must not add findings at any level. tools/run_remote has
# no extension, so it is named explicitly. Where shellcheck is not on
# PATH, pre-commit has installed one in its own cache. The tree's
# existing warnings are backlog; only findings on lines the diff
# touched are findings against the change
SC=$(command -v shellcheck || \
    find ~/.cache/pre-commit -path '*/bin/shellcheck' -type f | head -1)
git diff --name-only -z --diff-filter=d \
    "${AUDIT_RANGE:-origin/main...HEAD}" -- '*.sh' 'tools/run_remote' | \
    xargs -0r "${SC}"

# Interface removed from an action or a reusable workflow. Callers
# resolve both at @main, so a removed or renamed input, output or key
# fails a consumer branch nobody touched. Report only: a removed key
# also matches here when it was never an input, and the management
# session says which hits are interface
git diff "${AUDIT_RANGE:-origin/main...HEAD}" -- '*/action.yml' | \
    grep -nE '^-  [a-z][a-z0-9_-]*:\s*$'
git diff "${AUDIT_RANGE:-origin/main...HEAD}" -- \
    '.github/workflows/smoke-cluster.yml' \
    '.github/workflows/pr-auto-review.yml' \
    '.github/workflows/export-repo-config.yml' \
    '.github/workflows/issue-link-check.yml' | \
    grep -nE '^-      [a-z][a-z0-9_-]*:\s*$'

# Inventory groups removed. Consumer playbooks name these groups,
# and the legacy names are kept until every consumer has moved
git diff "${AUDIT_RANGE:-origin/main...HEAD}" -- 'tools/ci-make-inventory.py' | grep -nE '^-'

# Rules from AGENTS.md that a grep can see: no secrets: inherit,
# even in a usage example; no timeout-minutes inside a composite
# action; every new runs-on checked against the label rules
git diff "${AUDIT_RANGE:-origin/main...HEAD}" | grep -nE '^\+.*secrets:\s*inherit'
git diff "${AUDIT_RANGE:-origin/main...HEAD}" -- '*/action.yml' | grep -nE '^\+.*timeout-minutes'
git diff "${AUDIT_RANGE:-origin/main...HEAD}" -- '*.yml' | grep -nE '^\+.*runs-on'

# Hand edits to generated review files. REVIEWS.md and everything
# under .vscode/ except review-scope.toml are written by
# tools/review-tracking.sh
git diff "${AUDIT_RANGE:-origin/main...HEAD}" --name-only -- \
    'REVIEWS.md' '.vscode/' ':!.vscode/review-scope.toml'

# TODO / FIXME / HACK / XXX, and new suppressions. A new shellcheck
# disable must carry a comment saying why the expansion is intended
git diff "${AUDIT_RANGE:-origin/main...HEAD}" | \
    grep -nE '^\+.*\b(TODO|FIXME|HACK|XXX)\b'
git diff "${AUDIT_RANGE:-origin/main...HEAD}" | \
    grep -nE '^\+.*(# noqa|# type: ignore|shellcheck disable)'

# Documentation touched at all (warns if none)
git diff "${AUDIT_RANGE:-origin/main...HEAD}" --name-only -- 'docs/*' '*.md'
```

Exit condition: wave 1 passes when `pre-commit`, the unit tests
and the secret scan are clean, the ansible syntax check is clean
or recorded as not run, and each grep has either no hits or hits
the management session has looked at and accepted. The greps
report; they do not block.

`REVIEWS.md` needs no change in a pull request that changes code
or documentation. A change that adds or removes a file matched by
`.vscode/review-scope.toml` moves the header count, and a change
that edits a file carrying a review mark stales that mark, but the
`prune-reviews` workflow corrects both on the next push to main --
so do not prune, regenerate or commit the file here. Say instead
which marks the change stales. Never re-stamp -- the mark attests
that a person read that exact content. A review session is the
exception: `stamp` regenerates the file, and the marks are
committed together, as `docs/ci.md` describes.

## Wave 2: Deeper review

Only run wave 2 after wave 1 passes.

### 2a. Code quality

| Setting | Value |
|---------|-------|
| Model | sonnet |
| Effort | medium |

**Brief for sub-agent:**

The mechanical sweep has already extracted TODO/FIXME comments,
new suppressions, removed interface keys, `runs-on` lines and
shellcheck findings. Take that report as input, and triage each:
blocking or advisory, and why.

Then the judgment-level review of
`git diff "${AUDIT_RANGE:-origin/main...HEAD}"`. The composite
`action.yml` files are linted by nothing -- actionlint reads
workflows only -- so read every changed one as though no tool
had:

- **Unpinned fan-out.** For each changed workflow or composite
  action, which callers resolve it at `@main`, and what does
  this change do to a caller that does not pass the new input?
  A default is a fleet-wide decision here, not a local one.
  Answer it by grepping the fleet (sibling clones under
  `~/src/shakenfist/`, or `gh search code --owner shakenfist`),
  not this repository: consumers call these actions and run
  `tools/` scripts out of their own checkout of this one, so a
  script with no caller here is usually still live.
- **Gate exhaustiveness.** For any step condition keyed on an
  input's value, is there an input value for which a step runs
  and the step it depends on does not, or for which nothing runs
  at all and the job still succeeds? Enumerate the values rather
  than reading the conditions one at a time; a job that goes
  green having done nothing is the failure nobody notices.
- **Additive changes.** A removed or renamed input, output, job
  output or inventory group breaks consumers that have not moved
  yet. The pattern is to add the new name, keep the old one for a
  release cycle, and remove it once every consumer branch has
  moved. A removal is blocking unless the pull request names the
  consumers it checked.
- **References that look wrong but are not.** The `@main` pins
  inside `smoke-cluster.yml` are correct: a relative reference in
  a reusable workflow resolves against the caller's checkout.
  Relative references in `ci.yml` and `canary.yml` are also
  correct, because those run only here. A diff that "fixes"
  either is blocking.
- **Runner labels.** Every label must be in
  `.github/actionlint.yaml`. A static runner is
  `[self-hosted, static]` exactly -- an extra label asks for a
  runner that does not exist, and the job waits forever. A `vm`
  runner must also name a size, or it silently gets the
  smallest; anything that builds wheels or drives an ansible
  deploy needs at least `s`.
- **Composite action limits.** No `timeout-minutes` on a
  composite step; the timeout belongs on the consumer's step,
  and the usage examples in `docs/actions.md` show where.
- **Inline shell.** More than about five lines of shell in a
  workflow or action step belongs in a script under `tools/`,
  where it can be run and tested outside CI.
- **Remote-only scripts.** Some `tools/` scripts are copied to a
  cluster node by `tools/run_remote` and run there, and cannot be
  exercised locally or in CI. A change to one is untested until
  the canary runs; hold it to a higher standard of reading, and
  say which scripts in the diff are of this kind.
- **Python is run in place.** The helpers are executed directly
  by workflow steps, on the runner or on a cluster node, with no
  install step. A new third-party import is a new dependency on
  every machine that runs the script; PyYAML and Jinja2 are the
  only ones the tests assume.

<!-- shared-block: comment-proportion v1 -->
Comment proportion (shared block; do not edit -- the canonical
copy lives in shakenfist/development at
`templates/shared-blocks/comment-proportion.md`):

- A comment or docstring earns its length by saying what the code
  cannot: the contract, the units, the failure modes, the reason a
  surprising choice is correct. Restating the code in prose is not
  documentation.
- Treat as candidates any added comment or docstring that is longer
  than the code it documents, and any comment block over roughly
  fifteen lines attached to a body under ten. These are candidates,
  not verdicts -- a subtle algorithm, a public API contract, or a
  hard-won bug explanation can justify the length.
- Where the length is not justified the finding is advisory, and
  the fix is to cut the restatement rather than delete the comment:
  keep the why, drop the line-by-line narration of the what.
- Prose that documents user-visible behaviour rather than the
  implementation usually belongs in `docs/`, with the comment
  reduced to a pointer.
<!-- shared-block-end -->

<!-- shared-block: source-file-size v1 -->
Source file size (shared block; do not edit -- the canonical
copy lives in shakenfist/development at
`templates/shared-blocks/source-file-size.md`):

- Where a repository tracks whole-file human review, a file's cost
  is its length times how often it is touched: every change
  discards the review of the whole file, and the next session
  re-reads all of it. That, rather than taste, is why length is
  worth raising in review at all.
- Treat a source file over roughly 800 lines as a candidate to
  split, and one over roughly 1,500 as wanting a stated reason to
  stay whole. These hold whether or not a repository tracks review
  per file: tracking is what makes the cost repeat and become
  measurable, not what makes a long file expensive to read. Both
  are advisory. Neither is a gate, there is no hard cap, and a
  reviewer who raises one is opening a question, not recording a
  defect.
- Generated files, vendored trees and protocol or data tables are
  exempt: they are not read the way source is, and a tool that
  counts them is measuring the wrong thing.
- Split along a seam that already exists -- one module's public
  entry point, one check, one subcommand, one endpoint -- so that
  a later change touches one of the pieces rather than all of
  them. A file split at a line number rather than at a seam is
  worse than the long file it replaced.
- Length is never reduced by deleting the comments and docstrings
  that explain why the code is the way it is. Those are what make
  a long file reviewable, and trading them for a line count makes
  the review worse while making the number better. Cut duplicated
  scaffolding first; see `comment-proportion` for what earns its
  length.
<!-- shared-block-end -->

<!-- shared-block: plan-references-in-code v1 -->
Plan references in code (shared block; do not edit -- the
canonical copy lives in shakenfist/development at
`templates/shared-blocks/plan-references-in-code.md`):

- Code, comments, docstrings, test names, fixture descriptions and
  configuration describe the software as it is now. Which plan,
  phase, step or decision produced a line is history, and the plan
  and the commit log already keep it. Do not write "added in phase
  5", "per decision 3", "pending step 5f" or "the phase-4 leaks
  pass": a reader of the code has not read the plan, and the
  number tells them nothing.
- Where a comment cites a plan to explain why the code is the way
  it is, the explanation belongs in the comment. Write the reason
  -- the constraint, the measurement, the failure it prevents --
  and drop the citation. A pointer standing in for the reasoning
  costs every reader a detour, and rots when the plan is archived
  or renumbered.
- A plan link is acceptable only for work that is not built yet: a
  deliberate gap or refusal whose lifting is planned, where
  "deferred; see `PLAN-foo.md`" tells the reader the gap is known.
  The link comes out when the work lands. Prefer an issue link
  where one exists, and write a plan in another repository as an
  absolute URL; the `plan-source-references` audit checks that
  these links resolve.
- Plan documents and commit messages may cite phases and decisions
  freely; recording that history is their job.
- "Phase" in its ordinary sense -- a two-phase commit, a compiler's
  link phase -- is not a plan reference.
- A plan reference a diff adds to code is a finding to fix before
  pushing. References on lines the diff does not touch are backlog,
  not findings against the change.
<!-- shared-block-end -->

<!-- shared-block: python-version-discipline v1 -->
Python version and typing (shared block; do not edit -- the
canonical copy lives in shakenfist/development at
`templates/shared-blocks/python-version-discipline.md`):

- No syntax or standard library API newer than the floor in
  `requires-python`. Structural pattern matching, `X | Y` unions in
  annotations evaluated at runtime, `tomllib`, and
  `datetime.UTC` each raise on an interpreter the package still
  claims to support, and none of them fail in CI when CI runs only
  the newest version. This is the finding to look for first: it is
  a real break on a real user's machine, not a style point.
- New and modified code carries type hints, and mypy is expected to
  be clean over it. A project part way through a staged rollout is
  held to the new code, not to the whole tree.
- Prefer the walrus operator and f-strings where they make the code
  read better, subject to the floor above.
- Raising the floor in `requires-python` is a supported-platforms
  decision, not a convenience: it drops users. If it is genuinely
  right, the platforms table, `requires-python` and
  `constraints.python` in `renovate.json` all move together.
<!-- shared-block-end -->

- There is no `requires-python` here. The floor is the oldest
  interpreter a script meets: the runners are Debian 13, but
  scripts sent to cluster nodes meet whatever `base_image` a
  consumer passes to `smoke-cluster.yml`, which today includes
  Ubuntu 24.04 (Python 3.12). Grep the fleet's `base_image`
  values rather than trusting that list. Type hints and mypy are
  not expected of these helpers.

Report findings as a bullet list. For each, state the file, line,
and whether it is blocking or advisory.

### 2b. Test review

| Setting | Value |
|---------|-------|
| Model | sonnet |
| Effort | medium |

**Brief for sub-agent:**

Review `git diff "${AUDIT_RANGE:-origin/main...HEAD}"` for test
coverage. The suites are plain `unittest` in `tests/`, loaded by
path through `tests/helpers.py` because the scripts they cover
have hyphens in their names. `docs/ci.md` ("What the unit tests
cover") says what each one is for.

- Does a changed Python helper, or a shell script with logic that
  can run locally, have a test that would have failed before the
  change? The verdict and collection scripts behind the headroom
  check are the model: shell with its decisions tested from
  Python.
- Do the structural tests still hold the thing they pin?
  `test_workflow_references.py` checks how workflows reference
  actions, `test_smoke_cluster_inputs.py` holds the `test_kind`
  values equal to the gates that consume them, and
  `test_documentation_links.py` enforces the link rules. A change
  that adds an input value, a workflow or a document those tests
  do not know about is the usual gap.
- For anything that can only run on a cluster node, say so, and
  say what the canary will and will not exercise. Untestable is a
  finding to record, not a reason to stay silent.
- Does any new test reach outside its fixture -- the network,
  `gh`, a real cluster? Those pass locally and fail in CI.
- Does any test skip itself when a dependency is missing? A skip
  is not a failure, which is why PyYAML is a hard requirement
  here rather than an optional one.

<!-- shared-block: functional-test-coverage v1 -->
Functional test coverage (shared block; do not edit -- the
canonical copy lives in shakenfist/development at
`templates/shared-blocks/functional-test-coverage.md`):

- The standard is "do we run the code to do the real thing, and
  does it work as intended". Every subcommand exposed on the command
  line, and every endpoint exposed by an API, should have a test
  that exercises it for real rather than against a mock of itself.
- For a change that adds or alters user-visible behaviour, the
  question to answer is which functional test would have failed
  before it and passes after. If there is none, that is the finding,
  and it is a finding about this change rather than a note for
  later.
- Unit tests are held to no coverage percentage, but a branch that
  is reachable from outside the process and has no test is worth
  naming. Error paths and argument validation are where this bites:
  they are the code most often written once and never run again.
- Mocking the system under test proves nothing. Mock the boundary --
  the network, the clock, the hypervisor -- and let the code being
  tested actually run.
- Where a gap is real but out of scope for the change in hand, say
  so plainly and record it, rather than silently widening the
  change or silently leaving it unsaid.
<!-- shared-block-end -->

Report findings as a bullet list grouped by file.

### 2c. Documentation review

| Setting | Value |
|---------|-------|
| Model | sonnet |
| Effort | medium |

**Brief for sub-agent:**

Check that documentation matches the current code state. Read
`git diff "${AUDIT_RANGE:-origin/main...HEAD}"` and verify:

<!-- shared-block: readme-discipline v1 -->
README discipline (shared block; do not edit -- the canonical
copy lives in shakenfist/development at
`templates/shared-blocks/readme-discipline.md`):

- New user-visible features are documented in `docs/` (and
  `ARCHITECTURE.md` / `AGENTS.md` where appropriate), not by
  adding bullets to `README.md`.
- `README.md` is a pitch: what the project is, who it is for,
  minimal installation instructions, a small number of usage
  examples, and curated absolute links into `docs/`. It only
  changes when the pitch, the install story, or the
  documentation links change.
- README growth is itself a finding: if the diff adds README
  content that belongs in `docs/`, flag it as blocking and
  move it.
<!-- shared-block-end -->

<!-- shared-block: llm-doc-discipline v1 -->
AGENTS.md and ARCHITECTURE.md discipline (shared block; do not
edit -- the canonical copy lives in shakenfist/development at
`templates/shared-blocks/llm-doc-discipline.md`):

- `AGENTS.md` is a working guide: the conventions, invariants and
  gotchas an agent cannot infer by reading the code, plus curated
  links into `docs/`. It is loaded into every session, so every
  line costs context on every task.
- `ARCHITECTURE.md` is a map: the component inventory, how data
  moves between components, and why the shape is the way it is.
  A deep dive on one subsystem belongs in `docs/`, where humans
  benefit from it too.
- One canonical home per fact. If `docs/` covers it, link to it
  instead of restating it -- and the same rule applies between
  `AGENTS.md` and `ARCHITECTURE.md`.
- Neither file is a reference manual, a runbook, or a changelog.
  CLI flags, configuration keys, wire protocols, step-by-step
  procedures and plan history go to `docs/`.
- Growth in either file is itself a finding: if the diff adds
  content that belongs in `docs/`, flag it as blocking and move
  it.
<!-- shared-block-end -->

<!-- shared-block: diagram-discipline v1 -->
Diagram discipline (shared block; do not edit -- the canonical
copy lives in shakenfist/development at
`templates/shared-blocks/diagram-discipline.md`):

- A diagram of *structure or flow* -- components and the arrows
  between them, an ordered exchange of messages, a state machine
  -- is written as a fenced `mermaid` block, not drawn in ASCII.
  GitHub renders those natively and the mkdocs sites render them
  through `pymdownx.superfences`, so the same source is a picture
  in both places.
- Not every box of characters is a diagram. These stay as plain
  code fences, because mermaid cannot express them and would lose
  what they show: directory and file trees; memory maps, address
  space layouts and register or bit-field diagrams, where column
  alignment carries the meaning; wire-format and on-disk byte
  layouts; captured terminal output; and tables. The test is
  whether the picture is nodes and edges. Something that is a
  table with lines drawn on it is a table.
- Pick the diagram type that matches the claim: `flowchart` for
  components and data flow, `sequenceDiagram` for an ordered
  exchange between parties, `stateDiagram-v2` for a state
  machine, `erDiagram` for data relationships. A sequence drawn
  as a flowchart has thrown away the ordering it existed to show.
- A new ASCII box-and-arrow diagram in the diff is a finding.
  Converting one the diff already touches is in scope; converting
  every other diagram in the file is not, because a sweep is its
  own change and its own review.
<!-- shared-block-end -->

- In this repository the documentation consumers read is
  `docs/actions.md` (the composite actions, their inputs and
  outputs) and `docs/consuming.md` (the reusable workflows and how
  to call them). A new, changed or removed input or output that is
  not reflected in one of them is blocking: consumers have nothing
  else to read, and they are on `@main` already. Usage examples are
  copied verbatim, so an example must be correct as written --
  in particular, never `secrets: inherit`.
- `docs/ci.md` describes this repository's own CI, what is
  deliberately not linted, and the bot trigger phrases. A change
  to a lane, a lint, or a trigger belongs there.
- Links follow two rules `tests/test_documentation_links.py`
  enforces: every link in `README.md` is absolute, and a relative
  link inside `docs/` stays inside `docs/`, because `docs/` is
  republished without the tree above it.

<!-- shared-block: plan-phase-references v1 -->
Plan phase references (shared block; do not edit -- the canonical
copy lives in shakenfist/development at
`templates/shared-blocks/plan-phase-references.md`):

- Documentation outside plans directories describes the current
  state of the software, not the history of how it was built. Do
  not write "implemented in phase 5" or "since phase 3 of the
  two-tier CI plan": a reader wants to know whether a feature
  exists, not which phase of which plan delivered it.
- If a documented behaviour is implemented, describe it plainly.
  If it is planned but not yet implemented, link to the master
  plan in `docs/plans/` instead of citing a phase number.
- Reserve the word "phase" for plan documents. A procedural
  document describing a live multi-stage process (a release
  runbook, say) should call its stages "steps" or "stages", so
  that a phase reference in `docs/` is always a plan smell.
- The consistency audit greps `README.md` and `docs/` (excluding
  plans directories) for "phase <number>". Append
  `<!-- audit-ok: phase-reference -->` to a line only when the
  reference is genuinely not about an implementation plan.
<!-- shared-block-end -->

Report findings as a bullet list. "No documentation gaps found"
is a valid answer.

### 2d. Security review

| Setting | Value |
|---------|-------|
| Model | opus |
| Effort | high |

**Brief for sub-agent:**

Security review of
`git diff "${AUDIT_RANGE:-origin/main...HEAD}"`. Read the
actual code, not just the diff summary.

The threat model is that of a supply chain. This repository holds
no data and has no users of its own; what it has is code that
every Shaken Fist repository executes at `@main` with that
repository's token, on self-hosted runners and on CI nodes it
provisions as root. A weakness here is a weakness in all of them,
from the next run.

- **Privileged triggers.** `pr-retest.yml` and `pr-re-review.yml`
  run on `issue_comment`, with the base repository's permissions.
  `pr-bot-trigger` must decide authorisation -- write access and a
  pull request from a branch in this repository, never a fork --
  before anything else runs. Any new step that checks out or runs
  pull-request-controlled content in that context is critical.
- **Injection into shell and into prompts.** Pull request titles,
  branch names, comment bodies and review text are
  attacker-controlled. Are any interpolated with `${{ }}` straight
  into a `run:` script, a `gh` argument, or the reviewer's prompt,
  rather than passed through `env:` as data?
- **Secrets and token scope.** Does any workflow widen
  `permissions:`? A reusable workflow here declares the secrets it
  reads under `on.workflow_call.secrets`, and callers pass them by
  name; `secrets: inherit` would hand every secret a consumer holds
  to whatever lands here next, and is blocking anywhere, including
  documentation. Is a token echoed, written to a file, or passed as
  an argument where `ps` can see it? `pr-auto-review.yml`'s
  `permissions:` block is load-bearing and must stay.
- **The automated reviewer.** `review-pr-with-claude` reads a pull
  request's diff, which is attacker-controlled input to a model
  holding a token. Does the change widen what the model's output
  can cause -- the tools it may call, the shape
  `review-schema.json` lets through, the issues
  `create-review-issues.py` will file?
- **What reaches CI nodes as root.** `tools/run_remote` scripts,
  `ansible/` and `ansible/files/` (cloud-init, netplan, log
  shipper and gather configuration) all run with root on nodes
  that later run consumers' tests. Downloads must be verified
  against a pinned checksum or signature; package sources must be
  ones we chose.
- **Self-hosted runners.** Does any new job run untrusted code on
  a static runner, or leave state behind for the next job?
- **Secret scanning.** Does the diff add anything that looks like
  a credential, or widen the allowlist in `.gitleaks.toml`? An
  allowlist entry is a decision that the scanner will not look,
  and needs a stated reason.

<!-- shared-block: path-traversal-review v1 -->
Path construction from outside data (shared block; do not edit --
the canonical copy lives in shakenfist/development at
`templates/shared-blocks/path-traversal-review.md`):

- Treat as a candidate any filesystem path built from a value the
  process did not choose: a request parameter, an image name, tag or
  digest, a layer path, an archive member name, a filename out of a
  configuration file or a database row.
- The question is not whether the value looks dangerous but whether
  the resulting path is *proved* to stay inside its intended base
  directory. Resolve the joined path with `os.path.realpath()` and
  verify it still starts with the base; a check on the untrusted
  component alone is defeated by symlinks and by encodings the
  check did not anticipate.
- Prefer a helper that cannot be forgotten at a call site --
  `safe_path_join()` in occystrap, or the framework's own
  (`send_from_directory` in Flask) -- over an inline guard repeated
  at each join.
- Archive extraction is the case most often missed: a member name
  inside a tarball or zip is attacker-controlled in exactly the same
  way as a request parameter.
- Where a bare join is correct because every component is
  process-chosen, say so in a comment rather than leaving the
  reader to re-derive it.
<!-- shared-block-end -->

Report findings with severity (critical / high / medium / low /
informational). For each, state the file, line, the vulnerability
class, and a recommended fix.

## Management session checklist

After all agents complete:

- [ ] Wave 1 passed (`pre-commit run --all-files`, the unit tests
      and `tools/gitleaks-scan.sh` clean; the ansible syntax check
      clean or recorded as not run; greps reviewed).
- [ ] Wave 2 findings reviewed, and each report's insertion and
      deletion totals match the range it was given.
- [ ] Any blocking findings from 2a/2b/2c fixed and re-verified.
- [ ] Security findings assessed -- critical and high must be
      fixed before push.
- [ ] Every removed or renamed input, output or inventory group
      has its consumers named, or is kept alongside its
      replacement.
- [ ] `docs/actions.md` and `docs/consuming.md` match the inputs
      and outputs as they now are.
- [ ] Generated files are generated, not hand-edited:
      `REVIEWS.md` and `.vscode/`.
- [ ] Stale review marks named in the pull request body, not
      pruned.
- [ ] Commit history is clean -- no fixups that should be
      squashed, no accidental files, no WIP messages.
- [ ] Branch is up to date with `main`.
- [ ] Ready to push -- remembering that merging is deploying, and
      the canary run on `main` after it is the first integration
      test this change gets.
