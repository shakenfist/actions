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
cp ${GITHUB_WORKSPACE}/shakenfist/mkdocs.yml.tmpl ${GITHUB_WORKSPACE}/mkdocs.yml

# Sync external docs and generate mkdocs.yml from template. Note that these
# are in the order they appear in the components nav bar, and that their
# component names might have been overridden by a component.yml file in
# the target repo's docs directory.
for external in \
        cloudgood \
        clingwrap \
        development \
        agent-python \
        kerbside \
        occystrap; do
    python3 "${SYNC_SCRIPT}" "${external}" \
        "${GITHUB_WORKSPACE}/${external}/docs" \
        "${GITHUB_WORKSPACE}/shakenfist/docs/components/${external}" \
        --template ${GITHUB_WORKSPACE}/mkdocs.yml \
        --output ${GITHUB_WORKSPACE}/mkdocs.yml.new
    mv ${GITHUB_WORKSPACE}/mkdocs.yml.new ${GITHUB_WORKSPACE}/mkdocs.yml
    git add docs/components/${external}
done

mv ${GITHUB_WORKSPACE}/mkdocs.yml ${GITHUB_WORKSPACE}/shakenfist/mkdocs.yml

# Did we change anything?
echo
echo "Check if we changed anything..."
git status
echo

# Did we find something new?
if [ $(git status | grep -c modified || true) -gt 0 ]; then
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
