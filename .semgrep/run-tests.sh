#!/bin/sh
# Run every rule fixture in .semgrep/tests/ against .semgrep/sdlc.yml.
#
# semgrep --test pairs a config FILE with targets of the same stem, so a
# single ruleset with one fixture per rule is tested one target at a time.
# Run from the repository root, with semgrep on PATH.
set -eu
status=0
for target in .semgrep/tests/*.py; do
    echo "== $target"
    semgrep --test --config .semgrep/sdlc.yml "$target" || status=1
done
exit "$status"
