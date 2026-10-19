# Copyright 2026 Michael Still and contributors

"""Tests for tools/check-dist.py and check-dist/action.yml."""

import os
import subprocess
import sys
import tempfile
import unittest
import zipfile

import yaml

from tests import helpers


cd = helpers.load_script('tools/check-dist.py', 'check_dist')
SCRIPT = os.path.join(helpers.REPO_ROOT, 'tools', 'check-dist.py')


class CheckDistTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dist = os.path.join(tmp.name, 'dist')
        os.mkdir(self.dist)

    def wheel(self, name, members, directory=None):
        path = os.path.join(directory or self.dist, name)
        with zipfile.ZipFile(path, 'w') as zf:
            for member in members:
                zf.writestr(member, '')
        return path

    def run_script(self, path=None, forbid=None):
        env = {'PATH': os.environ.get('PATH', '')}
        if forbid is not None:
            env['CHECK_DIST_FORBID'] = forbid
        if path is not None:
            env['CHECK_DIST_PATH'] = path
        proc = subprocess.run([sys.executable, '-I', SCRIPT], env=env, capture_output=True, text=True)
        return proc.returncode, proc.stdout + proc.stderr

    def test_clean_wheel_passes(self):
        self.wheel('a-1-py3-none-any.whl', ['pkg/__init__.py', 'a-1.dist-info/METADATA'])
        rc, out = self.run_script(self.dist)
        self.assertEqual(0, rc, out)
        self.assertIn('a-1-py3-none-any.whl OK (2 members)', out)
        self.assertNotIn('::error::', out)

    def test_default_path_is_dist(self):
        self.wheel('a-1-py3-none-any.whl', ['pkg/__init__.py'])
        proc = subprocess.run([sys.executable, '-I', SCRIPT], env={}, cwd=os.path.dirname(self.dist),
                              capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stdout)

    def test_empty_path_falls_back_to_dist(self):
        self.wheel('a-1-py3-none-any.whl', ['pkg/tests/t.py'])
        proc = subprocess.run([sys.executable, '-I', SCRIPT], env={'CHECK_DIST_PATH': ''},
                              cwd=os.path.dirname(self.dist), capture_output=True, text=True)
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn(os.path.join('dist', 'a-1-py3-none-any.whl'), proc.stdout)
        self.assertIn('pkg/tests/t.py', proc.stdout)

    def test_tests_directory_fails(self):
        self.wheel('a.whl', ['pkg/__init__.py', 'pkg/tests/test_x.py'])
        rc, out = self.run_script(self.dist)
        self.assertEqual(1, rc)
        self.assertIn('::error::', out)
        self.assertIn('pkg/tests/test_x.py', out)
        self.assertNotIn('pkg/__init__.py', out)
        self.assertIn('include-package-data', out)
        self.assertIn('setuptools_scm', out)

    def test_underscore_tests_directory_fails(self):
        self.wheel('a.whl', ['pkg/corpus_tests/x.ovf'])
        rc, out = self.run_script(self.dist)
        self.assertEqual(1, rc)
        self.assertIn('pkg/corpus_tests/x.ovf', out)

    def test_nested_tests_directory_fails(self):
        self.wheel('a.whl', ['pkg/sub/tests/a.py'])
        rc, out = self.run_script(self.dist)
        self.assertEqual(1, rc)
        self.assertIn('pkg/sub/tests/a.py', out)

    def test_root_tests_directory_fails(self):
        self.wheel('a.whl', ['tests/a.py'])
        rc, _ = self.run_script(self.dist)
        self.assertEqual(1, rc)

    def test_directory_entry_fails(self):
        self.wheel('a.whl', ['pkg/tests/'])
        rc, _ = self.run_script(self.dist)
        self.assertEqual(1, rc)

    def test_forbid_glob_fails(self):
        self.wheel('a.whl', ['pkg/__init__.py', 'pkg/data/secret.key'])
        rc, out = self.run_script(self.dist, forbid='*.pem\n\n  pkg/data/*.key  \n')
        self.assertEqual(1, rc)
        self.assertIn('pkg/data/secret.key', out)
        self.assertIn('forbid glob', out)

    def test_forbid_only_match_gives_no_hint(self):
        self.wheel('a.whl', ['pkg/__init__.py', 'pkg/data/secret.key'])
        rc, out = self.run_script(self.dist, forbid='*.key')
        self.assertEqual(1, rc)
        self.assertIn('pkg/data/secret.key', out)
        self.assertNotIn('hint', out)

    def test_hint_follows_a_test_directory_beside_other_failures(self):
        self.wheel('a.whl', ['pkg/data/secret.key'])
        self.wheel('b.whl', ['pkg/tests/t.py'])
        with open(os.path.join(self.dist, 'c.whl'), 'w') as f:
            f.write('not a zip')
        rc, out = self.run_script(self.dist, forbid='*.key')
        self.assertEqual(1, rc)
        self.assertEqual(1, out.count('check-dist: hint: '), out)
        self.assertIn('include-package-data', out)

    def test_forbid_without_hit_passes(self):
        self.wheel('a.whl', ['pkg/__init__.py'])
        rc, out = self.run_script(self.dist, forbid='*.pem')
        self.assertEqual(0, rc, out)

    def test_component_not_substring(self):
        self.wheel('a.whl', ['pkg/contests.py', 'pkg/tests.py', 'pkg/attests/x.py', 'pkg/tests'])
        rc, out = self.run_script(self.dist)
        self.assertEqual(0, rc, out)

    def test_tests_inside_a_component_name_passes(self):
        # Only a component ending in _tests counts, not one containing it.
        self.wheel('a.whl', ['pkg/my_tests_x/a.py', 'pkg/tests_x/b.py', 'pkg/_testsx/c.py'])
        rc, out = self.run_script(self.dist)
        self.assertEqual(0, rc, out)
        self.assertIn('OK (3 members)', out)

    def test_two_wheels_one_bad_reports_both(self):
        self.wheel('good.whl', ['pkg/__init__.py'])
        self.wheel('bad.whl', ['pkg/tests/t.py'])
        rc, out = self.run_script(self.dist)
        self.assertEqual(1, rc)
        self.assertIn('good.whl OK', out)
        self.assertIn('bad.whl contains 1 member', out)

    def test_two_bad_wheels_both_reported(self):
        self.wheel('b1.whl', ['pkg/tests/t.py'])
        self.wheel('b2.whl', ['pkg/x_tests/t.py'])
        rc, out = self.run_script(self.dist)
        self.assertEqual(1, rc)
        self.assertIn('b1.whl contains', out)
        self.assertIn('b2.whl contains', out)

    def test_no_wheels_fails(self):
        with open(os.path.join(self.dist, 'a.tar.gz'), 'w') as f:
            f.write('x')
        rc, out = self.run_script(self.dist)
        self.assertEqual(1, rc)
        self.assertIn('::error::check-dist: no .whl file', out)
        self.assertNotIn('Traceback', out)

    def test_missing_directory_fails(self):
        rc, out = self.run_script(os.path.join(self.dist, 'nope'))
        self.assertEqual(1, rc)
        self.assertNotIn('Traceback', out)

    def test_non_zip_wheel_fails_cleanly(self):
        with open(os.path.join(self.dist, 'a.whl'), 'w') as f:
            f.write('not a zip')
        rc, out = self.run_script(self.dist)
        self.assertEqual(1, rc)
        self.assertIn('::error::check-dist:', out)
        self.assertIn('not a readable zip', out)
        self.assertNotIn('Traceback', out)
        self.assertNotIn('hint', out)

    def test_path_with_space(self):
        spaced = os.path.join(os.path.dirname(self.dist), 'my dist [x]')
        os.mkdir(spaced)
        self.wheel('a.whl', ['pkg/tests/t.py'], directory=spaced)
        rc, out = self.run_script(spaced)
        self.assertEqual(1, rc)
        self.assertIn('pkg/tests/t.py', out)

    def test_member_with_space_is_reported_whole(self):
        self.wheel('a.whl', ['pkg/tests/my file.py'])
        rc, out = self.run_script(self.dist)
        self.assertEqual(1, rc)
        self.assertIn('pkg/tests/my file.py', out)

    def test_output_is_escaped_so_a_member_cannot_inject_a_command(self):
        self.wheel('a.whl', ['pkg/tests/x\n::stop-commands::x/y.py'])
        rc, out = self.run_script(self.dist)
        self.assertEqual(1, rc)
        self.assertIn('x%0A::stop-commands::x', out)
        for line in out.splitlines():
            self.assertFalse(line.startswith('::stop-commands::'), line)

    def test_wheel_path_is_escaped(self):
        odd = os.path.join(os.path.dirname(self.dist), 'a%b')
        os.mkdir(odd)
        self.wheel('a.whl', ['pkg/tests/t.py'], directory=odd)
        rc, out = self.run_script(odd)
        self.assertEqual(1, rc)
        self.assertIn('a%25b', out)

    def test_backslash_in_member_name_fails(self):
        self.wheel('a.whl', ['pkg\\tests\\x.py'])
        rc, out = self.run_script(self.dist)
        self.assertEqual(1, rc)
        self.assertIn('backslash in member name (wheels use /)', out)

    def test_argv_path_overrides_environment(self):
        self.wheel('a.whl', ['pkg/tests/t.py'])
        good = os.path.join(os.path.dirname(self.dist), 'good')
        os.mkdir(good)
        self.wheel('b.whl', ['pkg/x.py'], directory=good)
        proc = subprocess.run([sys.executable, '-I', SCRIPT, good], env={'CHECK_DIST_PATH': self.dist},
                              capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stdout)
        self.assertIn('b.whl OK', proc.stdout)

    def test_wheels_in_subdirectories_are_not_checked(self):
        os.mkdir(os.path.join(self.dist, 'sub'))
        self.wheel('a.whl', ['pkg/tests/t.py'], directory=os.path.join(self.dist, 'sub'))
        rc, out = self.run_script(self.dist)
        self.assertEqual(1, rc)
        self.assertIn('no .whl file', out)

    def test_functions(self):
        self.assertIsNone(cd.offending_reason('pkg/contests.py', []))
        self.assertIsNotNone(cd.offending_reason('pkg/tests/x', []))
        self.assertIsNotNone(cd.offending_reason('pkg/a_tests/x', []))
        self.assertIsNotNone(cd.offending_reason('pkg/x.bak', ['*.bak']))
        self.assertEqual('a_tests', cd.test_directory('pkg/a_tests/tests/x'))
        self.assertIsNone(cd.test_directory('pkg/x.bak'))
        self.assertEqual(['a', 'b'], cd.parse_forbid('a\n\n b \n'))
        self.assertEqual([], cd.parse_forbid(None))
        self.assertEqual(1, cd.run(self.dist, []))


class ActionTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        with open(os.path.join(helpers.REPO_ROOT, 'check-dist', 'action.yml')) as f:
            self.action = yaml.safe_load(f)

    def test_inputs_reach_script_only_through_env(self):
        steps = self.action['runs']['steps']
        self.assertTrue(steps)
        for step in steps:
            self.assertIn('python3 -I ', step['run'])
            self.assertNotIn('inputs.', step['run'])
            self.assertNotIn('${{', step['run'])
            self.assertIn('check-dist.py', step['run'])
        env = steps[0]['env']
        self.assertEqual('${{ inputs.path }}', env['CHECK_DIST_PATH'])
        self.assertEqual('${{ inputs.forbid }}', env['CHECK_DIST_FORBID'])

    def test_defaults(self):
        self.assertEqual('dist/', self.action['inputs']['path']['default'])
        self.assertFalse(self.action['inputs']['forbid']['required'])
