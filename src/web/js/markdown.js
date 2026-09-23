/* Markdown 渲染：marked + DOMPurify 清洗 + highlight.js 高亮 + KaTeX 公式。 */
(function () {
  const gt = (k, v) => window.I18n ? window.I18n.t(k, v) : k;

  window.I18n && window.I18n.merge({ en: {
  } });
  "use strict";

  const renderer = new marked.Renderer();
  // 引用角标 [1] → 可点击按钮（在纯文本里才替换，避免破坏代码块）
  const origText = renderer.text.bind(renderer);
  renderer.text = function (token) {
    let html = origText(token);
    if (typeof html === "string" && html.indexOf("[") >= 0) {
      html = html.replace(/\[(\d{1,2})\]/g, (m, n) => `<button class="cite" data-cite="${n}">${n}</button>`);
    }
    return html;
  };

  marked.setOptions({
    renderer,
    gfm: true,
    breaks: true,
    highlight(code, lang) {
      try {
        if (lang && hljs.getLanguage(lang)) return hljs.highlight(code, { language: lang }).value;
      } catch (e) { /* 忽略高亮失败 */ }
      return hljs.highlightAuto(code).value;
    },
  });

  /** 把 [[c:N]] 统一替换成 [N]（服务端已校验过的产物不会再出现，双保险）。 */
  function normalize(text) {
    return String(text || "").replace(/\[\[\s*c\s*:\s*(\d+)\s*\]\]/g, "[$1]");
  }

  /** 渲染 Markdown 为安全 HTML。 */
  function render(md) {
    const dirty = marked.parse(normalize(md));
    return DOMPurify.sanitize(dirty, { ADD_ATTR: ["data-cite", "target"] });
  }

  /** 渲染**行内** Markdown（用于要点列表这类单行文本）：剥掉外层 <p>。
   *
   *  要点里的 `**关键术语**` 需要真的变成加粗——提示词已引导模型这么做，
   *  若用纯文本渲染会把星号原样显示出来。仍走 DOMPurify 清洗，产出不会成为注入点。
   */
  function inline(md) {
    return render(md).replace(/^\s*<p>/i, "").replace(/<\/p>\s*$/i, "").trim();
  }

  /** 用 KaTeX 渲染容器内未被 marked 处理的行内/块级公式（$...$ 与 $$...$$）。 */
  function math(el) {
    if (!window.katex || !el) return;
    const walk = (node) => {
      node.childNodes.forEach((child) => {
        if (child.nodeType === 3 && child.textContent.indexOf("$") >= 0) {
          const span = document.createElement("span");
          const parts = child.textContent.split(/(\$\$[^$]+\$\$|\$[^$\n]+\$)/g);
          if (parts.length > 1) {
            parts.forEach((p) => {
              if (/^\$\$[^$]+\$\$$/.test(p)) {
                const d = document.createElement("div");
                katex.render(p.slice(2, -2), d, { displayMode: true, throwOnError: false });
                span.appendChild(d);
              } else if (/^\$[^$\n]+\$$/.test(p)) {
                const s = document.createElement("span");
                katex.render(p.slice(1, -1), s, { displayMode: false, throwOnError: false });
                span.appendChild(s);
              } else {
                span.appendChild(document.createTextNode(p));
              }
            });
            child.replaceWith(span);
          }
        } else if (child.nodeType === 1 && !child.classList.contains("katex")) {
          walk(child);
        }
      });
    };
    walk(el);
  }

  /** 渲染并挂载到容器。 */
  function mount(el, md) {
    el.innerHTML = render(md);
    math(el);
    el.querySelectorAll("pre code").forEach((b) => { try { hljs.highlightElement(b); } catch (e) {} });
    el.querySelectorAll("a").forEach((a) => { a.target = "_blank"; a.rel = "noopener"; });
  }

  /** 活着的 markmap 实例。
   *  为什么要留着：markmap 会挂 ResizeObserver / 缩放监听，容器被切页重建（SVG 脱离文档）
   *  之后这些回调仍可能触发一次，去读已脱离文档的 SVG 尺寸 → d3 抛
   *  `NotSupportedError: SVGLength`（异步，逃出外层的 try/catch）。
   *  所以每次新建前，先把**已经不在页面上**的旧实例 destroy 掉。 */
  let mmAlive = [];

  function pruneMindmaps() {
    mmAlive = mmAlive.filter((m) => {
      const el = m && m.__zbBox;
      if (el && el.isConnected) return true;      // 还在页面上 → 留着
      try { if (m && typeof m.destroy === "function") m.destroy(); } catch (e) { /* 忽略 */ }
      return false;
    });
  }

  /** 用 markmap 渲染思维导图（依赖 d3 / markmap-view / markmap-lib 的加载顺序）。 */
  function mindmap(container, markdown) {
    pruneMindmaps();
    container.innerHTML = "";
    const box = document.createElement("div");
    box.className = "markmap";
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    box.appendChild(svg);
    container.appendChild(box);
    const draw = () => {
      try {
        const { Transformer, Markmap } = window.markmap;
        const transformer = new Transformer();
        const { root } = transformer.transform(normalize(markdown));
        // duration:0 —— 不给 markmap 留「异步过渡」这条路径：容器在过渡期间被切页
        // 重建/移除时，它的 fit 会去读已脱离文档的 SVG 尺寸 → d3 抛
        // NotSupportedError（逃出外层 try/catch，成为未捕获异常）。本项目的思维导图
        // 只是结构预览，不需要入场动画，关掉最省事。
        const inst = Markmap.create(svg,
          { autoFit: true, spacingVertical: 8, spacingHorizontal: 90, duration: 0 }, root);
        if (inst) {
          inst.__zbBox = container;   // 记容器：下次新建时据此判断「已脱离页面」并 destroy
          mmAlive.push(inst);
        }
        return svg;
      } catch (e) {
        box.remove();
        container.insertAdjacentHTML("beforeend",
          `<div class="empty">思维导图渲染失败：${String(e.message || e)}</div>`);
        return null;
      }
    };
    // ⚠️ 容器不可见（面板折叠、切页瞬间宽度为 0）时创建 markmap，autoFit 会去读 SVG 的
    // 相对长度 → d3 抛 `NotSupportedError: Failed to read the 'value' property from
    // 'SVGLength'`；而且它发生在 markmap 内部的异步 fit 里，**逃出上面的 try/catch**。
    // 所以：容器还没有尺寸就先不画，等它真的有尺寸再画（最多等 12 秒，不无限轮询）。
    if (container.clientWidth >= 40 && container.clientHeight >= 40) return draw();
    let tries = 0;
    const retry = () => {
      tries += 1;
      if (container.clientWidth >= 40 && container.clientHeight >= 40) { draw(); return; }
      if (tries < 60) setTimeout(retry, 200);
    };
    setTimeout(retry, 200);
    return null;
  }

  window.MD = { render, mount, mindmap, normalize, inline };
})();
