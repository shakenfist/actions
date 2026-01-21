#!/usr/bin/env python3
"""Sync documentation from a component repository into shakenfist docs.

This script copies markdown documentation from a component repository
(e.g., kerbside, clingwrap) into the shakenfist documentation structure,
updates internal links to work in the new location, and generates a
mkdocs.yml navigation snippet.

Usage:
    sync_component_docs.py <component_name> <source_dir> <dest_dir>

Example:
    sync_component_docs.py kerbside /workspace/kerbside/docs \
        /workspace/shakenfist/docs/components/kerbside

The script outputs a YAML navigation snippet to stdout that can be
inserted into mkdocs.yml.

Template substitution:
    Use --template and --output to substitute placeholders in a template file.
    The placeholder %%<component_name>%% will be replaced with the nav snippet.

    sync_component_docs.py kerbside src dest --template mkdocs.yml.tmpl \\
        --output mkdocs.yml
"""

import argparse
import os
import re
import shutil
import sys
from pathlib import Path


def update_markdown_links(content: str, component_name: str) -> str:
    """Update internal markdown links to work in the new location.

    Transforms relative links like `](somefile.md)` or `](./subdir/file.md)`
    to absolute paths like `](/components/kerbside/somefile.md)`.

    Only updates links to .md files that don't already have an absolute path
    or external URL.
    """
    # Pattern matches markdown links: ](path.md) or ](path.md#anchor)
    # Captures the path before .md and any anchor
    # Excludes links that start with http://, https://, or /
    pattern = r'\]\((?!https?://)(?!/)([^)#]+\.md)(#[^)]*)?\)'

    def replace_link(match: re.Match) -> str:
        path = match.group(1)
        anchor = match.group(2) or ''

        # Remove leading ./ if present
        if path.startswith('./'):
            path = path[2:]

        # Build the new absolute path
        new_path = f'/components/{component_name}/{path}'
        return f']({new_path}{anchor})'

    return re.sub(pattern, replace_link, content)


def copy_docs(
    component_name: str, source_dir: Path, dest_dir: Path
) -> list[tuple[str, str]]:
    """Copy markdown files from source to destination, updating links.

    Returns a list of (filename, title) tuples for non-index files.
    """
    # Clean the destination directory
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    doc_files = []

    # Find all markdown files in source
    for source_file in source_dir.rglob('*.md'):
        # Get relative path from source_dir
        rel_path = source_file.relative_to(source_dir)

        # Create destination path
        dest_file = dest_dir / rel_path

        # Ensure parent directories exist
        dest_file.parent.mkdir(parents=True, exist_ok=True)

        # Read, transform, and write the file
        content = source_file.read_text(encoding='utf-8')
        updated_content = update_markdown_links(content, component_name)
        dest_file.write_text(updated_content, encoding='utf-8')

        # Extract title from first heading (if present)
        title = extract_title(content, rel_path.stem)

        # Track non-index files for nav generation
        if rel_path.name != 'index.md':
            doc_files.append((str(rel_path), title))

    return doc_files


def extract_title(content: str, fallback: str) -> str:
    """Extract the title from markdown content.

    Looks for the first level-1 heading (# Title) or uses the fallback.
    """
    for line in content.split('\n'):
        line = line.strip()
        if line.startswith('# '):
            return line[2:].strip()
    # Fallback: convert filename to title case
    return fallback.replace('-', ' ').replace('_', ' ').title()


def generate_nav_snippet(
    component_name: str, doc_files: list[tuple[str, str]], indent: int = 8
) -> str:
    """Generate a mkdocs.yml navigation snippet for the component.

    Args:
        component_name: The component name (e.g., 'kerbside')
        doc_files: List of (filename, title) tuples
        indent: Base indentation (number of spaces)

    Returns:
        YAML navigation snippet as a string
    """
    display_name = component_name.title()
    base_path = f'components/{component_name}'
    spaces = ' ' * indent

    lines = [f'{spaces}- {display_name}: {base_path}/index.md']

    # Sort files alphabetically by title
    sorted_files = sorted(doc_files, key=lambda x: x[1].lower())

    for filename, title in sorted_files:
        lines.append(f'{spaces}    - "{title}": {base_path}/{filename}')

    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(
        description='Sync component documentation into shakenfist docs'
    )
    parser.add_argument(
        'component_name',
        help='Name of the component (e.g., kerbside, clingwrap)'
    )
    parser.add_argument(
        'source_dir',
        help='Source directory containing the component docs'
    )
    parser.add_argument(
        'dest_dir',
        help='Destination directory in shakenfist docs'
    )
    parser.add_argument(
        '--indent',
        type=int,
        default=8,
        help='Base indentation for YAML output (default: 8)'
    )
    parser.add_argument(
        '--template',
        help='Template file with %%component_name%% placeholders'
    )
    parser.add_argument(
        '--output',
        help='Output file for template substitution (requires --template)'
    )

    args = parser.parse_args()

    source_dir = Path(args.source_dir)
    dest_dir = Path(args.dest_dir)

    if not source_dir.exists():
        print(f'Error: Source directory does not exist: {source_dir}',
              file=sys.stderr)
        sys.exit(1)

    if not source_dir.is_dir():
        print(f'Error: Source is not a directory: {source_dir}',
              file=sys.stderr)
        sys.exit(1)

    # Validate template/output args
    if args.output and not args.template:
        print('Error: --output requires --template', file=sys.stderr)
        sys.exit(1)

    # Copy docs and get file list
    doc_files = copy_docs(args.component_name, source_dir, dest_dir)

    # Generate the nav snippet
    nav_snippet = generate_nav_snippet(
        args.component_name, doc_files, args.indent
    )

    # Handle template substitution or plain output
    if args.template:
        template_path = Path(args.template)
        if not template_path.exists():
            print(f'Error: Template file does not exist: {template_path}',
                  file=sys.stderr)
            sys.exit(1)

        template_content = template_path.read_text(encoding='utf-8')
        placeholder = f'%%{args.component_name}%%'
        result = template_content.replace(placeholder, nav_snippet)

        if args.output:
            output_path = Path(args.output)
            output_path.write_text(result, encoding='utf-8')
            print(f'Wrote {output_path}')
        else:
            print(result)
    else:
        print(nav_snippet)


if __name__ == '__main__':
    main()
