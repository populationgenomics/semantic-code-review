"""SVG figure sanitisation for the change explainer (ADR 0007).

A figure is inline SVG in a structured slot. The model supplies geometry
and class names; every fill, stroke, weight and font decision lives in
`viewer.css`. This module is what makes that a guarantee rather than an
instruction: it restricts elements to a drawing subset, keeps only the
geometry attributes each of those elements needs, filters `class` to the
closed vocabulary in `docs/explainer-presentation.md`, requires a
`viewBox`, and namespaces ids so two figures on one page cannot collide
over a marker.

Attributes are an allowlist, not a denylist. The contract names the
presentation attributes that must go (`fill`, `stroke`, `style`, …); an
allowlist removes those and everything else nobody thought of, at the
cost of counting a legitimate-but-unlisted attribute as a strip.

Sanitisation runs here before the document is written, and again in the
renderer (`viewer/assets/explainer_figure.ts`), because a document can
reach a browser from a run directory this process never wrote.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

log = logging.getLogger(__name__)

SVG_NAMESPACE = "http://www.w3.org/2000/svg"

#: The closed class vocabulary. Every entry resolves to a theme custom
#: property in `viewer.css`, which is why a figure is correct in both
#: colour schemes without the model knowing which is active. The
#: renderer holds the same list; `tests/test_explainer_figures.py`
#: fails if the two drift.
FIGURE_CLASSES = frozenset(
    {
        # Boxes and frames.
        "d-box",
        "d-box-alt",
        "d-box-acc",
        "d-box-acc2",
        "d-box-warn",
        "d-box-ok",
        "d-frame",
        "d-fill-bg",
        # Text.
        "t",
        "t-b",
        "t-sm",
        "t-cap",
        "t-mono",
        "t-mono-sm",
        "t-acc",
        "t-warn",
        "t-ok",
        # Lines and heads.
        "ln",
        "ln2",
        "ln-mut",
        "ln-warn",
        "ln-ok",
        "ln-thin",
        "head",
        "head2",
        "head-mut",
        "head-warn",
        "head-ok",
        # Marks.
        "hl",
        "chip",
        "rule",
    }
)

#: The type half of :data:`FIGURE_CLASSES`. These are the only classes
#: that set `font-*`; every other class is paint. The split decides which
#: elements a class may appear on, and misapplication is not cosmetic: a
#: line class on a glyph sets `fill: none` and renders it invisible, and
#: `chip` on one outlines the text instead of drawing the pill it names.
TEXT_CLASSES = frozenset({"t", "t-b", "t-sm", "t-cap", "t-mono", "t-mono-sm", "t-acc", "t-warn", "t-ok"})

#: A container takes either kind, because a class on one is inherited
#: styling for its children. `title` and `desc` are metadata and are
#: never painted, so a class on them means nothing and is dropped.
_CONTAINERS = frozenset({"g", "svg", "marker", "defs"})
_TEXT_ELEMENTS = frozenset({"text", "tspan"}) | _CONTAINERS
_PAINT_ELEMENTS = frozenset({"rect", "circle", "ellipse", "line", "polyline", "polygon", "path"}) | _CONTAINERS


def class_elements(token: str) -> frozenset[str]:
    """The elements `token` may appear on; empty when it is not vocabulary."""
    if token not in FIGURE_CLASSES:
        return frozenset()
    return _TEXT_ELEMENTS if token in TEXT_CLASSES else _PAINT_ELEMENTS


#: Marker references. Not paint, so they survive — but only pointing at
#: a marker in this figure's own `defs`.
_MARKER_REFS = frozenset({"marker-start", "marker-mid", "marker-end"})

#: Allowed on any allowed element. `transform` is geometry; `class` is
#: filtered to the classes :func:`class_elements` allows on that element.
_GLOBAL_ATTRS = frozenset({"class", "transform"})

#: Per-element geometry attributes. `width`/`height` appear on `rect`
#: and `marker` but never on the root `svg`: the renderer sizes a figure
#: from the column and its viewBox, so a root dimension would fight it.
_ELEMENT_ATTRS: dict[str, frozenset[str]] = {
    "svg": frozenset({"viewBox", "preserveAspectRatio"}),
    "g": frozenset(),
    "defs": frozenset(),
    "marker": frozenset(
        {"id", "viewBox", "refX", "refY", "markerWidth", "markerHeight", "markerUnits", "orient", "overflow"}
    ),
    "rect": frozenset({"x", "y", "width", "height", "rx", "ry"}),
    "circle": frozenset({"cx", "cy", "r"}),
    "ellipse": frozenset({"cx", "cy", "rx", "ry"}),
    "line": frozenset({"x1", "y1", "x2", "y2"}) | _MARKER_REFS,
    "polyline": frozenset({"points"}) | _MARKER_REFS,
    "polygon": frozenset({"points"}) | _MARKER_REFS,
    "path": frozenset({"d"}) | _MARKER_REFS,
    "text": frozenset({"x", "y", "dx", "dy", "text-anchor", "dominant-baseline"}),
    "tspan": frozenset({"x", "y", "dx", "dy", "text-anchor", "dominant-baseline"}),
    "title": frozenset(),
    "desc": frozenset(),
}

ALLOWED_ELEMENTS = frozenset(_ELEMENT_ATTRS)

#: Elements that are containers, and so are serialised with a closing
#: tag even when empty — a self-closed `<defs/>` is legal but a
#: self-closed `<text/>` reads as a bug.
_VOID_SAFE = frozenset({"rect", "circle", "ellipse", "line", "polyline", "polygon", "path"})

_URL_REF = re.compile(r"^url\(#([A-Za-z_][\w.:-]*)\)$")
_ID = re.compile(r"^[A-Za-z_][\w.:-]*$")
#: A DTD is the only way an SVG this size can be expensive to parse
#: (entity expansion). Nothing in the vocabulary needs one, so the
#: presence of one ends the figure rather than being sanitised around.
_DOCTYPE = re.compile(r"<!(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


@dataclasses.dataclass(frozen=True)
class SanitizedSvg:
    """The SVG as it may be rendered, and how much of it was removed.

    `stripped` counts removed elements (a subtree counts once),
    attributes and class tokens. A figure that loses content is kept
    with the count recorded, not dropped: the same policy invalid
    references get, for the same reason — a silent thinning is the
    failure mode.

    `svg` is empty when nothing survived, which is what a caller
    renders a placeholder for.
    """

    svg: str
    stripped: int


def sanitize_svg(svg: str, *, namespace: str) -> SanitizedSvg:
    """Reduce model-authored SVG to the drawing vocabulary.

    Args:
        svg: The SVG source as the model emitted it.
        namespace: A per-figure string prefixed onto every surviving
            `id`, and onto the `url(#…)` references that point at them.
            Two figures on one page therefore cannot collide over a
            marker id.

    Returns:
        The sanitised source and the strip count. An unparseable
        document, a root that is not `<svg>`, or a root with no
        `viewBox` all yield an empty `svg` and a non-zero count — the
        figure is kept and says so.
    """
    if not _ID.match(namespace):
        raise ValueError(f"figure namespace {namespace!r} is not usable as an id prefix")
    if _DOCTYPE.search(svg):
        log.warning("explainer figure %s carries a DTD — dropped whole", namespace)
        return SanitizedSvg("", 1)
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as e:
        log.warning("explainer figure %s is not well-formed XML (%s) — dropped whole", namespace, e)
        return SanitizedSvg("", 1)
    if _local(root.tag) != "svg":
        log.warning("explainer figure %s is rooted at <%s>, not <svg> — dropped whole", namespace, _local(root.tag))
        return SanitizedSvg("", 1)

    counter = _Counter()
    clean = _clean_element(root, namespace=namespace, counter=counter)
    if clean is None or "viewBox" not in clean.attrib:
        log.warning("explainer figure %s has no viewBox — dropped whole", namespace)
        return SanitizedSvg("", counter.n + 1)
    clean.attrib = {"xmlns": SVG_NAMESPACE, **clean.attrib}
    return SanitizedSvg(_serialize(clean), counter.n)


# --- internals ------------------------------------------------------------


@dataclasses.dataclass
class _Counter:
    n: int = 0


@dataclasses.dataclass
class _Node:
    tag: str
    attrib: dict[str, str]
    text: str
    children: list[_Node]


def _local(name: str) -> str:
    """The local part of an ElementTree `{ns}tag` or plain tag name."""
    return name.rsplit("}", 1)[-1] if name.startswith("{") else name


def _in_svg_namespace(tag: str) -> bool:
    """Whether an element belongs to SVG (or to no namespace at all).

    A namespace-less document is the common case for hand-written SVG,
    and is treated as SVG. Anything in a third namespace (`inkscape:`,
    `sodipodi:`, XHTML smuggled in) is not part of the vocabulary.
    """
    return not tag.startswith("{") or tag.startswith("{" + SVG_NAMESPACE + "}")


def _clean_element(el: ET.Element, *, namespace: str, counter: _Counter) -> _Node | None:
    tag = _local(el.tag)
    if not _in_svg_namespace(el.tag) or tag not in ALLOWED_ELEMENTS:
        counter.n += 1
        return None
    node = _Node(
        tag=tag,
        attrib=_clean_attrs(el, tag=tag, namespace=namespace, counter=counter),
        text=el.text or "",
        children=[],
    )
    for child in el:
        clean = _clean_element(child, namespace=namespace, counter=counter)
        if clean is not None:
            node.children.append(clean)
        # A dropped element's tail text is its parent's prose, so it
        # survives the element that carried it.
        if child.tail:
            node.children.append(_Node(tag="", attrib={}, text=child.tail, children=[]))
    return node


def _clean_attrs(el: ET.Element, *, tag: str, namespace: str, counter: _Counter) -> dict[str, str]:
    allowed = _GLOBAL_ATTRS | _ELEMENT_ATTRS[tag]
    out: dict[str, str] = {}
    for raw_name, value in el.attrib.items():
        name = _local(raw_name)
        # A namespaced attribute is never in the vocabulary — `xlink:href`
        # in particular is the external-reference vector.
        if raw_name.startswith("{") or name not in allowed:
            counter.n += 1
            continue
        kept = _clean_value(name, value, tag=tag, namespace=namespace, counter=counter)
        if kept is None:
            counter.n += 1
            continue
        out[name] = kept
    return out


def _clean_value(name: str, value: str, *, tag: str, namespace: str, counter: _Counter) -> str | None:
    if name == "class":
        return _clean_classes(value, tag=tag, counter=counter)
    if name == "id":
        return _namespaced(value, namespace) if _ID.match(value) else None
    if name in _MARKER_REFS:
        m = _URL_REF.match(value.strip())
        return f"url(#{_namespaced(m.group(1), namespace)})" if m else None
    return value


def _namespaced(value: str, namespace: str) -> str:
    """`value` under `namespace`, applied at most once.

    A document is written more than once — one prose pass per call — and
    every write sanitises. Prefixing unconditionally would grow the id
    on each of them, so an id that already carries this figure's
    namespace is left alone. A model-chosen id that happens to start
    with it is already unique to this figure, which is all the prefix is
    for.
    """
    prefix = f"{namespace}-"
    return value if value.startswith(prefix) else prefix + value


def _clean_classes(value: str, *, tag: str, counter: _Counter) -> str | None:
    kept = []
    for token in value.split():
        if tag in class_elements(token):
            kept.append(token)
        else:
            counter.n += 1
    return " ".join(kept) if kept else None


def _serialize(node: _Node) -> str:
    if not node.tag:
        return escape(node.text)
    attrs = "".join(f" {k}={quoteattr(v)}" for k, v in node.attrib.items())
    body = escape(node.text) + "".join(_serialize(c) for c in node.children)
    if not body and node.tag in _VOID_SAFE:
        return f"<{node.tag}{attrs}/>"
    return f"<{node.tag}{attrs}>{body}</{node.tag}>"


__all__ = [
    "ALLOWED_ELEMENTS",
    "FIGURE_CLASSES",
    "SVG_NAMESPACE",
    "TEXT_CLASSES",
    "SanitizedSvg",
    "class_elements",
    "sanitize_svg",
]
