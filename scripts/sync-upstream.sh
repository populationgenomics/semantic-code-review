#!/usr/bin/env bash
# Pull upstream releases into the fork and anchor each tag at a fork-side
# merge commit, so release.yml (which only exists on fork commits) fires
# when the tag is pushed. Run from a clean main checkout whenever upstream
# publishes a new release.

set -euo pipefail

UPSTREAM=upstream
ORIGIN=origin

[[ "$(git symbolic-ref --short HEAD)" == main ]] \
    || { echo "must be on main" >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] \
    || { echo "working tree dirty" >&2; exit 1; }

git fetch "$ORIGIN" --tags --prune
git fetch "$UPSTREAM" --tags --prune
git pull --ff-only "$ORIGIN" main

# Upstream tags vX.Y.Z that don't already point at a fork merge commit.
mapfile -t pending < <(
    git ls-remote --tags --refs "$UPSTREAM" \
        | awk '{print $2}' | sed 's@^refs/tags/@@' \
        | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V \
        | while read -r tag; do
            if [[ -z "$(git tag -l "$tag")" ]]; then
                echo "$tag"
                continue
            fi
            parents=$(git cat-file -p "$(git rev-parse "$tag")" | grep -c '^parent ')
            (( parents >= 2 )) || echo "$tag"
        done
)

if [[ ${#pending[@]} -eq 0 ]]; then
    echo "Nothing to do — all upstream tags are anchored on fork merges."
    exit 0
fi

echo "Tags to (re)anchor:"
printf '  %s\n' "${pending[@]}"
read -rp "Proceed? [y/N] " yn
[[ "$yn" =~ ^[Yy]$ ]] || exit 0

for tag in "${pending[@]}"; do
    upstream_sha=$(git rev-parse "refs/tags/$tag^{commit}")
    if git merge-base --is-ancestor "$upstream_sha" HEAD; then
        # Already merged — find the earliest first-parent merge containing it.
        anchor=$(git rev-list --first-parent --merges --reverse HEAD \
            | while read -r m; do
                if git merge-base --is-ancestor "$upstream_sha" "$m"; then
                    echo "$m"; break
                fi
              done)
        [[ -n "$anchor" ]] || { echo "no anchor merge found for $tag" >&2; exit 1; }
    else
        git merge --no-ff "refs/tags/$tag" -m "Merge upstream $tag"
        anchor=$(git rev-parse HEAD)
    fi
    git tag -d "$tag" 2>/dev/null || true
    git push "$ORIGIN" ":refs/tags/$tag" 2>/dev/null || true
    git tag "$tag" "$anchor"
done

git push "$ORIGIN" main
git push "$ORIGIN" --tags
