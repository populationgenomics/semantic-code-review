#!/usr/bin/env bash
# Re-download every vendored asset at its pinned version and verify the
# SHA-256 against VENDOR.md. Exits non-zero on any mismatch so CI or a
# reviewer can tell at a glance whether the vendored bytes match the
# documented provenance.
#
# Bump the version + hash block below when intentionally upgrading.

set -eu -o pipefail

cd "$(dirname "$0")"

HLJS_VERSION="11.11.1"
HLJS_BASE="https://raw.githubusercontent.com/highlightjs/cdn-release/${HLJS_VERSION}/build"

# file:remote-relative-path:expected-sha256
FILES=(
  "highlight.min.js:highlight.min.js:c4a399dd6f488bc97a3546e3476747b3e714c99c57b9473154c6fb8d259b9381"
  "github.min.css:styles/github.min.css:3a9a5def8b9c311e5ae43abde85c63133185eed4f0d9f67fea4b00a8308cf066"
  "github-dark.min.css:styles/github-dark.min.css:9f208d022102b1d0c7aebfecd8e42ca7997d5de636649d2b31ea63093d809019"
)

shasum_cmd() {
  if command -v shasum >/dev/null; then shasum -a 256 "$1" | awk '{print $1}';
  else sha256sum "$1" | awk '{print $1}'; fi
}

fail=0
for entry in "${FILES[@]}"; do
  local_name="${entry%%:*}"
  rest="${entry#*:}"
  remote_rel="${rest%%:*}"
  expected="${rest##*:}"

  echo "fetch $local_name  <-  $HLJS_BASE/$remote_rel"
  curl -fsSL "$HLJS_BASE/$remote_rel" -o "$local_name"

  got=$(shasum_cmd "$local_name")
  if [ "$got" != "$expected" ]; then
    echo "  !! hash mismatch for $local_name" >&2
    echo "     expected: $expected" >&2
    echo "     got:      $got" >&2
    fail=1
  else
    echo "  ok"
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "refresh failed: one or more hashes did not match" >&2
  exit 1
fi
echo "all vendored files match their pinned SHA-256"
