#!/usr/bin/env python3
# Copyright 2019 Michael Still and contributors
#
# Generate an ansible inventory for the shakenfist.shakenfist collection
# deploy (examples/_shared/site.yml) from the CI topology facts.
#
# The committed examples/cluster/inventory.yaml is the static, operator-facing
# multi-node inventory. CI provisions its nodes dynamically, so this script
# emits the topology-matched equivalent: the same group shape (allsf /
# hypervisors / network_node / etcd_master) and per-host vars (node_name /
# node_egress_* / node_mesh_*), but with real egress IPs and an SSH connection
# instead of ansible_connection: local.
#
# The node set comes from a JSON facts file written by the topology playbook
# (ansible/ci-include-common-localhost.yml). Each node records its name, egress
# IP, mesh IP, and the three capability flags (is_hypervisor / is_network_node /
# is_database_node) derived from its add_host group membership. This works for
# any node count: the single-node "smoke" (localhost) topology and the
# multi-node slim-primary / slim-tier topologies all flow through the same code.
#
# The output is plain YAML built by hand (no PyYAML dependency assumed).
import argparse
import json
import sys


def render_node_vars(node, indent):
    """Render the per-host variable block for a single node.

    `node` is a dict with the keys name, egress_ip, egress_nic, mesh_ip,
    mesh_nic, ssh_user, ssh_key. `indent` is the leading whitespace for the
    host key (the vars sit two spaces deeper).
    """
    pad = indent + '  '
    lines = [
        '%s%s:' % (indent, node['name']),
        '%sansible_host: %s' % (pad, node['egress_ip']),
        '%sansible_user: %s' % (pad, node['ssh_user']),
        '%sansible_ssh_private_key_file: %s' % (pad, node['ssh_key']),
        "%sansible_ssh_common_args: '-o StrictHostKeyChecking=no "
        "-o UserKnownHostsFile=/dev/null'" % pad,
        '%snode_name: %s' % (pad, node['name']),
        '%snode_egress_ip: %s' % (pad, node['egress_ip']),
        '%snode_egress_nic: %s' % (pad, node['egress_nic']),
        '%snode_mesh_ip: %s' % (pad, node['mesh_ip']),
        '%snode_mesh_nic: %s' % (pad, node['mesh_nic']),
    ]
    return lines


def render_group_member(node_name, indent):
    """Render a bare host membership entry (no vars, just the host key)."""
    return ['%s%s:' % (indent, node_name)]


def render_inventory(nodes):
    """Render the complete inventory YAML for the given list of nodes.

    Every node lands in allsf (with its full var block) and in each of the
    hypervisors / network_node / etcd_master groups it belongs to (bare
    membership; vars live on allsf). This matches the group shape of
    examples/cluster/inventory.yaml and collapses to a single node in every
    group for the single-node smoke case.
    """
    lines = ['---', 'all:', '  children:']

    # allsf carries the per-host variable blocks.
    lines.append('    allsf:')
    lines.append('      hosts:')
    for node in nodes:
        lines.extend(render_node_vars(node, '        '))

    # The capability groups carry bare membership; vars live on allsf above.
    # A node only appears in a group when its corresponding flag is set, so a
    # hypervisor that is not the network node (etc.) lands only where it should.
    group_flags = (
        ('hypervisors', 'is_hypervisor'),
        ('network_node', 'is_network_node'),
        ('etcd_master', 'is_database_node'),
    )
    for group, flag in group_flags:
        lines.append('    %s:' % group)
        lines.append('      hosts:')
        for node in nodes:
            if node[flag]:
                lines.extend(render_group_member(node['name'], '        '))

    return '\n'.join(lines) + '\n'


def build_node(spec, ssh_user, ssh_key):
    """Turn a single facts-file node spec into a render-ready node dict.

    The mesh NIC is the second interface (eth1) whenever the node has a mesh IP
    distinct from its egress IP (the multi-node case); a single-node topology
    shares one interface, so the mesh NIC collapses to eth0.
    """
    egress_ip = spec['egress_ip']
    mesh_ip = spec.get('mesh_ip') or egress_ip
    mesh_nic = 'eth1' if mesh_ip != egress_ip else 'eth0'
    return {
        'name': spec['name'],
        'egress_ip': egress_ip,
        'mesh_ip': mesh_ip,
        'egress_nic': 'eth0',
        'mesh_nic': mesh_nic,
        'ssh_user': ssh_user,
        'ssh_key': ssh_key,
        'is_hypervisor': bool(spec['is_hypervisor']),
        'is_network_node': bool(spec['is_network_node']),
        'is_database_node': bool(spec['is_database_node']),
    }


def main():
    parser = argparse.ArgumentParser(
        description='Generate an ansible inventory for the collection deploy.')
    parser.add_argument('--facts-file', required=True,
                        help='Path to the JSON topology facts file written by '
                             'the topology playbook.')
    parser.add_argument('--ssh-user', required=True,
                        help='SSH user for the provisioned node(s).')
    parser.add_argument('--ssh-key', required=True,
                        help='Path to the SSH private key file.')
    parser.add_argument('--output', required=True,
                        help='Path to write the generated inventory to.')

    args = parser.parse_args()

    with open(args.facts_file) as f:
        facts = json.load(f)

    node_specs = facts['nodes']
    if not node_specs:
        parser.error('The facts file lists no nodes.')

    nodes = [build_node(spec, args.ssh_user, args.ssh_key) for spec in node_specs]

    inventory = render_inventory(nodes)

    with open(args.output, 'w') as f:
        f.write(inventory)

    sys.stderr.write('Wrote inventory for %d node(s) to %s\n' % (len(nodes), args.output))
    sys.stderr.write(inventory)


if __name__ == '__main__':
    main()
