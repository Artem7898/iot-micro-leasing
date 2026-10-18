#!/usr/bin/env bash
# Apply protection only when master is unprotected; preserve existing protection/rulesets.
set -euo pipefail

repository=Artem7898/iot-micro-leasing
branch=master
script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

protected="$(gh api "repos/$repository/branches/$branch" --jq '.protected')"
if [[ "$protected" == true ]]; then
  printf '%s\n' "$repository:$branch is already protected; existing rules were preserved."
  exit 0
fi
if [[ "$protected" != false ]]; then
  printf '%s\n' 'Cannot establish the existing protection state; no settings changed.' >&2
  exit 1
fi

can_administer="$(gh api "repos/$repository" --jq '.permissions.admin // false')"
if [[ "$can_administer" != true ]]; then
  printf '%s\n' 'Repository administration access is required; no settings changed.' >&2
  exit 1
fi

gh api --method PUT "repos/$repository/branches/$branch/protection" \
  --input "$script_directory/branch-protection.json" \
  --jq '{required_status_checks, enforce_admins, required_pull_request_reviews, required_linear_history, allow_force_pushes, allow_deletions, required_conversation_resolution}'

protected="$(gh api "repos/$repository/branches/$branch" --jq '.protected')"
if [[ "$protected" != true ]]; then
  printf '%s\n' 'GitHub did not confirm branch protection; inspect repository settings.' >&2
  exit 1
fi
printf '%s\n' "$repository:$branch protection is enabled and verified."
