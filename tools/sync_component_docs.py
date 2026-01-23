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
import filecmp
import re
import shutil
import sys
from pathlib import Path

import yaml


def parse_order_file(order_path: Path) -> list[tuple[str, str]] | None:
    """Parse an order.yml file to get ordered list of files and titles.

    The order.yml format is a list of single-key dictionaries:
        - filename.md: Title
        - another.md: Another Title
        # - commented.md: This is skipped

    Returns a list of (filename, title) tuples, or None if the file doesn't
    exist or can't be parsed.
    """
    if not order_path.exists():
        return None

    try:
        content = order_path.read_text(encoding='utf-8')

        # Filter out commented lines before parsing
        lines = []
        for line in content.split('\n'):
            stripped = line.strip()
            if not stripped.startswith('#'):
                lines.append(line)
        filtered_content = '\n'.join(lines)

        data = yaml.safe_load(filtered_content)
        if not isinstance(data, list):
            print(f'Warning: order.yml is not a list, ignoring')
            return None

        result = []
        for item in data:
            if isinstance(item, dict) and len(item) == 1:
                filename, title = next(iter(item.items()))
                result.append((filename, title))
            else:
                print(f'Warning: Invalid entry in order.yml: {item}')

        return result
    except yaml.YAMLError as e:
        print(f'Warning: Failed to parse order.yml: {e}')
        return None


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
        print(f'            link fixup for path {path} with anchor {anchor}')

        # Pages pretend to be directories for reasons
        if path.endswith('.md'):
            path = path.replace('.md', '/')

        # Remove leading ./ if present
        if path.startswith('./'):
            path = path[2:]

        # Build the new absolute path
        new_path = f'/components/{component_name}/{path}'
        replacement = f']({new_path}{anchor})'

        print(f'            became {replacement}')
        return replacement

    return re.sub(pattern, replace_link, content)


def copy_docs(
    component_name: str,
    source_dir: Path,
    dest_dir: Path,
    ordered_files: list[tuple[str, str]] | None = None
) -> list[tuple[str, str]]:
    """Copy markdown files from source to destination, updating links.

    Args:
        component_name: The component name for link rewriting
        source_dir: Source directory containing markdown files
        dest_dir: Destination directory
        ordered_files: Optional list of (filename, title) tuples from
            order.yml. If provided, only these files are copied and index.md.
            If None, all .md files are discovered and copied.

    Returns a list of (filename, title) tuples for non-index files.
    """
    # Clean the destination directory
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    doc_files = []

    # Determine which files to process
    if ordered_files is not None:
        # Use the ordered list, but always include index.md first
        files_to_process = []
        index_file = source_dir / 'index.md'
        if index_file.exists():
            files_to_process.append(('index.md', None))

        for filename, title in ordered_files:
            if filename != 'index.md':
                files_to_process.append((filename, title))
    else:
        # Discover all markdown files
        files_to_process = []
        for source_file in source_dir.rglob('*.md'):
            rel_path = source_file.relative_to(source_dir)
            files_to_process.append((str(rel_path), None))

    # Process each file
    for filename, provided_title in files_to_process:
        source_file = source_dir / filename
        if not source_file.exists():
            print(f'Warning: File not found, skipping: {source_file}')
            continue

        rel_path = Path(filename)
        dest_file = dest_dir / rel_path

        print('... Processing source file')
        print(f'        From {source_file}')
        print(f'        To {dest_file}')

        # Ensure parent directories exist
        dest_file.parent.mkdir(parents=True, exist_ok=True)

        # Read, transform, and write the file
        content = source_file.read_text(encoding='utf-8')
        updated_content = update_markdown_links(content, component_name)
        dest_file.write_text(updated_content, encoding='utf-8')

        # Use provided title or extract from content
        if provided_title:
            title = provided_title
        else:
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
    component_name: str,
    doc_files: list[tuple[str, str]],
    indent: int = 8,
    preserve_order: bool = False
) -> str:
    """Generate a mkdocs.yml navigation snippet for the component.

    Args:
        component_name: The component name (e.g., 'kerbside')
        doc_files: List of (filename, title) tuples
        indent: Base indentation (number of spaces)
        preserve_order: If True, keep files in provided order. If False,
            sort alphabetically by title.

    Returns:
        YAML navigation snippet as a string
    """
    display_name = component_name.title()
    base_path = f'components/{component_name}'
    spaces = ' ' * indent

    lines = [
        f'{spaces}- {display_name}:',
        f'{spaces}    - "Introduction": {base_path}/index.md'
    ]

    # Sort files alphabetically by title, unless order should be preserved
    if preserve_order:
        files_to_use = doc_files
    else:
        files_to_use = sorted(doc_files, key=lambda x: x[1].lower())

    for filename, title in files_to_use:
        lines.append(f'{spaces}    - "{title}": {base_path}/{filename}')

    return '\n'.join(lines)


def copy_license_if_different(source_dir: Path, dest_dir: Path) -> bool:
    """Copy the component's LICENSE file if it differs from the main repo.

    The source LICENSE is expected in the parent of source_dir (since source_dir
    is typically the docs subdirectory). The main repo LICENSE is computed by
    traversing up from dest_dir to find the repository root.

    Args:
        source_dir: Source directory containing the component docs
        dest_dir: Destination directory in shakenfist docs

    Returns:
        True if a license was copied, False otherwise.
    """
    # Source LICENSE is in the component's root (parent of docs directory)
    source_license = source_dir.parent / 'LICENSE'
    if not source_license.exists():
        print('No LICENSE file found in component repository')
        return False

    # Main repo LICENSE: dest_dir is like .../shakenfist/docs/components/foo
    # We need to find .../shakenfist/LICENSE
    # Go up from dest_dir until we find a directory containing LICENSE
    main_license = None
    search_dir = dest_dir
    for _ in range(10):  # Safety limit
        search_dir = search_dir.parent
        candidate = search_dir / 'LICENSE'
        if candidate.exists():
            main_license = candidate
            break
        if search_dir == search_dir.parent:  # Reached filesystem root
            break

    if main_license is None:
        print('Warning: Could not find main repo LICENSE file')
        # Still copy the component license
        dest_license = dest_dir / 'LICENSE'
        shutil.copy2(source_license, dest_license)
        print(f'Copied component LICENSE to {dest_license}')
        return True

    # Compare licenses
    if filecmp.cmp(source_license, main_license, shallow=False):
        print('Component LICENSE matches main repo, not copying')
        return False

    # Licenses differ, copy the component license
    dest_license = dest_dir / 'LICENSE'
    shutil.copy2(source_license, dest_license)
    print(f'Component LICENSE differs from main repo, copied to {dest_license}')
    return True


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

    # Check for order.yml in source directory
    order_path = source_dir / 'order.yml'
    ordered_files = parse_order_file(order_path)
    if ordered_files is not None:
        print(f'Using order.yml with {len(ordered_files)} entries')
    else:
        print('No order.yml found, using filesystem discovery')

    # Copy docs and get file list
    doc_files = copy_docs(
        args.component_name, source_dir, dest_dir, ordered_files
    )

    # Copy component LICENSE if it differs from main repo
    copy_license_if_different(source_dir, dest_dir)

    # Generate the nav snippet
    nav_snippet = generate_nav_snippet(
        args.component_name, doc_files, args.indent,
        preserve_order=(ordered_files is not None)
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
