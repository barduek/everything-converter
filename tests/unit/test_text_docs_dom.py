"""Pruebas de src/converter/converters/text_docs/dom.py: árbol HTML desde el Texto_Decodificado,
saneamiento de Scripts y Recursos_Externos, Texto_Visible, HTML de CommonMark e inserción de
U+2060 (Req 2.12, 6.4, 6.6, 6.7, 6.13).

Los resultados esperados se escriben a mano a partir del glosario (Texto_Visible, Script,
Recurso_Externo) y de la lista de Recursos_Externos del diseño.
"""

from __future__ import annotations

import warnings

import pytest

from converter.converters.text_docs.dom import (
    WORD_JOINER,
    SanitizeReport,
    insert_word_joiners,
    markdown_to_html,
    parse_html,
    sanitize_dom,
    visible_text,
)

WJ = WORD_JOINER


def _sanitized(html: str) -> tuple[str, SanitizeReport]:
    doc = parse_html(html)
    report = sanitize_dom(doc)
    return str(doc), report


# --- parse_html ---


@pytest.mark.req("2.12")
def test_declared_charset_does_not_change_the_text() -> None:
    # El Texto_Decodificado ya es texto: la etiqueta meta charset no lo reinterpreta
    text = '<html><head><meta charset="iso-8859-5"></head><body><p>ñandú ¿qué?</p></body></html>'
    assert visible_text(parse_html(text)) == "ñandú ¿qué?"


@pytest.mark.req("2.12")
def test_a_document_that_looks_like_a_url_parses_without_warnings() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        doc = parse_html("http://example.com/index.html")
    assert visible_text(doc) == "http://example.com/index.html"


def test_attributes_are_plain_text() -> None:
    doc = parse_html('<p class="a b" rel="x y">t</p>')
    paragraph = doc.find("p")
    assert paragraph is not None
    assert paragraph.attrs == {"class": "a b", "rel": "x y"}


# --- sanitize_dom: Scripts ---


@pytest.mark.req("6.7", "6.13")
def test_script_elements_and_their_code_are_removed() -> None:
    html, report = _sanitized(
        "<head><script>alert('M1')</script></head>"
        "<body><p>antes</p><script src='x.js'>alert('M2')</script>"
        "<svg><script>alert('M3')</script></svg><p>después</p></body>"
    )
    assert "<script" not in html
    assert "M1" not in html and "M2" not in html and "M3" not in html
    assert report.scripts == 3
    assert report.external_resources == 0
    assert visible_text(parse_html(html)) == "antes\ndespués"


@pytest.mark.req("6.7", "6.13")
def test_event_handler_attributes_are_removed() -> None:
    html, report = _sanitized(
        '<body onload="go(\'M1\')"><p OnClick="go(\'M2\')" title="t">x</p>'
        "<svg><rect onmouseover=\"go('M3')\"/></svg></body>"
    )
    assert "M1" not in html and "M2" not in html and "M3" not in html
    assert 'title="t"' in html
    assert report.scripts == 3


@pytest.mark.req("6.7", "6.13")
@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert('M')",
        "JavaScript:alert('M')",
        "  javascript:alert('M')",
        "java\tscript:alert('M')",
        "\x01javascript:alert('M')",
    ],
)
def test_javascript_links_become_text(href: str) -> None:
    doc = parse_html(f'<p>ver <a href="{href}">enlace</a></p>')
    report = sanitize_dom(doc)
    link = doc.find("a")
    assert link is not None
    assert "href" not in link.attrs
    assert "alert" not in str(doc)
    assert visible_text(doc) == "ver enlace"
    assert report.scripts == 1


@pytest.mark.req("6.7", "6.13")
def test_javascript_urls_in_other_url_attributes_are_removed() -> None:
    html, report = _sanitized(
        '<form action="javascript:a(1)"><button formaction="javascript:a(2)">b</button></form>'
        '<iframe src="javascript:a(3)"></iframe>'
        '<svg><a xlink:href="javascript:a(4)"><text>s</text></a></svg>'
    )
    assert "javascript" not in html
    assert report.scripts == 4
    assert report.external_resources == 0


@pytest.mark.req("6.7", "6.13")
def test_markdown_javascript_link_is_neutralized_after_rendering() -> None:
    # CommonMark escribe el enlace como `a`; el saneamiento lo deja como texto
    doc = parse_html(markdown_to_html("[clic](javascript:alert('MARCA')) y <b onclick='x()'>b</b>"))
    report = sanitize_dom(doc)
    assert "MARCA" not in str(doc)
    assert visible_text(doc) == "clic y b"
    assert report.scripts == 2


# --- sanitize_dom: Recursos_Externos ---


@pytest.mark.req("6.6")
@pytest.mark.parametrize(
    "markup",
    [
        '<img src="https://h/a.png" alt="ALT">',
        '<img srcset="a.png 1x, a2.png 2x">',
        '<link rel="stylesheet" href="/estilo.css">',
        '<link rel="shortcut icon" href="favicon.ico">',
        '<link rel="preload" href="font.woff2" as="font">',
        '<iframe src="../marco.html"></iframe>',
        '<object data="file:///etc/passwd">alternativo</object>',
        '<embed src="//servidor/doc.pdf">',
        '<audio src="a.mp3"></audio>',
        '<video poster="p.jpg"></video>',
        '<video><source src="v.webm"></video>',
        '<video><track src="subs.vtt"></video>',
        '<input type="image" src="boton.png">',
        '<svg><image href="s.png"/></svg>',
        '<svg><use xlink:href="sprite.svg#icono"/></svg>',
        "<style>@import 'otra.css';</style>",
        '<style>p { background: url("fondo.png") }</style>',
        '<p style="background-image: url(\\\\servidor\\recurso.png)">x</p>',
    ],
)
def test_each_listed_external_resource_counts_once(markup: str) -> None:
    _, report = _sanitized(f"<body>{markup}</body>")
    assert report == SanitizeReport(external_resources=1, scripts=0)


@pytest.mark.req("6.6")
def test_navigation_links_data_urls_and_local_references_are_not_counted() -> None:
    html = (
        '<head><link rel="alternate" href="https://h/feed.xml">'
        '<link rel="stylesheet" href="data:text/css,p{color:red}"></head>'
        '<body><a href="https://h/otra.html">nav</a><map><area href="z.html"></map>'
        '<img src="data:image/png;base64,AAAA" alt="">'
        '<img src="">'
        '<iframe src="about:blank"></iframe>'
        '<svg><use href="#local"/></svg>'
        "<style>i { background: url(data:image/png;base64,AA) } b { filter: url(#f) }"
        "@namespace svg url(http://www.w3.org/2000/svg);"
        '/* url(comentario.png) */ q::before { content: "url(cadena.png)" }</style></body>'
    )
    result, report = _sanitized(html)
    assert report == SanitizeReport(external_resources=0, scripts=0)
    # Nada cambió: las referencias que no son Recursos_Externos se conservan
    assert result == str(parse_html(html))


@pytest.mark.req("6.6")
def test_resources_are_counted_per_element_and_per_css_reference() -> None:
    _, report = _sanitized(
        '<body><img src="a.png" srcset="a.png 1x, b.png 2x"><img src="a.png">'
        '<video src="v.mp4" poster="p.jpg"><source src="v.webm"><source src="v.ogv"></video>'
        "<style>@import url(a.css) screen; p { background: url(x.png), url(y.png) }</style>"
        "</body>"
    )
    # 2 img + 1 video + 2 source + 1 @import + 2 url()
    assert report.external_resources == 8


@pytest.mark.req("6.6")
def test_external_urls_do_not_remain_after_sanitizing() -> None:
    urls = [
        "https://h/a.png",
        "rel/b.png",
        "/abs/c.css",
        "//red/d.svg",
        "file:///e.mp4",
        "C:\\f.png",
        "g.css",
        "h.png",
    ]
    html, report = _sanitized(
        f'<head><link rel="stylesheet" href="{urls[2]}"></head><body>'
        f'<img src="{urls[0]}"><img srcset="{urls[1]} 2x"><svg><image href="{urls[3]}"/></svg>'
        f'<video src="{urls[4]}">texto de video</video><embed src="{urls[5]}">'
        f"<style>@import '{urls[6]}'; p {{ background: url({urls[7]}) }}</style></body>"
    )
    assert report.external_resources == len(urls)
    for url in urls:
        assert url not in html


@pytest.mark.req("6.6")
def test_elements_without_source_are_removed_and_fallback_content_is_kept() -> None:
    doc = parse_html(
        '<p>a<img src="x.png" alt="ALTERNATIVO">b</p>'
        '<object data="o.svg"><p>contenido alternativo</p></object>'
        '<video src="v.mp4">sin video</video>'
        '<iframe src="m.html">texto crudo</iframe>'
    )
    sanitize_dom(doc)
    assert doc.find("img") is None
    assert doc.find("iframe") is None
    object_element = doc.find("object")
    video = doc.find("video")
    assert object_element is not None and object_element.attrs == {}
    assert video is not None and video.attrs == {}
    # El texto alternativo de la imagen omitida no queda en el documento
    assert "ALTERNATIVO" not in str(doc)
    assert visible_text(doc) == "ab\ncontenido alternativo\nsin video"


@pytest.mark.req("6.6")
def test_srcset_keeps_its_data_candidates() -> None:
    doc = parse_html('<img src="a.png" srcset="b.png 2x, data:image/png;base64,QQ== 3x">')
    report = sanitize_dom(doc)
    image = doc.find("img")
    assert image is not None
    assert image.attrs == {"srcset": "data:image/png;base64,QQ== 3x"}
    assert report.external_resources == 1


@pytest.mark.req("6.6")
def test_css_imports_are_removed_and_urls_become_none() -> None:
    doc = parse_html(
        "<style>@import url(\"a.css\") print;\np { background: URL( 'b.png' ) no-repeat; }</style>"
        '<p style="color: red; background: url(c.png)">x</p>'
    )
    report = sanitize_dom(doc)
    style = doc.find("style")
    paragraph = doc.find("p")
    assert style is not None and paragraph is not None
    assert "".join(map(str, style.contents)) == "\np { background: none no-repeat; }"
    assert paragraph.attrs["style"] == "color: red; background: none"
    assert report.external_resources == 3


# --- visible_text ---


@pytest.mark.req("6.4")
def test_visible_text_excludes_markup_comments_scripts_and_styles() -> None:
    doc = parse_html(
        "<html><head><title>Título</title><style>p{}</style></head><body>"
        "<!-- comentario --><p>uno &amp; dos &#241; &lt;tres&gt;</p>"
        "<script>codigo()</script><style>q{}</style><template><p>plantilla</p></template>"
        "<noscript>sin scripts</noscript></body></html>"
    )
    assert visible_text(doc) == "uno & dos ñ <tres>\nsin scripts"


@pytest.mark.req("6.4")
def test_block_boundaries_and_forced_breaks_start_lines() -> None:
    doc = parse_html(
        "<h1>Título</h1><p>Un  párrafo\n con <b>negrita</b>y<i> cursiva </i></p>"
        "<ul><li>uno</li><li>dos<br>tres</li></ul>"
        "<table><tr><td>c1</td><th>c2</th></tr></table>"
        "<div>a<p>b</p>c</div><p></p><p>d<br><br>e</p>"
    )
    assert visible_text(doc) == (
        "Título\nUn párrafo con negritay cursiva\nuno\ndos\ntres\nc1\nc2\na\nb\nc\nd\n\ne"
    )


@pytest.mark.req("6.4")
def test_preformatted_text_keeps_spaces_and_lines() -> None:
    doc = parse_html("<p>antes</p><pre>\n  sangría\tfin  \n\notra</pre><p>después</p>")
    assert visible_text(doc) == "antes\n  sangría\tfin  \n\notra\ndespués"


@pytest.mark.req("6.4")
def test_markdown_visible_text() -> None:
    markdown = "# Título\n\nUn *párrafo*\ncon salto  \nforzado.\n\n- a\n- b\n\n```\nx  = 1\n```\n"
    doc = parse_html(markdown_to_html(markdown))
    # El salto de línea suave es espacio; el forzado (dos espacios al final) inicia una línea
    assert visible_text(doc) == "Título\nUn párrafo con salto\nforzado.\na\nb\nx  = 1\n"


@pytest.mark.req("6.4")
def test_list_markers_numbering_and_indentation() -> None:
    doc = parse_html(
        "<ul><li>uno</li><li><p>dos</p><ol start='9'><li>nueve</li><li>diez"
        "<ul><li>hondo</li></ul></li></ol></li></ul>"
        "<ol><li value='3'>tres</li><li>cuatro</li><li></li></ol>"
    )
    assert visible_text(doc, list_markers=True) == (
        "- uno\n- dos\n  9. nueve\n  10. diez\n      - hondo\n3. tres\n4. cuatro\n5."
    )
    # Sin marcadores, el mismo documento da el Texto_Visible
    assert visible_text(doc) == "uno\ndos\nnueve\ndiez\nhondo\ntres\ncuatro"


@pytest.mark.req("6.4")
def test_list_item_starting_with_a_nested_list() -> None:
    doc = parse_html("<ul><li><ul><li>x</li></ul></li></ul>")
    assert visible_text(doc, list_markers=True) == "-\n  - x"


def test_deeply_nested_document_does_not_exhaust_recursion() -> None:
    depth = 5000
    doc = parse_html("<span>" * depth + "hondo" + "</span>" * depth)
    assert visible_text(doc) == "hondo"


def test_frameset_document_has_no_visible_text() -> None:
    assert visible_text(parse_html('<frameset><frame src="a.html"></frameset>')) == ""


# --- markdown_to_html ---


def test_markdown_to_html_follows_commonmark() -> None:
    assert markdown_to_html("# T\n\n*a* **b** `c`\n\n1. x\n2. y\n") == (
        "<h1>T</h1>\n<p><em>a</em> <strong>b</strong> <code>c</code></p>\n"
        "<ol>\n<li>x</li>\n<li>y</li>\n</ol>\n"
    )


def test_markdown_to_html_keeps_embedded_html_and_every_link_scheme() -> None:
    # CommonMark no filtra esquemas de URL; el saneamiento depende del destino
    markdown = "<div onclick='x()'>h</div>\n\n[a](javascript:x()) ![i](file:///i.png)"
    assert markdown_to_html(markdown) == (
        "<div onclick='x()'>h</div>\n"
        '<p><a href="javascript:x()">a</a> <img src="file:///i.png" alt="i" /></p>\n'
    )


# --- insert_word_joiners ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("e-mail y/o a.b", f"e-{WJ}mail y/{WJ}o a.{WJ}b"),
        ("¿Qué? ¡Sí!", f"¿{WJ}Qué? ¡{WJ}Sí!"),
        ("fin. Coma, punto;\tlínea:\nnueva", "fin. Coma, punto;\tlínea:\nnueva"),
        ("(a)b", f"({WJ}a){WJ}b"),
        ("x--y", f"x-{WJ}-{WJ}y"),
        ("100%seguro $5", f"100%{WJ}seguro ${WJ}5"),
        # U+00B4 permite cortar antes de él, aunque siga a una letra
        ("a´b", f"a{WJ}´{WJ}b"),
        ("palabra", "palabra"),
        ("", ""),
    ],
)
def test_word_joiners_after_punctuation_inside_words(text: str, expected: str) -> None:
    assert insert_word_joiners(text) == expected


def test_word_joiners_respect_the_following_text() -> None:
    assert insert_word_joiners("foo-", following="bar") == f"foo-{WJ}"
    assert insert_word_joiners("foo-", following=" bar") == "foo-"
    assert insert_word_joiners("abc", following="´x") == f"abc{WJ}"
    assert insert_word_joiners("abc", following="def") == "abc"
    assert insert_word_joiners("", following="-x") == ""


def test_word_joiners_do_not_split_graphemes() -> None:
    # Marca combinante sobre un guion, secuencia de emoji con ZWJ y bandera
    assert insert_word_joiners("-\u0301x") == f"-\u0301{WJ}x"
    family = "\U0001f468\u200d\U0001f469"
    assert insert_word_joiners(family) == family
    flag = "\U0001f1e6\U0001f1f7"
    assert insert_word_joiners(f"{flag}{flag}") == f"{flag}{WJ}{flag}"


def test_word_joiners_are_idempotent_and_removable() -> None:
    text = "Hola, señor-a y/o Dña. García´s (prueba)texto ¿qué?¡sí! a\u2060-b"
    once = insert_word_joiners(text)
    assert insert_word_joiners(once) == once
    assert once.replace(WJ, "") == text.replace(WJ, "")
