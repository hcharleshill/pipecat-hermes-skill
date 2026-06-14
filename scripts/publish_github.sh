#!/usr/bin/env bash
# Create github.com/hcharleshill/pipecat-hermes-skill (if needed) and push main + tags.
#
# Option A — Personal Access Token (repo scope):
#   export GITHUB_TOKEN="ghp_..."
#   ./scripts/publish_github.sh
#
# Option B — GitHub CLI:
#   gh auth login
#   ./scripts/publish_github.sh
#
# Option C — Manual: create empty repo on GitHub, then:
#   git remote add origin https://github.com/hcharleshill/pipecat-hermes-skill.git
#   git push -u origin main --tags

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

OWNER="hcharleshill"
REPO="pipecat-hermes-skill"
REMOTE="https://github.com/${OWNER}/${REPO}.git"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Not a git repository. Run from project root after git init." >&2
  exit 1
fi

create_via_api() {
  if [[ -z "${GITHUB_TOKEN:-}" ]]; then
    return 1
  fi
  local code
  code=$(curl -sS -o /tmp/gh_create_repo.json -w "%{http_code}" -X POST \
    -H "Authorization: Bearer ${GITHUB_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/user/repos" \
    -d "{\"name\":\"${REPO}\",\"description\":\"Alpha: Asterisk voice bridge with local STT/TTS and Hermes agent\",\"private\":false}")
  if [[ "$code" == "201" ]]; then
    echo "Created https://github.com/${OWNER}/${REPO}"
    return 0
  fi
  if [[ "$code" == "422" ]]; then
    echo "Repository already exists on GitHub."
    return 0
  fi
  echo "GitHub API create failed (HTTP ${code}):" >&2
  cat /tmp/gh_create_repo.json >&2
  return 1
}

create_via_gh() {
  if ! command -v gh >/dev/null 2>&1; then
    return 1
  fi
  if ! gh auth status >/dev/null 2>&1; then
    return 1
  fi
  if gh repo view "${OWNER}/${REPO}" >/dev/null 2>&1; then
    echo "Repository ${OWNER}/${REPO} already exists."
  else
    gh repo create "${OWNER}/${REPO}" --public \
      --description "Alpha: Asterisk voice bridge with local STT/TTS and Hermes agent" \
      --source=. --remote=origin --push=false
  fi
  return 0
}

if git remote get-url origin >/dev/null 2>&1; then
  echo "Remote origin: $(git remote get-url origin)"
else
  if create_via_gh || create_via_api; then
    :
  else
    echo "Could not create repo automatically."
    echo "Create an empty repo at https://github.com/new named: ${REPO}"
    echo "Then re-run this script or: git remote add origin ${REMOTE}"
    read -r -p "Press Enter after creating the repo (or Ctrl-C to abort)..."
  fi
  if ! git remote get-url origin >/dev/null 2>&1; then
    git remote add origin "$REMOTE"
  fi
fi

echo "Pushing main and tags..."
if [[ -n "${GITHUB_TOKEN:-}" ]] && [[ "$(git remote get-url origin)" == https://* ]]; then
  git push "https://${GITHUB_TOKEN}@github.com/${OWNER}/${REPO}.git" main --tags
  git branch --set-upstream-to="origin/main" main 2>/dev/null || true
else
  git push -u origin main --tags
fi

echo "Done: https://github.com/${OWNER}/${REPO}"