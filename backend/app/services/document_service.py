"""文档解析服务：用 Docling 解析 Word/Excel/PDF 等文档"""
from typing import Optional
import tempfile
import os

# 单例：DocumentConverter 初始化需加载模型（约 20s），只创建一次，避免每次请求都重新初始化
_converter = None


def _get_converter():
    """懒加载单例 DocumentConverter"""
    global _converter
    if _converter is None:
        from docling.document_converter import DocumentConverter
        _converter = DocumentConverter()
    return _converter


def parse_docx_in_order(
    file_path: str,
    image_dir: Optional[str] = None,
    image_uri_prefix: Optional[str] = None,
) -> str:
    """按文档真实结构顺序解析 .docx，返回 Markdown。

    Docling 对 Word「浮动图片」（锚定在段落上、非嵌入式）的位置处理有缺陷，
    会把全部图片集中到文档开头。此解析器用 python-docx 按正文元素顺序遍历，
    图片就地插入其锚定段落之后，预览效果与 Word/WPS 一致。
    """
    import hashlib

    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    VML_IMAGEDATA = "{urn:schemas-microsoft-com:vml}imagedata"

    doc = Document(file_path)
    lines: list = []
    image_index = 0

    def save_image(part) -> Optional[str]:
        """保存图片到 image_dir，返回 markdown 引用（无 image_dir 时返回占位文本）"""
        nonlocal image_index
        blob = part.blob
        ext = os.path.splitext(str(part.partname))[1].lower() or ".png"
        if ext not in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".tiff", ".emf", ".wmf"):
            ext = ".png"
        name = f"image_{image_index:06d}_{hashlib.sha1(blob).hexdigest()[:24]}{ext}"
        image_index += 1
        if image_dir and image_uri_prefix:
            os.makedirs(image_dir, exist_ok=True)
            with open(os.path.join(image_dir, name), "wb") as f:
                f.write(blob)
            return f"![Image]({image_uri_prefix}{name})"
        return "![Image](Image)"  # 占位（与 Docling PLACEHOLDER 模式一致）

    def heading_level(p: Paragraph) -> int:
        try:
            name = (p.style.name or "").lower()
        except Exception:
            return 0
        for i in range(1, 7):
            if name in (f"heading {i}", f"标题 {i}", f"标题{i}"):
                return i
        return 0

    def is_list_item(p: Paragraph) -> bool:
        pPr = p._p.pPr
        return pPr is not None and pPr.numPr is not None

    def emit_paragraph(p: Paragraph):
        """按 run 级顺序递归遍历段落，文本行与图片就地穿插。

        - 很多文档（尤其从网页/聊天工具粘贴的）整篇正文挤在一个 w:p 里、
          用 w:br 软换行分行、图片内嵌在 runs 中间——必须下钻到 run 级。
        - 红头文件类模板的正文在浮动文本框（wps:txbx / v:textbox）里，
          须保留其内容（嵌套 w:p 作为分行）；mc:AlternateContent 的
          mc:Fallback 是 Choice 的冗余副本，跳过以免文本/图片重复。
        """
        FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
        blocks: list = []  # ("text", str) | ("img", part)
        buf: list = []
        seen_rids = set()

        def flush():
            if buf:
                text = "".join(buf).replace("\u200b", "").strip()
                if text:
                    blocks.append(("text", text))
                buf.clear()

        def add_image(rid):
            if rid and rid not in seen_rids:
                seen_rids.add(rid)
                flush()
                try:
                    blocks.append(("img", doc.part.related_parts[rid]))
                except KeyError:
                    pass

        def walk(el):
            for child in el:
                tag = child.tag
                if tag == FALLBACK:
                    continue
                if tag == qn("w:t"):
                    buf.append(child.text or "")
                elif tag in (qn("w:br"), qn("w:cr")):
                    flush()
                elif tag == qn("w:tab"):
                    buf.append("    ")
                elif tag == qn("a:blip"):
                    add_image(child.get(qn("r:embed")))
                elif tag == VML_IMAGEDATA:
                    add_image(child.get(qn("r:id")))
                elif tag == qn("w:p") and child is not p._p:
                    flush()  # 文本框内的嵌套段落 → 独立一行
                    walk(child)
                    flush()
                    continue
                walk(child)

        walk(p._p)
        flush()

        level = heading_level(p)
        list_item = is_list_item(p)
        applied_style = False
        for kind, val in blocks:
            if kind == "img":
                ref = save_image(val)
                if ref:
                    lines.append(ref)
                continue
            if not val:
                continue
            if level and not applied_style:
                lines.append(f"{'#' * level} {val}")
                applied_style = True
            elif list_item and not applied_style:
                lines.append(f"- {val}")
                applied_style = True
            else:
                lines.append(val)

    def emit_table(tbl: Table):
        rows = []
        for row in tbl.rows:
            cells = [c.text.strip().replace("\n", "<br>").replace("|", "\\|") for c in row.cells]
            rows.append(cells)
        if not rows:
            return
        width = max(len(r) for r in rows)
        for r in rows:
            r += [""] * (width - len(r))
        lines.append("| " + " | ".join(rows[0]) + " |")
        lines.append("|" + "---|" * width)
        for r in rows[1:]:
            lines.append("| " + " | ".join(r) + " |")

    # 按正文 XML 元素顺序遍历（段落与表格混排的真实顺序）
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            emit_paragraph(Paragraph(child, doc))
        elif child.tag == qn("w:tbl"):
            emit_table(Table(child, doc))

    # 压缩 3 个以上连续空行
    out, blank = [], 0
    for line in lines:
        if line == "":
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(line)
    return "\n".join(out).strip() + "\n"


def parse_document(
    file_path: str,
    image_dir: Optional[str] = None,
    image_uri_prefix: Optional[str] = None,
) -> str:
    """解析文档，返回 Markdown 文本

    .docx 优先用按结构顺序的自定义解析器（图片位置正确），
    失败时回退 Docling；其余格式（PDF/Excel/.doc 等）走 Docling。

    Args:
        file_path: 文档路径
        image_dir: 图片保存目录（为空则不提取图片）
        image_uri_prefix: 图片 URL 前缀（如 /api/source-documents/{id}/images/）
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext in (".docx", ".docm"):
        try:
            return parse_docx_in_order(file_path, image_dir=image_dir, image_uri_prefix=image_uri_prefix)
        except Exception as e:
            print(f"[警告] docx 顺序解析失败，回退 Docling: {e}")

    from docling_core.types.doc.base import ImageRefMode

    converter = _get_converter()
    result = converter.convert(file_path)

    # 配置图片模式
    if image_dir and image_uri_prefix:
        os.makedirs(image_dir, exist_ok=True)
        markdown = result.document.export_to_markdown(
            image_mode=ImageRefMode.REFERENCED,
            image_dir=image_dir,
            image_uri_prefix=image_uri_prefix,
        )
    else:
        # 无图片目录时，用占位符（纯文本）
        markdown = result.document.export_to_markdown(
            image_mode=ImageRefMode.PLACEHOLDER,
        )
    return markdown


def parse_uploaded_file(
    file_bytes: bytes,
    filename: str,
    image_dir: Optional[str] = None,
    image_uri_prefix: Optional[str] = None,
) -> str:
    """解析上传的文件，返回 Markdown 文本"""
    suffix = os.path.splitext(filename)[1] or ".txt"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    try:
        return parse_document(tmp_path, image_dir=image_dir, image_uri_prefix=image_uri_prefix)
    finally:
        # except BaseException：安全删除 shim 可能抛 SystemExit(1)，不能让它打断上传流程
        try:
            os.unlink(tmp_path)
        except BaseException:
            pass
