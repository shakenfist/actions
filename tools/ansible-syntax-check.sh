#!/bin/bash

# Syntax check the ansible playbooks.
#
# These playbooks have no other gate. Nothing in this repository calls
# them -- conductor runs them out of band on its own schedule -- so the
# first execution of a change is a nightly image build on the CI
# cluster, and a typo in a task name or a broken Jinja expression is
# found there or not at all.
#
# This is deliberately only a syntax check. yamllint and ansible-lint
# are both absent for the reasons written up in
# .pre-commit-config.yaml, and this is orthogonal to both: it parses
# what ansible will actually parse, and says nothing about style.
#
# Task files are skipped. --syntax-check wants a playbook, and a bare
# list of tasks is not one, so the playbooks are found by looking for
# a play with hosts rather than by listing them here -- a new playbook
# should be covered without anybody remembering to add it.
#
#   tools/ansible-syntax-check.sh [file ...]

set -o errexit
set -o nounset
set -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$#" -gt 0 ]; then
    candidates=("$@")
else
    mapfile -t candidates < <(find "${HERE}/ansible" -maxdepth 1 -name '*.yml' | sort)
fi

playbooks=()
for f in "${candidates[@]}"; do
    # A playbook's top level entries are plays, and a play names the
    # hosts it runs against.
    if grep -qE '^\s*(- )?hosts:' "${f}"; then
        playbooks+=("${f}")
    fi
done

if [ "${#playbooks[@]}" -eq 0 ]; then
    echo "No playbooks to check."
    exit 0
fi

echo "Syntax checking ${#playbooks[@]} playbook(s)."
# No inventory: --syntax-check parses the playbook and does not need
# one. It warns that the host patterns match nothing, which is true and
# not interesting here.
ANSIBLE_LOCALHOST_WARNING=False \
    ansible-playbook --syntax-check "${playbooks[@]}" 2>&1 \
    | grep -v 'Could not match supplied host pattern'
exit "${PIPESTATUS[0]}"
