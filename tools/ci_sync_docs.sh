#!/bin/bash

cd shakenfist

datestamp=$(date "+%Y%m%d")
git checkout develop
git pull
git checkout -b sync-docs
git rebase develop

cd docs/components

# Copy latest kerbside docs
rm -rf kerbside
mkdir kerbside
cd kerbside

for filename in $(find ${GITHUB_WORKSPACE}/kerbside/docs/ -type f -name "*.md" | \
        sed "s|${GITHUB_WORKSPACE}/kerbside/docs/||"); do
    cat "${GITHUB_WORKSPACE}/kerbside/docs/${filename}" | \
        sed 's|]\(\[(.*.md)\)|/\(components/kerbside/\1\)|' > ${filename}
done

KERBSIDE=$(
    echo "        - Kerbside: components/kerbside/index.md"
    for filename in $(find . -type f -name "*.md" | grep -v index.md); do
        title=$(head -1 ${filename} | sed 's|^# ||')
        filename=$(echo "${filename}" | sed 's|./||')
        echo "            - \"${title}\": components/kerbside/${filename}"
    done
)

cd ..
git add kerbside

# Regenerate mkdocs.yml
cd ${GITHUB_WORKSPACE}/shakenfist
sed "s/%%kerbside%%/${KERBSIDE}/" mkdocs.yml.tmpl > mkdocs.yml

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