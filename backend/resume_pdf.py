"""简历 PDF 导出：把同一份简历排成 A4 页面，再用本机浏览器的打印引擎落成 PDF。

不引入 PDF 库的原因
--------------------
项目环境里没有 reportlab / weasyprint，而中文字体嵌入与分页正是它们最重的部分。
Chrome / Edge 自带 ``--print-to-pdf``，是零依赖、字形完整、分页可靠的方案；
找不到浏览器时接口返回明确的 503，让用户改导出 Word，而不是给出坏文件。

版式与 ``resume_docx.py`` 共用一份实测参数，差异只在渲染手段：
  · 微软雅黑；姓名 20pt；小节标题 12pt 加粗、通栏细线；
  · 正文 10pt、行高 1.56、段间距 0；
  · 页边距 上 20 / 下 14 / 左右 13.7 mm；
  · 联系方式自然换行，求职意向按实测列位固定四列；
  · 条目行“时间—单位—角色”三段式；
  · 不插强制分页，内容多了由浏览器自然续页。
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
from html import escape
from pathlib import Path

from backend.resume_content import build_resume_content

__all__ = ["ResumePdfUnavailable", "build_resume_pdf", "build_resume_pdf_html", "html_to_pdf"]

_BROWSER_ENV = "RESUME_PDF_BROWSER"
_RENDER_TIMEOUT = 45

_STYLE = """
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:#fff;color:#000}
body{font-family:"Microsoft YaHei","微软雅黑","PingFang SC","Noto Sans SC",sans-serif;font-size:10pt;line-height:1.56;print-color-adjust:exact;-webkit-print-color-adjust:exact}
p{margin:0}
.head{display:flex;align-items:flex-start;justify-content:space-between;gap:6mm}
.head__main{flex:1 1 auto;min-width:0}
.name{margin:0 0 18pt;font-size:20pt;font-weight:700;line-height:1.2;letter-spacing:1.5pt}
.head__photo{flex:0 0 auto;margin-left:6mm}
.head__photo img{display:block;width:auto;height:auto;max-width:32mm;max-height:38mm;border:0.5pt solid #d9d9d9}
.flow{display:flex;flex-wrap:wrap;gap:1pt 6.2mm;margin-bottom:1pt}
.flow--intent{gap:0;margin-bottom:3pt}
.flow--intent .flow__item{flex:0 0 43.7mm}
.flow--intent .flow__item:last-child{flex:1 1 auto}
.flow__item{white-space:nowrap}
.sec{margin-top:13.5pt}
.head+.sec{margin-top:18pt}
.sec>h2{margin:0 0 11pt;padding-bottom:3.5pt;border-bottom:0.75pt solid #000;font-size:12pt;line-height:1.2;font-weight:700;break-after:avoid;page-break-after:avoid}
.headline{display:flex;align-items:flex-start;margin:8pt 0 3pt;font-weight:700;line-height:1.25;break-after:avoid;page-break-after:avoid}
.headline__time{flex:0 0 51.7mm;white-space:nowrap}
.headline__title{flex:0 0 85.3mm;overflow-wrap:anywhere}
.headline__role{flex:1 1 auto;min-width:0;overflow-wrap:anywhere}
.headline--edu .headline__time{flex-basis:106.2mm}
.headline--edu .headline__title{flex:1 1 auto}
@page{size:A4;margin:20mm 13.7mm 14mm 13.7mm}
"""


class ResumePdfUnavailable(RuntimeError):
    """本机找不到可用的浏览器，或浏览器没能产出文件。"""


def _text(value: object) -> str:
    return str(value or "").strip()


def _escape(value: object) -> str:
    return escape(_text(value))


def _contact_row(cells: list[dict]) -> str:
    items = "".join(
        f'<span class="flow__item">{_escape(cell.get("label"))}：{_escape(cell.get("value"))}</span>'
        for cell in cells
        if _text(cell.get("value"))
    )
    return f'<div class="flow">{items}</div>' if items else ""


def _intent_row(cells: list[dict]) -> str:
    items = "".join(
        f'<span class="flow__item">{_escape(cell.get("label"))}：{_escape(cell.get("value"))}</span>'
        for cell in cells
        if _text(cell.get("value"))
    )
    return f'<div class="flow flow--intent">{items}</div>' if items else ""


def _section(title: str, body: str) -> str:
    if not body:
        return ""
    return f'<section class="sec"><h2>{_escape(title)}</h2>{body}</section>'


def _headline(time: str, middle: str, right: str, css_class: str) -> str:
    if not any(_text(value) for value in (time, middle, right)):
        return ""
    return (
        f'<div class="headline {css_class}">'
        f'<span class="headline__time">{_escape(time)}</span>'
        f'<span class="headline__title">{_escape(middle)}</span>'
        f'<span class="headline__role">{_escape(right)}</span>'
        "</div>"
    )


def _grid_block(block: dict) -> str:
    rows = "".join(_intent_row(row) for row in block["rows"])
    return _section(block["title"], rows)


def _education_block(block: dict) -> str:
    items = []
    for index, item in enumerate(block["items"]):
        head = _headline(
            item.get("time", ""),
            item.get("degree", ""),
            "",
            "headline--edu headline--first" if index == 0 else "headline--edu",
        )
        lines = "".join(
            f'<p>{_escape(line.get("label"))}：{_escape(line.get("value"))}</p>'
            for line in item["lines"]
        )
        items.append(head + lines)
    return _section(block["title"], "".join(items))


def _entries_block(block: dict) -> str:
    numbered = block.get("numbered", True)
    items = []
    for index, item in enumerate(block["items"]):
        head = _headline(
            item.get("time", ""),
            item.get("title", ""),
            item.get("role", ""),
            "headline--first" if index == 0 else "",
        )
        lines = []
        for number, bullet in enumerate(item["bullets"], start=1):
            prefix = f"{number}. " if numbered else ""
            lines.append(f"<p>{prefix}{_escape(bullet)}</p>")
        items.append(head + "".join(lines))
    return _section(block["title"], "".join(items))


def _labeled_block(block: dict) -> str:
    lines = "".join(
        f'<p><strong>{_escape(item.get("label"))}：</strong>{_escape(item.get("value"))}</p>'
        for item in block["items"]
    )
    return _section(block["title"], lines)


def _paragraphs_block(block: dict) -> str:
    lines = []
    for item in block["items"]:
        lead = f'<strong>{_escape(item.get("lead"))}：</strong>' if _text(item.get("lead")) else ""
        lines.append(f"<p>{lead}{_escape(item.get('text'))}</p>")
    return _section(block["title"], "".join(lines))


def _block_html(block: dict) -> str:
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


def _header(content: dict) -> str:
    rows = "".join(_contact_row(row) for row in content["contacts"])
    photo = content.get("photo")
    photo_html = ""
    if photo:
        payload = base64.b64encode(photo["data"]).decode("ascii")
        photo_html = (
            f'<div class="head__photo">'
            f'<img src="data:{photo["mime"]};base64,{payload}" alt="" /></div>'
        )
    return (
        '<header class="head"><div class="head__main">'
        f'<div class="name">{_escape(content["name"])}</div>{rows}'
        f"</div>{photo_html}</header>"
    )


def build_resume_pdf_html(resume: dict) -> str:
    """把 ``ResumeData.model_dump()`` 排成连续 A4 HTML；只输出真实填写的内容。"""
    content = build_resume_content(resume)
    body = _header(content) + "".join(
        _block_html(block) for block in content["blocks"]
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>{_escape(content["name"])} · 简历</title>
<style>{_STYLE}</style>
</head>
<body>
{body}
</body>
</html>"""


def _find_browser() -> str | None:
    override = os.environ.get(_BROWSER_ENV, "").strip()
    if override:
        candidate = Path(override)
        return str(candidate) if candidate.is_file() else None

    for command in ("chrome", "google-chrome", "chromium", "chromium-browser", "msedge", "microsoft-edge"):
        found = shutil.which(command)
        if found:
            return found

    roots = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    relpaths = [
        r"Google\Chrome\Application\chrome.exe",
        r"Microsoft\Edge\Application\msedge.exe",
    ]
    for root in roots:
        if not root:
            continue
        for relpath in relpaths:
            candidate = Path(root) / relpath
            if candidate.is_file():
                return str(candidate)
    return None


def html_to_pdf(html: str) -> bytes:
    browser = _find_browser()
    if not browser:
        raise ResumePdfUnavailable(
            "没有找到 Chrome 或 Edge，暂时无法生成 PDF；可以先导出 Word，"
            "或设置环境变量 RESUME_PDF_BROWSER 指向浏览器程序。"
        )

    with tempfile.TemporaryDirectory(prefix="resume-pdf-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        html_path = root / "resume.html"
        pdf_path = root / "resume.pdf"
        profile_path = root / "profile"
        profile_path.mkdir()
        html_path.write_text(html, encoding="utf-8")

        command = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-sync",
            "--disable-background-networking",
            "--no-pdf-header-footer",
            f"--user-data-dir={profile_path}",
            f"--print-to-pdf={pdf_path}",
            html_path.as_uri(),
        ]
        try:
            subprocess.run(command, capture_output=True, timeout=_RENDER_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ResumePdfUnavailable("浏览器生成 PDF 超时或无法启动，请重试一次。") from exc

        if not pdf_path.is_file():
            raise ResumePdfUnavailable("浏览器没有生成 PDF，请改用导出 Word。")

        data = pdf_path.read_bytes()
        if not data.startswith(b"%PDF"):
            raise ResumePdfUnavailable("浏览器输出的文件不是有效 PDF，请改用导出 Word。")
        return data


def build_resume_pdf(resume: dict) -> bytes:
    return html_to_pdf(build_resume_pdf_html(resume))
