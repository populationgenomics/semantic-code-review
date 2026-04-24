# Vendored third-party assets

These files are committed to the repo rather than loaded from a CDN so
that supply-chain compromise of a third-party origin cannot affect the
viewer. Every file below is pinned to an exact release and recorded
with a SHA-256 hash; the refresh script verifies both.

## highlight.js

- **Upstream:** https://github.com/highlightjs/cdn-release
- **Pinned version:** `11.11.1`
- **Source URL base:** `https://raw.githubusercontent.com/highlightjs/cdn-release/11.11.1/build/`

| File                     | SHA-256                                                            |
| ------------------------ | ------------------------------------------------------------------ |
| `highlight.min.js`       | `c4a399dd6f488bc97a3546e3476747b3e714c99c57b9473154c6fb8d259b9381` |
| `github.min.css`         | `3a9a5def8b9c311e5ae43abde85c63133185eed4f0d9f67fea4b00a8308cf066` |
| `github-dark.min.css`    | `9f208d022102b1d0c7aebfecd8e42ca7997d5de636649d2b31ea63093d809019` |

## Refreshing

Run `./refresh.sh` from this directory. It re-downloads every pinned
file, recomputes its SHA-256, and fails loudly if any hash doesn't
match the values above. Bump the version constants in the script to
upgrade — do not edit the files here by hand.
