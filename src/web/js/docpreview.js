/* 材料原件预览（可复用组件）——工作台右栏、课程页「原件栏」共用同一份。
 *
 * 为什么要单独抽出来：PDF 的适配（按容器宽自适应、翻页、缩放）不便宜，
 * 工作台和课程页都要用，各写一遍必然走形。
 *
 * 两条渲染路径：
 *   ① PDF 且 pdf.js 可用 → 用 pdf.js 渲染**原页图像**（这才是用户说的「预览 PDF」）。
 *      数据来自 `GET /api/courses/documents/{id}/raw`（`CourseMediaService.raw_file`，
 *      只按 document_id 取原始文件，不要求文档被某门课引用 —— 所以工作台也能用）。
 *   ② 其他格式（md / txt / docx / html）→ 按页取**文本**。
 *
 * ⚠️ 踩过的坑：`GET /api/documents/{id}/preview` 是给「引用跳转定位」用的，
 *    不带 page_no 时**只返回第 1 页**（`target_page = page_no or 1`）。
 *    曾经拿它当「整篇预览」用 → PDF 只出第 1 页、而 PDF 首页常是无文字层的封面
 *    → 页面显示「没有可显示的文本」，看起来就是「预览失败」。
 */
(function () {
  "use strict";

  const el = (tag, cls, html) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html !== undefined) n.innerHTML = html;
    return n;
  };
  const esc = (s) => String(s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /** 文本路径一次最多拉多少页（防极端文档把界面拖死）。 */
  const TEXT_PAGE_CAP = 60;

  /** 把某份材料的预览挂到 host（会清空 host）。
   *
   * @param {HTMLElement} host 容器
   * @param {{id:string,title?:string,fmt?:string,page_count?:number}} doc
   * @param {{page?:number, startPage?:number}} [opts]
   * @returns {Promise<void>}
   */
  async function mount(host, doc, opts) {
    opts = opts || {};
    host.innerHTML = "";
    const isPdf = String(doc.fmt || "").toLowerCase() === "pdf";
    // ⚠️ 必须用 ready() 而不是 available()：pdf.min.mjs 是**动态 import**（ESM 延迟执行），
    // 页面/视图刚渲染时 window.PdfJS 往往还没挂上。只看 available() 会把「还没加载完」
    // 误判成「不支持」→ PDF 退回文本。症状是「第一次点预览是文字、刷新一下又能看图」。
    const pdfOk = isPdf && !!(window.PdfView && await window.PdfView.ready(8000));

    if (pdfOk) {
      try {
        await mountPdf(host, doc, opts);
        return;
      } catch (e) {
        host.innerHTML = "";
        host.appendChild(el("div", "hint",
          `原页渲染失败：${esc(e.message)}<br>下面按文字显示。`));
      }
    } else if (isPdf) {
      host.appendChild(el("div", "hint", "PDF 渲染组件未加载，下面按文字显示。"));
    }
    await mountText(host, doc, opts);
  }

  /** PDF：原页图像 + 翻页 + 缩放（1 = 适应容器宽度）。 */
  async function mountPdf(host, doc, opts) {
    const wrap = el("div", "pv");
    const bar = el("div", "pv-bar");
    const prev = el("button", "btn small", "‹");
    const next = el("button", "btn small", "›");
    const label = el("span", "pv-page", "…");
    const zoomOut = el("button", "btn small", "−");
    const zoomIn = el("button", "btn small", "＋");
    const fit = el("button", "btn small on", "适宽");
    prev.title = "上一页"; next.title = "下一页";
    zoomOut.title = "缩小"; zoomIn.title = "放大"; fit.title = "适应容器宽度";
    bar.append(prev, label, next, zoomOut, zoomIn, fit);

    const stage = el("div", "pv-stage");
    const canvas = document.createElement("canvas");
    stage.appendChild(canvas);
    wrap.append(bar, stage);
    host.appendChild(wrap);

    let page = Math.max(1, opts.page || opts.startPage || 1);
    let total = 0;
    let zoom = 1;              // 相对「适应宽度」的倍数
    let drawing = false;

    const pdf = await window.PdfView.load(doc.id);
    total = pdf.numPages;
    page = Math.min(page, total);

    async function draw() {
      if (drawing) return;
      const wide = stage.clientWidth - 16;      // 减去 padding
      // 面板收起（宽度 0）或还没布局时不画：等它展开后会再调一次
      if (wide < 40) { label.textContent = `${page} / ${total}`; return; }
      drawing = true;
      try {
        const p = await pdf.getPage(page);
        const base = p.getViewport({ scale: 1 });
        await window.PdfView.renderPage(doc.id, page, canvas, (wide / base.width) * zoom);
        label.textContent = `${page} / ${total}`;
        prev.disabled = page <= 1;
        next.disabled = page >= total;
      } finally {
        drawing = false;
      }
    }

    prev.onclick = () => { if (page > 1) { page--; draw(); } };
    next.onclick = () => { if (page < total) { page++; draw(); } };
    zoomIn.onclick = () => {
      if (zoom >= 4) return;
      zoom = Math.min(4, zoom + 0.25); fit.classList.remove("on"); draw();
    };
    zoomOut.onclick = () => {
      if (zoom <= 0.5) return;
      zoom = Math.max(0.5, zoom - 0.25); fit.classList.remove("on"); draw();
    };
    fit.onclick = () => { zoom = 1; fit.classList.add("on"); draw(); };

    await draw();

    // 面板宽度会变（右栏收起/展开、窗口缩放）→ 重新按宽适配。
    // 不用 ResizeObserver 兜底的话，展开后画面会一直停在收起时的小尺寸。
    if (window.ResizeObserver) {
      const ro = new ResizeObserver(() => { if (zoom === 1) draw(); });
      ro.observe(stage);
    }
  }

  /** 非 PDF：逐页取文本拼起来（边拉边显示，不用等全部完成）。 */
  async function mountText(host, doc, opts) {
    const total = Math.max(1, Math.min(Number(doc.page_count) || 1, TEXT_PAGE_CAP));
    const pre = el("pre", "doc-preview");
    pre.textContent = "加载中…";
    host.appendChild(pre);

    const parts = [];
    for (let p = 1; p <= total; p++) {
      try {
        const r = await Api.get(`/api/documents/${doc.id}/preview?page_no=${p}`);
        const pg = (r.pages || [])[0];
        if (pg && pg.text) parts.push(total > 1 ? `【第 ${pg.page_no} 页】\n${pg.text}` : pg.text);
      } catch (e) { /* 单页失败不阻断其余页 */ }
      if (parts.length) pre.textContent = parts.join("\n\n");
    }
    if (!parts.length) {
      pre.textContent = "（这份材料没有可提取的文字）\n\n" +
        "如果是扫描版 PDF（页面是图片、没有文字层），这是正常的 —— " +
        "请用上方的「原页」方式查看，或直接看课程里的引用配图。";
    }
  }

  window.DocPreview = { mount };
})();
