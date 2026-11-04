#!/usr/bin/env python3

"""The Proxmox console account is no wider than the privileges it needs.

`tasks/proxmox/api.yml` mints the API token the substrate lane publishes
as `token_id`, and the lane proves only that the token works: every mint
in it passes whether the credential is narrow or wide. So the shape of
the account is pinned here instead -- the privilege set, the path, the
privilege separation and the grants -- and so are the three expressions
that decide whether a node that is re-run rather than rebuilt actually
converges on that shape. `docs/ansible.md`, "Proxmox node", says why
the privileges are the ones they are.

The Jinja is rendered rather than pattern-matched, because an `and`
that short-circuits the wrong way or a filter that treats PVE's `"0"`
as truthy fails only on a real node, days later, as a credential that
is wider than the file says it is.
"""

import json
import os
import re
import unittest

import jinja2
import yaml

from tests.helpers import REPO_ROOT


VARS = os.path.join(REPO_ROOT, 'ansible', 'vars', 'proxmox.yml')
API_TASKS = os.path.join(REPO_ROOT, 'ansible', 'tasks', 'proxmox', 'api.yml')

# The proven pair, from kerbside's docs/console-sources.md, "Proxmox VE
# account". Changing this set is a decision about the credential every
# consumer of the action holds, so it is spelled out here rather than
# read back out of the file under test.
PROVEN_PRIVS = {'VM.Audit', 'VM.Console'}
PROVEN_PATH = '/vms'

# Stock PVE roles that have been granted here before, or would be the
# obvious shortcut back: all of them carry more than the proven pair.
STOCK_ROLES = ('Administrator', 'PVEAdmin', 'PVEAuditor', 'PVEVMAdmin', 'PVEVMUser',
               'PVEDatastoreAdmin', 'PVEDatastoreUser', 'PVESysAdmin', 'PVEPoolAdmin')

# Roles kerbside's Proxmox lane creates by name on this same node, after
# deploying this action: the documented KerbsideConsole and the two
# single-privilege roles its negative checks use. "pveum role add"
# refuses a role that exists, and the lane fails on the first command it
# cannot run, so taking any of these names here breaks that lane rather
# than this one. See proxmox_api_role in ansible/vars/proxmox.yml.
KERBSIDE_LANE_ROLES = ('KerbsideConsole', 'KerbsideAuditOnly', 'KerbsideConsoleOnly')

# The tasks that can see the token secret: the one PVE prints it to, and
# the one that writes it to the runner. Each must carry no_log, which is
# the invariant the file's header comment states.
SECRET_TASKS = ('Create the API token', 'Write the token secret to the runner')


def load(path):
    with open(path) as f:
        return yaml.safe_load(f)


def command_of(task):
    """The command a task runs, whitespace folded, or '' if it runs none."""
    for key in ('ansible.builtin.command', 'command', 'ansible.builtin.shell', 'shell'):
        value = task.get(key)
        if isinstance(value, str):
            return ' '.join(value.split())
        if isinstance(value, dict) and isinstance(value.get('cmd'), str):
            return ' '.join(value['cmd'].split())
    return ''


def environment():
    """Jinja2 with stand-ins for the Ansible filters the file uses."""
    env = jinja2.Environment()
    env.filters['from_json'] = json.loads
    return env


class ProxmoxConsoleAccountTestCase(unittest.TestCase):
    def setUp(self):
        self.vars = load(VARS)
        self.tasks = load(API_TASKS)
        self.commands = [command_of(task) for task in self.tasks]
        self.named = {task['name']: task for task in self.tasks}
        self.assertEqual(len(self.named), len(self.tasks), 'two tasks share a name')
        # Nothing below means anything if the file stopped being a list
        # of tasks that run commands.
        self.assertTrue([c for c in self.commands if c], '%s runs no commands' % API_TASKS)

    def task(self, name):
        self.assertIn(name, self.named,
                      'no task named %r; the tests below are about that task, so a rename has to '
                      'come here too' % name)
        return self.named[name]

    def pveum(self, *words):
        """The pveum commands whose text contains all of words."""
        return [c for c in self.commands
                if c.startswith('pveum ') and all(word in c for word in words)]

    def render(self, name, key='when', **context):
        """Render one task's Jinja expression and return it as a string."""
        expression = self.task(name)[key]
        self.assertIsInstance(expression, str, '%s of %r is not a template' % (key, name))
        return environment().from_string('{{ %s }}' % expression.strip()).render(**context).strip()

    # ------------------------------------------------------------------
    # The shape of the account.

    def test_the_role_holds_exactly_the_proven_privileges(self):
        privs = self.vars['proxmox_api_privs']
        self.assertEqual(set(privs.split()), PROVEN_PRIVS,
                         'proxmox_api_privs is %r; the proven set is %s. Widening it widens the '
                         'credential every consumer of deploy-proxmox-on-shakenfist holds, and no '
                         'mint in the lane would notice' % (privs, ' '.join(sorted(PROVEN_PRIVS))))

    def test_the_role_is_granted_on_vms_and_nowhere_else(self):
        self.assertEqual(self.vars['proxmox_api_acl_path'], PROVEN_PATH)
        for command in self.pveum('acl modify'):
            self.assertIn('{{ proxmox_api_acl_path }}', command,
                          'an ACL is granted on a path other than proxmox_api_acl_path: %r' % command)

    def test_the_role_is_not_a_stock_pve_role(self):
        role = self.vars['proxmox_api_role']
        self.assertNotIn(role, STOCK_ROLES,
                         '%r is a stock PVE role, and every stock role carries more than the '
                         'proven pair' % role)

    def test_the_role_does_not_collide_with_kerbsides_lane(self):
        role = self.vars['proxmox_api_role']
        self.assertNotIn(role, KERBSIDE_LANE_ROLES,
                         '%r is a role kerbside\'s Proxmox lane creates by name on a node this '
                         'action has just deployed. "pveum role add" refuses a role that exists, '
                         'so this name fails that lane at its first documented command' % role)

    def test_no_stock_role_is_granted(self):
        for command in self.commands:
            for stock in STOCK_ROLES:
                self.assertNotIn(stock, command,
                                 'the stock role %s is named by %r' % (stock, command))

    def test_the_role_is_created_from_the_privilege_variable(self):
        created = self.pveum('role add')
        self.assertEqual(len(created), 1, 'expected one "pveum role add", found %r' % created)
        self.assertIn('--privs "{{ proxmox_api_privs }}"', created[0],
                      'the role is created with privileges other than proxmox_api_privs: %r' % created[0])

    def test_the_token_is_privilege_separated(self):
        created = self.pveum('user token add')
        self.assertEqual(len(created), 1, 'expected one "pveum user token add", found %r' % created)
        self.assertRegex(created[0], r'--privsep\s+1',
                         'the token is not privilege separated, so it holds whatever its user '
                         'holds, now and later: %r' % created[0])

    def test_both_the_token_and_its_user_are_granted_the_role(self):
        # A privilege-separated token is capped by its own ACL as well
        # as its user's, so either grant alone leaves a credential that
        # cannot mint a console ticket.
        grants = self.pveum('acl modify')
        for flag, subject in (('--users', '{{ proxmox_api_user }}'),
                              ('--tokens', '{{ proxmox_api_token_id }}')):
            matching = [c for c in grants
                        if re.search(r'%s %s' % (re.escape(flag), re.escape(subject)), c)]
            self.assertEqual(len(matching), 1,
                             'expected one "pveum acl modify %s %s", found %r'
                             % (flag, subject, grants))
            self.assertIn('--roles {{ proxmox_api_role }}', matching[0])

    # ------------------------------------------------------------------
    # Ordering, and which read each decision is made from.

    def test_the_token_is_granted_its_role_only_once_the_token_exists(self):
        # pveum refuses an ACL for a token that is not there yet, so the
        # grant has to come after the creation, not with the user's.
        create = self.tasks.index(self.task('Create the API token'))
        grant = self.tasks.index(self.task('Grant the API token its role'))
        self.assertLess(create, grant, 'the token ACL is granted before the token is created')
        self.assertIn('--tokens', command_of(self.task('Grant the API token its role')))

    def test_the_token_grant_reads_the_acl_as_it_is_after_the_token(self):
        # Keyed on the first read instead, the grant is skipped on a node
        # whose token was just replaced -- leaving a published credential
        # with no privileges at all.
        reread = self.task('Read the ACL again, now that the token exists')
        register = reread['register']
        grant = self.task('Grant the API token its role')
        self.assertIn(register, grant['when'],
                      'the token grant is decided from a read older than the token itself')
        self.assertLess(self.tasks.index(self.task('Create the API token')), self.tasks.index(reread))

    def test_the_token_id_is_recorded_before_anything_uses_it(self):
        fact = self.task('Record the token id')
        self.assertEqual(fact['ansible.builtin.set_fact']['proxmox_api_token_id'],
                         '{{ proxmox_api_user }}!{{ proxmox_api_token }}')
        users = [i for i, task in enumerate(self.tasks)
                 if 'proxmox_api_token_id' in json.dumps(task) and task is not fact]
        self.assertTrue(users, 'nothing uses proxmox_api_token_id, so this file no longer needs it')
        self.assertLess(self.tasks.index(fact), min(users),
                        'proxmox_api_token_id is used before it is set, which is an undefined '
                        'variable on a real run')

    def test_every_task_that_can_see_the_secret_has_no_log(self):
        # The file's contract: PVE prints the secret once, and nothing
        # that handles it may reach the log.
        for name in SECRET_TASKS:
            self.assertIs(self.task(name).get('no_log'), True,
                          '%r handles the token secret without no_log: true' % name)

    # ------------------------------------------------------------------
    # The expressions that make a re-run converge, rendered for real.

    def _roles(self, *entries):
        return {'stdout': json.dumps(list(entries))}

    def test_an_existing_role_is_corrected_rather_than_left_as_it_was(self):
        # Re-running against a node created by an earlier revision of
        # this file must narrow its role, not leave the old set in
        # place: "pveum role add" refuses a role that exists.
        modified = self.pveum('role modify')
        self.assertEqual(len(modified), 1, 'expected one "pveum role modify", found %r' % modified)
        self.assertIn('--privs "{{ proxmox_api_privs }}"', modified[0])

        role, privs = 'ShakenFistConsole', 'VM.Audit VM.Console'
        cases = (
            # (what pveum reports, should the role be modified)
            ('absent', [{'roleid': 'PVEVMUser', 'privs': 'VM.Audit,VM.Console,VM.PowerMgmt'}], 'False'),
            ('same', [{'roleid': role, 'privs': 'VM.Audit,VM.Console'}], 'False'),
            ('same, other order', [{'roleid': role, 'privs': 'VM.Console,VM.Audit'}], 'False'),
            ('wider', [{'roleid': role, 'privs': 'VM.Audit,VM.Console,VM.PowerMgmt'}], 'True'),
            ('narrower', [{'roleid': role, 'privs': 'VM.Console'}], 'True'),
            ('no privileges', [{'roleid': role, 'privs': ''}], 'True'),
            ('privs key absent', [{'roleid': role}], 'True'),
        )
        for label, entries, expected in cases:
            with self.subTest(role=label):
                self.assertEqual(
                    self.render("Correct the console role's privileges",
                                _pve_roles=self._roles(*entries),
                                proxmox_api_role=role, proxmox_api_privs=privs),
                    expected)

    def test_a_token_that_predates_privilege_separation_is_separated(self):
        separated = self.pveum('user token modify')
        self.assertEqual(len(separated), 1,
                         'expected one "pveum user token modify", found %r' % separated)
        self.assertRegex(separated[0], r'--privsep\s+1')

        skipped = type('Skipped', (dict,), {})()  # a stand-in for a skipped register
        cases = (
            # PVE has printed privsep both as a number and as a string,
            # and a newly created token must not be modified at all.
            ('privsep 0', [{'tokenid': 'console', 'privsep': 0}], True, 'True'),
            ('privsep "0"', [{'tokenid': 'console', 'privsep': '0'}], True, 'True'),
            ('privsep 1', [{'tokenid': 'console', 'privsep': 1}], True, 'False'),
            ('privsep "1"', [{'tokenid': 'console', 'privsep': '1'}], True, 'False'),
            ('privsep absent', [{'tokenid': 'console'}], True, 'False'),
            ('another token only', [{'tokenid': 'other', 'privsep': 0}], True, 'False'),
            # After a remove and re-add, the stale read still shows the old
            # token: the guard on the create, not the read, is what skips this.
            ('just created', [{'tokenid': 'console', 'privsep': 0}], False, 'False'),
        )
        env = environment()
        env.tests['skipped'] = lambda value: value is skipped
        expression = self.task('Separate the privileges of a token that predates privsep')['when']
        for label, entries, was_skipped, expected in cases:
            with self.subTest(token=label):
                rendered = env.from_string('{{ %s }}' % expression.strip()).render(
                    _pve_token_add=skipped if was_skipped else {'changed': True},
                    _pve_tokens={'stdout': json.dumps(entries)},
                    proxmox_api_token='console').strip()
                self.assertEqual(rendered, expected)

    def test_an_earlier_revisions_grants_are_revoked(self):
        revoke = self.task('Revoke any other role this user or token holds')
        command = command_of(revoke)
        self.assertIn('pveum acl delete', command)
        self.assertIn('--roles {{ item.roleid }}', command)
        self.assertIn("'--tokens' if item.type == 'token' else '--users'", command,
                      'a token grant is deleted with --users, or the reverse, which pveum ignores')

        user, token, role = 'kerbside@pve', 'kerbside@pve!console', 'ShakenFistConsole'
        acl = [
            # What an earlier revision left behind, both of which go.
            {'path': '/vms', 'ugid': user, 'roleid': 'PVEVMUser', 'type': 'user'},
            {'path': '/', 'ugid': user, 'roleid': 'PVEAuditor', 'type': 'user'},
            # Ours, which stay.
            {'path': '/vms', 'ugid': user, 'roleid': role, 'type': 'user'},
            {'path': '/vms', 'ugid': token, 'roleid': role, 'type': 'token'},
            # A token grant of an older revision, which goes.
            {'path': '/', 'ugid': token, 'roleid': 'PVEAuditor', 'type': 'token'},
            # Somebody else's, which is not ours to remove.
            {'path': '/vms', 'ugid': 'kerbside-lane@pve', 'roleid': 'KerbsideConsole',
             'type': 'user'},
            {'path': '/', 'ugid': 'root@pam', 'roleid': 'Administrator', 'type': 'user'},
        ]
        # The loop value carries its own braces, unlike a when:.
        self.assertTrue(revoke['loop'].strip().startswith('{{'))
        rendered = environment().from_string(revoke['loop'].strip()).render(
                _pve_acl={'stdout': json.dumps(acl)},
                proxmox_api_user=user, proxmox_api_token_id=token, proxmox_api_role=role)
        revoked = [(entry['ugid'], entry['roleid']) for entry in yaml.safe_load(rendered)]
        self.assertEqual(sorted(revoked), sorted([(user, 'PVEVMUser'), (user, 'PVEAuditor'),
                                                  (token, 'PVEAuditor')]))

    def test_the_revocation_is_decided_before_our_own_grants_are_made(self):
        # Keyed on a read taken after the grants, the loop would find and
        # revoke the role this file has just granted.
        read = self.tasks.index(self.task('Read the current ACL'))
        revoke = self.tasks.index(self.task('Revoke any other role this user or token holds'))
        grant = self.tasks.index(self.task('Grant the console user its role'))
        self.assertLess(read, revoke)
        self.assertLess(revoke, grant)


if __name__ == '__main__':
    unittest.main()
