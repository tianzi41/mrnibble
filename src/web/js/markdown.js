/* Markdown 渲染：marked + DOMPurify 清洗 + highlight.js 高亮 + KaTeX 公式。 */
(function () {
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

  /** 用 markmap 渲染思维导图（依赖 d3 / markmap-view / markmap-lib 的加载顺序）。 */
  function mindmap(container, markdown) {
    container.innerHTML = "";
    const box = document.createElement("div");
    box.className = "markmap";
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    box.appendChild(svg);
    container.appendChild(box);
    try {
      const { Transformer, Markmap } = window.markmap;
      const transformer = new Transformer();
      const { root } = transformer.transform(normalize(markdown));
      Markmap.create(svg, { autoFit: true, spacingVertical: 8, spacingHorizontal: 90, duration: 300 }, root);
      return svg;
    } catch (e) {
      box.remove();
      container.insertAdjacentHTML("beforeend",
        `<div class="empty">思维导图渲染失败：${String(e.message || e)}</div>`);
      return null;
    }
  }

  window.MD = { render, mount, mindmap, normalize, inline };
})();
