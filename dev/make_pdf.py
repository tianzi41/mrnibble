"""生成最小可解析的 PDF（测试夹具用，无第三方写入依赖）。

为什么需要它：材料标注（高亮/圈注）与图片题依赖**分页**材料——只有 PDF/PPTX 的
切片带 ``page_no``。自测要覆盖这条链路，就必须有一份「pypdf 能提取出文本」的 PDF。
项目已装 pypdf（读取侧），但写入侧没有依赖，因此这里手写 PDF 结构。

用法（供测试 import）::

    from make_pdf import make_text_pdf
    data = make_text_pdf(["line 1", "line 2"])
"""

from __future__ import annotations

__all__ = ["make_text_pdf"]


def _escape(text: str) -> str:
    """转义 PDF 字符串字面量中的特殊字符。"""
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def make_text_pdf(lines: list[str], *, font_size: int = 14) -> bytes:
    """生成单页 PDF，每行一段文本（Helvetica，仅 ASCII 可读）。

    Args:
        lines: 每行文本（**仅 ASCII**：标准字体不含中文字形）。
        font_size: 字号。

    Returns:
        完整 PDF 字节。
    """
    y = 780
    parts = []
    for line in lines:
        parts.append(
            f"BT /F1 {font_size} Tf 60 {y} Td ({_escape(line)}) Tj ET"
        )
        y -= font_size + 10
    stream = "\n".join(parts).encode("latin-1")

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
         b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)


if __name__ == "__main__":  # pragma: no cover - 手动验证
    import sys
    from pathlib import Path

    from pypdf import PdfReader

    data = make_text_pdf([
        "L Hopital rule applies to 0/0 and inf/inf indeterminate forms",
        "Verify the indeterminate form before applying the rule",
        "A limit describes the trend of a function near a point",
    ])
    tmp = Path(sys.argv[1] if len(sys.argv) > 1 else "make_pdf_sample.pdf")
    tmp.write_bytes(data)
    reader = PdfReader(str(tmp))
    print("pages:", len(reader.pages))
    print("text:", repr(reader.pages[0].extract_text()))
