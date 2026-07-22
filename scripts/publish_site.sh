#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -d web ]]; then
  echo "web/ directory not found" >&2
  exit 1
fi

if git diff --quiet -- web && git diff --cached --quiet -- web && [[ -z "$(git ls-files --others --exclude-standard web)" ]]; then
  echo "No web/ changes to publish."
  exit 0
fi

git add \
  web/package.json \
  web/package-lock.json \
  web/astro.config.mjs \
  web/tsconfig.json \
  web/README.md \
  web/public \
  web/src

if git diff --cached --quiet -- web; then
  echo "No staged web/ changes to publish."
  exit 0
fi

echo "Staged files:"
git diff --cached --name-only -- web

if git diff --cached --name-only | grep -Ev '^(web/|$)' >/dev/null; then
  echo "Refusing to commit non-web files:" >&2
  git diff --cached --name-only | grep -Ev '^(web/|$)' >&2
  exit 1
fi

message="${1:-chore: update public site data}"
git commit -m "$message"
git push
