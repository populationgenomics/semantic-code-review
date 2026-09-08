"""Locating the viewer's static assets: the bundled `viewer.js`, the
stylesheet, `index.html` and the vendored libraries under `assets/`.

Every asset is read from the packaged `assets/` directory: `viewer.js`
ships as package_data (built into the wheel by release.yml), so wheel
and plugin installs both serve it from there. `SCR_VIEWER_BUILD_DIR`
(set by the test harness, which bundles a fresh `viewer.js`
out-of-tree) wins for `viewer.js` only; everything else is always read
from `assets/`.
"""

from __future__ import annotations

import os
import pathlib

ASSETS_DIR = pathlib.Path(__file__).resolve().parent / "assets"

#: The compiled viewer bundle's name — the one asset a build dir can
#: override, and the one whose mtime and size enter the build identity.
BUNDLE = "viewer.js"


def resolve_asset(rel: str) -> pathlib.Path:
    """Locate a static asset, honouring `SCR_VIEWER_BUILD_DIR` for the
    bundle.

    Raises:
        FileNotFoundError: `rel` climbs out of the assets directory, or
            no file by that name exists where it is looked for.
    """
    if ".." in pathlib.Path(rel).parts:
        raise FileNotFoundError(f"refused path-traversal asset: {rel!r}")
    build_dir = os.environ.get("SCR_VIEWER_BUILD_DIR")
    if build_dir and rel == BUNDLE:
        p = pathlib.Path(build_dir) / rel
        if p.exists():
            return p
    p = ASSETS_DIR / rel
    if p.exists():
        return p
    raise FileNotFoundError(f"asset not found: {rel} (looked in {ASSETS_DIR}{', ' + build_dir if build_dir else ''})")
