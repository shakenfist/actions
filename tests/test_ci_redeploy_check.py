#!/usr/bin/env python3

"""Tests for the second-deploy restart check and the profile's test environment.

tools/ci-redeploy-check.py decides whether a second deploy restarted a
daemon. The failure worth pinning is a check that passes without having
looked: a restart read as unchanged, a unit that disappeared read as
fine, or a snapshot of nothing compared against a snapshot of nothing.
tools/ci-deploy-cluster.sh runs both deploys from one command line, and
must never deploy twice for a caller that did not ask.
tools/ci-ship-test-env.sh puts a profile's test_env into the suite's
environment, and must add nothing to a run without a profile.
"""

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

import yaml

from tests.helpers import REPO_ROOT, load_script


redeploy = load_script('tools/ci-redeploy-check.py', 'ci_redeploy_check')
inventory_generator = load_script('tools/ci-make-inventory.py', 'ci_make_inventory_for_redeploy')
profiles = load_script('tools/ci-apply-deploy-profile.py', 'ci_apply_deploy_profile_for_redeploy')

DEPLOY_CLUSTER = os.path.join(REPO_ROOT, 'tools', 'ci-deploy-cluster.sh')
SHIP_TEST_ENV = os.path.join(REPO_ROOT, 'tools', 'ci-ship-test-env.sh')
ACTION = os.path.join(REPO_ROOT, 'build-smoke-cluster', 'action.yml')
WORKFLOW = os.path.join(REPO_ROOT, '.github', 'workflows', 'smoke-cluster.yml')

GLOBS = ['sf-*.service', 'kerbside-*.service']

BEFORE = {
    ('primary', 'sf-api.service'): 'a1',
    ('primary', 'sf-database.service'): 'a2',
    ('sf2', 'kerbside-api.service'): 'b1',
    ('sf2', 'sf-agent.service'): 'b2',
}


def state_with(**changes):
    """Return a copy of BEFORE with (host, unit) keys changed, added or removed (None)."""
    state = dict(BEFORE)
    for key, value in changes.items():
        host, unit = key.split('__')
        if value is None:
            del state[(host, unit)]
        else:
            state[(host, unit)] = value
    return state


def slim_tier_hosts():
    """The hosts of a real slim-tier inventory, with a profile group naming sf2 again."""
    nodes = []
    for name, egress, mesh in (('primary', '10.0.0.10', '10.0.1.10'), ('sf1', '10.0.0.20', '10.0.1.11'),
                               ('sf2', '10.0.0.21', '10.0.1.12')):
        nodes.append({'name': name, 'egress_ip': egress, 'egress_nic': 'eth0', 'mesh_ip': mesh,
                      'mesh_nic': 'eth1', 'ssh_user': 'debian', 'ssh_key': '/tmp/ci_id_key',
                      'is_hypervisor': True, 'is_network_node': name == 'primary',
                      'is_database_node': name != 'sf2'})
    inventory = yaml.safe_load(inventory_generator.render_inventory(nodes))
    profiles.merge_groups(inventory, {'kerbside': {'hosts': ['sf2'], 'vars': {'api_url': 'x'}}})
    return redeploy.inventory_hosts(inventory)


def fake_runner(per_host, unreachable=(), failing=()):
    """A runner answering REMOTE_SCRIPT's output from {address: {unit: id}}."""
    def runner(host_vars, globs):
        address = host_vars['ansible_host']
        if address in unreachable:
            return redeploy.SSH_UNREACHABLE, ''
        if address in failing:
            return 1, ''
        lines = []
        for unit, invocation in sorted(per_host.get(address, {}).items()):
            glob = [g for g in globs if g.split('-')[0] == unit.split('-')[0]]
            if glob:
                lines.append('%s %s %s' % (glob[0], unit, invocation))
        return 0, ''.join('%s\n' % line for line in lines)
    return runner


SLIM_TIER_UNITS = {
    '10.0.0.10': {'sf-api.service': 'p1', 'sf-database.service': 'p2'},
    '10.0.0.20': {'sf-database.service': 's1'},
    '10.0.0.21': {'sf-agent.service': 'k1', 'kerbside-api.service': 'k2'},
}


class CompareTest(unittest.TestCase):
    def test_identical_snapshots_pass(self):
        verdicts = redeploy.compare(BEFORE, dict(BEFORE))
        self.assertFalse(redeploy.failed(verdicts))
        self.assertEqual(verdicts['unchanged'], sorted(BEFORE))
        self.assertEqual(verdicts['changed'] + verdicts['vanished'] + verdicts['new'], [])

    def test_a_changed_invocation_id_fails(self):
        verdicts = redeploy.compare(BEFORE, state_with(**{'sf2__kerbside-api.service': 'b9'}))
        self.assertTrue(redeploy.failed(verdicts))
        self.assertEqual(verdicts['changed'], [('sf2', 'kerbside-api.service')])
        self.assertEqual(len(verdicts['unchanged']), 3)

    def test_a_vanished_unit_fails(self):
        verdicts = redeploy.compare(BEFORE, state_with(**{'primary__sf-database.service': None}))
        self.assertTrue(redeploy.failed(verdicts))
        self.assertEqual(verdicts['vanished'], [('primary', 'sf-database.service')])
        self.assertEqual(verdicts['changed'], [])

    def test_a_new_unit_is_reported_but_passes(self):
        verdicts = redeploy.compare(BEFORE, state_with(**{'sf1__sf-agent.service': 'c1'}))
        self.assertFalse(redeploy.failed(verdicts))
        self.assertEqual(verdicts['new'], [('sf1', 'sf-agent.service')])

    def test_a_unit_that_starts_for_the_first_time_counts_as_changed(self):
        before = state_with(**{'sf2__kerbside-sync.service': '-'})
        after = state_with(**{'sf2__kerbside-sync.service': 'd1'})
        self.assertEqual(redeploy.compare(before, after)['changed'], [('sf2', 'kerbside-sync.service')])

    def test_the_report_names_every_unit_and_annotates_failures(self):
        after = state_with(**{'sf2__kerbside-api.service': 'b9', 'primary__sf-database.service': None,
                              'sf1__sf-agent.service': 'c1'})
        lines = redeploy.report(redeploy.compare(BEFORE, after), BEFORE, after)
        text = '\n'.join(lines)
        self.assertIn('unchanged  primary sf-api.service', text)
        self.assertIn('new        sf1 sf-agent.service', text)
        self.assertIn('::error title=Redeploy restarted a unit::kerbside-api.service on sf2', text)
        self.assertIn('b1 became b9', text)
        self.assertIn('::error title=Redeploy lost a unit::sf-database.service on primary', text)
        self.assertEqual(lines[-1], '2 unchanged, 1 new, 1 restarted, 1 vanished.')


class StateFileTest(unittest.TestCase):
    def test_the_state_file_round_trips(self):
        text = redeploy.format_state(BEFORE)
        self.assertEqual(text.splitlines()[0], 'primary sf-api.service a1')
        self.assertEqual(redeploy.parse_state(text), BEFORE)

    def test_a_malformed_state_file_is_refused(self):
        with self.assertRaises(redeploy.CheckError):
            redeploy.parse_state('primary sf-api.service\n')


class InventoryTest(unittest.TestCase):
    def test_every_host_is_found_once_with_its_connection_vars(self):
        hosts = dict(slim_tier_hosts())
        self.assertEqual(sorted(hosts), ['primary', 'sf1', 'sf2'])
        # The profile's bare membership of sf2 does not lose allsf's vars.
        self.assertEqual(hosts['sf2']['ansible_host'], '10.0.0.21')
        self.assertEqual(hosts['sf2']['ansible_user'], 'debian')

    def test_the_ssh_command_is_the_one_the_deploy_used(self):
        argv = redeploy.ssh_command(dict(slim_tier_hosts())['sf2'], GLOBS)
        self.assertEqual(argv[:3], ['ssh', '-i', '/tmp/ci_id_key'])
        self.assertIn('StrictHostKeyChecking=no', argv)
        self.assertIn('UserKnownHostsFile=/dev/null', argv)
        self.assertEqual(argv[-2], 'debian@10.0.0.21')
        # Globs reach the remote shell quoted, so it cannot expand them.
        self.assertEqual(argv[-1], "bash -s -- 'sf-*.service' 'kerbside-*.service'")

    def test_a_host_without_an_address_is_refused(self):
        with self.assertRaises(redeploy.CheckError):
            redeploy.ssh_command({'ansible_user': 'debian', 'ansible_ssh_private_key_file': 'k'}, GLOBS)


class QuietTestCase(unittest.TestCase):
    """Capture what the tool prints, so a test can read it and the run stays quiet."""

    def setUp(self):
        super().setUp()
        self.output = io.StringIO()
        quiet = contextlib.redirect_stdout(self.output)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)


class SnapshotTest(QuietTestCase):
    def test_units_are_recorded_on_every_host(self):
        state = redeploy.snapshot(slim_tier_hosts(), GLOBS, runner=fake_runner(SLIM_TIER_UNITS))
        self.assertEqual(state[('sf2', 'kerbside-api.service')], 'k2')
        self.assertEqual(state[('sf1', 'sf-database.service')], 's1')
        self.assertEqual(len(state), 5)

    def test_zero_units_overall_is_refused(self):
        with self.assertRaisesRegex(redeploy.CheckError, 'prove nothing'):
            redeploy.snapshot(slim_tier_hosts(), GLOBS, runner=fake_runner({}))

    def test_a_glob_matching_nothing_is_refused_even_when_another_matches(self):
        units = {address: {u: i for u, i in units.items() if not u.startswith('kerbside')}
                 for address, units in SLIM_TIER_UNITS.items()}
        with self.assertRaisesRegex(redeploy.CheckError, r'kerbside-\*\.service'):
            redeploy.snapshot(slim_tier_hosts(), GLOBS, runner=fake_runner(units))

    def test_a_host_that_does_not_answer_is_skipped(self):
        state = redeploy.snapshot(slim_tier_hosts(), ['sf-*.service'],
                                  runner=fake_runner(SLIM_TIER_UNITS, unreachable=('10.0.0.20',)))
        self.assertNotIn('sf1', {host for host, _ in state})
        self.assertIn(('primary', 'sf-api.service'), state)
        self.assertIn('::warning title=Redeploy check skipped a host::sf1', self.output.getvalue())

    def test_no_host_answering_is_refused(self):
        with self.assertRaises(redeploy.CheckError):
            redeploy.snapshot(slim_tier_hosts(), GLOBS, runner=fake_runner(
                SLIM_TIER_UNITS, unreachable=('10.0.0.10', '10.0.0.20', '10.0.0.21')))

    def test_a_host_that_answers_and_fails_is_an_error(self):
        with self.assertRaisesRegex(redeploy.CheckError, 'sf1'):
            redeploy.snapshot(slim_tier_hosts(), GLOBS, runner=fake_runner(SLIM_TIER_UNITS, failing=('10.0.0.20',)))

    def test_a_glob_with_shell_syntax_is_refused(self):
        with self.assertRaises(redeploy.CheckError):
            redeploy.snapshot(slim_tier_hosts(), ['sf-*; reboot'], runner=fake_runner(SLIM_TIER_UNITS))

    def test_unexpected_remote_output_is_refused(self):
        def runner(host_vars, globs):
            return 0, 'garbage\n'
        with self.assertRaises(redeploy.CheckError):
            redeploy.snapshot(slim_tier_hosts(), GLOBS, runner=runner)


class RemoteScriptTest(unittest.TestCase):
    """Run the script each host runs, against a stub systemctl."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        systemctl = os.path.join(self.tmp, 'systemctl')
        with open(systemctl, 'w') as f:
            f.write('''#!/bin/bash
if [ "$1" = "list-units" ]; then
    for arg in "$@"; do glob="${arg}"; done
    case "${glob}" in
        sf-*)
            echo "sf-api.service loaded active running Shaken Fist API"
            echo "sf-old.service not-found inactive dead sf-old.service"
            echo "sf-oneshot.service loaded inactive dead Shaken Fist oneshot"
            ;;
    esac
    exit 0
fi
for arg in "$@"; do unit="${arg}"; done
case "${unit}" in
    sf-api.service) echo "0123abcd" ;;
    *) echo "" ;;
esac
''')
        os.chmod(systemctl, 0o755)
        self.environment = dict(os.environ, PATH='%s:%s' % (self.tmp, os.environ['PATH']))

    def run_remote(self, *globs):
        result = subprocess.run(['bash', '-s', '--'] + list(globs), input=redeploy.REMOTE_SCRIPT,
                                capture_output=True, text=True, env=self.environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_loaded_units_are_listed_with_their_invocation_ids(self):
        output = self.run_remote('sf-*.service', 'kerbside-*.service')
        self.assertEqual(output, 'sf-*.service sf-api.service 0123abcd\n'
                                 'sf-*.service sf-oneshot.service -\n')
        units, counts = redeploy.parse_remote_output('primary', output, ['sf-*.service', 'kerbside-*.service'])
        self.assertEqual(units, {'sf-api.service': '0123abcd', 'sf-oneshot.service': '-'})
        self.assertEqual(counts, {'sf-*.service': 2, 'kerbside-*.service': 0})


class CommandLineTest(QuietTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.inventory = os.path.join(self.tmp, 'ci-inventory.yaml')
        with open(self.inventory, 'w') as f:
            yaml.safe_dump({'all': {'children': {'allsf': {'hosts': dict(slim_tier_hosts())}}}}, f)
        self.state = os.path.join(self.tmp, 'state')

    def main(self, mode, units):
        return redeploy.main([mode, self.inventory, self.state] + GLOBS, runner=fake_runner(units))

    def test_an_unchanged_redeploy_passes(self):
        self.assertEqual(self.main('snapshot', SLIM_TIER_UNITS), 0)
        self.assertEqual(len(redeploy.parse_state(open(self.state).read())), 5)
        self.assertEqual(self.main('compare', SLIM_TIER_UNITS), 0)

    def test_a_restart_fails(self):
        self.assertEqual(self.main('snapshot', SLIM_TIER_UNITS), 0)
        after = dict(SLIM_TIER_UNITS, **{'10.0.0.21': {'sf-agent.service': 'k1', 'kerbside-api.service': 'k9'}})
        self.assertEqual(self.main('compare', after), 1)
        self.assertIn('kerbside-api.service on sf2 restarted', self.output.getvalue())

    def test_a_glob_matching_nothing_after_the_redeploy_is_a_vanishing_not_a_refusal(self):
        self.assertEqual(self.main('snapshot', SLIM_TIER_UNITS), 0)
        after = dict(SLIM_TIER_UNITS, **{'10.0.0.21': {'sf-agent.service': 'k1'}})
        self.assertEqual(self.main('compare', after), 1)

    def test_a_vacuous_snapshot_writes_no_state(self):
        self.assertEqual(self.main('snapshot', {}), 2)
        self.assertFalse(os.path.exists(self.state))

    def test_compare_refuses_an_empty_state_file(self):
        open(self.state, 'w').close()
        self.assertEqual(self.main('compare', SLIM_TIER_UNITS), 2)


class ScriptCopyTestCase(unittest.TestCase):
    """Run a copy of a tools/ shell script with /srv/github moved into a temporary directory."""

    SCRIPT = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.state_dir = os.path.join(self.tmp, 'srv-github')
        os.makedirs(self.state_dir)
        self.tools = os.path.join(self.tmp, 'workspace', 'actions', 'tools')
        os.makedirs(self.tools)
        self.stubs = os.path.join(self.tmp, 'stubs')
        os.makedirs(self.stubs)
        self.log = os.path.join(self.tmp, 'calls')

        with open(self.SCRIPT) as f:
            script = f.read()
        self.assertIn('/srv/github', script)
        self.script = os.path.join(self.tmp, os.path.basename(self.SCRIPT))
        with open(self.script, 'w') as f:
            f.write(script.replace('/srv/github', self.state_dir))

        self.environment = dict(os.environ)
        self.environment['PATH'] = '%s:%s' % (self.stubs, os.environ['PATH'])
        self.environment['GITHUB_WORKSPACE'] = os.path.join(self.tmp, 'workspace')
        self.environment.pop('DEPLOY_PROFILE', None)

    def recording_stub(self, path, label, extra=''):
        """A stub that appends [label, argv...] as a JSON line to the call log."""
        with open(path, 'w') as f:
            f.write('#!/bin/bash\npython3 -c \'import json, sys; print(json.dumps(sys.argv[1:]))\' %s "$@" >> %s\n%s\n'
                    % (label, self.log, extra))
        os.chmod(path, 0o755)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def state_path(self, name):
        return os.path.join(self.state_dir, name)


class DeployClusterTest(ScriptCopyTestCase):
    SCRIPT = DEPLOY_CLUSTER

    def setUp(self):
        super().setUp()
        self.recording_stub(os.path.join(self.tools, 'deploy-collection.sh'), 'deploy',
                            'exit "${DEPLOY_EXIT:-0}"')
        # The wrapper runs the check with python3, so the stub is a Python file.
        with open(os.path.join(self.tools, 'ci-redeploy-check.py'), 'w') as f:
            f.write('import json, os, sys\n'
                    'open(%r, "a").write(json.dumps(["check"] + sys.argv[1:]) + "\\n")\n'
                    'sys.exit(int(os.environ.get("COMPARE_EXIT", "0")) if sys.argv[1] == "compare" else 0)\n'
                    % self.log)
        self.environment.update({'MARIADB_PASSWORD': 'pw', 'AUTH_SECRET': 'seed', 'SYSTEM_KEY': 'key'})
        self.inventory = self.state_path('ci-inventory.yaml')
        self.extra_vars = self.state_path(profiles.EXTRA_VARS_FILE)
        self.units_file = self.state_path(profiles.REDEPLOY_UNITS_FILE)
        self.check_state = self.state_path('ci-redeploy-check-state')

    def run_wrapper(self, topology='localhost', **environment):
        env = dict(self.environment, **environment)
        return subprocess.run(['bash', self.script, topology], env=env, capture_output=True, text=True)

    def write_units(self):
        with open(self.units_file, 'w') as f:
            f.write(''.join('%s\n' % glob for glob in GLOBS))

    def test_without_a_profile_there_is_one_deploy_with_the_old_arguments(self):
        # A stale units file from an earlier job on this runner must not count.
        self.write_units()
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [['deploy', self.inventory, 'pw', 'seed', 'key', 'http://127.0.0.1:3100',
                                         '127.0.0.1']])
        self.assertIn('The first deploy took', result.stdout)

    def test_a_multi_node_topology_uses_the_primary_mesh_address(self):
        with open(self.state_path('ci-topology-facts.json'), 'w') as f:
            json.dump({'nodes': [{'name': 'sf1', 'mesh_ip': '10.0.1.11'}, {'name': 'primary', 'mesh_ip': '10.0.1.10'}]},
                      f)
        result = self.run_wrapper('slim-tier')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[0][5:], ['http://10.0.1.10:3100', '10.0.1.10'])

    def test_a_profile_without_a_redeploy_check_deploys_once_with_its_extra_vars(self):
        result = self.run_wrapper(DEPLOY_PROFILE='p.yml.j2')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [['deploy', self.inventory, 'pw', 'seed', 'key', 'http://127.0.0.1:3100',
                                         '127.0.0.1', self.extra_vars]])

    def test_a_redeploy_check_snapshots_deploys_identically_and_compares(self):
        self.write_units()
        result = self.run_wrapper(DEPLOY_PROFILE='p.yml.j2')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual([call[0] for call in calls], ['deploy', 'check', 'deploy', 'check'])
        self.assertEqual(calls[0], calls[2])
        self.assertEqual(calls[1], ['check', 'snapshot', self.inventory, self.check_state] + GLOBS)
        self.assertEqual(calls[3], ['check', 'compare', self.inventory, self.check_state] + GLOBS)
        self.assertIn('The first deploy took', result.stdout)
        self.assertIn('The second deploy took', result.stdout)

    def test_a_restart_fails_the_deploy(self):
        self.write_units()
        result = self.run_wrapper(DEPLOY_PROFILE='p.yml.j2', COMPARE_EXIT='1')
        self.assertNotEqual(result.returncode, 0)

    def test_a_failed_first_deploy_stops_before_the_check(self):
        self.write_units()
        result = self.run_wrapper(DEPLOY_PROFILE='p.yml.j2', DEPLOY_EXIT='3')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([call[0] for call in self.calls()], ['deploy'])
        self.assertIn('The first deploy took', result.stdout)
        self.assertIn('exit status 3', result.stdout)


class ShipTestEnvTest(ScriptCopyTestCase):
    SCRIPT = SHIP_TEST_ENV

    def setUp(self):
        super().setUp()
        self.recording_stub(os.path.join(self.stubs, 'scp'), 'scp')
        self.test_env = self.state_path(profiles.TEST_ENV_FILE)

    def run_ship(self, **environment):
        env = dict(self.environment, **environment)
        return subprocess.run(['bash', self.script, 'debian', '10.0.0.10'], env=env, capture_output=True, text=True)

    def write_test_env(self, text):
        with open(self.test_env, 'w') as f:
            f.write(text)

    def test_without_a_profile_nothing_is_shipped_or_sourced(self):
        self.write_test_env("export STALE='1'\n")
        result = self.run_ship()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(self.calls(), [])

    def test_an_empty_test_env_ships_nothing(self):
        self.write_test_env('')
        result = self.run_ship(DEPLOY_PROFILE='p.yml.j2')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(self.calls(), [])

    def test_a_test_env_is_shipped_and_sourced_after_sfrc(self):
        self.write_test_env(profiles.render_test_env({'SF_CI_EXPECT_VDI_CONSOLE_PROXY': '1', 'OTHER': 'a b'}))
        result = self.run_ship(DEPLOY_PROFILE='p.yml.j2')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '. ~/ci-deploy-profile-test-env.sh;\n')
        self.assertEqual(self.calls()[0][-2:], [self.test_env, 'debian@10.0.0.10:ci-deploy-profile-test-env.sh'])
        self.assertIn('-p', self.calls()[0])
        self.assertIn('SF_CI_EXPECT_VDI_CONSOLE_PROXY OTHER', result.stderr)
        self.assertNotIn('a b', result.stderr)

    def test_the_fragment_sources_the_values(self):
        self.write_test_env(profiles.render_test_env({'SF_CI_EXPECT_VDI_CONSOLE_PROXY': '1', 'OTHER': "it's"}))
        fragment = self.run_ship(DEPLOY_PROFILE='p.yml.j2').stdout
        home = os.path.join(self.tmp, 'home')
        os.makedirs(home)
        shutil.copy(self.test_env, os.path.join(home, 'ci-deploy-profile-test-env.sh'))
        result = subprocess.run(['bash', '-c', 'cd /; %s echo "${SF_CI_EXPECT_VDI_CONSOLE_PROXY}|${OTHER}"' % fragment],
                                env=dict(self.environment, HOME=home), capture_output=True, text=True)
        self.assertEqual(result.stdout, "1|it's\n")


class WiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(ACTION) as f:
            cls.action = yaml.safe_load(f)
        cls.action_steps = {step['name']: step for step in cls.action['runs']['steps']}
        with open(WORKFLOW) as f:
            cls.workflow = yaml.safe_load(f)
        cls.workflow_steps = cls.workflow['jobs']['smoke_cluster']['steps']

    def test_the_scripts_are_executable(self):
        for path in (DEPLOY_CLUSTER, SHIP_TEST_ENV, os.path.join(REPO_ROOT, 'tools', 'ci-redeploy-check.py')):
            self.assertTrue(os.stat(path).st_mode & stat.S_IXUSR, path)

    def test_the_deploy_step_runs_the_wrapper_with_its_inputs_in_the_environment(self):
        step = self.action_steps['Deploy Shaken Fist via the collection']
        self.assertIn('tools/ci-deploy-cluster.sh "${TOPOLOGY}"', step['run'])
        self.assertEqual(step['env']['TOPOLOGY'], '${{ inputs.topology }}')
        self.assertEqual(step['env']['DEPLOY_PROFILE'], '${{ inputs.deploy_profile }}')
        self.assertNotIn('${{', step['run'])
        self.assertNotIn('set -x', step['run'])

    def test_the_wrapper_redeploys_only_for_a_profile_that_asks(self):
        with open(DEPLOY_CLUSTER) as f:
            wrapper = f.read()
        self.assertIn('REDEPLOY_UNITS_FILE="${STATE_DIR}/%s"' % profiles.REDEPLOY_UNITS_FILE, wrapper)
        self.assertIn('if [ -z "${DEPLOY_PROFILE}" ] || [ ! -f "${REDEPLOY_UNITS_FILE}" ]; then', wrapper)

    def test_the_redeploy_comes_before_anything_that_restarts_sf_api(self):
        # Inside the action, the deploy step is followed by nothing that
        # restarts a daemon; in the workflow the action runs before the JWKS
        # and drain steps, which restart sf-api on purpose.
        names = [step.get('name') for step in self.workflow_steps]
        build = names.index('Build the smoke cluster')
        self.assertLess(build, names.index('Trust a throwaway JWKS certificate authority'))
        self.assertLess(build, names.index('Check sf-api drain'))

    def test_both_suites_source_the_test_env_after_sfrc(self):
        steps = {step.get('name'): step for step in self.workflow_steps}
        for name in ('Run functional tests', 'Run ansible module tests'):
            step = steps[name]
            self.assertEqual(step['env']['DEPLOY_PROFILE'], '${{ inputs.deploy_profile }}')
            run = step['run']
            ship = run.index('tools/ci-ship-test-env.sh ${{ inputs.base_image_user }} ${primary}')
            self.assertLess(run.index('. /etc/sf/sfrc;'), ship, name)


if __name__ == '__main__':
    unittest.main()
