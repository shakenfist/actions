#!/usr/bin/env python3

"""Tests that every kerbside playbook reserves the deployment VIP first.

The deployment brings up kolla_internal_vip_address inside a guest with
keepalived, so Shaken Fist never allocated it and IPAM is free to hand
the same address to the next interface on that network. When it does,
kolla-ansible's prechecks ping the VIP, something answers, and the deploy
refuses to start -- kerbside-patches CI run 35202102331, and again in
kerbside's merge queue in run 35305610981.

The fix is ansible/tasks/reserve-deployment-vip.yml, and it only works if
it is imported after the network exists and before the first address on
it is allocated. That ordering is exactly the kind of invariant which
rots quietly, for the same reasons test_ansible_readiness.py exists: the
fabric is not available on a dev host, nothing in CI runs these playbooks
before merge, and a collision is a one-in-253 draw, so the first sign of
a regression looks like flake rather than breakage.

The literal address is the other half. It appears in three playbooks and
in setup-kerbside-environment's input default, and has to match
kolla_internal_vip_address in a different repository. Changing it in only
some of those places reintroduces the bug quietly, so the copies are
compared here.
"""

import ipaddress
import os
import re
import unittest

import yaml

from tests.helpers import REPO_ROOT
from tests.test_ansible_readiness import (ANSIBLE_DIR, creates_instance,
                                          documents, included_file, tasks)


RESERVATION = 'tasks/reserve-deployment-vip.yml'
ACTION = os.path.join(REPO_ROOT, 'setup-kerbside-environment', 'action.yml')

# The playbooks which stand up a kerbside test environment, named
# explicitly so that a fourth one is a deliberate edit here rather than a
# silent gap in what is covered. The same set is derived from the action
# below, so adding a topology there without a reservation fails this file
# rather than a deploy months later.
KERBSIDE_PLAYBOOKS = {
    'kerbside-multi-node-2.yml',
    'kerbside-multi-node.yml',
    'kerbside-single-node.yml',
}


def action_playbooks():
    """The playbooks setup-kerbside-environment picks between.

    The action switches on its topology input and assigns playbook=, so
    the shell it runs is the authoritative list of playbooks which deploy
    kolla -- and therefore of playbooks which must reserve the VIP.
    """
    with open(ACTION) as f:
        return set(re.findall(r'playbook="([^"]+\.yml)"', f.read()))


def creates_network(task):
    """True if this task asks Shaken Fist to create a network.

    Mirrors creates_instance(): the same module deletes networks with
    state: absent, and state defaults to present, so only a literal
    state: absent excludes a task.
    """
    for key, value in task.items():
        if key.split('.')[-1] != 'sf_network' or not isinstance(value, dict):
            continue
        state = value.get('state', 'present')
        if not isinstance(state, str) or '{{' in state or state == 'present':
            return True
    return False


def network_netblock(task):
    """The netblock a network-creating task asks for, if it is a literal."""
    for key, value in task.items():
        if key.split('.')[-1] != 'sf_network' or not isinstance(value, dict):
            continue
        netblock = value.get('netblock')
        if isinstance(netblock, str) and '{{' not in netblock:
            return netblock
    return None


def shell_commands(task):
    """Every shell or command string in a task, as a single string."""
    found = []
    for key, value in task.items():
        if key.split('.')[-1] not in ('shell', 'command', 'raw'):
            continue
        if isinstance(value, dict):
            value = value.get('cmd', '')
        if isinstance(value, str):
            found.append(value)
    return '\n'.join(found)


class VipReservationTest(unittest.TestCase):
    def setUp(self):
        self.docs = dict(documents())

        # Task files which create instances, so that a playbook which
        # only includes one still counts as allocating an address.
        self.creating_includes = set()
        for name, doc in self.docs.items():
            if any(creates_instance(t) for t in tasks(doc)):
                self.creating_includes.add(name)

    def ordered_tasks(self, name):
        return list(tasks(self.docs[name]))

    def allocates_address(self, task):
        """True if this task takes an address out of the network's pool.

        Two ways exist in these playbooks: creating an instance on the
        network, directly or through kerbside-create-instance.yml, and
        adding the network to the CI runner as a second interface, which
        is the allocation that actually collided with the VIP.
        """
        if creates_instance(task):
            return True
        included = included_file(task)
        if included and '{{' not in included:
            if os.path.normpath(included) in self.creating_includes:
                return True
        return 'add-interface' in shell_commands(task)

    def test_the_reservation_exists(self):
        self.assertTrue(
            os.path.exists(os.path.join(ANSIBLE_DIR, RESERVATION)),
            '%s is missing; every kerbside playbook imports it' % RESERVATION)

    def test_the_kerbside_playbooks_are_the_expected_ones(self):
        # Other playbooks here create networks too -- the slim topologies
        # build a Shaken Fist cluster -- and none of them deploy anything
        # which brings up a VIP. What distinguishes the kerbside ones is
        # that setup-kerbside-environment runs them, so read the set out
        # of the action rather than guessing at it from the tree, and
        # compare it against the named set so that neither can drift
        # alone.
        self.assertEqual(KERBSIDE_PLAYBOOKS, action_playbooks())
        for name in sorted(KERBSIDE_PLAYBOOKS):
            with self.subTest(playbook=name):
                self.assertIn(name, self.docs)
                self.assertTrue(
                    any(creates_network(t) for t in self.ordered_tasks(name)),
                    '%s creates no network, so this file is no longer '
                    'looking at what it thinks it is' % name)

    def test_the_reservation_runs_before_any_address_is_allocated(self):
        # Ordering is the whole point: a reservation after the runner's
        # interface has been added is a reservation of an address which
        # may already be in use, and it fails or -- worse -- succeeds
        # against a network where the damage is done.
        for name in sorted(KERBSIDE_PLAYBOOKS):
            ordered = self.ordered_tasks(name)
            network = [i for i, t in enumerate(ordered) if creates_network(t)]
            reserved = [i for i, t in enumerate(ordered)
                        if included_file(t) == RESERVATION]
            allocated = [i for i, t in enumerate(ordered)
                         if self.allocates_address(t)]

            with self.subTest(playbook=name):
                self.assertTrue(
                    reserved,
                    '%s does not import %s, so the deployment VIP stays in '
                    'the pool and CI collides with it roughly one run in '
                    '253. See the comment at the top of ansible/%s.'
                    % (name, RESERVATION, RESERVATION))
                self.assertTrue(network, '%s creates no network' % name)
                self.assertTrue(
                    allocated, '%s allocates no addresses, which means this '
                    'check has stopped recognising them' % name)
                self.assertLess(
                    min(network), min(reserved),
                    '%s imports %s before the network it reserves on exists'
                    % (name, RESERVATION))
                self.assertLess(
                    min(reserved), min(allocated),
                    '%s allocates an address (task %d) before importing %s '
                    '(task %d), so IPAM can hand out the VIP first'
                    % (name, min(allocated), RESERVATION, min(reserved)))

    def play_vars(self, name):
        merged = {}
        for play in self.docs[name]:
            if isinstance(play, dict) and isinstance(play.get('vars'), dict):
                merged.update(play['vars'])
        return merged

    def test_every_copy_of_the_vip_agrees(self):
        # The address is written down in each playbook and again as the
        # action's input default, and has to match
        # kolla_internal_vip_address in kerbside-patches. Changing one
        # copy and not the others is silent until a deploy fails.
        with open(ACTION) as f:
            action = yaml.safe_load(f)
        addresses = {
            'setup-kerbside-environment/action.yml':
                action['inputs']['vip_address']['default']}
        for name in sorted(KERBSIDE_PLAYBOOKS):
            addresses[name] = self.play_vars(name).get('vip_address')

        self.assertEqual(
            1, len(set(addresses.values())),
            'the deployment VIP is spelled differently in different places, '
            'so at least one of them is now reserving or deploying the wrong '
            'address: %s' % addresses)
        self.assertIsNotNone(list(addresses.values())[0])

    def test_the_vip_lies_inside_the_network_it_is_reserved_on(self):
        vip = ipaddress.ip_address(self.play_vars('kerbside-single-node.yml')['vip_address'])
        checked = 0
        for name in sorted(KERBSIDE_PLAYBOOKS):
            for task in self.ordered_tasks(name):
                netblock = network_netblock(task)
                if not netblock:
                    continue
                checked += 1
                with self.subTest(playbook=name, netblock=netblock):
                    self.assertIn(
                        vip, ipaddress.ip_network(netblock),
                        '%s creates %s, which does not contain the VIP %s '
                        'the same playbook reserves' % (name, netblock, vip))
        self.assertEqual(len(KERBSIDE_PLAYBOOKS), checked)

    def reservation_tasks(self):
        with open(os.path.join(ANSIBLE_DIR, RESERVATION)) as f:
            return list(tasks(yaml.safe_load(f)))

    def test_the_guard_agrees_with_the_netblock(self):
        # The task validates vip_address with a literal pattern, because
        # the runners carry ansible-core alone and have no ipaddr filter.
        # A literal is a second copy of the netblock, so check it accepts
        # the VIP and rejects an address outside the block rather than
        # trusting that it was updated alongside it.
        patterns = []
        for task in self.reservation_tasks():
            for value in task.get('assert', {}).get('that', []):
                patterns += re.findall(r"match\('([^']+)'\)", str(value))
        self.assertEqual(
            1, len(patterns),
            '%s no longer validates vip_address with exactly one pattern, so '
            'an empty or out-of-block override reaches the API as a 400: %s'
            % (RESERVATION, patterns))

        vip = self.play_vars('kerbside-single-node.yml')['vip_address']
        network = ipaddress.ip_network(
            network_netblock(
                [t for t in self.ordered_tasks('kerbside-single-node.yml')
                 if creates_network(t)][0]))
        outside = ipaddress.ip_address(
            (int(network.network_address) + (1 << 8)) % (1 << 32))

        self.assertRegex(vip, patterns[0])
        self.assertNotRegex(
            str(outside), patterns[0],
            '%s accepts %s, which is outside %s, so the guard has drifted '
            'from the network it is meant to describe'
            % (RESERVATION, outside, network))

    def test_an_old_client_cannot_abort_the_run(self):
        # The reservation is gated on a probe rather than on the strings
        # an old or missing sf-client prints, because "No such command
        # 'reserve-address'" (rc 2) and "command not found" (rc 127) are
        # indistinguishable from a real refusal after the fact. Dropping
        # the gate would break every consumer on the next run, which is
        # the failure this fails-soft design exists to avoid.
        probe = None
        reserve = None
        for task in self.reservation_tasks():
            if task.get('register') == 'vip_reservation_probe':
                probe = task
            if task.get('register') == 'vip_reservation':
                reserve = task

        self.assertIsNotNone(
            probe, '%s no longer probes for sf-client support' % RESERVATION)
        self.assertIs(
            False, probe.get('failed_when'),
            'the probe in %s must not be able to fail the run' % RESERVATION)
        self.assertIsNotNone(reserve, '%s reserves nothing' % RESERVATION)
        self.assertIn(
            'vip_reservation_probe', str(reserve.get('when', '')),
            'the reservation in %s is no longer gated on the probe, so an '
            'sf-client without the verb aborts the playbook' % RESERVATION)

    def test_the_task_is_documented(self):
        with open(os.path.join(REPO_ROOT, 'docs', 'ansible.md')) as f:
            docs = f.read()
        self.assertIn('## The deployment VIP', docs)
        self.assertIn(RESERVATION, docs)


if __name__ == '__main__':
    unittest.main()
