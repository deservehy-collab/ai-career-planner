"""简历 Word 导出：用标准库直接拼一个最小 DOCX 包。

为什么不引入 python-docx
--------------------------
本项目的运行环境由 ``requirements.txt`` 一次性装好，Word 导出只是把一个
静态表格写成文档，不需要库级排版能力。用标准库 ``zipfile`` 生成 OOXML
可以避免为一项导出功能新增 lxml / python-docx 依赖，安装面更小。

版式取自一份真实校招简历（A4）逐像素量出来的参数：
  · 微软雅黑；姓名 20pt；小节标题 12pt 加粗、通栏细线；
  · 正文 10pt、行距固定 15.6pt（1.56 倍）、段间距 0；
  · 页边距 上 20 / 下 14 / 左右 13.7 mm；
  · 联系方式两行四列，求职意向两行四列，列位按参考件实测；
  · 条目行“时间—单位—角色”三段式；
  · 不插强制分页，内容多了自然续页。
"""

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

from backend.resume_content import build_resume_content, photo_extent

__all__ = ["build_resume_docx"]

_FONT = "微软雅黑"
_FONT_ASCII = "Microsoft YaHei"

_TEXT_WIDTH = 10352
_INTENT_TABS = (2480, 4960, 7440)
_ENTRY_TITLE_TAB = 2930
_ENTRY_ROLE_TAB = 7768
_EDU_DEGREE_TAB = 6022

_NAME_SIZE = 40
_HEAD_SIZE = 24
_BODY_SIZE = 20
_LINE_BODY = "312"
_LINE_TIGHT = "240"

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Default Extension="jpg" ContentType="image/jpeg"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_IMAGE_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdPhoto" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/{name}"/>
</Relationships>"""

_SECT_PR = (
    "<w:sectPr>"
    '<w:pgSz w:w="11906" w:h="16838"/>'
    '<w:pgMar w:top="1134" w:right="777" w:bottom="794" w:left="777" '
    'w:header="0" w:footer="0" w:gutter="0"/>'
    '<w:cols w:space="720"/>'
    "</w:sectPr>"
)

_TABLE_BORDERS = (
    "<w:tblBorders>"
    + "".join(
        f'<w:{edge} w:val="none" w:sz="0" w:space="0" w:color="auto"/>'
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    + "</w:tblBorders>"
)

_TABLE_CELL_MARGIN = (
    "<w:tblCellMar>"
    '<w:top w:w="0" w:type="dxa"/><w:left w:w="0" w:type="dxa"/>'
    '<w:bottom w:w="0" w:type="dxa"/><w:right w:w="0" w:type="dxa"/>'
    "</w:tblCellMar>"
)


def _escape(value: object) -> str:
    return escape(str(value or ""), {'"': "&quot;"})


def _text_xml(value: str) -> str:
    """把文本写进 ``w:t``；制表符换成 ``w:tab``，空格保留。"""
    parts = str(value or "").split("\t")
    pieces: list[str] = []
    for index, part in enumerate(parts):
        if index:
            pieces.append("<w:tab/>")
        if part:
            pieces.append(f'<w:t xml:space="preserve">{_escape(part)}</w:t>')
    return "".join(pieces)


def _run(value: str, *, size: int = _BODY_SIZE, bold: bool = False) -> str:
    if value is None or value == "":
        return ""
    bold_xml = "<w:b/><w:bCs/>" if bold else ""
    return (
        "<w:r><w:rPr>"
        f'<w:rFonts w:ascii="{_FONT_ASCII}" w:hAnsi="{_FONT_ASCII}" '
        f'w:eastAsia="{_FONT}" w:cs="{_FONT_ASCII}"/>'
        f"{bold_xml}<w:sz w:val=\"{size}\"/><w:szCs w:val=\"{size}\"/>"
        "</w:rPr>"
        f"{_text_xml(value)}</w:r>"
    )


def _tabs(*stops: int) -> str:
    if not stops:
        return ""
    return "<w:tabs>" + "".join(
        f'<w:tab w:val="left" w:pos="{stop}"/>' for stop in stops
    ) + "</w:tabs>"


def _paragraph(
    runs: str,
    *,
    before: int = 0,
    after: int = 0,
    line: str = _LINE_BODY,
    line_rule: str = "exact",
    border: bool = False,
    tabs: tuple[int, ...] = (),
    keep_next: bool = False,
    align: str = "",
) -> str:
    props: list[str] = []
    if keep_next:
        props.append("<w:keepNext/>")
    if border:
        props.append(
            '<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="000000"/></w:pBdr>'
        )
    props.append(_tabs(*tabs))
    props.append(
        f'<w:spacing w:before="{before}" w:after="{after}" w:line="{line}" w:lineRule="{line_rule}"/>'
    )
    if align:
        props.append(f'<w:jc w:val="{align}"/>')
    return f"<w:p><w:pPr>{''.join(props)}</w:pPr>{runs}</w:p>"


def _section_title(title: str, before: int = 220, after: int = 210) -> str:
    return _paragraph(
        _run(title, size=_HEAD_SIZE, bold=True),
        before=before,
        after=after,
        line=_LINE_TIGHT,
        line_rule="auto",
        border=True,
        keep_next=True,
    )


def _grid_text(cells: list[dict]) -> str:
    last = -1
    for index, cell in enumerate(cells):
        if cell.get("value"):
            last = index
    parts: list[str] = []
    for index in range(last + 1):
        if index:
            parts.append("\t")
        if cells[index].get("value"):
            parts.append(f"{cells[index]['label']}：{cells[index]['value']}")
    return "".join(parts)


def _contact_paragraph(cells: list[dict]) -> str:
    text = "   ".join(
        f"{cell['label']}：{cell['value']}" for cell in cells if cell.get("value")
    )
    return _paragraph(_run(text), after=20) if text else ""


def _grid_paragraph(cells: list[dict], stops: tuple[int, ...], *, after: int) -> str:
    text = _grid_text(cells)
    if not text.strip():
        return ""
    return _paragraph(_run(text), after=after, tabs=stops)


def _name_block(content: dict) -> str:
    name = _run(content["name"], size=_NAME_SIZE, bold=True)
    paragraphs = [
        _paragraph(name, after=328, line="480", line_rule="exact")
    ]
    for row in content["contacts"]:
        paragraph = _contact_paragraph(row)
        if paragraph:
            paragraphs.append(paragraph)
    return "".join(paragraphs)


def _photo_paragraph(photo: dict, rel_id: str) -> str:
    width, height = photo_extent(photo)
    drawing = (
        "<w:r><w:drawing>"
        '<wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{width}" cy="{height}"/>'
        '<wp:docPr id="1" name="照片"/>'
        '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:nvPicPr><pic:cNvPr id="1" name="photo.{photo["ext"]}"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{rel_id}"/>'
        '<a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{width}" cy="{height}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        '<a:ln w="6350"><a:solidFill><a:srgbClr val="D9D9D9"/></a:solidFill></a:ln>'
        "</pic:spPr></pic:pic></a:graphicData></a:graphic>"
        "</wp:inline></w:drawing></w:r>"
    )
    return _paragraph(drawing, line=_LINE_TIGHT, line_rule="auto", align="right")


def _header(content: dict, rel_id: str | None) -> str:
    body = _name_block(content)
    photo = content.get("photo")
    if not photo or not rel_id:
        return body

    left = (
        '<w:tc><w:tcPr><w:tcW w:w="8452" w:type="dxa"/>'
        '<w:vAlign w:val="top"/></w:tcPr>' + body + "</w:tc>"
    )
    right = (
        '<w:tc><w:tcPr><w:tcW w:w="1900" w:type="dxa"/>'
        '<w:vAlign w:val="top"/></w:tcPr>'
        + _photo_paragraph(photo, rel_id)
        + "</w:tc>"
    )
    return (
        "<w:tbl><w:tblPr>"
        f'<w:tblW w:w="{_TEXT_WIDTH}" w:type="dxa"/>'
        '<w:tblLayout w:type="fixed"/>'
        f"{_TABLE_BORDERS}{_TABLE_CELL_MARGIN}"
        "</w:tblPr>"
        '<w:tblGrid><w:gridCol w:w="8452"/><w:gridCol w:w="1900"/></w:tblGrid>'
        f'<w:tr><w:trPr><w:cantSplit/></w:trPr>{left}{right}</w:tr>'
        "</w:tbl>"
    )


def _grid_block(block: dict) -> str:
    if not block["rows"]:
        return ""
    paragraphs = [_section_title(block["title"], before=342, after=190)]
    for row in block["rows"]:
        paragraph = _grid_paragraph(row, _INTENT_TABS, after=60)
        if paragraph:
            paragraphs.append(paragraph)
    return "".join(paragraphs)


def _education_block(block: dict) -> str:
    paragraphs = [_section_title(block["title"])]
    for index, item in enumerate(block["items"]):
        head = ""
        if item.get("time"):
            head += _run(item["time"], bold=True)
        if item.get("degree"):
            head += _run("	" + item["degree"], bold=True)
        if head.strip():
            paragraphs.append(_paragraph(
                head,
                before=60 if index == 0 else 120,
                after=30,
                line="250",
                line_rule="exact",
                tabs=(_EDU_DEGREE_TAB,),
                keep_next=True,
            ))
        for line in item["lines"]:
            paragraphs.append(_paragraph(_run(f"{line['label']}：{line['value']}")))
    return "".join(paragraphs)


def _entries_block(block: dict) -> str:
    paragraphs = [_section_title(block["title"])]
    numbered = block.get("numbered", True)
    for index, item in enumerate(block["items"]):
        head = ""
        if item.get("time"):
            head += _run(item["time"], bold=True)
        if item.get("title"):
            head += _run("	" + item["title"], bold=True)
        if item.get("role"):
            head += _run("	" + item["role"], bold=True)
        if head.strip():
            paragraphs.append(_paragraph(
                head,
                before=60 if index == 0 else 188,
                after=44,
                line="250",
                line_rule="exact",
                tabs=(_ENTRY_TITLE_TAB, _ENTRY_ROLE_TAB),
                keep_next=True,
            ))
        for number, bullet in enumerate(item["bullets"], start=1):
            text = f"{number}. {bullet}" if numbered else bullet
            paragraphs.append(_paragraph(_run(text)))
    return "".join(paragraphs)


def _labeled_block(block: dict) -> str:
    paragraphs = [_section_title(block["title"])]
    for line in block["items"]:
        paragraphs.append(_paragraph(
            _run(f"{line['label']}：", bold=True) + _run(line["value"]),
        ))
    return "".join(paragraphs)


def _paragraphs_block(block: dict) -> str:
    paragraphs = [_section_title(block["title"])]
    for item in block["items"]:
        runs = _run(f"{item['lead']}：", bold=True) if item.get("lead") else ""
        paragraphs.append(_paragraph(runs + _run(item["text"])))
    return "".join(paragraphs)


def _block_xml(block: dict) -> str:
    kind = block.get("kind")
    if kind == "grid":
        return _grid_block(block)
    if kind == "education":
        return _education_block(block)
    if kind == "entries":
        return _entries_block(block)
    if kind == "labeled":
        return _labeled_block(block)
    if kind == "paragraphs":
        return _paragraphs_block(block)
    return ""


def _document_body(content: dict) -> str:
    rel_id = "rIdPhoto" if content.get("photo") else None
    return _header(content, rel_id) + "".join(
        _block_xml(block) for block in content["blocks"]
    )


def _document_xml(content: dict) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
        f"<w:body>{_document_body(content)}{_SECT_PR}</w:body>"
        "</w:document>"
    )


def _add(zf: zipfile.ZipFile, name: str, content: str | bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    payload = content.encode("utf-8") if isinstance(content, str) else content
    zf.writestr(info, payload)


def build_resume_docx(resume: dict) -> bytes:
    """把清洗后的简历字典写成 DOCX 字节流。没有数据库或文件系统副作用。"""
    content = build_resume_content(resume)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        _add(archive, "[Content_Types].xml", _CONTENT_TYPES)
        _add(archive, "_rels/.rels", _RELS)
        _add(archive, "word/document.xml", _document_xml(content))
        photo = content.get("photo")
        if photo:
            name = f"photo.{photo['ext']}"
            _add(archive, "word/_rels/document.xml.rels", _IMAGE_RELS.format(name=name))
            _add(archive, f"word/media/{name}", photo["data"])
    return buffer.getvalue()
