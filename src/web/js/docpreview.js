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
  const gt = (k, v) => window.I18n ? window.I18n.t(k, v) : k;

  window.I18n && window.I18n.merge({ en: {
    "（这份材料没有可提取的文字）如果是扫描版 PDF（页面是图片、没有文字层），": "(this material has no extractable text) If it is a scanned PDF (pages are images with no text layer),",
    "这是正常的 —— 请用上面的「原页」方式查看，或直接看课程里的引用配图。": "This is normal — view it with the \"Original page\" mode above, or see the referenced figures in the course.",
    "PDF 渲染组件未加载，下面按文字显示。": "PDF renderer not loaded; showing text instead.",
    "当前缩放（100% = 按容器宽适配）": "Current zoom (100% = fit container width)",
    "适应容器宽度": "Fit width",
    "（无文字层）": "(no text layer)",
    "加载中…": "Loading…",
    "读取失败": "Read failed",
    "上一页": "Previous",
    "下一页": "Next",
    "放大": "Zoom in",
    "缩小": "Zoom out",
    "适宽": "Fit",
  } });
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

  /** 引用片段 → 搜索正则。
   *
   *  为什么不能直接 indexOf：引用片段是服务端从**切片文本**里截的，与页面上渲染出来的
   *  原文在空白 / 换行上并不一致（切片去掉过空白、材料里有换行）。所以按「去掉空白后的
   *  前 24 个字、字间允许任意空白」来搜。
   */
  function snippetRe(snippet) {
    const chars = Array.from(String(snippet || "").replace(/\s+/g, "")).slice(0, 24);
    if (chars.length < 4) return null;
    const esc = (c) => c.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return new RegExp(chars.map(esc).join("\\s*"), "i");
  }

  /** 在元素内高亮第一处命中（该元素的文本只有一个文本节点 → 直接按 index 切分是安全的）。 */
  function markFirst(el, re) {
    const txt = el.textContent || "";
    const m = re.exec(txt);
    if (!m) return false;
    const before = txt.slice(0, m.index);
    const hit = txt.slice(m.index, m.index + m[0].length);
    const after = txt.slice(m.index + m[0].length);
    el.textContent = "";
    if (before) el.appendChild(document.createTextNode(before));
    const mk = document.createElement("mark");
    mk.className = "pv-hit";
    mk.textContent = hit;
    el.appendChild(mk);
    if (after) el.appendChild(document.createTextNode(after));
    return true;
  }

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
        return await mountPdf(host, doc, opts);
      } catch (e) {
        host.innerHTML = "";
        host.appendChild(el("div", "hint",
          `原页渲染失败：${esc(e.message)}<br>下面按文字显示。`));
      }
    } else if (isPdf) {
      host.appendChild(el("div", "hint", gt("PDF 渲染组件未加载，下面按文字显示。")));
    }
    // 文本路径同样返回跳页句柄（否则调用方的「跳原文」在 md/txt 材料上会静默失效）
    return await mountText(host, doc, opts);
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
    const zoomPct = el("span", "pv-page", "100%");
    const fit = el("button", "btn small on", gt("适宽"));
    prev.title = gt("上一页"); next.title = gt("下一页");
    zoomOut.title = gt("缩小"); zoomIn.title = gt("放大"); fit.title = gt("适应容器宽度");
    zoomPct.title = gt("当前缩放（100% = 按容器宽适配）");
    bar.append(prev, label, next, zoomOut, zoomIn, zoomPct, fit);

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
      const wide = stage.clientWidth - 18;      // 减去 padding(8×2) 与边框(1×2)
      // 面板收起（宽度 0）或还没布局时不画：等它展开后会再调一次
      if (wide < 40) { label.textContent = `${page} / ${total}`; return; }
      drawing = true;
      try {
        const p = await pdf.getPage(page);
        const base = p.getViewport({ scale: 1 });
        // 缩放 1 = 页宽正好等于容器内宽（适宽）；放大后 canvas 会超出容器，
        // 由 .pv-stage 的 overflow:auto 滚动查看 —— 不再用 CSS max-width 去压它
        // （压宽度、不压高度 = 画面被压扁变形）。
        await window.PdfView.renderPage(doc.id, page, canvas, (wide / base.width) * zoom);
        label.textContent = `${page} / ${total}`;
        zoomPct.textContent = Math.round(zoom * 100) + "%";
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

    // 返回一个跳页句柄：调用方（课程页的引用角标）可以「翻到第 N 页」而**不用重建整个预览** ——
    // 重建会重新拉 PDF、重新算布局，点一下角标闪一次，体验很差。
    return {
      goTo(p) {
        const n = Math.max(1, Math.min(total, Number(p) || 1));
        if (n !== page) { page = n; draw(); }
      },
      /** 没有页码的引用：按片段在**前 40 页**里逐页搜文本（pdf.js 的 getTextContent），
       *  命中即翻到该页。搜索是渐进的，找不到就返回 false（调用方会给出提示）。 */
      async goToSnippet(snippet) {
        const re = snippetRe(snippet);
        if (!re) return false;
        const cap = Math.min(total, 40);
        for (let p = 1; p <= cap; p++) {
          let txt = "";
          try {
            const pg = await pdf.getPage(p);
            const tc = await pg.getTextContent();
            txt = (tc.items || []).map((it) => it.str || "").join("");
          } catch (e) { continue; }
          if (re.test(txt)) { page = p; await draw(); return true; }
        }
        return false;
      },
      current() { return page; },
    };
  }

  /** 非 PDF：逐页取文本，**每页一个独立块**（点引用角标才能滚到对应页）。
   *
   * 早期是一整块 <pre> 把所有页拼起来 —— 于是 md / txt 材料上「点角标看原文」根本
   * 无处可跳：没有可定位的节点，跳页代码只能静默什么都不做（用户实测「点了没反应」）。
   */
  async function mountText(host, doc, opts) {
    const total = Math.max(1, Math.min(Number(doc.page_count) || 1, TEXT_PAGE_CAP));
    const wrap = el("div", "pv-text");
    const blocks = new Map();
    for (let p = 1; p <= total; p++) {
      const blk = el("div", "pv-text-page");
      blk.dataset.page = String(p);
      const pre = el("pre", "doc-preview");
      pre.textContent = total > 1 ? `【第 ${p} 页】加载中…` : gt("加载中…");
      blk.appendChild(pre);
      blocks.set(p, blk);
      wrap.appendChild(blk);
    }
    host.appendChild(wrap);

    let any = false;
    for (let p = 1; p <= total; p++) {
      const pre = blocks.get(p).querySelector("pre");
      try {
        const r = await Api.get(`/api/documents/${doc.id}/preview?page_no=${p}`);
        const pg = (r.pages || [])[0];
        const txt = (pg && pg.text) || "";
        any = any || !!txt;
        pre.textContent = txt
          ? (total > 1 ? `【第 ${p} 页】\n${txt}` : txt)
          : (total > 1 ? `【第 ${p} 页】（这一页没有可提取的文字）` : gt("（无文字层）"));
      } catch (e) {
        pre.textContent = total > 1 ? `【第 ${p} 页】读取失败` : gt("读取失败");
      }
    }
    if (!any) {
      wrap.appendChild(el("div", "hint",
        gt("（这份材料没有可提取的文字）如果是扫描版 PDF（页面是图片、没有文字层），") +
        gt("这是正常的 —— 请用上面的「原页」方式查看，或直接看课程里的引用配图。")));
    }
    let cur = Math.max(1, opts.page || opts.startPage || 1);
    return {
      goTo(p) {
        const n = Math.max(1, Math.min(total, Number(p) || 1));
        cur = n;
        const blk = blocks.get(n);
        if (blk && blk.scrollIntoView) blk.scrollIntoView({ block: "start", behavior: "smooth" });
      },
      /** 按引用片段定位（材料大多没有页码，见 lesson.js 的说明）：找到就滚过去 + 高亮。 */
      goToSnippet(snippet) {
        const re = snippetRe(snippet);
        if (!re) return false;
        for (const [p, blk] of blocks) {
          const pre = blk.querySelector("pre");
          if (re.test(pre.textContent || "")) {
            cur = p;
            markFirst(pre, re);
            if (blk.scrollIntoView) blk.scrollIntoView({ block: "start", behavior: "smooth" });
            return true;
          }
        }
        return false;
      },
      current() { return cur; },
    };
  }

  window.DocPreview = { mount };
})();
