# Vendored third-party assets

These files are committed to the repo rather than loaded from a CDN so
that supply-chain compromise of a third-party origin cannot affect the
viewer. Every file below is pinned to an exact release and recorded
with a SHA-256 hash; the refresh script verifies both.

## highlight.js

- **Upstream:** https://github.com/highlightjs/highlight.js
  (CDN mirror: https://github.com/highlightjs/cdn-release)
- **Pinned version:** `11.11.1`
- **License:** BSD 3-Clause — see the adjacent `LICENSE` file, a verbatim
  copy of `highlight.js/LICENSE` at the pinned tag.
- **Source URLs:**
  - `https://raw.githubusercontent.com/highlightjs/cdn-release/11.11.1/build/` for the built JS + styles
  - `https://raw.githubusercontent.com/highlightjs/highlight.js/11.11.1/LICENSE` for the license

| File                     | SHA-256                                                            |
| ------------------------ | ------------------------------------------------------------------ |
| `highlight.min.js`       | `c4a399dd6f488bc97a3546e3476747b3e714c99c57b9473154c6fb8d259b9381` |
| `github.min.css`         | `3a9a5def8b9c311e5ae43abde85c63133185eed4f0d9f67fea4b00a8308cf066` |
| `github-dark.min.css`    | `9f208d022102b1d0c7aebfecd8e42ca7997d5de636649d2b31ea63093d809019` |
| `LICENSE`                | `6c081431591d9df696c82dc598fe1423765b8a299b200ed00b281afd0f64c490` |

## mermaid

Lazy-loaded by the review console (`<script>` injection) the first time
an answer completes a `mermaid` fence — it is MB-class and rarely used,
so it never enters the main `viewer.js` bundle.

- **Upstream:** https://github.com/mermaid-js/mermaid
- **Pinned version:** `11.16.0`
- **License:** MIT — see the adjacent `mermaid.LICENSE` file, a verbatim
  copy of the license shipped in the npm package at the pinned version.
- **Source URLs:** `https://cdn.jsdelivr.net/npm/mermaid@11.16.0/` for both
  the bundle and the license.
- **Note:** mermaid 11 ships ESM-only on npm (code-split into many
  chunks); `dist/mermaid.min.js` is jsdelivr's single self-contained
  classic-script bundle of that version, whose final line assigns
  `globalThis.mermaid`. That is what the `<script>`-injection loader
  reads. If jsdelivr ever regenerates the bundle the SHA-256 will change
  and `refresh.sh` will flag it.

| File                     | SHA-256                                                            |
| ------------------------ | ------------------------------------------------------------------ |
| `mermaid.min.js`         | `74d7c46dabca328c2294733910a8aa1ed0c37451776e8d5295da38a2b758fb9b` |
| `mermaid.LICENSE`        | `ec9fb67dcb25eccc416ed56e1aab819222c805a2a4bfe4cb19e7556bf2ffde80` |

## Refreshing

Run `./refresh.sh` from this directory. It re-downloads every pinned
file, recomputes its SHA-256, and fails loudly if any hash doesn't
match the values above. Bump the version constants in the script to
upgrade — do not edit the files here by hand.
