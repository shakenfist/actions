"""Shared helper for loading the repository's Python helper scripts.

The scripts under tools/ and review-pr-with-claude/ are named with
hyphens and carry no .py-importable package, so they cannot be imported
by name. Load them from their path instead, the same way
shakenfist/development loads its own audit script for testing.
"""

import importlib.util
import os


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_script(relative_path, module_name):
    """Import a script by path and return it as a module.

    relative_path is relative to the repository root, for example
    'tools/ci-make-inventory.py'.
    """
    path = os.path.join(REPO_ROOT, relative_path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
