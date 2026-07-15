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

## katex

Lazy-loaded by rendered-markdown mode (`<script>` + `<link>` injection)
the first time a `.md` flipped to rendered mode contains math — it is
rarely used, so it never enters the main `viewer.js` bundle. The UMD
bundle sets `globalThis.katex`, which the loader reads. `katex.min.css`
references its fonts by the relative `fonts/` path, so the woff2 fonts
are vendored into `fonts/` beside it; only woff2 is vendored (the CSS
lists woff2 first, so modern browsers never fall back to woff/ttf).

- **Upstream:** https://github.com/KaTeX/KaTeX
- **Pinned version:** `0.17.0`
- **License:** MIT — see the adjacent `katex.LICENSE` file, a verbatim
  copy of the license shipped in the npm package at the pinned version.
- **Source URLs:** `https://cdn.jsdelivr.net/npm/katex@0.17.0/` for the
  bundle, stylesheet, fonts, and license.

| File                                 | SHA-256                                                            |
| ------------------------------------ | ------------------------------------------------------------------ |
| `katex.min.js`                       | `45fbe318fea878fdc0a111913dc1f87894b2c439360d0228c086ef313f213efc` |
| `katex.min.css`                      | `a34ad8fc188e8f5a3af7ceaa2a58d7210c6c9171335a15bff2b48ebcd6a6f5b0` |
| `katex.LICENSE`                      | `766ccc1f306c885aa45542a9846bbd0a505b27a0374f146778171c2254ce18e3` |
| `fonts/KaTeX_AMS-Regular.woff2`      | `0cdd387c9590a1a9f9794560022dbb59654a7d86f187aa0c81495ad42d3a7308` |
| `fonts/KaTeX_Caligraphic-Bold.woff2` | `de7701e42cf1f4cf0b766c03fb27977207eee2f4fd5d76fa82188406da43ea4c` |
| `fonts/KaTeX_Caligraphic-Regular.woff2` | `5d53e70ad607c2352162dec9e0923fb54ecdafaccbf604cd8dcf7d00facb989b` |
| `fonts/KaTeX_Fraktur-Bold.woff2`     | `74444efd593c005e3f4573b44524704c0af0a937fe911cca9e94068d0d140d3f` |
| `fonts/KaTeX_Fraktur-Regular.woff2`  | `51814d270d06ff0255dba0799994fa4d8c84d11f09951d47595f4abb1f3602dc` |
| `fonts/KaTeX_Main-Bold.woff2`        | `0f60d1b897938ec918c8ce073092411baf9438f6739465693ff18b0f9d20b021` |
| `fonts/KaTeX_Main-BoldItalic.woff2`  | `99cd42a3c072d918f2f44984a807cf7aa16e13545fd0875fc07c6c65f99e715b` |
| `fonts/KaTeX_Main-Italic.woff2`      | `97479ca6cce906abc961ecac96faa5f9ca2e61b8e7670d475826bcdee9a7c267` |
| `fonts/KaTeX_Main-Regular.woff2`     | `c2342cd8b869e01752a9321dc17213fc40d4d04c79688c1d43f2cf316abd7866` |
| `fonts/KaTeX_Math-BoldItalic.woff2`  | `dc47344dbb6cb5b655c8460d561f4df5f501b90c804ad3c6cec65fe322351ab1` |
| `fonts/KaTeX_Math-Italic.woff2`      | `7af58c5ec8f132a2ddde9027c6d7814decce4d3b822a11192a42a20e2e973264` |
| `fonts/KaTeX_SansSerif-Bold.woff2`   | `e99ae51144bf1232efcc1bfe5add36262c6866b0faab24fa75740e1b98577a62` |
| `fonts/KaTeX_SansSerif-Italic.woff2` | `00b26ac825e2095056396e0553b8ac26d3f8ad158c3826e28b4c45b385c4714a` |
| `fonts/KaTeX_SansSerif-Regular.woff2` | `68e8c73ef42afd3ccec58bf0fba302cce448938e7fc020a5e31f8a952eee1342` |
| `fonts/KaTeX_Script-Regular.woff2`   | `036d4e95149b69ff9bcc0cd55771efeb25ffa3947293e69acd78d5ac328c684b` |
| `fonts/KaTeX_Size1-Regular.woff2`    | `6b47c40166b6dbe21a5dfca7718413f2147fd2399be1ba605d8ad39cedf25dfe` |
| `fonts/KaTeX_Size2-Regular.woff2`    | `d04c54219f9eaec6d4d4fd42dfb28785975a4794d6b2fc71e566b9cd6db842dd` |
| `fonts/KaTeX_Size3-Regular.woff2`    | `73d591271b1604960cb10bb90fee021670af7297017e0e98480b332d11f51995` |
| `fonts/KaTeX_Size4-Regular.woff2`    | `a4af7d414440a1c1790825cfb700cf9cf43b0f2c4b04f0ebc523011ad9853ec0` |
| `fonts/KaTeX_Typewriter-Regular.woff2` | `71d517d67827787cfabdf186914cc3358eda539e37931941f2b2fd4a21f68c0b` |

## Refreshing

Run `./refresh.sh` from this directory. It re-downloads every pinned
file, recomputes its SHA-256, and fails loudly if any hash doesn't
match the values above. Bump the version constants in the script to
upgrade — do not edit the files here by hand.
