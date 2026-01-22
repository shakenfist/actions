#!/bin/bash

cd ${GITHUB_WORKSPACE}

echo "Current state of ${GITHUB_WORKSPACE} workspace:"
ls -lrt
echo

cd shakenfist

datestamp=$(date "+%Y%m%d")
git checkout develop
git pull
git checkout -b sync-docs
git rebase develop

# Path to the sync script (in the actions repository)
SYNC_SCRIPT="${GITHUB_WORKSPACE}/actions/tools/sync_component_docs.py"

# Sync kerbside docs and generate mkdocs.yml from template
# The --template and --output flags handle the %%kerbside%% placeholder substitution
python3 "${SYNC_SCRIPT}" kerbside \
    "${GITHUB_WORKSPACE}/kerbside/docs" \
    "${GITHUB_WORKSPACE}/shakenfist/docs/components/kerbside" \
    --template ${GITHUB_WORKSPACE}/shakenfist/mkdocs.yml.tmpl \
    --output ${GITHUB_WORKSPACE}/shakenfist/mkdocs.yml
git add docs/components/kerbside

# To add additional component syncs, chain them by using the previous output
# as the next template. For example:
# python3 "${SYNC_SCRIPT}" clingwrap \
#     "${GITHUB_WORKSPACE}/clingwrap/docs" \
#     "${GITHUB_WORKSPACE}/shakenfist/docs/components/clingwrap" \
#     --template ${GITHUB_WORKSPACE}/shakenfist/mkdocs.yml \
#     --output ${GITHUB_WORKSPACE}/shakenfist/mkdocs.yml
# git add docs/components/clingwrap

# Did we change anything?
echo
echo "Check if we changed anything..."
git status
echo

# Did we find something new?
if [ $(git diff | wc -l) -gt 0 ]; then
    echo "Change detected..."
    echo

    git config --global user.name "shakenfist-bot"
    git config --global user.email "bot@shakenfist.com"
    git commit -a -m "Automated documentation sync for ${datestamp}."
    git push -f origin sync-docs
    echo
    gh pr create \
        --assignee mikalstill \
        --reviewer mikalstill \
        --title "Automated documentation sync for ${datestamp}." \
        --body "Automated documentation sync."
    echo
    echo "Pull request created."
fi