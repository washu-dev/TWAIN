#!/usr/bin/env bash
# Record a component's release AFTER its deploy succeeded (#173): write the
# version to <component>/VERSION, commit "chore(<component>): release v<ver>
# [skip ci]", tag "<component>-v<ver>", and push both to master.
#
#   scripts/release-commit.sh api 2026.10.06.001
#
# [skip ci] is load-bearing: without it the bump commit would trigger another
# deploy, which would bump again. The API and app deploys can finish at the same
# moment, so a non-fast-forward push is rebased and retried; the two only ever
# touch their own VERSION file, so the rebase never conflicts. The tag prefix
# keeps their tags apart in the repo's one tag namespace. Mirrors ris-api.
set -euo pipefail

component="${1:?usage: release-commit.sh <api|app> <version>}"
version="${2:?usage: release-commit.sh <api|app> <version>}"
branch="${RELEASE_BRANCH:-master}"

printf '%s\n' "$version" > "$component/VERSION"
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add "$component/VERSION"
git commit -m "chore($component): release v$version [skip ci]"
git tag "$component-v$version"

for attempt in 1 2 3; do
  if git push origin "HEAD:$branch" && git push origin "$component-v$version"; then
    echo "released $component v$version"
    exit 0
  fi
  echo "push race on attempt $attempt/3 -- rebasing onto origin/$branch and retrying"
  git fetch origin "$branch"
  # --autostash: a build step may have touched a tracked file in the workspace.
  git rebase --autostash "origin/$branch"
  # The tag must follow the rebased commit.
  git tag -f "$component-v$version"
  sleep $((attempt * ${RELEASE_RETRY_SLEEP:-5}))
done
echo "::error::could not push the $component v$version release commit/tag (the deploy itself succeeded)"
exit 1
