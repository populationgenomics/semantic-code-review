"""Which build of scr a process is running (ADR 0009).

Two checkouts, two venvs or a rebuilt viewer bundle at the same version
serve different code, and a detached review server keeps serving the
build it started from. `this_build()` names the build this process is,
so a server can record it in `server.json` and a later CLI can decide
whether the server holding a run is the build it would start.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.metadata
import pathlib
import sys

import semantic_code_review

from ..viewer import static

#: How many hex digits of the digest a `build` carries: enough to
#: tell two checkouts apart on one machine, short enough for a `ps` row.
_BUILD_DIGITS = 12


@dataclasses.dataclass(frozen=True)
class BuildIdentity:
    """A build: the released version plus a digest that separates
    installs of the same version.

    `build` digests the package's installed location, the interpreter
    and the viewer bundle's mtime and size; `package` is that location,
    kept alongside so a message can say where a build came from.
    """

    version: str
    build: str
    package: str


def this_build() -> BuildIdentity:
    """The build this process runs. Deterministic for one install until
    the bundle is rebuilt.
    """
    package = pathlib.Path(semantic_code_review.__file__).parent.resolve()
    try:
        bundle = static.resolve_asset(static.BUNDLE)
        st = bundle.stat()
        bundle_part = f"{bundle}:{st.st_mtime_ns}:{st.st_size}"
    except FileNotFoundError:
        # A checkout without a built bundle is still a build; it serves
        # no viewer, and a rebuilt bundle makes it a different one.
        bundle_part = "no-bundle"
    digest = hashlib.sha256("\n".join([str(package), sys.executable, bundle_part]).encode("utf-8"))
    return BuildIdentity(
        version=importlib.metadata.version("semantic-code-review"),
        build=digest.hexdigest()[:_BUILD_DIGITS],
        package=str(package),
    )


__all__ = ["BuildIdentity", "this_build"]
