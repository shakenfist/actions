#!/bin/bash

cd shakenfist

datestamp=$(date "+%Y%m%d")
git checkout develop
git pull
git checkout -b sync-docs
git rebase develop

# Copy latest kerbside docs
cd docs
rm -rf kerbside
mkdir kerbside
cp -Rp ${GITHUB_WORKSPACE}/kerbside/docs/* kerbside/

# Did we find something new?
    if [ $(git diff | wc -l) -gt 0 ]; then
    echo "Change detected..."
    echo
    git diff

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