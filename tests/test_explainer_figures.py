"""Figure sanitisation — the guarantee that the model cannot pick a colour.

The point of the sanitiser is that an off-theme, unsafe or colliding
diagram is not something the model *should not* emit, it is something it
*cannot* emit. These cases exercise that directly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from semantic_code_review.augment import explainer, explainer_figures, explainer_schema, prompts
from semantic_code_review.augment.agents import Client

_ASSETS = Path(__file__).resolve().parents[1] / "semantic_code_review" / "viewer" / "assets"


def _svg(body: str, *, root_attrs: str = 'viewBox="0 0 100 50"') -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" {root_attrs}>{body}</svg>'


def _clean(
    body: str,
    *,
    root_attrs: str = 'viewBox="0 0 100 50"',
    namespace: str = "f0",
) -> explainer_figures.SanitizedSvg:
    return explainer_figures.sanitize_svg(_svg(body, root_attrs=root_attrs), namespace=namespace)


# --- Paint is not the model's to choose -----------------------------------


def test_a_hardcoded_fill_is_stripped_and_counted() -> None:
    out = _clean('<rect class="d-box" x="1" y="2" width="10" height="8" fill="#ff00ff" stroke="red"/>')
    assert "ff00ff" not in out.svg
    assert "stroke" not in out.svg
    assert 'class="d-box"' in out.svg
    # Geometry survives; only the paint went.
    assert 'x="1"' in out.svg and 'width="10"' in out.svg
    assert out.stripped == 2


@pytest.mark.parametrize(
    "attr",
    [
        'fill="#123456"',
        'stroke="#123456"',
        'stroke-width="9"',
        'stroke-dasharray="1 2"',
        'style="fill:red"',
        'font-family="Comic Sans"',
        'font-size="40"',
        'font-weight="900"',
        'color="red"',
        'opacity="0.2"',
    ],
)
def test_every_presentation_attribute_named_by_the_contract_goes(attr: str) -> None:
    out = _clean(f'<rect class="d-box" x="0" y="0" width="4" height="4" {attr}/>')
    assert out.stripped == 1
    assert out.svg == (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50">'
        '<rect class="d-box" x="0" y="0" width="4" height="4"/></svg>'
    )


def test_an_unknown_class_is_removed_but_the_known_ones_stay() -> None:
    out = _clean('<rect class="d-box brand-purple hl" x="0" y="0" width="4" height="4"/>')
    assert 'class="d-box hl"' in out.svg
    assert "brand-purple" not in out.svg
    assert out.stripped == 1


def test_a_class_attribute_with_nothing_known_left_is_dropped_entirely() -> None:
    out = _clean('<rect class="brand-purple" x="0" y="0" width="4" height="4"/>')
    assert "class" not in out.svg
    # One for the unknown token, one for the now-empty attribute.
    assert out.stripped == 2


# --- Script and friends ----------------------------------------------------


def test_a_script_element_is_removed_with_its_contents() -> None:
    out = _clean('<script>alert(1)</script><rect class="d-box" x="0" y="0" width="4" height="4"/>')
    assert "script" not in out.svg
    assert "alert" not in out.svg
    assert "<rect" in out.svg
    assert out.stripped == 1


def test_an_event_handler_is_removed() -> None:
    out = _clean('<rect class="d-box" x="0" y="0" width="4" height="4" onclick="alert(1)"/>')
    assert "onclick" not in out.svg
    assert "alert" not in out.svg
    assert out.stripped == 1


def test_a_foreign_object_is_removed() -> None:
    out = _clean('<foreignObject width="10" height="10"><b>hi</b></foreignObject>')
    assert "foreignObject" not in out.svg
    assert out.stripped == 1


def test_an_external_reference_does_not_survive() -> None:
    out = _clean('<image href="https://evil.example/x.png" x="0" y="0"/>')
    assert "evil.example" not in out.svg
    assert out.stripped == 1


def test_a_dtd_ends_the_figure_rather_than_being_parsed() -> None:
    src = '<!DOCTYPE svg [<!ENTITY lol "ha">]><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"/>'
    out = explainer_figures.sanitize_svg(src, namespace="f0")
    assert out.svg == ""
    assert out.stripped == 1


def test_text_content_is_escaped_not_interpreted() -> None:
    out = _clean('<text class="t" x="0" y="0">&lt;script&gt;a &amp; b</text>')
    assert "&lt;script&gt;" in out.svg
    assert "<script>" not in out.svg


# --- Structural requirements ----------------------------------------------


def test_a_root_without_a_viewbox_loses_the_whole_figure() -> None:
    out = _clean('<rect class="d-box" x="0" y="0" width="4" height="4"/>', root_attrs='width="100" height="50"')
    assert out.svg == ""
    assert out.stripped > 0


def test_root_width_and_height_are_stripped_but_a_rect_keeps_its_own() -> None:
    out = _clean(
        '<rect class="d-box" x="0" y="0" width="4" height="4"/>',
        root_attrs='viewBox="0 0 100 50" width="800" height="400"',
    )
    assert 'width="4"' in out.svg
    assert 'width="800"' not in out.svg and 'height="400"' not in out.svg
    assert out.stripped == 2


def test_a_figure_that_is_not_well_formed_is_kept_empty_and_counted() -> None:
    out = explainer_figures.sanitize_svg("<svg><rect>", namespace="f0")
    assert out.svg == ""
    assert out.stripped == 1


def test_a_root_that_is_not_an_svg_is_rejected() -> None:
    out = explainer_figures.sanitize_svg("<html><body>hi</body></html>", namespace="f0")
    assert out.svg == ""
    assert out.stripped == 1


def test_the_serialised_root_always_declares_the_svg_namespace() -> None:
    out = explainer_figures.sanitize_svg('<svg viewBox="0 0 2 2"><g/></svg>', namespace="f0")
    assert out.svg.startswith('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 2 2">')


def test_namespace_must_be_usable_as_an_id_prefix() -> None:
    with pytest.raises(ValueError, match="not usable as an id prefix"):
        explainer_figures.sanitize_svg('<svg viewBox="0 0 1 1"/>', namespace="oops #1")


# --- Marker ids ------------------------------------------------------------

_MARKER = (
    '<defs><marker id="arrow" viewBox="0 0 6 6" refX="5" refY="3" markerWidth="6" markerHeight="6" orient="auto">'
    '<path class="head" d="M0,0 L6,3 L0,6 z"/></marker></defs>'
    '<line class="ln" x1="0" y1="0" x2="50" y2="0" marker-end="url(#arrow)"/>'
)


def test_marker_ids_are_namespaced_per_figure_so_two_figures_cannot_collide() -> None:
    a = _clean(_MARKER, namespace="intuition-0")
    b = _clean(_MARKER, namespace="intuition-1")
    assert 'id="intuition-0-arrow"' in a.svg
    assert 'marker-end="url(#intuition-0-arrow)"' in a.svg
    assert 'id="intuition-1-arrow"' in b.svg
    assert 'marker-end="url(#intuition-1-arrow)"' in b.svg
    # Nothing either figure declares is addressable from the other.
    assert set(re.findall(r'id="([^"]+)"', a.svg)).isdisjoint(re.findall(r'id="([^"]+)"', b.svg))
    assert a.stripped == 0 and b.stripped == 0


def test_a_marker_reference_that_is_not_a_same_document_url_is_dropped() -> None:
    out = _clean('<line class="ln" x1="0" y1="0" x2="1" y2="1" marker-end="url(https://evil.example/m.svg#a)"/>')
    assert "marker-end" not in out.svg
    assert "evil.example" not in out.svg
    assert out.stripped == 1


# --- Document-level wiring -------------------------------------------------


def _doc_with_figure(svg: str) -> explainer_schema.ExplainerDocument:
    return explainer_schema.ExplainerDocument(
        base_sha="aaaa1111",
        head_sha="bbbb2222",
        verdict="narrate",
        sections=[
            explainer_schema.Section(
                id="intuition",
                kind="intuition",
                pass_id="walkthrough",
                title="Intuition",
                state="ready",
                figures=[explainer_schema.Figure(svg=svg, alt="the request path", caption="how a read resolves")],
            )
        ],
    )


def test_saving_a_document_sanitises_its_figures_and_records_the_count(tmp_path: Path) -> None:
    doc = _doc_with_figure(_svg('<rect class="d-box loud" x="0" y="0" width="4" height="4" fill="hotpink"/>'))
    written = explainer_schema.save_explainer(tmp_path, doc)

    assert "hotpink" not in written.sections[0].figures[0].svg
    assert written.sections[0].figures[0].stripped == 2
    # The caller's copy is untouched; what is on disk is what was returned.
    assert "hotpink" in doc.sections[0].figures[0].svg
    loaded = explainer_schema.load_explainer(tmp_path, base_sha="aaaa1111", head_sha="bbbb2222")
    assert loaded is not None
    assert loaded.sections[0].figures[0].svg == written.sections[0].figures[0].svg


def test_a_strip_count_survives_the_documents_next_write(tmp_path: Path) -> None:
    """Every prose call rewrites the whole document, so a figure the
    first one wrote is sanitised again by the second — which finds
    nothing left to remove. Recording that as zero would lose the count
    the reader is shown."""
    doc = _doc_with_figure(_svg('<rect class="d-box loud" x="0" y="0" width="4" height="4" fill="hotpink"/>'))
    first = explainer_schema.save_explainer(tmp_path, doc)
    second = explainer_schema.save_explainer(tmp_path, first)

    assert second.sections[0].figures[0].stripped == first.sections[0].figures[0].stripped == 2


def test_re_sanitising_a_figure_leaves_its_ids_where_they_were(tmp_path: Path) -> None:
    """The prefix is applied once, not once per write: an id that grew
    on every save would be a different id in the next document."""
    doc = _doc_with_figure(_svg(_MARKER))
    first = explainer_schema.save_explainer(tmp_path, doc)
    second = explainer_schema.save_explainer(tmp_path, first)

    assert 'id="intuition-0-arrow"' in first.sections[0].figures[0].svg
    assert second.sections[0].figures[0].svg == first.sections[0].figures[0].svg


def test_a_figure_that_loses_everything_is_kept_not_dropped(tmp_path: Path) -> None:
    written = explainer_schema.save_explainer(tmp_path, _doc_with_figure("<svg><rect>"))
    figure = written.sections[0].figures[0]
    assert figure.svg == ""
    assert figure.stripped == 1
    assert figure.alt == "the request path"
    assert figure.caption == "how a read resolves"


def test_subsection_figures_are_sanitised_too(tmp_path: Path) -> None:
    doc = _doc_with_figure(_svg('<rect class="d-box" x="0" y="0" width="4" height="4"/>'))
    doc.sections[0].subsections = [
        explainer_schema.Section(
            id="the proto",
            kind="code",
            pass_id="walkthrough",
            title="The proto",
            figures=[explainer_schema.Figure(svg=_svg('<rect fill="red" x="0" y="0" width="1" height="1"/>'), alt="a")],
        )
    ]
    written = explainer_schema.save_explainer(tmp_path, doc)
    nested = written.sections[0].subsections[0].figures[0]
    assert "red" not in nested.svg
    assert nested.stripped == 1


def test_a_model_chosen_subsection_id_still_yields_a_usable_id_prefix(tmp_path: Path) -> None:
    doc = _doc_with_figure(_svg('<rect class="d-box" x="0" y="0" width="1" height="1"/>'))
    doc.sections[0].subsections = [
        explainer_schema.Section(
            id="4 the/proto!",
            kind="code",
            pass_id="walkthrough",
            title="The proto",
            figures=[explainer_schema.Figure(svg=_svg('<defs><marker id="a" viewBox="0 0 1 1"/></defs>'), alt="a")],
        )
    ]
    written = explainer_schema.save_explainer(tmp_path, doc)
    assert 'id="s-4-the-proto-0-a"' in written.sections[0].subsections[0].figures[0].svg


# --- The two halves of the contract ---------------------------------------


def test_the_renderer_and_the_sanitiser_agree_on_the_class_vocabulary() -> None:
    """One list, two languages. Drift is how the look degrades."""
    ts = (_ASSETS / "explainer_figure.ts").read_text(encoding="utf-8")
    block = re.search(r"FIGURE_CLASSES[^[]*\[(.*?)\]", ts, re.S)
    assert block is not None, "explainer_figure.ts no longer declares FIGURE_CLASSES as an array literal"
    assert set(re.findall(r'"([^"]+)"', block.group(1))) == set(explainer_figures.FIGURE_CLASSES)


def test_every_vocabulary_class_is_styled_by_the_viewer() -> None:
    css = (_ASSETS / "viewer.css").read_text(encoding="utf-8")
    styled = set(re.findall(r"\.explainer-figure svg \.([\w-]+)", css))
    assert set(explainer_figures.FIGURE_CLASSES) - styled == set()


def test_the_prompt_names_exactly_the_vocabulary_the_sanitiser_keeps() -> None:
    """The third consumer of the contract. A class the guidance offers
    and the sanitiser strips renders unstyled; one the sanitiser keeps
    and the guidance never mentions is never used."""
    named = set(re.findall(r"`([\w-]+)`", prompts.EXPLAINER_FIGURE_GUIDANCE))
    assert set(explainer_figures.FIGURE_CLASSES) - named == set()


def test_the_prompt_names_every_element_the_sanitiser_allows() -> None:
    named = set(re.findall(r"`([\w-]+)`", prompts.EXPLAINER_FIGURE_GUIDANCE))
    assert set(explainer_figures.ALLOWED_ELEMENTS) - named == set()


def _ts_tokens(ts: str, name: str) -> set[str]:
    """The strings of a `const <name> = [...]` literal, resolving a
    `...OTHER` spread against the same file."""
    block = re.search(rf"const {name}\b[^[]*\[(.*?)\]", ts, re.S)
    assert block is not None, f"explainer_figure.ts no longer declares {name} as an array literal"
    body = block.group(1)
    out = set(re.findall(r'"([^"]+)"', body))
    for spread in re.findall(r"\.\.\.(\w+)", body):
        out |= _ts_tokens(ts, spread)
    return out


def test_the_renderer_and_the_sanitiser_agree_on_which_classes_are_text() -> None:
    """The applicability half of the vocabulary. A class kept on the
    wrong element renders wrong rather than unstyled."""
    ts = (_ASSETS / "explainer_figure.ts").read_text(encoding="utf-8")
    assert _ts_tokens(ts, "TEXT_CLASSES") == set(explainer_figures.TEXT_CLASSES)


def test_the_renderer_and_the_sanitiser_agree_on_which_elements_take_a_class() -> None:
    """The class lists agreeing is not enough: the two sides must also
    agree on where each class may go. Drift here renders a figure
    differently depending on which server wrote the document."""
    ts = (_ASSETS / "explainer_figure.ts").read_text(encoding="utf-8")
    text_classes = _ts_tokens(ts, "TEXT_CLASSES")
    text_elements = _ts_tokens(ts, "TEXT_ELEMENTS")
    paint_elements = _ts_tokens(ts, "PAINT_ELEMENTS")
    for cls in explainer_figures.FIGURE_CLASSES:
        expected = text_elements if cls in text_classes else paint_elements
        assert expected == set(explainer_figures.class_elements(cls)), cls


def test_a_font_setting_class_is_declared_a_text_class() -> None:
    """`TEXT_CLASSES` is hand-maintained and the stylesheet is evidence
    for it, in one direction only: a class that sets a font but is not
    declared text gets stripped off every `<text>` it belongs on, so its
    font never applies. The converse is allowed — a text class may set
    only colour, as a status label reasonably would.

    Iterates the vocabulary, so a rule in the stylesheet that no class
    names is not in scope here; the sanitiser drops those anyway.
    """
    css = (_ASSETS / "viewer.css").read_text(encoding="utf-8")
    for cls in explainer_figures.FIGURE_CLASSES:
        rule = re.search(rf"\.explainer-figure svg \.{re.escape(cls)} \{{(.*?)\}}", css, re.S)
        assert rule is not None, f"{cls} has no rule in viewer.css"
        if "font-" in rule.group(1):
            assert cls in explainer_figures.TEXT_CLASSES, (
                f"{cls} sets a font property but is not in TEXT_CLASSES, so it would be "
                f"stripped from the elements it is meant to style"
            )


def test_the_stylesheet_does_not_decide_text_alignment() -> None:
    """`text-anchor` binds to the `x` the figure computed, so only the
    figure can choose it. A stylesheet default silently re-anchors every
    label that omitted the attribute to mean `start`."""
    css = (_ASSETS / "viewer.css").read_text(encoding="utf-8")
    offenders = [
        selector.strip()
        for selector, body in re.findall(r"([^{}]*\.explainer-figure[^{]*)\{([^}]*)\}", css)
        if "text-anchor" in body
    ]
    assert offenders == []
    assert "text-anchor` defaults to `start`" in prompts.EXPLAINER_FIGURE_GUIDANCE


def test_a_shape_class_on_a_text_element_is_dropped() -> None:
    """`chip` names a pill. On a `<text>` it outlines the glyphs instead,
    which is how it reached a rendered document."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        '<text class="chip" x="1" y="2">=1000</text>'
        '<text class="t-b" x="1" y="4">kept</text>'
        '<rect class="chip" x="0" y="0" width="2" height="2"/>'
        "</svg>"
    )
    out = explainer_figures.sanitize_svg(svg, namespace="f")
    assert '<text x="1" y="2">=1000</text>' in out.svg
    assert '<text class="t-b" x="1" y="4">kept</text>' in out.svg
    assert '<rect class="chip"' in out.svg


def test_a_container_takes_either_kind_of_class() -> None:
    """A class on a container is inherited styling, so it cannot be
    resolved to one kind. `title` and `desc` are never painted."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" class="t-sm">'
        '<g class="ln"><line class="ln" x1="0" y1="0" x2="4" y2="4"/></g>'
        '<title class="t-b">alt</title>'
        '<desc class="t-sm">d</desc>'
        "</svg>"
    )
    out = explainer_figures.sanitize_svg(svg, namespace="f")
    assert 'class="t-sm"' in out.svg
    assert '<g class="ln">' in out.svg
    assert "<title>alt</title>" in out.svg
    assert "<desc>d</desc>" in out.svg


def test_a_text_class_on_a_shape_is_dropped() -> None:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        '<rect class="t-b" x="0" y="0" width="2" height="2"/>'
        "</svg>"
    )
    out = explainer_figures.sanitize_svg(svg, namespace="f")
    assert "t-b" not in out.svg


# --- Threading the family and cast into a prose call ----------------------


def test_figure_context_carries_the_family_and_the_cast() -> None:
    out = prompts.format_figure_context("boxes are services, dashed arrows are events", ["Cursor", "ListRequest"])
    assert "boxes are services, dashed arrows are events" in out
    assert "Cursor, ListRequest" in out


def test_figure_context_refuses_a_document_with_no_family() -> None:
    with pytest.raises(ValueError, match="no figure family"):
        prompts.format_figure_context("   ", ["Cursor"])


def test_prose_guidance_is_the_vocabulary_plus_this_document_s_decisions() -> None:
    doc = _doc_with_figure(_svg('<g class="d-box"/>'))
    doc.figure_family = "boxes are services"
    doc.cast = ["Cursor"]
    out = explainer.prose_figure_guidance(doc)
    assert prompts.EXPLAINER_FIGURE_GUIDANCE in out
    assert "boxes are services" in out
    assert "Cursor" in out


def test_prose_guidance_is_empty_when_the_skeleton_fixed_no_family() -> None:
    """No family means nothing keeps three sections drawing the same
    component the same way — better no figures than three languages."""
    assert explainer.prose_figure_guidance(_doc_with_figure(_svg("<g/>"))) == ""


def test_figure_guidance_rides_the_same_carrier_as_the_rest() -> None:
    doc = _doc_with_figure(_svg("<g/>"))
    doc.figure_family = "boxes are services"
    guidance = explainer.prose_figure_guidance(doc)

    sdk_system, sdk_prefix = explainer.carry_guidance(Client(model="anthropic:x"), guidance)
    assert guidance in sdk_system
    assert sdk_prefix == ""

    cli_system, cli_prefix = explainer.carry_guidance(Client(model="anthropic:x", is_subprocess_backend=True), guidance)
    assert cli_prefix == guidance
    assert guidance not in cli_system
