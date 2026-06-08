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

Per-directory `order.yml`:
    A directory containing an `order.yml` file is treated as a strict
    nav whitelist: only files listed there appear in the navigation,
    and subdirectories are recursed into only when they have their own
    `order.yml`. Files in subdirectories are always copied to the
    destination regardless of nav visibility, so cross-links from
    navigable pages keep resolving to files that are intentionally
    hidden from the nav (for example, phase plans linked from a master
    plan).

    A root-level `order.yml` (in the docs root) is additionally treated
    as an allowlist for which root-level files are copied at all. Files
    at the docs root that are commented out of (or absent from)
    `order.yml` are not copied. This preserves the historical contract
    used by components like cloudgood, where commenting an entry out
    of `order.yml` is how an incomplete page is kept unpublished.

    Directories without an `order.yml` retain the historical behaviour:
    every `.md` file is navigable, sorted alphabetically by title, and
    every subdirectory containing markdown is recursed into.

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


def update_markdown_links(
    content: str, component_name: str, file_rel_path: Path | None = None
) -> str:
    """Update internal markdown links to work in the new location.

    Transforms relative links like `](somefile.md)` or `](./subdir/file.md)`
    to absolute paths like `](/components/kerbside/somefile.md)`.

    For files in subdirectories, handles relative paths like `](../index.md)`
    correctly by resolving them relative to the file's location.

    Args:
        content: The markdown content to process
        component_name: The component name for link rewriting
        file_rel_path: The file's path relative to the docs root (e.g.,
            Path('qcow2/qcow2-format.md')). Used to resolve relative links.

    Only updates links to .md files that don't already have an absolute path
    or external URL.
    """
    # Pattern matches markdown links: ](path.md) or ](path.md#anchor)
    # Captures the path before .md and any anchor
    # Excludes links that start with http://, https://, or /
    pattern = r'\]\((?!https?://)(?!/)([^)#]+\.md)(#[^)]*)?\)'

    # Determine the directory containing the current file
    if file_rel_path is not None:
        file_dir = file_rel_path.parent
    else:
        file_dir = Path('.')

    def replace_link(match: re.Match) -> str:
        path = match.group(1)
        anchor = match.group(2) or ''
        print(f'            link fixup for path {path} with anchor {anchor}')

        # Resolve the link relative to the file's directory
        # This handles cases like '../index.md' from a subdirectory
        link_path = Path(path)
        if file_dir != Path('.'):
            # Get the path relative to what would be the docs root
            # We need to work with string manipulation since we don't have
            # the actual filesystem paths here
            resolved_parts = (file_dir / link_path).parts
            # Normalize by removing . and resolving ..
            normalized_parts = []
            for part in resolved_parts:
                if part == '.':
                    continue
                elif part == '..':
                    if normalized_parts:
                        normalized_parts.pop()
                else:
                    normalized_parts.append(part)
            path = '/'.join(normalized_parts) if normalized_parts else path
        else:
            # Remove leading ./ if present
            if path.startswith('./'):
                path = path[2:]

        # Pages pretend to be directories for reasons
        if path.endswith('.md'):
            path = path.replace('.md', '/')

        # Build the new absolute path
        new_path = f'/components/{component_name}/{path}'
        replacement = f']({new_path}{anchor})'

        print(f'            became {replacement}')
        return replacement

    return re.sub(pattern, replace_link, content)


def copy_all_markdown(
    component_name: str, source_dir: Path, dest_dir: Path
) -> None:
    """Copy markdown files under source_dir to dest_dir.

    A root-level `order.yml` (`source_dir/order.yml`) is treated as a
    strict allowlist for root-level files: only `index.md` and files
    listed there are copied. This preserves the historical contract for
    components like cloudgood, where commenting an entry out of
    `order.yml` is the way to mark a page as incomplete and keep it
    unpublished.

    Files in subdirectories are always copied so cross-links from
    navigable pages (e.g. master plans linking to phase plans) resolve
    regardless of whether the target appears in the nav.

    The subdirectory structure is preserved and internal markdown links
    are rewritten via update_markdown_links.
    """
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    root_order = parse_order_file(source_dir / 'order.yml')
    root_allowlist: set[str] | None = None
    if root_order is not None:
        root_allowlist = {filename for filename, _ in root_order}
        root_allowlist.add('index.md')

    for source_file in sorted(source_dir.rglob('*.md')):
        rel_path = source_file.relative_to(source_dir)

        if (
            len(rel_path.parts) == 1
            and root_allowlist is not None
            and rel_path.name not in root_allowlist
        ):
            print(
                f'Skipping (not in root order.yml allowlist): {rel_path}'
            )
            continue

        dest_file = dest_dir / rel_path

        print('... Processing source file')
        print(f'        From {source_file}')
        print(f'        To {dest_file}')

        dest_file.parent.mkdir(parents=True, exist_ok=True)
        content = source_file.read_text(encoding='utf-8')
        updated = update_markdown_links(content, component_name, rel_path)
        dest_file.write_text(updated, encoding='utf-8')


def build_nav_tree(
    source_dir: Path, current_rel: Path | None = None
) -> dict:
    """Build a nav tree for current_rel relative to source_dir.

    A directory's `order.yml` is a strict whitelist for that directory's
    nav: files not listed are hidden, and subdirectories are recursed
    into only when they have their own `order.yml`. A directory without
    `order.yml` is fully discovered -- all `.md` files appear in the
    nav (sorted alphabetically by title), and every subdirectory
    containing markdown is recursed into.

    Returns a dict shaped:
        {
            'index': (filename, title) | None,  # basename of dir-local index
            'files': [(filename, title), ...],   # basenames in this dir
            'subdirs': {dirname: <nav_tree>, ...},
        }
    """
    if current_rel is None:
        current_rel = Path('.')
    abs_dir = source_dir / current_rel
    order_entries = parse_order_file(abs_dir / 'order.yml')

    index_entry: tuple[str, str] | None = None
    files: list[tuple[str, str]] = []

    if order_entries is not None:
        for filename, title in order_entries:
            target = abs_dir / filename
            if not target.exists():
                print(
                    f'Warning: order.yml entry not found, skipping: '
                    f'{target}'
                )
                continue
            if filename == 'index.md':
                index_entry = (filename, title)
            else:
                files.append((filename, title))
    else:
        for md in sorted(abs_dir.glob('*.md')):
            content = md.read_text(encoding='utf-8')
            title = extract_title(content, md.stem)
            if md.name == 'index.md':
                index_entry = (md.name, title)
            else:
                files.append((md.name, title))
        files.sort(key=lambda x: x[1].lower())

    subdirs: dict[str, dict] = {}
    for child in sorted(abs_dir.iterdir()):
        if not child.is_dir():
            continue
        if not any(child.rglob('*.md')):
            continue
        # When the current directory has an order.yml, only recurse into
        # subdirectories that also declare their own ordering. Without
        # an opt-in, an order.yml-controlled directory acts as a strict
        # whitelist for its nav.
        if order_entries is not None and not (child / 'order.yml').exists():
            continue
        subdirs[child.name] = build_nav_tree(
            source_dir, current_rel / child.name
        )

    return {
        'index': index_entry,
        'files': files,
        'subdirs': subdirs,
    }


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


def yaml_quote_title(title: str) -> str:
    """Quote a title for use as a YAML double-quoted scalar.

    Escapes backslashes and double quotes so titles containing inner
    quotes (e.g. `Diagnosing "video stream not keeping up" reports`)
    produce valid YAML.
    """
    escaped = title.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def generate_nav_snippet(
    component_name: str,
    nav_tree: dict,
    indent: int = 8,
    display_name_override: str | None = None,
) -> str:
    """Render a mkdocs.yml navigation snippet from a nav tree.

    The root section header uses display_name_override (e.g. from
    `component.yml`) or the title-cased component name. Subdirectory
    headers are derived from the directory name. Per-directory ordering
    and visibility come from the nav_tree built by build_nav_tree.
    """
    display_name = display_name_override or component_name.title()
    base_path = f'components/{component_name}'

    lines: list[str] = [f'{" " * indent}- {display_name}:']
    _emit_dir(
        lines, nav_tree, indent + 4, base_path, Path('.'), is_root=True
    )
    return '\n'.join(lines)


def _emit_dir(
    lines: list[str],
    tree: dict,
    indent: int,
    base_path: str,
    rel_dir: Path,
    is_root: bool,
) -> None:
    """Emit nav entries for a directory's tree node.

    The root section always labels the component's `index.md` as
    "Introduction" for consistency across the components nav.
    Subdirectory index pages use the title from `order.yml` (if listed)
    or the file's H1 heading.
    """
    spaces = ' ' * indent
    prefix = '' if rel_dir == Path('.') else f'{rel_dir.as_posix()}/'

    if is_root:
        lines.append(
            f'{spaces}- "Introduction": {base_path}/index.md'
        )
    elif tree['index'] is not None:
        filename, title = tree['index']
        lines.append(
            f'{spaces}- {yaml_quote_title(title)}: '
            f'{base_path}/{prefix}{filename}'
        )

    for filename, title in tree['files']:
        lines.append(
            f'{spaces}- {yaml_quote_title(title)}: '
            f'{base_path}/{prefix}{filename}'
        )

    for subdir_name, subtree in tree['subdirs'].items():
        display = subdir_name.replace('_', ' ').replace('-', ' ').title()
        lines.append(f'{spaces}- {display}:')
        _emit_dir(
            lines, subtree, indent + 4, base_path,
            rel_dir / subdir_name, is_root=False,
        )


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

    # Check for component.yml to override display name
    display_name_override = None
    component_yml_path = source_dir / 'component.yml'
    if component_yml_path.exists():
        try:
            component_data = yaml.safe_load(
                component_yml_path.read_text(encoding='utf-8')
            )
            if isinstance(component_data, dict):
                title = component_data.get('title')
                if title:
                    display_name_override = title
                    print(
                        f'Using title from component.yml:'
                        f' {display_name_override}'
                    )
        except yaml.YAMLError as e:
            print(f'Warning: Failed to parse component.yml: {e}')

    # Copy every markdown file (so cross-links resolve), then build the
    # nav tree from per-directory order.yml files.
    copy_all_markdown(args.component_name, source_dir, dest_dir)
    nav_tree = build_nav_tree(source_dir)

    # Copy component LICENSE if it differs from main repo
    copy_license_if_different(source_dir, dest_dir)

    # Generate the nav snippet
    nav_snippet = generate_nav_snippet(
        args.component_name, nav_tree, args.indent,
        display_name_override=display_name_override,
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
