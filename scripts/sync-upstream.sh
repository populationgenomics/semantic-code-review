#!/usr/bin/env bash
# Pull upstream releases into the fork. For each upstream tag vX.Y.Z,
# merge it into fork main and create a fork-side `cpg-vX.Y.Z` tag at
# the merge commit. release.yml triggers on `cpg-v*` tag pushes.
#
# Run from a clean main checkout whenever upstream publishes a new
# release.
#
# Why the prefix: anchoring the bare upstream tag at a fork-side
# merge commit (the previous scheme) caused the same tag name to
# diverge between the two remotes, which made `git fetch --tags`
# refuse the conflict. The cpg-v* prefix keeps upstream and fork
# tag namespaces separate, so fetches Just Work.

set -euo pipefail

UPSTREAM=upstream
ORIGIN=origin
PREFIX=cpg-

# Older upstream tags (v0.14 and earlier) were anchored under the
# pre-prefix scheme; don't try to retro-create cpg-v* tags for them.
# After the first cpg-v* tag exists, the "no fork tag yet" filter
# below picks up where we left off automatically.
FLOOR_TAG=v0.15.0

[[ "$(git symbolic-ref --short HEAD)" == main ]] \
    || { echo "must be on main" >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] \
    || { echo "working tree dirty" >&2; exit 1; }

# --force is defensive: local v* tags should always converge to
# upstream's view (since the fork doesn't push to that namespace
# under the new scheme), and local cpg-v* tags should always
# converge to origin's. Without --force, leftover v* tags from the
# previous anchoring scheme block the upstream fetch.
git fetch "$ORIGIN" --tags --prune --force
git fetch "$UPSTREAM" --tags --prune --force
git pull --ff-only "$ORIGIN" main

# Upstream tags that don't already have a corresponding cpg-v* tag,
# and aren't older than FLOOR_TAG.
mapfile -t pending < <(
    git ls-remote --tags --refs "$UPSTREAM" \
        | awk '{print $2}' | sed 's@^refs/tags/@@' \
        | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V \
        | while read -r tag; do
            if [[ -n "$(git tag -l "${PREFIX}${tag}")" ]]; then
                continue
            fi
            oldest=$(printf '%s\n%s\n' "$FLOOR_TAG" "$tag" | sort -V | head -1)
            [[ "$oldest" == "$FLOOR_TAG" ]] || continue
            echo "$tag"
        done
)

if [[ ${#pending[@]} -eq 0 ]]; then
    echo "Nothing to do — every upstream tag at or after $FLOOR_TAG already has a $PREFIX counterpart."
    exit 0
fi

echo "Upstream tags to merge & re-tag:"
for tag in "${pending[@]}"; do
    echo "  $tag -> ${PREFIX}${tag}"
done
read -rp "Proceed? [y/N] " yn
[[ "$yn" =~ ^[Yy]$ ]] || exit 0

for tag in "${pending[@]}"; do
    fork_tag="${PREFIX}${tag}"
    upstream_sha=$(git rev-parse "refs/tags/$tag^{commit}")
    if git merge-base --is-ancestor "$upstream_sha" HEAD; then
        # Already merged in a prior session — find the earliest
        # first-parent merge containing it and anchor there.
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
    git tag "$fork_tag" "$anchor"
done

git push "$ORIGIN" main
for tag in "${pending[@]}"; do
    git push "$ORIGIN" "${PREFIX}${tag}"
done
