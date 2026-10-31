# Slim topologies: replay a hypervisor node's preflight capacity abort

## Prompt

Before responding to questions or discussion points in this document,
explore the repositories it names thoroughly. Read the relevant files
and ground your answers in what they actually say. Do not speculate
about the code when you could read it instead. Flag any uncertainty
explicitly rather than guessing.

This plan resolves shakenfist/actions#207. It changes two
repositories: shakenfist/shakenfist (the `shakenfist.shakenfist`
collection's `sf_instance` module) and shakenfist/actions (the slim
topology playbooks). The plan lives here because the issue does. Each
repository gets its own pull request, and they must land in order.

shakenfist/development's `PLAN-TEMPLATE.md` supplies the shared
blocks this plan follows -- the execution model (implementation in
sub-agents, review in the management session), the effort ladder,
the model roster, the review checklist and the push-audit phase --
and they are not repeated here. This repository has no
`PLAN-TEMPLATE.md` of its own.

**Planning effort:** high. The fix is a new mode in a module every
Shaken Fist consumer picks up at once, and the two pull requests
have an ordering constraint that the fleet has already been bitten by
once. **Review effort:** high.

## Situation

Read on 2026-10-31 against shakenfist/actions `main` at `4ba6949`,
shakenfist/shakenfist `develop` at `61a14af571` (the local checkout;
`origin/develop` was at `8b60c15c4e`) and the local client-python
`develop` checkout. Line numbers below are at those commits; re-grep
before relying on one.

**What happens.** `ci-topology-slim-primary.yml` and
`ci-topology-slim-tier.yml` create their nodes through one include,
`ansible/tasks/create-topology-node.yml`, in a loop over
`topology_nodes`. The primary carries `await: true`; every `sf1..sfN`
hypervisor takes the include's default, `await: "{{ node.await |
default(false) }}"` (`create-topology-node.yml:52`). An unawaited
create returns as soon as the under-cloud accepts it. If preflight
then aborts the instance for capacity -- `Unable to find suitable
node` -- nothing in the build notices. `ci-include-common-localhost.yml`
waits for an SSH banner on every node for 300 seconds, and the build
dies there, five minutes later, with `Timeout when waiting for search
string OpenSSH`, which is shakenfist/shakenfist#3718's signature and
reads as a boot or network fault.

**How often.** private-ci's placeable-claims phase 4 reading
(`docs/plans/PLAN-placeable-claims-phase-04-build-wait.md`, "Step 4f
reading", 2026-10-27) measured seven days, 2026-10-18T03:04Z to
2026-10-25T03:04Z. Of 61 in-job capacity refusals, 55 were rescued by
`capacity_wait`. Three were preflight aborts on unawaited slim
hypervisors, and each one killed its job:

| Time (UTC) | Run / job | Node | Stage |
|---|---|---|---|
| 2026-10-22 07:59:38 | 39735010744, Guests (collection) | `sf4`, `t-5Uctl-4` | `sufficient_idle_cpu` |
| 2026-10-24 20:42:55 | 40125210814, Debian 13 tier | `sf2`, `t-FucF0-2` | none logged |
| 2026-10-24 20:53:36 | 40125210814, Ubuntu 24.04 cluster | `sf2`, `t-o35cR-2` | `sufficient_idle_disk` |

Phase 4's decision `D2` accepted this gap because phase 0's baseline
window held no in-job preflight abort. This window held three.

**Why the module cannot see it.** Three facts from the code:

1. `sf_instance`'s capacity wait already knows this abort. It
   replays a preflight abort through `_CapacityWait.preflight_abort()`
   (`sf_instance.py:626`) and `refused()` (`:648`), matching
   `PREFLIGHT_CAPACITY_MESSAGES` in `PREFLIGHT_ABORT_STATES` (`:367-379`),
   and it deletes the refused record by uuid and waits for the name to
   free (`_await_record_gone()`, `:495`). But it reaches that code only
   from the `except` around `client.await_instance_create()`
   (`:1278-1295`), which runs only when `await` is true. For an
   unawaited create the loop breaks at `:1262-1264` ("Not awaiting
   instance") before preflight has reported.
2. The client's blocking create does not wait for placement. With the
   module's default `ASYNC_BLOCK` strategy and no `timeout`,
   `create_instance()` polls until `i['state'] not in ['initial',
   'creating']` (client-python `apiclient.py:803`). `preflight` is not
   in that list, so the call returns the moment the instance enters
   `preflight` -- before the scheduler has said yes or no. That is why
   the module reports `changed` a few seconds before sfcbr's log shows
   `preflight-error`.
3. A second `state: present` against the name would not help.
   `_check_instance()` (`:787-1047`) compares the requested spec with
   the existing record and never reads its `state`. A record in state
   `error` with the same spec is not dirty, so the module would adopt
   the dead instance and exit `changed: false`. An actions-side replay
   would therefore have to delete the record by uuid, wait for the
   name to free, re-include the create, and loop for a second abort --
   re-implementing `_await_record_gone()` in YAML.

**The budget machinery is already in place.** Both slim playbooks set
`capacity_wait_budget: 600`, hand each create what is left of it, and
count what it spent in `sf_capacity_waited` (`create-topology-node.yml:54-60`).
`tests/test_capacity_wait.py` holds that pattern. `docs/ansible.md:218-219`
records the gap in one sentence: "For those, only a 507 is replayed,
because the module returns before preflight runs."

## Mission and problem statement

Make a preflight capacity abort on an unawaited create visible to
`sf_instance`, so that the existing `capacity_wait` replays it under
the same budget, and so that any other preflight abort fails the
build at once with the server's message rather than five minutes
later with an SSH timeout. Do it without serialising the hypervisor
boots that `await: false` exists to overlap.

Out of scope:

- A server-side machine-readable marker for preflight aborts
  (shakenfist/shakenfist#4568). The module keeps matching the message.
- Changing the client's `create_instance()` wait to include
  `preflight`. That would alter every blocking caller in the fleet;
  it is filed under Future work.
- Re-sizing `capacity_wait_budget`. placeable-claims phase 7 owns
  that.
- The `-released` and `-upgrade` topologies, which no workflow runs
  and which pass no `capacity_wait` today.

## Decisions

**D1. The replay lives in the module, as a placement-only await.**
`sf_instance` gains `await_placement` (boolean, default `false`).
After an accepted create it polls the instance until its state leaves
`initial` and `preflight`, then:

- placed (`creating` or `created`): return, exactly as an unawaited
  create does today, with `capacity_waited.outcome` `not_refused`;
- an error state matching `_preflight_capacity_abort()` and
  `capacity_wait` > 0: `capacity.refused('preflight', ...)` and
  replay, exactly as the awaited path does;
- any other error state, or a capacity abort with `capacity_wait` at
  0: fail at once with the instance's `error_message`;
- still unplaced when the budget runs out: fail, naming the last
  state.

Placement normally takes seconds, so the hypervisor creates still
overlap their boots. The actions-side alternative (fact 3 above) was
rejected as replay logic in YAML; awaiting the hypervisors
(`await: true`) was rejected because it serialises five boots behind
a 600 second `await_timeout` each; `async`/`poll` creates were
rejected because the run-wide budget depends on the creates being
counted one after another (`docs/ansible.md`, "Waiting for capacity").

**D2. A separate boolean, not a tri-state `await`.** `await` is
`type: bool` and the fleet writes `await: true`. Turning it into a
string with choices would change how every existing value is parsed.
`await: true` already covers placement, so `await_placement` with
`await` is redundant and harmless; the module logs that the full
await covers it and does nothing extra.

**D3. The placement wait spends `await_timeout`, as the await does.**
The budget starts at `operation_started` and deducts what the delete
and the create spent, mirroring `:1270-1273`. Time counted by
`_CapacityWait` is excluded in the same way, so a task's worst case
stays `capacity_wait` plus `await_timeout`.

**D4. The create passes `timeout=0` when placement is awaited.** The
`_create_accepts_timeout()` guard at `:1215` extends to
`await_placement`, so the module's own bounded loop does the waiting
rather than the client's strategy deadline. With a client that
predates the argument, the elapsed-time deduction covers it, as it
does for `await`.

**D5. Detect the option rather than rely on landing order.**
`install-collection.sh` builds the collection from the job's shakenfist
checkout. That is `develop` for most consumers, but for a shakenfist
`/retest` or `workflow_dispatch` it is the branch head (`pr-retest.yml`
dispatches with `--ref` set to the pull request's ref). So ordering
alone cannot protect open shakenfist branches: Ansible rejects an
option the module does not know even when it is false, and every slim
build on a branch cut before #4746 would fail with `Unsupported
parameters: await_placement`, as canary 39091877078 did on 2026-10-18
when actions#177 landed eight minutes before shakenfist#4569. The
include therefore asks `ansible-doc` whether the installed `sf_instance`
has the option and omits it, with a warning, when it does not. That
also makes a later revert of #4746 harmless. The actions pull request
is still held until #4746 merges, so the option is active from the
start.

## Open questions

- **Q1.** Should `await_placement` default to `true` for every
  create, so that no caller can hit this again? Default answer: no.
  It changes the duration of every unawaited `sf_instance` task in
  the fleet by a few seconds and adds API calls to tasks that never
  asked; the two slim includes are the only known callers that need
  it. Revisit if a second caller appears.
- **Q2.** Should the actions include set `await_placement: true` on
  every node, or only where `await` is false? Default answer: every
  node. One line on the include's one create is what
  `tests/test_capacity_wait.py` can check, and `D2` makes the redundant
  case free.

## Execution

| Phase | Status | Merged |
|-------|--------|--------|
| 1. `sf_instance` awaits placement (shakenfist/shakenfist) | In progress | |
| 2. Slim topologies await placement (shakenfist/actions) | In progress | |
| 3. Push audit | Not started | |

### Phase 1: `sf_instance` awaits placement

Work in a new shakenfist/shakenfist worktree, branch
`sf-instance-await-placement` from `origin/develop`. One pull
request, three commits. Run that repository's `PUSH-AUDIT.md` before
pushing; this plan's phase 3 cites that audit rather than repeating
it.

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| 1a | high | opus | none (the worktree above) | **The module.** In `shakenfist/deploy/collection/plugins/modules/sf_instance.py`, read `D1` to `D4` of this plan and the module's `_CapacityWait` class (`:575-770`) and `run_module()` create loop (`:1237-1320`) first. (i) Add `'await_placement': {'required': False, 'type': 'bool', 'default': False}` to `argument_spec` beside `'await'` (`:1113`). (ii) Document it in `DOCUMENTATION` after `await` (`:149-153`): it waits only until the scheduler has placed the instance or preflight has aborted it, it spends `await_timeout`, and `await` supersedes it. In `capacity_wait`'s description (`:175-210`) change "on an awaited create" to cover a create awaited to boot or to placement. (iii) Add `UNPLACED_STATES = ('initial', 'preflight')` beside `PREFLIGHT_ABORT_STATES` (`:379`) with a comment saying why the module has to wait for these itself: the client's blocking `create_instance()` returns once the state is not `initial` or `creating` (client-python `apiclient.py:803`), so it returns at `preflight`, before the scheduler's verdict. (iv) Add a module-level `_await_placement(client, uuid, deadline)` that reads the instance with `client.get_instance(uuid)`, returns it as soon as its `state` is not in `UNPLACED_STATES`, and otherwise sleeps `min(CAPACITY_RECORD_POLL, remaining)` through `_sleep` until `deadline` (a `time.monotonic()` value) passes, then returns the last read. An `APIException` propagates. (v) In `run_module()`, extend the `timeout=0` guard at `:1215` to `module.params.get('await') or module.params.get('await_placement')`, and update its log lines. Before the `if not module.params.get('await')` break at `:1262`, add the placement branch: when `await_placement` is set and `await` is not, compute the budget exactly as `:1270-1273` does (`await_timeout - (monotonic - operation_started - capacity.seconds)`, floored at 0), log it, call `_await_placement()`, then: if `_is_error_state(state)`, read `aborted = capacity.preflight_abort(i['uuid']) if needs_replacement else None` -- note `preflight_abort()` already returns `None` with a budget of 0 and re-reads the instance, which is acceptable -- and on a match call `capacity.refused('preflight', None, aborted['error_message'], i['uuid'], None, 'Waiting for instance placement failed', aborted['error_message'])` and `continue`; otherwise `fail_json` with `msg='Waiting for instance placement failed: instance %s is in state %s: %s' % (uuid, state, error_message)`, `meta=None`, `log=log`, `**capacity.failure()`. If the state is still in `UNPLACED_STATES`, fail with `'Instance %s was not placed within the %d second await_timeout (last state: %s)'` and `**capacity.failure()`. If placed, log it and `break`. When both `await` and `await_placement` are set, log that the full await covers placement and take the existing `await` path unchanged. (vi) Nothing changes for a task that sets neither option, and nothing changes for `await: true`: same API calls, same messages. Run `tox -e py3 -- shakenfist.tests.test_ansible_sf_instance` and `pre-commit run --all-files` from the worktree root (the hooks include flake8, ansible-lint and mypy). Commit subject: `collection: sf_instance can await placement.` |
| 1b | high | opus | none | **The tests.** In `shakenfist/tests/test_ansible_sf_instance.py`, extend `SfInstanceCapacityWaitTestCase._run()` (`:1189`) with `await_placement=False`, written into `params['await_placement']`. Follow the scripted-client style of `test_a_preflight_abort_record_is_awaited_by_uuid_before_the_replay` (`:1430`): `creates` are the outcomes of successive `create_instance()` calls, `lookups` maps a uuid to successive `get_instance()` results with the last repeating, and `self.calls` records every client call in order. Add, each with a name that says what it proves: (i) a placement-awaited create whose lookups run `preflight` then `creating` returns with `outcome` `not_refused`, passes `timeout=0` to the create, never calls `await_instance_create`, and makes no delete; (ii) one whose lookups run `preflight`, then `preflight-error` with `Unable to find suitable node`, then `error`, then `deleted`, followed by a second create that is placed, replays once, reports `placed`, records one refusal of kind `preflight`, and awaits the aborted record by uuid before the replay (assert the full call list, as `:1440-1453` does); (iii) an affinity abort fails at once with a message carrying the instance's `error_message`, makes no delete, and returns no `capacity_waited`; (iv) `capacity_wait=0` with a capacity abort fails at once with one create and no delete; (v) an instance that stays in `preflight` past `await_timeout` fails with the "not placed within" message, and the fake clock shows the sleeps were bounded by the budget; (vi) `await=True` with `await_placement=True` produces the same call list as `await=True` alone. Keep the existing tests green unchanged. Run `tox -e py3 -- shakenfist.tests.test_ansible_sf_instance`. Commit subject: `collection: test sf_instance's placement await.` |
| 1c | medium | sonnet | none | **The user guide.** In `docs/user_guide/ansible.md`, add an `await_placement` row after `await` (`:285`) saying what it waits for, that it spends `await_timeout`, that `await` supersedes it, and that with `capacity_wait` it lets an unawaited create replay a preflight capacity abort. In the `capacity_wait` row (`:286`) change "(only with `await`)" to "(with `await` or `await_placement`)". Read the "Waiting for capacity" section (`:303` onwards) and correct any sentence that says a preflight abort is replayed only on an awaited create. Run `pre-commit run --all-files` (`check-doc-anchors` runs there). Commit subject: `docs: document sf_instance's await_placement.` |

### Phase 2: slim topologies await placement

Work in this repository's worktree for branch
`slim-preflight-replay`. One pull request whose body closes
#207. Before it is opened, record with the `plan-blockers` skill that
this plan waits on the phase 1 pull request merging to `develop`; do
not merge it before that (`D5`).

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| 2a | medium | sonnet | none | **The include and its documentation.** In `ansible/tasks/create-topology-node.yml`, pass `await_placement` on the create only when the installed collection has it (`D5`): a task run once per play asks `ansible-doc -t module -j shakenfist.shakenfist.sf_instance` on the controller, a `set_fact` records `sf_instance_has_await_placement` (false if `ansible-doc` fails or its output is not the expected shape), a `debug` reports the result once (a warning when false), and the create passes `await_placement: "{{ true if sf_instance_has_await_placement \| bool else omit }}"`. A node awaited only to placement gets a 300 second `await_timeout`, as placement normally takes seconds; an awaited one keeps 600. A `debug` after the count logs each node's placement lines from the module's `log`, which is otherwise printed only on failure. The header comment says why every node is awaited to placement (#207) and that the option is detected. Fix the two playbook comments that say the hypervisors "are not awaited": `ci-topology-slim-primary.yml:78-79` and `ci-topology-slim-tier.yml:19-20`. In `docs/ansible.md`, change the bullet at `:172` to cover a create awaited to boot or to placement, and replace the paragraph at `:218-219` with one that says the slim workers are awaited to placement, why, that boot is still not awaited, that unawaited nodes get a 300 second `await_timeout`, and that the option is passed only when the collection has it. Keep the budget table unchanged. Verify with `python3 -m unittest discover -s tests -t .` and `tools/ansible-syntax-check.sh ansible/ci-topology-slim-primary.yml ansible/ci-topology-slim-tier.yml`, and run the detection tasks in a throwaway playbook against a collection with and one without the option. Commit subject: `slim topologies: await placement of every node.` |
| 2b | medium | sonnet | none | **The structural test.** In `tests/test_capacity_wait.py` add `test_every_topology_node_create_awaits_placement`: the include holds one `ansible-doc` task naming `shakenfist.shakenfist.sf_instance`, a `set_fact` that computes `sf_instance_has_await_placement` from its output and looks for `'await_placement'`, and every create `creates(NODE_INCLUDE)` returns comes after it and passes a template that references the fact and `omit` and renders to `True` or to the omit placeholder. Its docstring and failure messages say why: an unawaited create cannot replay a preflight capacity abort (#207), and a literal would break every slim build on a collection without the option. Prove it fails when the option line is deleted, made a literal `true`, or the detection task is deleted. Run `python3 -m unittest tests.test_capacity_wait` and `pre-commit run --all-files`. Commit subject: `tests: every topology node create awaits placement.` |
| 2c | medium | opus | none | **Land and watch. By the management session.** Run `PUSH-AUDIT.md` over `origin/main...HEAD`, push, and open the pull request once phase 1 has merged. The canary does not exercise this change: `canary.yml` calls `smoke-cluster.yml` with no `topology` input, which defaults to `localhost`, and only the slim topologies include `create-topology-node.yml`. The first real run is the first shakenfist merge-queue or scheduled slim job after the actions merge (shakenfist's `functional-tests.yml`, `functional-lanes.yml` and `scheduled-tests.yml` run `slim-primary` and `slim-tier`). In that job's `Build infrastructure` log, confirm that the detection reported the option found and that each hypervisor's "Log how ... was awaited" line shows "was placed". If the job fails at `Build infrastructure` with `Unsupported parameters`, revert the merge at once and diagnose afterwards. Record the run id and result in this plan's Back brief, and record phase 1 and 2's merge commits in the Execution table's `Merged` column in the phase 3 commit. |

### Phase 3: push audit

Run `PUSH-AUDIT.md` with `AUDIT_RANGE` set to phase 2's merge commit
(`<sha>^1..<sha>`). Phase 1 landed in shakenfist/shakenfist and is
audited by that repository's own `PUSH-AUDIT.md` as part of its pull
request; cite that run here rather than re-running it. Findings land
as their own pull request; where there are none, say so in one
sentence.

## Agent guidance

The shared blocks in shakenfist/development's `PLAN-TEMPLATE.md`
apply: all implementation in sub-agents, review in the management
session against the review checklist, effort and model as the step
tables say.

Project-specific checks, per repository:

- shakenfist/shakenfist: `tox -e py3 -- shakenfist.tests.test_ansible_sf_instance`
  for the module's tests, `pre-commit run --all-files` for the rest
  (flake8, ansible-lint, mypy, `check-doc-anchors`, `check-plan-status`
  -- the last is untouched by this plan, which adds no plan file there).
- shakenfist/actions: `pre-commit run --all-files` (actionlint,
  shellcheck, flake8, skillsaw and the unit tests), and
  `tools/ansible-syntax-check.sh` where the collection is installed.
  `AGENTS.md` says what cannot be tested before merge: nothing here
  can exercise a slim build on a pull request, and the canary builds
  only `localhost`, so the first real run is the first shakenfist slim
  job after the merge (`2c`).

## Administration and logistics

### Success criteria

- A slim hypervisor node that preflight aborts for capacity is
  replayed under the run's `capacity_wait_budget`, and the module's
  log says so.
- A slim hypervisor node that preflight aborts for any other reason
  fails the build within seconds, with the server's message, rather
  than after a 300 second SSH banner wait.
- Slim builds that are not refused take no longer than before, to
  within the few seconds placement takes. The hypervisors' boots still
  overlap.
- An `sf_instance` task that sets neither `await` nor
  `await_placement`, or that sets `await: true`, behaves exactly as
  it does today: same API calls, same messages.
- `tests/test_capacity_wait.py` fails if `await_placement` is removed
  from `create-topology-node.yml`, made a literal, or no longer
  detected.
- The first shakenfist slim job after the actions merge reports the
  option detected, and each hypervisor create logs "was placed".
- A slim build whose shakenfist checkout lacks `await_placement` still
  builds, logging the warning.
- `docs/ansible.md` here and `docs/user_guide/ansible.md` in
  shakenfist describe the behaviour; `AGENTS.md`, `ARCHITECTURE.md`
  and `README.md` are unchanged, because no convention and no
  component changed.

### Definition of done

1. Phase 1 merged to shakenfist `develop`.
2. Phase 2 merged to actions `main`, closing #207, with the first
   shakenfist slim job after it reporting the option detected and
   each hypervisor create logging "was placed".
3. The `Merged` column filled for phases 1 and 2.
4. Phase 3's audit recorded, with findings resolved or declined.
5. Seven days after the actions merge, `tools/measure-placement-refusals.py`
   in private-ci (with `--outcomes`) shows no `preflight`
   `unreplayed` refusal on a slim hypervisor. Record the reading on
   #207 before closing it, if it is still open, and in this plan.

### Future work

- client-python `create_instance()`'s blocking wait returns once the
  state is not `initial` or `creating` (`apiclient.py:803`). The list
  predates the `preflight` state, so every blocking caller gets an
  instance back before the scheduler has judged it. Adding
  `preflight` would fix the class of problem for every caller, but
  changes the duration of every blocking create in the fleet. File it
  as a client-python issue rather than folding it into this plan.
- `sf_instance`'s `_check_instance()` never reads the existing
  record's `state`. `state: present` against a name whose only record
  is in state `error` with a matching spec adopts the dead instance
  and exits `changed: false`. File it as a shakenfist issue; the
  module's `_await_record_gone()` docstring already notes the awaited
  variant of this.
- shakenfist/shakenfist#4568 asks preflight to record its stage and
  a transient flag on the instance. When it lands, the module's
  message matching can give way to it.
- The `sf_instance` test harness's `_Succeeded` and `_Failed` outcome
  exceptions subclass `SystemExit`. When a test expects one and the
  module raises the other, it escapes `assertRaises` and kills its
  stestr worker, so that worker's remaining tests drop out of the
  totals; `python -m testtools.run` on a single test then exits 0.
  tox still fails, so nothing is hidden in CI. Found while
  mutation-testing step 1b; it predates this plan. File it as a
  shakenfist issue.
- `ci-topology-slim-primary-released.yml`, `ci-topology-localhost-released.yml`
  and `ci-topology-localhost-upgrade.yml` still pass no
  `capacity_wait`. No workflow runs them.

### Bugs fixed during this work

- shakenfist/actions#207, this plan's subject.

Related, not fixed here: shakenfist/shakenfist#3718 (the SSH banner
timeout this abort masquerades as; it carries a comment on telling
the two apart), private-ci#153 (a wait that ran out of budget, which
is sizing, not a missing retry).

### Back brief

Before executing any step of this plan, back brief the operator on
your understanding of the plan and how the work you intend to do
aligns with it. Record here, as the phases land: the shakenfist and
actions pull request numbers, the first shakenfist slim job after the
actions merge and its result, and the seven-day reading.

- Phase 1 is open as shakenfist/shakenfist#4746.
