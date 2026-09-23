/* 白板 / 讲义导出：SVG 排版 → PNG 下载，以及文本类导出。
 *
 * 为什么不截图 DOM：项目前端零构建、不引入 html2canvas 这类大依赖。
 * 讲义本身是结构化数据（summary / cards / keypoints / recap），直接排版成 SVG
 * 再经 canvas 转 PNG，产物清晰、可控，也不受页面滚动位置影响。
 */
(function () {
  "use strict";

  const PAD = 36;
  const GAP = 16;
  const CARD_W = 320;
  const FONT = "'Microsoft YaHei', 'PingFang SC', sans-serif";

  const COLORS = {
    bg: "#ffffff", text: "#1f2430", muted: "#6b7280", line: "#e3e6ee",
    accent: "#4f46e5", soft: "#eef0ff",
    kind: {
      concept: ["概念", "#eef2ff"], example: ["例子", "#ecfdf5"],
      formula: ["公式", "#fff7ed"], quote: ["材料原文", "#fdf4ff"], note: ["补充", "#f8fafc"],
    },
  };

  const esc = (s) => String(s || "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

  /** 去掉 Markdown 标记，得到用于排版/朗读的纯文本。 */
  function plain(md) {
    return String(md || "")
      .replace(/\[\[\s*c\s*:\s*\d+\s*\]\]/g, "")
      .replace(/\[\d+\]/g, "")
      .replace(/```[\s\S]*?```/g, "（代码块）")
      .replace(/`([^`]*)`/g, "$1")
      .replace(/^#{1,6}\s*/gm, "")
      .replace(/\*\*([^*]*)\*\*/g, "$1")
      .replace(/[*_>|]/g, "")
      .replace(/[ \t]+/g, " ")
      .trim();
  }

  /** 按可用宽度折行（CJK 记 1 个全角宽，ASCII 记 0.55）。 */
  function wrap(text, maxUnits) {
    const out = [];
    let line = "";
    let w = 0;
    for (const ch of String(text || "")) {
      const unit = ch.charCodeAt(0) > 255 ? 1 : 0.55;
      if (w + unit > maxUnits && line) {
        out.push(line);
        line = ch;
        w = unit;
      } else {
        line += ch;
        w += unit;
      }
    }
    if (line) out.push(line);
    return out.length ? out : [""];
  }

  function textBlock(x, y, lines, opts) {
    opts = opts || {};
    const size = opts.size || 13;
    const lh = opts.lh || Math.round(size * 1.55);
    const fill = opts.fill || COLORS.text;
    const weight = opts.weight || 400;
    const parts = lines.map((t, i) =>
      `<text x="${x}" y="${y + i * lh}" font-family="${FONT}" font-size="${size}"`
      + ` font-weight="${weight}" fill="${fill}">${esc(t)}</text>`);
    return { svg: parts.join(""), height: lines.length * lh };
  }

  /**
   * 把讲义数据排版为 SVG 字符串。
   * @param {object} board {summary, cards, outline, keypoints, recap}
   * @param {{width?:number, title?:string}} opts
   */
  function buildSvg(board, opts) {
    opts = opts || {};
    const width = opts.width || 900;
    const colW = Math.floor((width - PAD * 2 - GAP) / 2);
    const parts = [];
    let y = PAD;

    // 标题
    if (opts.title) {
      const t = textBlock(PAD, y + 22, wrap(opts.title, 46), { size: 22, weight: 700 });
      parts.push(t.svg);
      y += t.height + 6;
    }
    // 导览
    if (board && board.summary) {
      const lines = wrap(plain(board.summary), 58);
      const h = lines.length * 22 + 20;
      parts.push(`<rect x="${PAD}" y="${y}" width="${width - PAD * 2}" height="${h}" rx="10"`
        + ` fill="${COLORS.soft}" stroke="#dcdcff"/>`);
      parts.push(textBlock(PAD + 14, y + 26, lines, { size: 14, lh: 22 }).svg);
      y += h + GAP;
    }

    // 卡片（两列）
    const cards = (board && board.cards) || [];
    if (cards.length) {
      parts.push(textBlock(PAD, y + 14, ["讲义内容"], { size: 15, weight: 700 }).svg);
      y += 30;
      let col = 0;
      let rowY = y;
      let rowH = 0;
      cards.forEach((c) => {
        const [kindName, bg] = COLORS.kind[c.kind] || COLORS.kind.note;
        const titleLines = wrap(plain(c.title) || "要点", Math.floor(colW / 15) - 4);
        const bodyLines = wrap(plain(c.body), Math.floor(colW / 13) - 4);
        const h = 18 + titleLines.length * 21 + bodyLines.length * 20 + 22;
        const x = PAD + col * (colW + GAP);
        parts.push(`<rect x="${x}" y="${rowY}" width="${colW}" height="${h}" rx="10"`
          + ` fill="${bg}" stroke="${COLORS.line}"/>`);
        parts.push(`<rect x="${x + 12}" y="${rowY + 12}" width="${kindName.length * 13 + 12}"`
          + ` height="18" rx="9" fill="#ffffff" stroke="${COLORS.line}"/>`);
        parts.push(textBlock(x + 18, rowY + 25, [kindName], { size: 11, fill: COLORS.muted }).svg);
        let ty = rowY + 50;
        parts.push(textBlock(x + 14, ty, titleLines, { size: 14, weight: 700 }).svg);
        ty += titleLines.length * 21 + 4;
        parts.push(textBlock(x + 14, ty, bodyLines, { size: 13, fill: "#374151" }).svg);
        rowH = Math.max(rowH, h);
        col += 1;
        if (col === 2) { col = 0; rowY += rowH + GAP; rowH = 0; }
      });
      if (col === 1) rowY += rowH + GAP;
      y = rowY;
    }

    // 关键术语
    const kps = (board && board.keypoints) || [];
    if (kps.length) {
      parts.push(textBlock(PAD, y + 14, ["关键术语"], { size: 15, weight: 700 }).svg);
      y += 30;
      kps.forEach((k) => {
        const lines = wrap(plain(k.term) + "：" + plain(k.desc), 58);
        parts.push(textBlock(PAD, y + 13, lines, { size: 13 }).svg);
        y += lines.length * 20 + 6;
      });
      y += GAP;
    }

    // 本讲回顾
    if (board && board.recap) {
      const lines = wrap(plain(board.recap), 58);
      parts.push(`<rect x="${PAD}" y="${y}" width="4" height="${lines.length * 21 + 12}" fill="${COLORS.accent}"/>`);
      parts.push(textBlock(PAD + 14, y + 18, lines, { size: 13, fill: "#374151" }).svg);
      y += lines.length * 21 + 24;
    }

    const height = Math.max(y + PAD, 240);
    return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}"`
      + ` viewBox="0 0 ${width} ${height}">`
      + `<rect width="${width}" height="${height}" fill="${COLORS.bg}"/>`
      + parts.join("") + "</svg>";
  }

  /** 下载文本文件。 */
  function downloadText(text, filename, mime) {
    const blob = new Blob([text], { type: (mime || "text/markdown") + ";charset=utf-8" });
    triggerDownload(blob, filename);
  }

  function triggerDownload(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }

  /** 讲义 → PNG 下载。 */
  function downloadPng(board, filename, opts) {
    const svg = buildSvg(board, opts);
    const blob = new Blob([svg], { type: "image/svg+xml;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const img = new Image();
    img.onload = () => {
      try {
        const canvas = document.createElement("canvas");
        const scale = 2; // 2x 输出，文字更清晰
        canvas.width = img.width * scale;
        canvas.height = img.height * scale;
        const ctx = canvas.getContext("2d");
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.scale(scale, scale);
        ctx.drawImage(img, 0, 0);
        canvas.toBlob((out) => {
          if (out) triggerDownload(out, filename);
          URL.revokeObjectURL(url);
        }, "image/png");
      } catch (e) {
        URL.revokeObjectURL(url);
        throw e;
      }
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      // SVG 转图失败（极少数浏览器策略）：退化为下载 SVG，内容不丢。
      triggerDownload(new Blob([svg], { type: "image/svg+xml" }), filename.replace(/\.png$/, ".svg"));
    };
    img.src = url;
  }

  window.BoardExport = { buildSvg, downloadPng, downloadText, plain };
})();
