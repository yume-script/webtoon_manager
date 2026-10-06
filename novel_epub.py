# -*- coding: utf-8 -*-
"""
웹소설 회차 -> EPUB 3 파일 만들기 (표준 라이브러리만 사용)
------------------------------------------------------------
회차 하나 = EPUB 하나. 카카오 본문 조각(sections)을 각각 XHTML 문서로 넣고,
삽화는 images/ 아래에 넣는다. 표지(isCover 이미지 또는 작품 표지)를 EPUB 표지로 지정.
"""
import os
import uuid
import zipfile
from xml.sax.saxutils import escape

DEFAULT_CSS = """
body { margin: 0 4%; line-height: 1.8; word-break: keep-all; }
p { margin: 0 0 0.6em 0; text-indent: 0; }
h1 { font-size: 1.3em; text-align: center; margin: 1.5em 0; }
div.img { text-align: center; margin: 1em 0; }
div.img img { max-width: 100%; height: auto; }
div.cover img { max-height: 95vh; }
"""


def _xhtml(title, body, css_files):
    links = "".join('<link rel="stylesheet" type="text/css" href="%s"/>' % c for c in css_files)
    return ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
            'xml:lang="ko" lang="ko"><head><meta charset="utf-8"/><title>%s</title>%s</head>'
            '<body>%s</body></html>' % (escape(title), links, body))


def build_epub(path, book_title, series_title, author, sections, images, css="",
               cover=None, identifier=None, language="ko", episode_no=None, publisher="카카오페이지"):
    """path에 EPUB을 원자적으로 만든다.
    sections: XHTML body 조각 목록, images: {파일명: (bytes, media_type)},
    cover: (bytes, media_type) - 본문에 표지 이미지가 없을 때 쓸 작품 표지."""
    ident = identifier or ("urn:uuid:%s" % uuid.uuid4())
    images = dict(images or {})
    cover_name = None
    # 본문 첫 이미지가 표지 클래스면 그걸 표지로, 아니면 작품 표지를 추가
    if images and sections and 'class="img cover"' in sections[0]:
        cover_name = sorted(images.keys())[0]
    elif cover:
        ext = {"image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}.get(cover[1], ".jpg")
        cover_name = "cover" + ext
        images[cover_name] = cover
        sections = ['<div class="img cover"><img src="images/%s" alt="cover"/></div>' % cover_name] + list(sections)

    css_files = ["style/default.css"] + (["style/kakao.css"] if css.strip() else [])
    docs = []
    for i, body in enumerate(sections, 1):
        docs.append(("text/part%03d.xhtml" % i, _xhtml(book_title, body, ["../" + c for c in css_files])))

    nav = _xhtml(book_title, '<nav epub:type="toc" id="toc"><h1>%s</h1><ol><li><a href="%s">%s</a></li></ol></nav>'
                 % (escape(book_title), docs[0][0] if docs else "", escape(book_title)), css_files)

    manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                '<item id="css0" href="style/default.css" media-type="text/css"/>']
    if css.strip():
        manifest.append('<item id="css1" href="style/kakao.css" media-type="text/css"/>')
    spine = []
    for i, (href, _x) in enumerate(docs, 1):
        manifest.append('<item id="p%03d" href="%s" media-type="application/xhtml+xml"/>' % (i, href))
        spine.append('<itemref idref="p%03d"/>' % i)
    for name, (_data, mt) in sorted(images.items()):
        props = ' properties="cover-image"' if name == cover_name else ""
        manifest.append('<item id="img_%s" href="text/images/%s" media-type="%s"%s/>' % (
            name.replace(".", "_"), name, mt, props))

    meta_extra = ""
    if series_title:
        meta_extra += ('<meta property="belongs-to-collection" id="series">%s</meta>'
                       '<meta refines="#series" property="collection-type">series</meta>' % escape(series_title))
        if episode_no is not None and episode_no < 9000:
            meta_extra += '<meta refines="#series" property="group-position">%d</meta>' % episode_no
    if cover_name:
        meta_extra += '<meta name="cover" content="img_%s"/>' % cover_name.replace(".", "_")
    opf = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="%s">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           '<dc:identifier id="bookid">%s</dc:identifier><dc:title>%s</dc:title>'
           '<dc:creator>%s</dc:creator><dc:publisher>%s</dc:publisher><dc:language>%s</dc:language>'
           '<meta property="dcterms:modified">2000-01-01T00:00:00Z</meta>%s</metadata>'
           '<manifest>%s</manifest><spine>%s</spine></package>') % (
        language, escape(ident), escape(book_title), escape(author or ""), escape(publisher), language,
        meta_extra, "".join(manifest), "".join(spine))
    container = ('<?xml version="1.0" encoding="utf-8"?>\n'
                 '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                 '</rootfiles></container>')

    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w") as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/nav.xhtml", nav, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/style/default.css", DEFAULT_CSS, compress_type=zipfile.ZIP_DEFLATED)
        if css.strip():
            zf.writestr("OEBPS/style/kakao.css", css, compress_type=zipfile.ZIP_DEFLATED)
        for href, x in docs:
            zf.writestr("OEBPS/" + href, x, compress_type=zipfile.ZIP_DEFLATED)
        for name, (data, _mt) in images.items():
            zf.writestr("OEBPS/text/images/" + name, data, compress_type=zipfile.ZIP_STORED)
    os.replace(tmp, path)
    return path


# ---- 1.32.0 이전에 만든 EPUB 복구 -------------------------------------------
# 예전 버전은 카카오 원문에 이미 들어 있던 엔티티(&lt; &gt; &quot; ...)를 한 번 더
# 이스케이프해서 본문에 '&lt;어둠탐사기록&gt;' 같은 코드가 그대로 보였다.
# 파일을 다시 받지 않고 EPUB 안의 XHTML만 고쳐 쓴다.
import html as _html
import re as _re

_DOUBLE_ENT_RE = _re.compile(r"&amp;(#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]{1,31});")
_XML_ENTS = {"lt", "gt", "amp", "quot", "apos"}


def _fix_double_entities(text):
    def _sub(m):
        name = m.group(1)
        if name in _XML_ENTS or name.startswith("#"):
            return "&%s;" % name
        ch = _html.unescape("&%s;" % name)
        if ch == "&%s;" % name:          # 모르는 이름이면 손대지 않음
            return m.group(0)
        return escape(ch)
    for _ in range(3):
        new = _DOUBLE_ENT_RE.sub(_sub, text)
        if new == text:
            break
        text = new
    return text


def repair_epub(path):
    """본문에 이중 이스케이프된 엔티티가 있으면 고쳐서 다시 저장. 고쳤으면 True."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            items = [(i, zf.read(i.filename)) for i in infos]
    except Exception:  # noqa: BLE001
        return False
    changed = False
    out = []
    for info, data in items:
        if info.filename.lower().endswith((".xhtml", ".html")) and b"&amp;" in data:
            txt = data.decode("utf-8", "replace")
            fixed = _fix_double_entities(txt)
            if fixed != txt:
                data = fixed.encode("utf-8")
                changed = True
        out.append((info, data))
    if not changed:
        return False
    tmp = path + ".tmp"
    try:
        with zipfile.ZipFile(tmp, "w") as zf:
            for info, data in out:
                ct = zipfile.ZIP_STORED if info.filename == "mimetype" else info.compress_type
                zf.writestr(info.filename, data, compress_type=ct)
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
