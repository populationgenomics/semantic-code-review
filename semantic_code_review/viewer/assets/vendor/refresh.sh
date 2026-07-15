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

# katex: the UMD bundle sets globalThis.katex, read by the <script>
# injection loader; the CSS references its woff2 fonts by the relative
# `fonts/` path, so they are vendored into the fonts/ subdirectory.
KATEX_VERSION="0.17.0"
KATEX_BASE="https://cdn.jsdelivr.net/npm/katex@${KATEX_VERSION}"

mkdir -p fonts

# local-name:source-url:expected-sha256
FILES=(
  "highlight.min.js:${HLJS_CDN_BASE}/highlight.min.js:c4a399dd6f488bc97a3546e3476747b3e714c99c57b9473154c6fb8d259b9381"
  "github.min.css:${HLJS_CDN_BASE}/styles/github.min.css:3a9a5def8b9c311e5ae43abde85c63133185eed4f0d9f67fea4b00a8308cf066"
  "github-dark.min.css:${HLJS_CDN_BASE}/styles/github-dark.min.css:9f208d022102b1d0c7aebfecd8e42ca7997d5de636649d2b31ea63093d809019"
  "LICENSE:${HLJS_SRC_BASE}/LICENSE:6c081431591d9df696c82dc598fe1423765b8a299b200ed00b281afd0f64c490"
  "mermaid.min.js:${MERMAID_BASE}/dist/mermaid.min.js:74d7c46dabca328c2294733910a8aa1ed0c37451776e8d5295da38a2b758fb9b"
  "mermaid.LICENSE:${MERMAID_BASE}/LICENSE:ec9fb67dcb25eccc416ed56e1aab819222c805a2a4bfe4cb19e7556bf2ffde80"
  "katex.min.js:${KATEX_BASE}/dist/katex.min.js:45fbe318fea878fdc0a111913dc1f87894b2c439360d0228c086ef313f213efc"
  "katex.min.css:${KATEX_BASE}/dist/katex.min.css:a34ad8fc188e8f5a3af7ceaa2a58d7210c6c9171335a15bff2b48ebcd6a6f5b0"
  "katex.LICENSE:${KATEX_BASE}/LICENSE:766ccc1f306c885aa45542a9846bbd0a505b27a0374f146778171c2254ce18e3"
  "fonts/KaTeX_AMS-Regular.woff2:${KATEX_BASE}/dist/fonts/KaTeX_AMS-Regular.woff2:0cdd387c9590a1a9f9794560022dbb59654a7d86f187aa0c81495ad42d3a7308"
  "fonts/KaTeX_Caligraphic-Bold.woff2:${KATEX_BASE}/dist/fonts/KaTeX_Caligraphic-Bold.woff2:de7701e42cf1f4cf0b766c03fb27977207eee2f4fd5d76fa82188406da43ea4c"
  "fonts/KaTeX_Caligraphic-Regular.woff2:${KATEX_BASE}/dist/fonts/KaTeX_Caligraphic-Regular.woff2:5d53e70ad607c2352162dec9e0923fb54ecdafaccbf604cd8dcf7d00facb989b"
  "fonts/KaTeX_Fraktur-Bold.woff2:${KATEX_BASE}/dist/fonts/KaTeX_Fraktur-Bold.woff2:74444efd593c005e3f4573b44524704c0af0a937fe911cca9e94068d0d140d3f"
  "fonts/KaTeX_Fraktur-Regular.woff2:${KATEX_BASE}/dist/fonts/KaTeX_Fraktur-Regular.woff2:51814d270d06ff0255dba0799994fa4d8c84d11f09951d47595f4abb1f3602dc"
  "fonts/KaTeX_Main-Bold.woff2:${KATEX_BASE}/dist/fonts/KaTeX_Main-Bold.woff2:0f60d1b897938ec918c8ce073092411baf9438f6739465693ff18b0f9d20b021"
  "fonts/KaTeX_Main-BoldItalic.woff2:${KATEX_BASE}/dist/fonts/KaTeX_Main-BoldItalic.woff2:99cd42a3c072d918f2f44984a807cf7aa16e13545fd0875fc07c6c65f99e715b"
  "fonts/KaTeX_Main-Italic.woff2:${KATEX_BASE}/dist/fonts/KaTeX_Main-Italic.woff2:97479ca6cce906abc961ecac96faa5f9ca2e61b8e7670d475826bcdee9a7c267"
  "fonts/KaTeX_Main-Regular.woff2:${KATEX_BASE}/dist/fonts/KaTeX_Main-Regular.woff2:c2342cd8b869e01752a9321dc17213fc40d4d04c79688c1d43f2cf316abd7866"
  "fonts/KaTeX_Math-BoldItalic.woff2:${KATEX_BASE}/dist/fonts/KaTeX_Math-BoldItalic.woff2:dc47344dbb6cb5b655c8460d561f4df5f501b90c804ad3c6cec65fe322351ab1"
  "fonts/KaTeX_Math-Italic.woff2:${KATEX_BASE}/dist/fonts/KaTeX_Math-Italic.woff2:7af58c5ec8f132a2ddde9027c6d7814decce4d3b822a11192a42a20e2e973264"
  "fonts/KaTeX_SansSerif-Bold.woff2:${KATEX_BASE}/dist/fonts/KaTeX_SansSerif-Bold.woff2:e99ae51144bf1232efcc1bfe5add36262c6866b0faab24fa75740e1b98577a62"
  "fonts/KaTeX_SansSerif-Italic.woff2:${KATEX_BASE}/dist/fonts/KaTeX_SansSerif-Italic.woff2:00b26ac825e2095056396e0553b8ac26d3f8ad158c3826e28b4c45b385c4714a"
  "fonts/KaTeX_SansSerif-Regular.woff2:${KATEX_BASE}/dist/fonts/KaTeX_SansSerif-Regular.woff2:68e8c73ef42afd3ccec58bf0fba302cce448938e7fc020a5e31f8a952eee1342"
  "fonts/KaTeX_Script-Regular.woff2:${KATEX_BASE}/dist/fonts/KaTeX_Script-Regular.woff2:036d4e95149b69ff9bcc0cd55771efeb25ffa3947293e69acd78d5ac328c684b"
  "fonts/KaTeX_Size1-Regular.woff2:${KATEX_BASE}/dist/fonts/KaTeX_Size1-Regular.woff2:6b47c40166b6dbe21a5dfca7718413f2147fd2399be1ba605d8ad39cedf25dfe"
  "fonts/KaTeX_Size2-Regular.woff2:${KATEX_BASE}/dist/fonts/KaTeX_Size2-Regular.woff2:d04c54219f9eaec6d4d4fd42dfb28785975a4794d6b2fc71e566b9cd6db842dd"
  "fonts/KaTeX_Size3-Regular.woff2:${KATEX_BASE}/dist/fonts/KaTeX_Size3-Regular.woff2:73d591271b1604960cb10bb90fee021670af7297017e0e98480b332d11f51995"
  "fonts/KaTeX_Size4-Regular.woff2:${KATEX_BASE}/dist/fonts/KaTeX_Size4-Regular.woff2:a4af7d414440a1c1790825cfb700cf9cf43b0f2c4b04f0ebc523011ad9853ec0"
  "fonts/KaTeX_Typewriter-Regular.woff2:${KATEX_BASE}/dist/fonts/KaTeX_Typewriter-Regular.woff2:71d517d67827787cfabdf186914cc3358eda539e37931941f2b2fd4a21f68c0b"
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
