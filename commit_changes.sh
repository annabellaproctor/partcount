#!/usr/bin/env bash
# commit_changes.sh — automates lint/syntax check and git commit for lab-inventory-tracker.
# Usage: ./commit_changes.sh "Your commit message"
#
# What it does:
#   1. Runs a Python syntax check (py_compile) on all changed .py files.
#   2. Runs flake8 (if installed) for basic style lint — non-blocking, warnings only.
#   3. Stages all changes and commits with the provided message (or a default).
#   4. Optionally pushes if PUSH=1 is set in the environment.

set -euo pipefail

COMMIT_MSG="${1:-"chore: apply lab-inventory-tracker changes"}"

echo "=== Lab Inventory Tracker — commit helper ==="
echo ""

# ── 1. Python syntax check ──────────────────────────────────────────────────
echo "[1/3] Syntax check (py_compile) on modified .py files..."
CHANGED_PY=$(git diff --cached --name-only --diff-filter=ACMR | grep '\.py$' || true)
if [[ -z "$CHANGED_PY" ]]; then
    # Nothing staged yet — check working tree
    CHANGED_PY=$(git diff --name-only --diff-filter=ACMR | grep '\.py$' || true)
fi

SYNTAX_ERRORS=0
if [[ -n "$CHANGED_PY" ]]; then
    while IFS= read -r f; do
        if [[ -f "$f" ]]; then
            if python3 -m py_compile "$f" 2>&1; then
                echo "  OK  $f"
            else
                echo "  ERR $f"
                SYNTAX_ERRORS=$((SYNTAX_ERRORS + 1))
            fi
        fi
    done <<< "$CHANGED_PY"
else
    echo "  No modified Python files found."
fi

if [[ $SYNTAX_ERRORS -gt 0 ]]; then
    echo ""
    echo "✗ $SYNTAX_ERRORS file(s) have syntax errors. Fix before committing."
    exit 1
fi

# ── 2. flake8 lint (non-blocking) ───────────────────────────────────────────
echo ""
echo "[2/3] flake8 lint (warnings only, non-blocking)..."
if command -v flake8 &>/dev/null; then
    flake8 app/ \
        --max-line-length=120 \
        --extend-ignore=E501,W503,E203 \
        --statistics \
        || echo "  ⚠  flake8 reported issues (non-fatal — review above)."
else
    echo "  flake8 not installed — skipping style lint."
fi

# ── 3. Git stage + commit ────────────────────────────────────────────────────
echo ""
echo "[3/3] Staging all changes and committing..."
git add -A
git status --short

if git diff --cached --quiet; then
    echo "  Nothing to commit — working tree is clean."
    exit 0
fi

git commit -m "$COMMIT_MSG"
echo ""
echo "✓ Committed: $COMMIT_MSG"

# ── Optional push ─────────────────────────────────────────────────────────────
if [[ "${PUSH:-0}" == "1" ]]; then
    echo "Pushing..."
    git push
    echo "✓ Pushed."
fi
