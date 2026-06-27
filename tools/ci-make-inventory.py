#!/usr/bin/env python3
# Copyright 2019 Michael Still and contributors
#
# Generate an ansible inventory for the shakenfist.shakenfist collection
# deploy (examples/_shared/site.yml) from the CI topology facts.
#
# The committed examples/single-node/inventory.yaml is the static,
# operator-facing inventory (one box, ansible_connection: local). CI provisions
# its node dynamically, so this script emits the topology-matched equivalent:
# the same group shape (allsf / hypervisors / network_node / etcd_master) and
# per-host vars (node_name / node_egress_* / node_mesh_*), but with a real
# egress IP and an SSH connection instead of ansible_connection: local.
#
# Only the "smoke" tier (one node in every group) needs to work today. The
# "full" tier is sketched in the design doc and raises NotImplementedError.
#
# The output is plain YAML built by hand (no PyYAML dependency assumed).
import argparse
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
    hypervisors / network_node / etcd_master groups (bare membership). This
    matches examples/single-node/inventory.yaml for the single-node case and
    generalises to the multi-node case.
    """
    lines = ['---', 'all:', '  children:']

    # allsf carries the per-host variable blocks.
    lines.append('    allsf:')
    lines.append('      hosts:')
    for node in nodes:
        lines.extend(render_node_vars(node, '        '))

    # The capability groups carry bare membership; vars live on allsf above.
    for group in ('hypervisors', 'network_node', 'etcd_master'):
        lines.append('    %s:' % group)
        lines.append('      hosts:')
        for node in nodes:
            lines.extend(render_group_member(node['name'], '        '))

    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(
        description='Generate an ansible inventory for the collection deploy.')
    parser.add_argument('--tier', choices=['smoke', 'full'], default='smoke',
                        help='Cluster tier (only smoke is implemented).')
    parser.add_argument('--ssh-user', required=True,
                        help='SSH user for the provisioned node(s).')
    parser.add_argument('--ssh-key', required=True,
                        help='Path to the SSH private key file.')
    parser.add_argument('--output', required=True,
                        help='Path to write the generated inventory to.')

    # Single-node (smoke) node data. The full tier will take repeated node
    # specifications instead; that is not implemented yet.
    parser.add_argument('--node-name', default='primary',
                        help='Name of the single smoke node.')
    parser.add_argument('--egress-ip',
                        help='Egress IP of the single smoke node.')
    parser.add_argument('--mesh-ip',
                        help='Mesh IP of the node (defaults to the egress IP).')
    parser.add_argument('--egress-nic', default='eth0',
                        help='Egress NIC name (default eth0).')
    parser.add_argument('--mesh-nic', default='eth0',
                        help='Mesh NIC name (default eth0).')

    args = parser.parse_args()

    if args.tier == 'full':
        raise NotImplementedError(
            'The full/cluster tier is not implemented yet. It will accept '
            'multiple node specifications and place them into capability '
            'groups from the topology add_host facts; ship smoke first.')

    if not args.egress_ip:
        parser.error('--egress-ip is required for the smoke tier.')

    node = {
        'name': args.node_name,
        'egress_ip': args.egress_ip,
        'mesh_ip': args.mesh_ip or args.egress_ip,
        'egress_nic': args.egress_nic,
        'mesh_nic': args.mesh_nic,
        'ssh_user': args.ssh_user,
        'ssh_key': args.ssh_key,
    }

    inventory = render_inventory([node])

    with open(args.output, 'w') as f:
        f.write(inventory)

    sys.stderr.write('Wrote inventory for tier %s to %s\n' % (args.tier, args.output))
    sys.stderr.write(inventory)


if __name__ == '__main__':
    main()
