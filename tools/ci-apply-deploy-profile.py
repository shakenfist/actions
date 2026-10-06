#!/usr/bin/env python3
# Copyright 2026 Michael Still and contributors

"""Apply a caller's deploy profile to a CI cluster deploy.

A deploy profile is a Jinja2 template in the caller's checkout, named by
the deploy_profile input of smoke-cluster.yml and build-smoke-cluster as a
path relative to GITHUB_WORKSPACE. It lets a repository add inventory
groups, extra variables and MariaDB databases to the CI deploy without
any of that content living in this repository. docs/consuming.md
documents the schema for callers; this docstring is the reference.

Rendering
---------

The template is rendered with StrictUndefined, so naming a node or a field
that does not exist fails the run rather than rendering an empty string.
It renders against the topology facts file the topology playbook writes,
/srv/github/ci-topology-facts.json (ansible/ci-include-common-localhost.yml),
never against ansible: everything ansible sees is already a fully rendered
string. The template sees exactly these names:

  nodes      A mapping from topology host name to that node's facts. Each
             node has the fields:
               name              the inventory host name ('primary', 'sf1')
               egress_ip         the address the runner reaches it on
               mesh_ip           its address on the cluster mesh network,
                                 equal to egress_ip on a single-node
                                 topology (the facts file may leave it
                                 null there, and it is filled in here)
               is_hypervisor     bool
               is_network_node   bool
               is_database_node  bool
             So the primary's mesh address is {{ nodes.primary.mesh_ip }}
             and sf2's is {{ nodes.sf2.mesh_ip }}. Host names come from the
             topology: localhost has only 'primary'; slim-tier has
             'primary', 'sf1' and 'sf2'; slim-primary has 'primary' and
             'sf1' to 'sf5'. A hyphenated name needs nodes['sf-1'].
  workspace  The absolute GITHUB_WORKSPACE, under which the caller's and
             the component checkouts sit side by side. Absent, and so an
             error to use, when the workspace is not known.

Schema
------

The rendered text must be a YAML mapping (or empty, which applies nothing)
with only these top-level keys, all optional:

  groups          {name: {hosts: [host, ...], vars: {key: value}}}. Each
                  group is added under all.children of the inventory, with
                  the named hosts (bare membership) and the vars as group
                  vars. A group name must be a valid ansible identifier and
                  must not already exist; every host must already be in the
                  inventory. hosts is required and non-empty; vars is
                  optional.
  extra_vars      A mapping, passed to site.yml as a second --extra-vars
                  @file after deploy-collection.sh's fixed string, so a key
                  here overrides a fixed one of the same name.
  mariadb_sql     A string of SQL, run once with sudo mariadb on the
                  primary, before the deploy.
  redeploy_check  {units: [glob, ...]}: deploy a second time and require
                  that no unit matching a glob restarted. Globs may use
                  letters, digits and . _ - @ : * ? [ ] only.
  test_env        {NAME: 'value'}: exported into the functional test run.
                  Names are shell identifiers; values must be strings, so
                  quote numbers.

Anything else -- an unknown key at any level, a value of the wrong type, an
unknown host -- is refused before anything is written.

Outputs
-------

Every file is written beside the inventory, mode 0600, because extra vars
and SQL carry credentials:

  ci-inventory.yaml (the --inventory path)
                                   rewritten with the profile's groups,
                                   only when the profile has groups
  ci-deploy-profile.json           the validated profile, every key present
                                   (empty when the profile omitted it)
  ci-deploy-profile-extra-vars.json
                                   the extra vars, '{}' when there are none;
                                   deploy-collection.sh's seventh argument
  ci-deploy-profile.sql            the SQL, empty when there is none
  ci-deploy-profile-test-env.sh    one "export NAME='value'" line per
                                   test_env entry, shell-quoted, so it can
                                   be sourced; empty when there are none
  ci-deploy-profile-redeploy-units one unit glob per line; absent when the
                                   profile asks for no redeploy check

Nothing secret is logged: the summary on stderr names groups, hosts and
variable names, never values. Errors name keys and hosts, never values,
and a YAML error reports a line and column rather than the offending text.
"""

import argparse
import json
import os
import re
import shlex
import sys

import jinja2
import yaml


TOP_LEVEL_KEYS = ('groups', 'extra_vars', 'mariadb_sql', 'redeploy_check', 'test_env')
GROUP_KEYS = ('hosts', 'vars')
REDEPLOY_CHECK_KEYS = ('units',)
RESERVED_GROUPS = ('all', 'ungrouped')

IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
UNIT_GLOB = re.compile(r'^[A-Za-z0-9_.@:*?\[\]-]+$')

PROFILE_FILE = 'ci-deploy-profile.json'
EXTRA_VARS_FILE = 'ci-deploy-profile-extra-vars.json'
SQL_FILE = 'ci-deploy-profile.sql'
TEST_ENV_FILE = 'ci-deploy-profile-test-env.sh'
REDEPLOY_UNITS_FILE = 'ci-deploy-profile-redeploy-units'


class ProfileError(Exception):
    pass


def facts_context(facts, workspace=None):
    """Build the template context from a loaded topology facts file."""
    nodes = {}
    for spec in facts['nodes']:
        node = dict(spec)
        node['mesh_ip'] = spec.get('mesh_ip') or spec['egress_ip']
        nodes[spec['name']] = node
    context = {'nodes': nodes}
    if workspace:
        context['workspace'] = workspace
    return context


def render(template_text, context):
    """Render a profile template and parse the result as YAML."""
    environment = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    try:
        rendered = environment.from_string(template_text).render(**context)
    except jinja2.TemplateSyntaxError as e:
        raise ProfileError('template syntax error at line %d: %s' % (e.lineno, e.message))
    except jinja2.UndefinedError as e:
        raise ProfileError('template names something the facts do not define: %s' % e.message)

    try:
        return yaml.safe_load(rendered)
    except yaml.MarkedYAMLError as e:
        # The default rendering of this error quotes the offending line,
        # which may hold a credential, so report only where it is.
        mark = e.problem_mark
        raise ProfileError('rendered profile is not valid YAML at line %d column %d: %s'
                           % (mark.line + 1, mark.column + 1, e.problem))
    except yaml.YAMLError:
        raise ProfileError('rendered profile is not valid YAML')


def _type_name(value):
    return type(value).__name__


def _require_mapping(value, where):
    if not isinstance(value, dict):
        raise ProfileError('%s must be a mapping, not %s' % (where, _type_name(value)))
    for key in value:
        if not isinstance(key, str):
            raise ProfileError('%s has a non-string key of type %s' % (where, _type_name(key)))
    return value


def _optional(mapping, key, default):
    """Return mapping[key], or default when it is absent or null."""
    value = mapping.get(key)
    return default if value is None else value


def _refuse_unknown_keys(value, allowed, where):
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ProfileError('%s has unknown key(s) %s; allowed: %s'
                           % (where, ', '.join(unknown), ', '.join(allowed)))


def validate(profile, inventory_hosts, inventory_groups):
    """Check a parsed profile and return it normalised.

    The result has every top-level key, with an empty value where the
    profile omitted one, and redeploy_check None when no check is asked
    for.
    """
    if profile is None:
        profile = {}
    _require_mapping(profile, 'the profile')
    _refuse_unknown_keys(profile, TOP_LEVEL_KEYS, 'the profile')

    groups = _optional(profile, 'groups', {})
    _require_mapping(groups, 'groups')
    for name, group in groups.items():
        where = 'group %s' % name
        if not IDENTIFIER.match(name):
            raise ProfileError('%s is not a valid ansible group name' % where)
        if name in inventory_groups or name in RESERVED_GROUPS:
            raise ProfileError('%s already exists in the inventory; a profile may only add groups' % where)
        _require_mapping(group, where)
        _refuse_unknown_keys(group, GROUP_KEYS, where)
        hosts = group.get('hosts')
        if not isinstance(hosts, list) or not hosts:
            raise ProfileError('%s needs a non-empty list of hosts' % where)
        for host in hosts:
            if not isinstance(host, str):
                raise ProfileError('%s lists a host of type %s' % (where, _type_name(host)))
            if host not in inventory_hosts:
                raise ProfileError('%s names host %s, which is not in the inventory (known: %s)'
                                   % (where, host, ', '.join(sorted(inventory_hosts))))
        if len(set(hosts)) != len(hosts):
            raise ProfileError('%s lists a host more than once' % where)
        if group.get('vars') is not None:
            _require_mapping(group['vars'], '%s vars' % where)

    extra_vars = _require_mapping(_optional(profile, 'extra_vars', {}), 'extra_vars')

    mariadb_sql = _optional(profile, 'mariadb_sql', '')
    if not isinstance(mariadb_sql, str):
        raise ProfileError('mariadb_sql must be a string, not %s' % _type_name(mariadb_sql))

    redeploy_check = profile.get('redeploy_check')
    if redeploy_check is not None:
        _require_mapping(redeploy_check, 'redeploy_check')
        _refuse_unknown_keys(redeploy_check, REDEPLOY_CHECK_KEYS, 'redeploy_check')
        units = redeploy_check.get('units')
        if not isinstance(units, list) or not units:
            raise ProfileError('redeploy_check needs a non-empty list of units')
        for unit in units:
            if not isinstance(unit, str) or not UNIT_GLOB.match(unit):
                raise ProfileError('redeploy_check unit %r is not a plain unit glob' % (unit,))

    test_env = _require_mapping(_optional(profile, 'test_env', {}), 'test_env')
    for name, value in test_env.items():
        if not IDENTIFIER.match(name):
            raise ProfileError('test_env name %s is not a shell identifier' % name)
        if not isinstance(value, str):
            raise ProfileError('test_env %s must be a string, not %s; quote it'
                               % (name, _type_name(value)))

    return {
        'groups': groups,
        'extra_vars': extra_vars,
        'mariadb_sql': mariadb_sql,
        'redeploy_check': redeploy_check,
        'test_env': test_env,
    }


def inventory_names(inventory):
    """Return the (hosts, groups) an inventory already defines."""
    hosts = set()
    groups = set()

    def walk(name, group):
        groups.add(name)
        group = group or {}
        hosts.update((group.get('hosts') or {}).keys())
        for child, body in (group.get('children') or {}).items():
            walk(child, body)

    for name, body in inventory.items():
        walk(name, body)
    return hosts, groups


def merge_groups(inventory, groups):
    """Add the profile's groups under all.children, leaving the rest alone."""
    children = inventory['all']['children']
    for name, group in groups.items():
        body = {'hosts': {host: None for host in group['hosts']}}
        if group.get('vars'):
            body['vars'] = group['vars']
        children[name] = body
    return inventory


def render_test_env(test_env):
    return ''.join('export %s=%s\n' % (name, shlex.quote(value)) for name, value in test_env.items())


def write_private(path, text):
    """Write text to path, mode 0600 even if the file already existed."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(text)


def resolve_profile_path(workspace, profile):
    if os.path.isabs(profile):
        raise ProfileError('deploy_profile %s must be relative to GITHUB_WORKSPACE' % profile)
    path = os.path.join(workspace, profile)
    if not os.path.isfile(path):
        raise ProfileError('deploy_profile %s does not exist under %s' % (profile, workspace))
    return path


def apply(profile_path, facts, inventory_path, workspace=None):
    """Render, validate and apply a profile; return the normalised profile.

    Every output is rendered before the first is written, so a profile
    that fails anywhere leaves the inventory and the output files as they
    were.
    """
    with open(profile_path) as f:
        template_text = f.read()
    with open(inventory_path) as f:
        inventory = yaml.safe_load(f)

    hosts, groups = inventory_names(inventory)
    profile = validate(render(template_text, facts_context(facts, workspace)), hosts, groups)

    output_dir = os.path.dirname(os.path.abspath(inventory_path))
    outputs = {}
    try:
        outputs[os.path.join(output_dir, PROFILE_FILE)] = json.dumps(profile, indent=2) + '\n'
        outputs[os.path.join(output_dir, EXTRA_VARS_FILE)] = json.dumps(profile['extra_vars'], indent=2) + '\n'
    except TypeError:
        raise ProfileError('the profile holds a value that is not plain data, such as an unquoted date; quote it')
    outputs[os.path.join(output_dir, SQL_FILE)] = profile['mariadb_sql']
    outputs[os.path.join(output_dir, TEST_ENV_FILE)] = render_test_env(profile['test_env'])
    units_path = os.path.join(output_dir, REDEPLOY_UNITS_FILE)
    if profile['redeploy_check']:
        outputs[units_path] = ''.join('%s\n' % unit for unit in profile['redeploy_check']['units'])
    if profile['groups']:
        outputs[inventory_path] = '---\n' + yaml.safe_dump(
            merge_groups(inventory, profile['groups']), default_flow_style=False, sort_keys=False)

    for path, text in outputs.items():
        write_private(path, text)
    if not profile['redeploy_check'] and os.path.exists(units_path):
        os.unlink(units_path)
    return profile


def summary(profile):
    """Describe what a profile applied, naming keys but never values."""
    lines = []
    for name, group in profile['groups'].items():
        lines.append('group %s: hosts %s; vars %s' % (
            name, ', '.join(group['hosts']), ', '.join(sorted(group.get('vars') or {})) or '(none)'))
    lines.append('extra_vars: %s' % (', '.join(sorted(profile['extra_vars'])) or '(none)'))
    lines.append('mariadb_sql: %d bytes' % len(profile['mariadb_sql'].encode()))
    if profile['redeploy_check']:
        lines.append('redeploy_check: %s' % ', '.join(profile['redeploy_check']['units']))
    else:
        lines.append('redeploy_check: (none)')
    lines.append('test_env: %s' % (', '.join(sorted(profile['test_env'])) or '(none)'))
    return lines


def main():
    parser = argparse.ArgumentParser(description='Apply a deploy profile to a CI cluster deploy.')
    parser.add_argument('--profile', required=True,
                        help='Path to the profile template, relative to the workspace.')
    parser.add_argument('--facts-file', required=True,
                        help='Path to the JSON topology facts file written by the topology playbook.')
    parser.add_argument('--inventory', required=True,
                        help='Path to the generated inventory; outputs are written beside it.')
    parser.add_argument('--workspace', default=os.environ.get('GITHUB_WORKSPACE'),
                        help='The workspace the profile path is relative to (default: $GITHUB_WORKSPACE).')
    args = parser.parse_args()
    if not args.workspace:
        parser.error('--workspace is required when GITHUB_WORKSPACE is not set')

    with open(args.facts_file) as f:
        facts = json.load(f)

    try:
        profile = apply(resolve_profile_path(args.workspace, args.profile), facts, args.inventory,
                        workspace=os.path.abspath(args.workspace))
    except ProfileError as e:
        print('::error title=Deploy profile rejected::%s: %s' % (args.profile, e))
        return 1

    sys.stderr.write('Applied deploy profile %s\n' % args.profile)
    for line in summary(profile):
        sys.stderr.write('  %s\n' % line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
