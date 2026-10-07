#!/usr/bin/env python3
# Copyright 2026 Michael Still and contributors

"""Prove that deploying a second time restarted nothing.

A deploy that restarts a daemon on every run, whether or not anything
changed, takes the cluster down for no reason each time an operator runs
it. Ansible's changed count cannot show that: many site.yml tasks report
changed on every run by design. What can show it is systemd's
InvocationID, a fresh random ID that a unit gets every time it starts --
including a start made by its own Restart= policy, which a timestamp
comparison can miss.

    ci-redeploy-check.py snapshot <inventory> <state-file> <unit-glob>...
    ci-redeploy-check.py compare <inventory> <state-file> <unit-glob>...

snapshot connects to every host in the inventory and records one line per
loaded unit matching a glob, as '<host> <unit> <InvocationID>' ('-' for a
unit that has never started). It refuses, rather than recording, when a
glob matched no unit on any host, since a check over nothing passes
whatever the deploy did.

compare takes a fresh snapshot the same way and prints every unit's
verdict against the state file. A changed InvocationID means the unit
restarted; a vanished unit means it was removed or its host stopped
answering. Either fails. A unit that is new since the snapshot is
reported but allowed: the second deploy may legitimately finish starting
something.

Hosts are reached the way the deploy reached them: each inventory host's
ansible_host, ansible_user, ansible_ssh_private_key_file and
ansible_ssh_common_args, which tools/ci-make-inventory.py writes from the
topology facts. A host that does not answer during snapshot is warned
about and skipped, as ansible/ci-node-checks.yml skips it, because
slim-primary deliberately lists a hypervisor that never exists; a host
that answered during snapshot and not during compare has vanished units.

Exit status: 0 when nothing restarted, 1 when something restarted or
vanished, 2 when the check could not be made.
"""

import argparse
import re
import shlex
import subprocess
import sys

import yaml


# The same restriction tools/ci-apply-deploy-profile.py puts on a profile's
# globs. They reach the remote shell quoted, so this is belt and braces.
UNIT_GLOB = re.compile(r'^[A-Za-z0-9_.@:*?\[\]-]+$')

# Connection failures make ssh itself exit 255; anything else is the
# remote script's status.
SSH_UNREACHABLE = 255

# Run on each host with the globs as arguments. --all includes loaded units
# that are inactive; the LOAD column filter drops units that are merely
# referenced (not-found), which have no state to compare.
REMOTE_SCRIPT = """\
set -euo pipefail
for glob in "$@"; do
    units=$(systemctl list-units --all --plain --no-legend -- "${glob}" | awk '$2 == "loaded" {print $1}')
    for unit in ${units}; do
        id=$(systemctl show -p InvocationID --value -- "${unit}")
        printf '%s %s %s\\n' "${glob}" "${unit}" "${id:--}"
    done
done
"""


class CheckError(Exception):
    pass


def inventory_hosts(inventory):
    """Return [(host, vars)] for every host in a parsed inventory.

    A host may appear in several groups, with its connection vars in one
    (allsf, for ci-make-inventory.py's output) and bare membership in the
    rest, so vars are merged across every appearance.
    """
    hosts = {}

    def walk(group):
        group = group or {}
        for name, host_vars in (group.get('hosts') or {}).items():
            hosts.setdefault(name, {}).update(host_vars or {})
        for child in (group.get('children') or {}).values():
            walk(child)

    for group in (inventory or {}).values():
        walk(group)
    return sorted(hosts.items())


def ssh_command(host_vars, globs):
    """Build the argv that runs REMOTE_SCRIPT on a host."""
    for key in ('ansible_host', 'ansible_user', 'ansible_ssh_private_key_file'):
        if not host_vars.get(key):
            raise CheckError('an inventory host has no %s' % key)
    argv = ['ssh', '-i', host_vars['ansible_ssh_private_key_file']]
    argv.extend(shlex.split(host_vars.get('ansible_ssh_common_args', '')))
    # The phantom slim-primary hypervisor never answers; do not wait out
    # the kernel's SYN retries for it.
    argv.extend(['-o', 'ConnectTimeout=30'])
    argv.append('%s@%s' % (host_vars['ansible_user'], host_vars['ansible_host']))
    argv.append(' '.join(['bash', '-s', '--'] + [shlex.quote(glob) for glob in globs]))
    return argv


def run_ssh(host_vars, globs):
    """Run REMOTE_SCRIPT on a host and return (returncode, stdout)."""
    result = subprocess.run(ssh_command(host_vars, globs), input=REMOTE_SCRIPT, capture_output=True, text=True)
    if result.returncode not in (0, SSH_UNREACHABLE):
        sys.stderr.write(result.stderr)
    return result.returncode, result.stdout


def parse_remote_output(host, text, globs):
    """Parse REMOTE_SCRIPT's output into ({unit: id}, {glob: count})."""
    units = {}
    counts = {glob: 0 for glob in globs}
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 3 or fields[0] not in counts:
            raise CheckError('%s returned an unexpected line: %r' % (host, line))
        glob, unit, invocation = fields
        units[unit] = invocation
        counts[glob] += 1
    return units, counts


def check_globs(globs):
    if not globs:
        raise CheckError('no unit globs were given')
    for glob in globs:
        if not UNIT_GLOB.match(glob):
            raise CheckError('%r is not a plain unit glob' % glob)


def collect(hosts, globs, runner=run_ssh):
    """Return ({(host, unit): InvocationID}, {glob: units matched}).

    Hosts that do not answer are warned about and skipped. Raises
    CheckError when a host fails any other way.
    """
    check_globs(globs)
    state = {}
    totals = {glob: 0 for glob in globs}
    for host, host_vars in hosts:
        returncode, output = runner(host_vars, globs)
        if returncode == SSH_UNREACHABLE:
            print('::warning title=Redeploy check skipped a host::%s did not answer, so its units are not checked'
                  % host)
            continue
        if returncode != 0:
            raise CheckError('listing units on %s failed with exit status %d' % (host, returncode))
        units, counts = parse_remote_output(host, output, globs)
        for unit, invocation in units.items():
            state[(host, unit)] = invocation
        for glob, count in counts.items():
            totals[glob] += count
    return state, totals


def snapshot(hosts, globs, runner=run_ssh):
    """Collect, refusing when any glob matched no unit on any host.

    compare does not refuse this way: there, a glob matching nothing is
    every unit it matched before having vanished, which compare reports.
    """
    state, totals = collect(hosts, globs, runner=runner)
    empty = [glob for glob in globs if not totals[glob]]
    if empty:
        raise CheckError('no loaded unit on any answering host matches %s, so a redeploy check would prove nothing'
                         % ', '.join(empty))
    return state


def format_state(state):
    return ''.join('%s %s %s\n' % (host, unit, state[(host, unit)]) for host, unit in sorted(state))


def parse_state(text):
    state = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 3:
            raise CheckError('state file line %d is not "<host> <unit> <InvocationID>"' % number)
        state[(fields[0], fields[1])] = fields[2]
    return state


def compare(before, after):
    """Compare two snapshots.

    Returns a dict of sorted [(host, unit)] lists under 'changed',
    'vanished', 'new' and 'unchanged'.
    """
    verdicts = {'changed': [], 'vanished': [], 'new': [], 'unchanged': []}
    for key in sorted(set(before) | set(after)):
        if key not in after:
            verdicts['vanished'].append(key)
        elif key not in before:
            verdicts['new'].append(key)
        elif before[key] != after[key]:
            verdicts['changed'].append(key)
        else:
            verdicts['unchanged'].append(key)
    return verdicts


def failed(verdicts):
    return bool(verdicts['changed'] or verdicts['vanished'])


def report(verdicts, before, after):
    """Return the lines compare prints: every unit, then a summary."""
    lines = []
    for host, unit in verdicts['unchanged']:
        lines.append('unchanged  %s %s' % (host, unit))
    for host, unit in verdicts['new']:
        lines.append('new        %s %s (started during the second deploy; allowed)' % (host, unit))
    for host, unit in verdicts['changed']:
        lines.append('::error title=Redeploy restarted a unit::%s on %s restarted during the second deploy '
                     '(InvocationID %s became %s)' % (unit, host, before[(host, unit)], after[(host, unit)]))
    for host, unit in verdicts['vanished']:
        lines.append('::error title=Redeploy lost a unit::%s on %s is gone after the second deploy, '
                     'or its host stopped answering' % (unit, host))
    lines.append('%d unchanged, %d new, %d restarted, %d vanished.' % (
        len(verdicts['unchanged']), len(verdicts['new']), len(verdicts['changed']), len(verdicts['vanished'])))
    return lines


def load_inventory_hosts(path):
    with open(path) as f:
        hosts = inventory_hosts(yaml.safe_load(f))
    if not hosts:
        raise CheckError('%s lists no hosts' % path)
    return hosts


def main(argv=None, runner=run_ssh):
    parser = argparse.ArgumentParser(description='Prove that deploying a second time restarted nothing.')
    parser.add_argument('mode', choices=('snapshot', 'compare'))
    parser.add_argument('inventory', help='The ansible inventory the deploy used.')
    parser.add_argument('state_file', help='Where snapshot records, and compare reads, the first snapshot.')
    parser.add_argument('globs', nargs='+', metavar='unit-glob', help='systemd unit globs to check.')
    args = parser.parse_args(argv)

    try:
        hosts = load_inventory_hosts(args.inventory)
        if args.mode == 'snapshot':
            state = snapshot(hosts, args.globs, runner=runner)
            with open(args.state_file, 'w') as f:
                f.write(format_state(state))
            for host, unit in sorted(state):
                print('recorded   %s %s' % (host, unit))
            print('Recorded %d units on %d hosts.' % (len(state), len({host for host, _ in state})))
            return 0

        with open(args.state_file) as f:
            before = parse_state(f.read())
        if not before:
            raise CheckError('the state file %s records no units' % args.state_file)
        after, _ = collect(hosts, args.globs, runner=runner)
        verdicts = compare(before, after)
        for line in report(verdicts, before, after):
            print(line)
        return 1 if failed(verdicts) else 0
    except (CheckError, OSError) as e:
        print('::error title=Redeploy check could not run::%s' % e)
        return 2


if __name__ == '__main__':
    sys.exit(main())
