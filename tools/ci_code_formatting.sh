#!/bin/bash

# $1 is the minimum python version, as a small string. For example "36".

export skip_grpc="false"
export positional_args=()

while [[ $# -gt 0 ]]; do
  case $1 in
    --skip-grpc)
      export skip_grpc="true"
      echo "Will skip grpc generation."
      shift
      ;;
    -*|--*)
      echo "Unknown option $1"
      exit 1
      ;;
    *)
      positional_args+=("$1")
      shift
      ;;
  esac
done

pyupgrade --help > /dev/null
reorder-python-imports --help > /dev/null

datestamp=$(date "+%Y%m%d")
git checkout develop
git pull
git checkout -b formatting-automations
git rebase develop

# We only want to change five files at a time
changed=0

# Regenerate gPRC code and see if its changed
if [ ${skip_grpc} == "false" ]; then
    cd shakenfist
    ../protos/_make_stubs.sh
    delta=$( git diff | grep -c "diff" || true )
    echo "${delta} gRPC generated files were modified"
    changed=$(( ${changed} + ${delta} ))
fi

if [ ${changed} -lt 5 ]; then
    # Run our code formatting tools
    for file in $( find . -type f -name "*.py" | egrep -v "(_pb2.py|pb2_grpc.py|.github)"); do
        # pyupgrade
        out=$( pyupgrade --py${positional_args[1]}-plus \
            --exit-zero-even-if-changed ${file} 2>&1 || true )
        rewrites=$( echo ${out} | grep -c "Rewriting" || true )
        if [ ${rewrites} -gt 0 ]; then
            echo "${file} was modified"
        fi
        changed=$(( ${changed} + ${rewrites} ))

        if [ ${changed} -gt 4 ]; then
            break
        fi

        # reorder imports
        out=$( reorder-python-imports --py${positional_args[1]}-plus \
            --application-directories=.:shakenfist \
            --exit-zero-even-if-changed ${file} 2>&1 || true )
        rewrites=$( echo ${out} | grep -c "Reordering" || true )
        if [ ${rewrites} -gt 0 ]; then
            echo "${file} was modified"
        fi
        changed=$(( ${changed} + ${rewrites} ))

        if [ ${changed} -gt 4 ]; then
            break
        fi
    done
fi

# Did we find something new?
if [ $(git diff | wc -l) -gt 0 ]; then
echo "Code change detected..."
echo
git diff

git config --global user.name "shakenfist-bot"
git config --global user.email "bot@shakenfist.com"
git commit -a -m "Automated code formatting for ${datestamp}."
git push -f origin formatting-automations
echo
gh pr create \
    --assignee mikalstill \
    --reviewer mikalstill \
    --title "Automated code formatting for ${datestamp}." \
    --body "Automated code formatting."
echo
echo "Pull request created."
fi