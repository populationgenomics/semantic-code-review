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
HLJS_CDN_BASE="https://raw.githubusercontent.com/highlightjs/cdn-release/${HLJS_VERSION}/build"
HLJS_SRC_BASE="https://raw.githubusercontent.com/highlightjs/highlight.js/${HLJS_VERSION}"

# mermaid ships ESM-only on npm; the single self-contained classic-script
# build (sets globalThis.mermaid) is jsdelivr's bundle of the pinned
# version. The LICENSE is the copy shipped inside the npm package.
MERMAID_VERSION="11.16.0"
MERMAID_BASE="https://cdn.jsdelivr.net/npm/mermaid@${MERMAID_VERSION}"

# local-name:source-url:expected-sha256
FILES=(
  "highlight.min.js:${HLJS_CDN_BASE}/highlight.min.js:c4a399dd6f488bc97a3546e3476747b3e714c99c57b9473154c6fb8d259b9381"
  "github.min.css:${HLJS_CDN_BASE}/styles/github.min.css:3a9a5def8b9c311e5ae43abde85c63133185eed4f0d9f67fea4b00a8308cf066"
  "github-dark.min.css:${HLJS_CDN_BASE}/styles/github-dark.min.css:9f208d022102b1d0c7aebfecd8e42ca7997d5de636649d2b31ea63093d809019"
  "LICENSE:${HLJS_SRC_BASE}/LICENSE:6c081431591d9df696c82dc598fe1423765b8a299b200ed00b281afd0f64c490"
  "mermaid.min.js:${MERMAID_BASE}/dist/mermaid.min.js:74d7c46dabca328c2294733910a8aa1ed0c37451776e8d5295da38a2b758fb9b"
  "mermaid.LICENSE:${MERMAID_BASE}/LICENSE:ec9fb67dcb25eccc416ed56e1aab819222c805a2a4bfe4cb19e7556bf2ffde80"
)

shasum_cmd() {
  if command -v shasum >/dev/null; then shasum -a 256 "$1" | awk '{print $1}';
  else sha256sum "$1" | awk '{print $1}'; fi
}

fail=0
for entry in "${FILES[@]}"; do
  local_name="${entry%%:*}"
  rest="${entry#*:}"
  # Split the rest on the LAST colon: everything before is the URL (which
  # itself contains colons), everything after is the expected hash.
  url="${rest%:*}"
  expected="${rest##*:}"

  echo "fetch $local_name  <-  $url"
  curl -fsSL "$url" -o "$local_name"

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
