/* pdf.js 封装：材料原页渲染（材料标注页 + 图片题共用）。
 *
 * 为什么用前端渲染而不是服务端出图：pdf.js 已随包内置，而服务端渲染需要
 * PyMuPDF（AGPL 许可），本项目刻意只用宽松许可依赖。前端渲染同时天然支持缩放。
 */
(function () {
  const gt = (k, v) => window.I18n ? window.I18n.t(k, v) : k;

  window.I18n && window.I18n.merge({ en: {
    "pdf.js 未加载": "pdf.js not loaded",
  } });
  "use strict";

  // docId -> Promise<PDFDocumentProxy>（同一份材料只加载一次）
  const cache = new Map();

  function available() {
    return !!window.PdfJS;
  }

  // pdf.min.mjs 是 ES module（浏览器默认延迟执行），可能在视图首次渲染时
  // 还没执行完。这里提供一次性等待，避免把「组件还没加载完」误报成「渲染失败」。
  let readyPromise = null;

  function ready(timeoutMs) {
    if (available()) return Promise.resolve(true);
    if (!readyPromise) {
      readyPromise = new Promise((resolve) => {
        const limit = timeoutMs || 8000;
        const t0 = Date.now();
        const timer = setInterval(() => {
          if (available()) {
            clearInterval(timer);
            resolve(true);
          } else if (Date.now() - t0 > limit) {
            clearInterval(timer);
            resolve(false);
          }
        }, 60);
      });
    }
    return readyPromise;
  }

  /** 加载材料原始 PDF（同一 docId 复用）。 */
  function load(docId) {
    if (!available()) return Promise.reject(new Error(gt("pdf.js 未加载")));
    if (cache.has(docId)) return cache.get(docId);
    const url = "/api/courses/documents/" + encodeURIComponent(docId) + "/raw";
    const task = window.PdfJS.getDocument({ url, withCredentials: false });
    const p = task.promise.catch((e) => {
      cache.delete(docId);   // 失败不缓存，允许重试
      throw e;
    });
    cache.set(docId, p);
    return p;
  }

  /**
   * 把某页渲染到 canvas。
   * @param {string} docId 材料 id
   * @param {number} pageNo 页码（1 起）
   * @param {HTMLCanvasElement} canvas 目标画布
   * @param {number} scale 缩放（1 = 100%）
   * @returns {Promise<{width:number,height:number,pageCount:number}>} 逻辑尺寸（CSS 像素）
   */
  async function renderPage(docId, pageNo, canvas, scale) {
    const pdf = await load(docId);
    const page = await pdf.getPage(pageNo);
    const dpr = window.devicePixelRatio || 1;
    const viewport = page.getViewport({ scale: scale || 1 });
    canvas.width = Math.floor(viewport.width * dpr);
    canvas.height = Math.floor(viewport.height * dpr);
    canvas.style.width = viewport.width + "px";
    canvas.style.height = viewport.height + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    await page.render({ canvasContext: ctx, viewport }).promise;
    return { width: viewport.width, height: viewport.height, pageCount: pdf.numPages };
  }

  /** 读取材料页数（未加载时返回 0）。 */
  async function pageCount(docId) {
    try {
      const pdf = await load(docId);
      return pdf.numPages;
    } catch (e) {
      return 0;
    }
  }

  window.PdfView = { available, ready, load, renderPage, pageCount };
})();
