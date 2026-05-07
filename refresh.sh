#!/bin/bash
# Manual upstream refresh: fetch nechry/main, fast-forward our main if possible.
# CI runs an equivalent flow on a schedule (see .gitlab-ci.yml refresh job).
set -e
set -x

git remote add upstream https://github.com/nechry/elco-remocon-net-appdaemon 2>/dev/null \
  || git remote set-url upstream https://github.com/nechry/elco-remocon-net-appdaemon

git fetch --all
git checkout -B main origin/main
git merge --no-edit upstream/main \
  || { git merge --abort; echo "CONFLICT merging upstream/main — resolve manually"; exit 1; }

# git push origin main
