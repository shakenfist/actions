"""Tests for tools/sync_component_docs.py nav generation."""

import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.helpers import load_script


sync = load_script('tools/sync_component_docs.py', 'sync_component_docs')


class NavSectionTitleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.write('index.md', '# Kerbside\n')

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel_path, content):
        path = self.root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding='utf-8')

    def nav(self):
        return sync.generate_nav_snippet('kerbside', sync.build_nav_tree(self.root), indent=0)

    def test_list_form_keeps_title_cased_directory_label(self):
        self.write('spice/order.yml', """\
            - channels.md: Channels
            """)
        self.write('spice/channels.md', '# Channels\n')
        self.assertEqual(
            '- Kerbside:\n'
            '    - "Introduction": components/kerbside/index.md\n'
            '    - Spice:\n'
            '        - "Channels": components/kerbside/spice/channels.md',
            self.nav())

    def test_directory_without_order_file_keeps_its_label(self):
        self.write('use-cases/vdi.md', '# VDI\n')
        self.assertIn('    - Use Cases:\n', self.nav())

    def test_mapping_form_names_the_section(self):
        self.write('spice/order.yml', """\
            title: SPICE protocol
            pages:
              - channels.md: Channels
              # - hidden.md: Hidden
              - auth.md: Authentication
            """)
        self.write('spice/channels.md', '# Channels\n')
        self.write('spice/auth.md', '# Auth\n')
        self.write('spice/hidden.md', '# Hidden\n')
        self.assertEqual(
            '- Kerbside:\n'
            '    - "Introduction": components/kerbside/index.md\n'
            '    - "SPICE protocol":\n'
            '        - "Channels": components/kerbside/spice/channels.md\n'
            '        - "Authentication": components/kerbside/spice/auth.md',
            self.nav())

    def test_title_with_yaml_special_characters_is_quoted(self):
        self.write('spice/order.yml', """\
            title: 'SPICE: the "protocol"'
            pages:
              - channels.md: Channels
            """)
        self.write('spice/channels.md', '# Channels\n')
        self.assertIn('    - "SPICE: the \\"protocol\\"":\n', self.nav())

    def test_mapping_without_title_keeps_directory_label(self):
        self.write('spice/order.yml', """\
            pages:
              - channels.md: Channels
            """)
        self.write('spice/channels.md', '# Channels\n')
        self.assertIn('    - Spice:\n', self.nav())

    def test_non_string_title_is_ignored(self):
        self.write('spice/order.yml', """\
            title: [not, a, string]
            pages:
              - channels.md: Channels
            """)
        self.write('spice/channels.md', '# Channels\n')
        self.assertIn('    - Spice:\n', self.nav())

    def test_root_order_title_does_not_rename_component(self):
        self.write('order.yml', """\
            title: Something else
            pages:
              - guide.md: Guide
            """)
        self.write('guide.md', '# Guide\n')
        self.assertEqual(
            '- Kerbside:\n'
            '    - "Introduction": components/kerbside/index.md\n'
            '    - "Guide": components/kerbside/guide.md',
            self.nav())

    def test_mapping_form_root_order_still_gates_copying(self):
        self.write('order.yml', """\
            title: Ignored
            pages:
              - guide.md: Guide
            """)
        self.write('guide.md', '# Guide\n')
        self.write('draft.md', '# Draft\n')
        with tempfile.TemporaryDirectory() as dest:
            sync.copy_all_markdown('kerbside', self.root, Path(dest))
            copied = sorted(p.name for p in Path(dest).rglob('*.md'))
        self.assertEqual(['guide.md', 'index.md'], copied)


if __name__ == '__main__':
    unittest.main()
