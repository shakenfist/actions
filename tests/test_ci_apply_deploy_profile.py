#!/usr/bin/env python3

"""Tests for deploy profiles: tools/ci-apply-deploy-profile.py and its wiring.

A deploy profile is content from another repository that this one renders,
merges into the inventory every CI deploy is driven from, and turns into
credentials on disk and SQL run as root on the primary. The failure worth
pinning is the quiet one: a profile naming a node that is not there, or a
key nobody reads, rendering into a valid deploy that lacks what the caller
asked for. The rest of this file holds the guarantee every consumer at
@main relies on -- that without a profile, nothing about the deploy
changes.
"""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

import jinja2  # noqa: F401 -- a hard dependency, as PyYAML is; see docs/ci.md
import yaml

from tests.helpers import REPO_ROOT, load_script


profiles = load_script('tools/ci-apply-deploy-profile.py', 'ci_apply_deploy_profile')
inventory_generator = load_script('tools/ci-make-inventory.py', 'ci_make_inventory_for_profiles')

SCRIPT = os.path.join(REPO_ROOT, 'tools', 'ci-apply-deploy-profile.py')
DEPLOY_COLLECTION = os.path.join(REPO_ROOT, 'tools', 'deploy-collection.sh')
ACTION = os.path.join(REPO_ROOT, 'build-smoke-cluster', 'action.yml')
WORKFLOW = os.path.join(REPO_ROOT, '.github', 'workflows', 'smoke-cluster.yml')

# The shape ansible/ci-include-common-localhost.yml writes for slim-tier.
SLIM_TIER_FACTS = {
    'nodes': [
        {'name': 'primary', 'egress_ip': '10.0.0.10', 'mesh_ip': '10.0.1.10',
         'is_hypervisor': True, 'is_network_node': True, 'is_database_node': True},
        {'name': 'sf1', 'egress_ip': '10.0.0.20', 'mesh_ip': '10.0.1.11',
         'is_hypervisor': True, 'is_network_node': False, 'is_database_node': True},
        {'name': 'sf2', 'egress_ip': '10.0.0.21', 'mesh_ip': '10.0.1.12',
         'is_hypervisor': True, 'is_network_node': False, 'is_database_node': False},
    ]
}

KERBSIDE_PROFILE = """\
# A profile shaped like the one D6 of the kerbside deployer plan describes.
groups:
  kerbside:
    hosts: [sf2]
    vars:
      api_url: http://{{ nodes.primary.mesh_ip }}:13000
extra_vars:
  kerbside_url: http://{{ nodes.sf2.mesh_ip }}:13002
  kerbside_public_fqdn: '{{ nodes.sf2.mesh_ip }}'
  kerbside_sql_url: mysql://kerbside:ci-password@{{ nodes.primary.mesh_ip }}/kerbside
mariadb_sql: |
  CREATE DATABASE IF NOT EXISTS kerbside;
redeploy_check:
  units: ['sf-*.service', 'kerbside-*.service']
test_env:
  SF_CI_EXPECT_VDI_CONSOLE_PROXY: '1'
  SF_CI_QUOTED: "it's spaced"
"""

OUTPUT_FILES = (profiles.PROFILE_FILE, profiles.EXTRA_VARS_FILE, profiles.SQL_FILE, profiles.TEST_ENV_FILE,
                profiles.REDEPLOY_UNITS_FILE)


class ProfileTestCase(unittest.TestCase):
    """Each test gets a directory holding a real generated inventory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        nodes = [inventory_generator.build_node(spec, 'debian', '/tmp/ci_id_key')
                 for spec in SLIM_TIER_FACTS['nodes']]
        self.inventory = os.path.join(self.tmp, 'ci-inventory.yaml')
        with open(self.inventory, 'w') as f:
            f.write(inventory_generator.render_inventory(nodes))
        with open(self.inventory) as f:
            self.original_inventory = f.read()

    def write_profile(self, text):
        path = os.path.join(self.tmp, 'profile.yml.j2')
        with open(path, 'w') as f:
            f.write(text)
        return path

    def apply(self, text, workspace='/srv/ws'):
        return profiles.apply(self.write_profile(text), SLIM_TIER_FACTS, self.inventory, workspace=workspace)

    def output(self, name):
        with open(os.path.join(self.tmp, name)) as f:
            return f.read()

    def assertNothingWritten(self):
        with open(self.inventory) as f:
            self.assertEqual(f.read(), self.original_inventory)
        for name in OUTPUT_FILES:
            self.assertFalse(os.path.exists(os.path.join(self.tmp, name)), '%s was written' % name)

    def assertRefused(self, text, fragment):
        with self.assertRaises(profiles.ProfileError) as cm:
            self.apply(text)
        self.assertIn(fragment, str(cm.exception))
        self.assertNothingWritten()
        return cm.exception


class RenderTest(ProfileTestCase):
    def test_profile_renders_against_the_facts(self):
        profile = self.apply(KERBSIDE_PROFILE)
        self.assertEqual(profile['groups']['kerbside']['vars']['api_url'], 'http://10.0.1.10:13000')
        self.assertEqual(profile['extra_vars']['kerbside_url'], 'http://10.0.1.12:13002')
        self.assertEqual(profile['extra_vars']['kerbside_public_fqdn'], '10.0.1.12')

    def test_workspace_is_available_to_the_template(self):
        profile = self.apply('extra_vars:\n  wheel: "{{ workspace }}/kerbside/dist"\n')
        self.assertEqual(profile['extra_vars']['wheel'], '/srv/ws/kerbside/dist')

    def test_a_null_mesh_ip_falls_back_to_the_egress_ip(self):
        # The single-node topologies may leave mesh_ip null; the inventory
        # generator fills it in from the egress address, and so must this.
        context = profiles.facts_context({'nodes': [
            {'name': 'primary', 'egress_ip': '10.0.0.5', 'mesh_ip': None, 'is_hypervisor': True,
             'is_network_node': True, 'is_database_node': True}]})
        self.assertEqual(context['nodes']['primary']['mesh_ip'], '10.0.0.5')

    def test_the_documented_fields_are_the_fields_the_facts_carry(self):
        # The docstring is the reference a profile is written against.
        node = profiles.facts_context(SLIM_TIER_FACTS)['nodes']['sf2']
        for field in node:
            self.assertIn('  %s ' % field, profiles.__doc__, '%s is not documented' % field)


class StrictUndefinedTest(ProfileTestCase):
    def test_an_unknown_node_fails(self):
        self.assertRefused('extra_vars:\n  x: "{{ nodes.sf9.mesh_ip }}"\n', 'sf9')

    def test_an_unknown_field_fails(self):
        self.assertRefused('extra_vars:\n  x: "{{ nodes.sf2.mesh_address }}"\n', 'mesh_address')

    def test_an_unknown_top_level_name_fails(self):
        self.assertRefused('extra_vars:\n  x: "{{ primary_mesh_ip }}"\n', 'primary_mesh_ip')

    def test_workspace_is_undefined_when_unknown(self):
        with self.assertRaises(profiles.ProfileError):
            self.apply('extra_vars:\n  x: "{{ workspace }}"\n', workspace=None)
        self.assertNothingWritten()

    def test_a_template_syntax_error_fails(self):
        self.assertRefused('extra_vars:\n  x: "{{ nodes.sf2.mesh_ip "\n', 'syntax error')


class MergeTest(ProfileTestCase):
    def test_groups_are_added_and_existing_groups_are_untouched(self):
        self.apply(KERBSIDE_PROFILE)
        original = yaml.safe_load(self.original_inventory)['all']['children']
        merged = yaml.safe_load(self.output('ci-inventory.yaml'))['all']['children']

        self.assertEqual(list(merged)[:len(original)], list(original))
        for name, body in original.items():
            self.assertEqual(merged[name], body, '%s changed' % name)
        self.assertEqual(merged['kerbside'], {
            'hosts': {'sf2': None},
            'vars': {'api_url': 'http://10.0.1.10:13000'},
        })

    def test_a_group_without_vars_carries_no_vars(self):
        self.apply('groups:\n  extra:\n    hosts: [primary, sf1]\n')
        merged = yaml.safe_load(self.output('ci-inventory.yaml'))['all']['children']
        self.assertEqual(merged['extra'], {'hosts': {'primary': None, 'sf1': None}})

    def test_outputs_are_written_private(self):
        self.apply(KERBSIDE_PROFILE)
        for name in OUTPUT_FILES + ('ci-inventory.yaml',):
            with self.subTest(name=name):
                mode = stat.S_IMODE(os.stat(os.path.join(self.tmp, name)).st_mode)
                self.assertEqual(mode, 0o600)

    def test_an_existing_output_file_is_made_private(self):
        path = os.path.join(self.tmp, profiles.EXTRA_VARS_FILE)
        with open(path, 'w') as f:
            f.write('stale')
        os.chmod(path, 0o644)
        self.apply(KERBSIDE_PROFILE)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_output_contents(self):
        self.apply(KERBSIDE_PROFILE)
        extra_vars = json.loads(self.output(profiles.EXTRA_VARS_FILE))
        self.assertEqual(extra_vars['kerbside_sql_url'], 'mysql://kerbside:ci-password@10.0.1.10/kerbside')
        self.assertEqual(self.output(profiles.SQL_FILE), 'CREATE DATABASE IF NOT EXISTS kerbside;\n')
        self.assertEqual(self.output(profiles.REDEPLOY_UNITS_FILE), 'sf-*.service\nkerbside-*.service\n')

        rendered = json.loads(self.output(profiles.PROFILE_FILE))
        self.assertEqual(sorted(rendered), sorted(profiles.TOP_LEVEL_KEYS))
        self.assertEqual(rendered['redeploy_check'], {'units': ['sf-*.service', 'kerbside-*.service']})
        self.assertEqual(rendered['test_env']['SF_CI_EXPECT_VDI_CONSOLE_PROXY'], '1')

    def test_the_test_env_file_sources_back_to_the_same_values(self):
        self.apply(KERBSIDE_PROFILE)
        result = subprocess.run(
            ['bash', '-c', '. "$1"; printf "%s\\n%s\\n" "$SF_CI_EXPECT_VDI_CONSOLE_PROXY" "$SF_CI_QUOTED"', '-',
             os.path.join(self.tmp, profiles.TEST_ENV_FILE)],
            check=True, stdout=subprocess.PIPE, text=True)
        self.assertEqual(result.stdout, "1\nit's spaced\n")


class RefusalTest(ProfileTestCase):
    def test_an_unknown_top_level_key_is_refused(self):
        self.assertRefused('extra_var:\n  x: 1\n', 'extra_var')

    def test_an_unknown_group_key_is_refused(self):
        self.assertRefused('groups:\n  kerbside:\n    hosts: [sf2]\n    children: {}\n', 'children')

    def test_an_unknown_redeploy_check_key_is_refused(self):
        self.assertRefused('redeploy_check:\n  units: [sf-*.service]\n  hosts: [sf2]\n', 'hosts')

    def test_an_unknown_host_is_refused(self):
        self.assertRefused('groups:\n  kerbside:\n    hosts: [sf3]\n', 'sf3')

    def test_an_unknown_host_beside_a_known_one_is_refused(self):
        self.assertRefused('groups:\n  kerbside:\n    hosts: [sf2, sf-2]\n', 'sf-2')

    def test_an_existing_group_is_refused(self):
        for name in ('hypervisors', 'allsf', 'all'):
            with self.subTest(group=name):
                self.assertRefused('groups:\n  %s:\n    hosts: [sf2]\n' % name, 'already exists')

    def test_a_group_with_no_hosts_is_refused(self):
        self.assertRefused('groups:\n  kerbside:\n    vars: {a: b}\n', 'non-empty list of hosts')

    def test_a_non_string_test_env_value_is_refused(self):
        self.assertRefused('test_env:\n  SF_CI_X: 1\n', 'quote it')

    def test_a_test_env_name_that_is_not_an_identifier_is_refused(self):
        self.assertRefused('test_env:\n  "X; rm -rf /": "1"\n', 'not a shell identifier')

    def test_a_unit_glob_with_shell_syntax_is_refused(self):
        self.assertRefused("redeploy_check:\n  units: ['sf-*.service; reboot']\n", 'not a plain unit glob')

    def test_a_profile_that_is_not_a_mapping_is_refused(self):
        self.assertRefused('- groups\n', 'must be a mapping')

    def test_invalid_yaml_does_not_quote_the_offending_line(self):
        error = self.assertRefused('extra_vars:\n  password: hunter2: oops\n', 'line 2')
        self.assertNotIn('hunter2', str(error))


class EmptyProfileTest(ProfileTestCase):
    def test_an_empty_profile_applies_nothing(self):
        for text in ('', '\n', '{# nothing to add #}\n# nor here\n'):
            with self.subTest(text=text):
                profile = self.apply(text)
                self.assertEqual(profile, {'groups': {}, 'extra_vars': {}, 'mariadb_sql': '',
                                           'redeploy_check': None, 'test_env': {}})
                with open(self.inventory) as f:
                    self.assertEqual(f.read(), self.original_inventory)
                self.assertEqual(json.loads(self.output(profiles.EXTRA_VARS_FILE)), {})
                self.assertEqual(self.output(profiles.SQL_FILE), '')
                self.assertEqual(self.output(profiles.TEST_ENV_FILE), '')
                self.assertFalse(os.path.exists(os.path.join(self.tmp, profiles.REDEPLOY_UNITS_FILE)))

    def test_a_stale_redeploy_units_file_is_removed(self):
        self.apply(KERBSIDE_PROFILE)
        self.apply('')
        self.assertFalse(os.path.exists(os.path.join(self.tmp, profiles.REDEPLOY_UNITS_FILE)))


class CommandLineTest(ProfileTestCase):
    def run_script(self, profile_argument):
        facts = os.path.join(self.tmp, 'facts.json')
        with open(facts, 'w') as f:
            json.dump(SLIM_TIER_FACTS, f)
        return subprocess.run(
            ['python3', SCRIPT, '--profile', profile_argument, '--facts-file', facts,
             '--inventory', self.inventory, '--workspace', self.tmp],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_a_relative_profile_is_applied_and_values_are_not_logged(self):
        self.write_profile(KERBSIDE_PROFILE)
        result = self.run_script('profile.yml.j2')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('group kerbside: hosts sf2; vars api_url', result.stderr)
        self.assertIn('kerbside_sql_url', result.stderr)
        self.assertNotIn('ci-password', result.stdout + result.stderr)
        self.assertNotIn('CREATE DATABASE', result.stdout + result.stderr)

    def test_an_absolute_profile_path_is_refused(self):
        result = self.run_script(self.write_profile(KERBSIDE_PROFILE))
        self.assertEqual(result.returncode, 1)
        self.assertIn('::error title=Deploy profile rejected::', result.stdout)
        self.assertNothingWritten()

    def test_a_rejected_profile_fails_with_an_annotation(self):
        self.write_profile('groups:\n  kerbside:\n    hosts: [sf3]\n')
        result = self.run_script('profile.yml.j2')
        self.assertEqual(result.returncode, 1)
        self.assertIn('::error title=Deploy profile rejected::', result.stdout)
        self.assertIn('sf3', result.stdout)


class DeployCollectionCommandTest(unittest.TestCase):
    """Run deploy-collection.sh with every external command stubbed out.

    The ansible-playbook stub records its argv, so these tests compare the
    command the deploy would really run. The script hardcodes its venv
    path under /tmp, which a test must not touch, so a copy is run with
    that one path redirected.
    """

    POSITIONAL = ['/srv/github/ci-inventory.yaml', 'pw', 'seed', 'key', 'http://10.0.1.10:3100', '10.0.1.10']

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        os.makedirs(os.path.join(self.tmp, 'workspace', 'shakenfist'))
        stubs = os.path.join(self.tmp, 'stubs')
        os.makedirs(stubs)

        with open(DEPLOY_COLLECTION) as f:
            script = f.read()
        self.assertIn('/tmp/collection-venv', script)
        self.script = os.path.join(self.tmp, 'deploy-collection.sh')
        with open(self.script, 'w') as f:
            f.write(script.replace('/tmp/collection-venv', os.path.join(self.tmp, 'venv')))

        self.record = os.path.join(self.tmp, 'argv')
        self.stub(stubs, 'python3', 'if [ "$1" = "-mvenv" ]; then mkdir -p "$2/bin"; touch "$2/bin/activate"; fi')
        self.stub(stubs, 'pip', 'true')
        self.stub(stubs, 'ansible-galaxy', 'true')
        self.stub(stubs, 'ansible-playbook', 'printf "%%s\\0" "$@" > %s' % self.record)

        self.environment = dict(os.environ)
        self.environment['PATH'] = '%s:%s' % (stubs, os.environ['PATH'])
        self.environment['GITHUB_WORKSPACE'] = os.path.join(self.tmp, 'workspace')

    def stub(self, directory, name, body):
        path = os.path.join(directory, name)
        with open(path, 'w') as f:
            f.write('#!/bin/bash\n%s\n' % body)
        os.chmod(path, 0o755)

    def run_deploy(self, *extra):
        subprocess.run(['bash', self.script] + self.POSITIONAL + list(extra), check=True, env=self.environment,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(self.record) as f:
            return f.read().split('\0')[:-1]

    def test_without_a_profile_the_command_has_one_extra_vars_string(self):
        argv = self.run_deploy()
        self.assertEqual(argv[:4], ['-i', '/srv/github/ci-inventory.yaml', 'examples/_shared/site.yml',
                                    '--extra-vars'])
        self.assertEqual(len(argv), 5)
        self.assertIn('mariadb_host=10.0.1.10', argv[4].split())
        self.assertIn('extra_config=[]', argv[4].split())

    def test_an_empty_seventh_argument_changes_nothing(self):
        self.assertEqual(self.run_deploy(''), self.run_deploy())

    def test_a_profile_appends_its_file_after_the_unchanged_command(self):
        without = self.run_deploy()
        argv = self.run_deploy('/srv/github/ci-deploy-profile-extra-vars.json')
        self.assertEqual(argv[:len(without)], without)
        self.assertEqual(argv[len(without):], ['--extra-vars', '@/srv/github/ci-deploy-profile-extra-vars.json'])


class WiringTest(unittest.TestCase):
    """The action and the workflow plumb the input the way the script expects."""

    @classmethod
    def setUpClass(cls):
        with open(ACTION) as f:
            cls.action = yaml.safe_load(f)
        cls.steps = {step['name']: step for step in cls.action['runs']['steps']}
        cls.names = [step['name'] for step in cls.action['runs']['steps']]
        with open(WORKFLOW) as f:
            cls.workflow = yaml.safe_load(f)

    def test_the_input_defaults_to_empty(self):
        self.assertEqual(self.action['inputs']['deploy_profile']['default'], '')
        self.assertEqual(self.workflow[True]['workflow_call']['inputs']['deploy_profile']['default'], '')

    def test_the_workflow_passes_the_input_to_the_action(self):
        steps = self.workflow['jobs']['smoke_cluster']['steps']
        build = [s for s in steps if s.get('uses', '').startswith('shakenfist/actions/build-smoke-cluster@')][0]
        self.assertEqual(build['with']['deploy_profile'], '${{ inputs.deploy_profile }}')

    def test_the_profile_is_applied_between_inventory_and_deploy(self):
        apply_index = self.names.index('Apply the deploy profile')
        self.assertLess(self.names.index('Install BYO MariaDB on primary'), apply_index)
        self.assertLess(self.names.index('Generate the deploy inventory'), apply_index)
        self.assertLess(apply_index, self.names.index('Deploy Shaken Fist via the collection'))

    def test_the_profile_step_only_runs_with_a_profile(self):
        step = self.steps['Apply the deploy profile']
        self.assertEqual(step['if'], "inputs.deploy_profile != ''")
        self.assertEqual(step['env']['DEPLOY_PROFILE'], '${{ inputs.deploy_profile }}')
        self.assertNotIn('${{', step['run'])
        self.assertNotIn('set -x', step['run'])
        # The SQL travels on stdin.
        self.assertIn("'sudo mariadb' < /srv/github/%s" % profiles.SQL_FILE, step['run'])

    def test_the_deploy_passes_the_extra_vars_file_only_with_a_profile(self):
        step = self.steps['Deploy Shaken Fist via the collection']
        self.assertEqual(step['env']['DEPLOY_PROFILE'], '${{ inputs.deploy_profile }}')
        self.assertIn('profile_args=(/srv/github/%s)' % profiles.EXTRA_VARS_FILE, step['run'])
        self.assertIn('"${profile_args[@]}"', step['run'])
        self.assertNotIn('set -x', step['run'])


if __name__ == '__main__':
    unittest.main()
