#!/usr/bin/env python3

"""Tests for tools/ci-make-inventory.py.

This script writes the ansible inventory that every CI cluster deploy is
driven from, for both the single-node smoke topology and the multi-node
slim ones. A mistake here does not fail loudly -- it produces a
syntactically valid inventory with a node in the wrong group, and the
deploy then fails somewhere much further along.
"""

import unittest

from tests.helpers import load_script


inventory = load_script('tools/ci-make-inventory.py', 'ci_make_inventory')


def spec(name, egress_ip, mesh_ip=None, hypervisor=True,
         network=False, database=False):
    """Build a facts-file node spec of the shape the topology playbook emits."""
    return {
        'name': name,
        'egress_ip': egress_ip,
        'mesh_ip': mesh_ip,
        'is_hypervisor': hypervisor,
        'is_network_node': network,
        'is_database_node': database,
    }


class BuildNodeTest(unittest.TestCase):
    def test_single_node_collapses_mesh_nic_to_eth0(self):
        # A single-node topology has one interface, so the mesh and
        # egress addresses are the same and the mesh NIC must not be
        # eth1 -- there is no eth1 to bind to.
        node = inventory.build_node(
            spec('sf-1', '10.0.0.5'), 'debian', '/srv/github/id_ci')
        self.assertEqual(node['mesh_ip'], '10.0.0.5')
        self.assertEqual(node['mesh_nic'], 'eth0')
        self.assertEqual(node['egress_nic'], 'eth0')

    def test_distinct_mesh_address_uses_eth1(self):
        node = inventory.build_node(
            spec('sf-1', '10.0.0.5', mesh_ip='192.168.1.5'),
            'debian', '/srv/github/id_ci')
        self.assertEqual(node['mesh_nic'], 'eth1')
        self.assertEqual(node['egress_nic'], 'eth0')

    def test_absent_mesh_ip_falls_back_to_egress(self):
        # The facts file omits mesh_ip entirely for single-node runs.
        bare = spec('sf-1', '10.0.0.5')
        del bare['mesh_ip']
        node = inventory.build_node(bare, 'debian', '/srv/github/id_ci')
        self.assertEqual(node['mesh_ip'], '10.0.0.5')
        self.assertEqual(node['mesh_nic'], 'eth0')

    def test_null_mesh_ip_falls_back_to_egress(self):
        node = inventory.build_node(
            spec('sf-1', '10.0.0.5', mesh_ip=None),
            'debian', '/srv/github/id_ci')
        self.assertEqual(node['mesh_ip'], '10.0.0.5')

    def test_capability_flags_are_coerced_to_bool(self):
        # The playbook writes these out of jinja, so they can arrive as
        # strings or integers rather than real booleans.
        node = inventory.build_node(
            {'name': 'sf-1', 'egress_ip': '10.0.0.5', 'mesh_ip': None,
             'is_hypervisor': 1, 'is_network_node': 0,
             'is_database_node': 'yes'},
            'debian', '/srv/github/id_ci')
        self.assertIs(node['is_hypervisor'], True)
        self.assertIs(node['is_network_node'], False)
        self.assertIs(node['is_database_node'], True)

    def test_ssh_details_are_carried_through(self):
        node = inventory.build_node(
            spec('sf-1', '10.0.0.5'), 'ubuntu', '/tmp/key')
        self.assertEqual(node['ssh_user'], 'ubuntu')
        self.assertEqual(node['ssh_key'], '/tmp/key')


class RenderInventoryTest(unittest.TestCase):
    def render(self, specs):
        nodes = [inventory.build_node(s, 'debian', '/srv/github/id_ci')
                 for s in specs]
        return inventory.render_inventory(nodes)

    def group_members(self, text, group):
        """Pull the host names listed under a named group."""
        lines = text.splitlines()
        start = lines.index('    %s:' % group)
        members = []
        for line in lines[start + 2:]:
            if not line.startswith('        '):
                break
            members.append(line.strip().rstrip(':'))
        return members

    def test_single_node_appears_in_every_group(self):
        text = self.render([
            spec('sf-1', '10.0.0.5', hypervisor=True, network=True,
                 database=True),
        ])
        for group in ('hypervisors', 'network_node', 'database_node'):
            self.assertEqual(self.group_members(text, group), ['sf-1'],
                             'sf-1 missing from %s' % group)

    def test_nodes_only_land_in_groups_their_flags_select(self):
        text = self.render([
            spec('sf-1', '10.0.0.1', hypervisor=True, network=True,
                 database=True),
            spec('sf-2', '10.0.0.2', hypervisor=True),
            spec('sf-3', '10.0.0.3', hypervisor=False, database=True),
        ])
        self.assertEqual(self.group_members(text, 'hypervisors'),
                         ['sf-1', 'sf-2'])
        self.assertEqual(self.group_members(text, 'network_node'), ['sf-1'])
        self.assertEqual(self.group_members(text, 'database_node'),
                         ['sf-1', 'sf-3'])

    def test_database_tier_is_mirrored_into_legacy_etcd_master(self):
        # Pre-phase-7 copies of examples/_shared/site.yml only read
        # groups['etcd_master']. actions@main is consumed at runtime by
        # every shakenfist branch, so dropping this silently breaks the
        # older ones. Remove this test with the fallback.
        text = self.render([
            spec('sf-1', '10.0.0.1', database=True),
            spec('sf-2', '10.0.0.2', database=False),
        ])
        self.assertEqual(self.group_members(text, 'database_node'), ['sf-1'])
        self.assertEqual(self.group_members(text, 'etcd_master'), ['sf-1'])

    def test_allsf_carries_the_variable_blocks(self):
        text = self.render([spec('sf-1', '10.0.0.5', mesh_ip='192.168.1.5')])
        self.assertIn('        sf-1:', text)
        self.assertIn('          ansible_host: 10.0.0.5', text)
        self.assertIn('          ansible_user: debian', text)
        self.assertIn(
            '          ansible_ssh_private_key_file: /srv/github/id_ci', text)
        self.assertIn('          node_name: sf-1', text)
        self.assertIn('          node_egress_ip: 10.0.0.5', text)
        self.assertIn('          node_egress_nic: eth0', text)
        self.assertIn('          node_mesh_ip: 192.168.1.5', text)
        self.assertIn('          node_mesh_nic: eth1', text)

    def test_group_membership_entries_carry_no_vars(self):
        # Vars live on allsf only. Repeating them under the capability
        # groups would let the two copies drift.
        text = self.render([spec('sf-1', '10.0.0.5', network=True)])
        after_groups = text.split('    hypervisors:', 1)[1]
        self.assertNotIn('ansible_host', after_groups)
        self.assertNotIn('node_egress_ip', after_groups)

    def test_output_is_parseable_yaml_with_the_expected_shape(self):
        try:
            import yaml
        except ImportError:
            self.skipTest('PyYAML not installed')
        text = self.render([
            spec('sf-1', '10.0.0.1', mesh_ip='192.168.1.1', network=True,
                 database=True),
            spec('sf-2', '10.0.0.2', mesh_ip='192.168.1.2'),
        ])
        parsed = yaml.safe_load(text)
        children = parsed['all']['children']
        self.assertEqual(sorted(children), [
            'allsf', 'database_node', 'etcd_master', 'hypervisors',
            'network_node'])
        self.assertEqual(sorted(children['allsf']['hosts']), ['sf-1', 'sf-2'])
        self.assertEqual(
            children['allsf']['hosts']['sf-1']['ansible_host'], '10.0.0.1')
        self.assertEqual(
            children['allsf']['hosts']['sf-2']['node_mesh_nic'], 'eth1')

    def test_output_ends_with_a_newline(self):
        text = self.render([spec('sf-1', '10.0.0.5')])
        self.assertTrue(text.endswith('\n'))


if __name__ == '__main__':
    unittest.main()
