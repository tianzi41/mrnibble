/* 课件可视化渲染：mermaid 图（P1）+ echarts 图表（P2）。
 *
 * 零构建、按序加载：本文件须在 mermaid / echarts 两个 vendor 之后、
 * lesson.js 之前被 index.html 引入。暴露 window.Viz 供 lesson.js 与测试 harness 调用。
 *
 * 约定：
 *   - 任意一步失败一律「回退要点」，绝不留空白（回退 DOM 由调用方照常渲染 bullets）。
 *   - mermaid.render 是异步的（返回 Promise）；renderMermaid 因此可能返回
 *     Promise<true|false>，调用方需对「返回值是 Promise」的情况 await。
 */
(function () {
  const gt = (k, v) => window.I18n ? window.I18n.t(k, v) : k;

  window.I18n && window.I18n.merge({ en: {
    "系列": "Series",
  } });
  "use strict";

  let _mermaidInit = false;          // mermaid.initialize 只跑一次（模块级布尔）
  let _seq = 0;                      // render id 计数器，避免同名冲突
  // 中性配色，避免与浅色主题打架
  const PALETTE = ["#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#76b7b2"];

  function fallbackHTML(host) {
    host.innerHTML = '<div class="viz-fallback">本页图示未能渲染，以下为文字要点</div>';
  }

  /** mermaid 渲染：懒初始化一次；parse 预检通过后再 render。
   *  统一 async：任何失败 → false（调用方回退）。
   *  注意：**不在本函数里清空 host**——失败时 host 保持原样，回退 DOM 统一由
   *  renderVisual 写入；否则多个异步分支的清理顺序会互相覆盖（实测踩过：
   *  parse 失败后链上后续分支晚于回退 DOM 执行，把 fallback 又清掉）。 */
  async function renderMermaid(host, code) {
    if (!host || !code || typeof code !== "string") return false;
    if (!window.mermaid) return false;
    if (!_mermaidInit) {
      window.mermaid.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        theme: "neutral",
        fontFamily: "inherit",
        // useMaxWidth:false：SVG 不再被强行拉伸到容器宽度——
        // 纵向窄图曾被等比放大到满宽，节点巨大、空间浪费严重（用户实测）。
        // 原始尺寸输出 + CSS max-width:100% 兜底超宽图，小图由样式居中。
        flowchart: { useMaxWidth: false, nodeSpacing: 30, rankSpacing: 34, padding: 8 },
        sequence: { useMaxWidth: false },
        state: { useMaxWidth: false },
        class: { useMaxWidth: false },
        themeVariables: { fontSize: "14px" },
      });
      _mermaidInit = true;
    }
    try {
      const ok = await window.mermaid.parse(code, { suppressErrors: true });
      if (!ok) return false;
      const id = "viz-" + Date.now() + "-" + (_seq++);
      const r = await window.mermaid.render(id, code);
      if (!r || !r.svg) return false;
      host.innerHTML = r.svg;
      return !!host.querySelector("svg");
    } catch (e) {
      return false;
    }
  }

  /** 极简 chart spec → echarts option（bar/line/pie）。
   *  数据非法（NaN/空/非数字）或缺 vendor → false。 */
  function renderChart(host, chart) {
    if (!host || !chart || !window.echarts) return false;
    try {
      const type = chart.type;
      if (type !== "bar" && type !== "line" && type !== "pie") return false;
      const cats = Array.isArray(chart.categories) ? chart.categories : [];
      const series = Array.isArray(chart.series) ? chart.series : [];
      if (!series.length) return false;
      // 数据合法性：series.data 中不得有 null / 空 / 非数字（NaN）
      for (let i = 0; i < series.length; i++) {
        const d = Array.isArray(series[i].data) ? series[i].data : [];
        for (let j = 0; j < d.length; j++) {
          const v = d[j];
          if (v === null || v === undefined || v === "") return false;
          if (isNaN(Number(v))) return false;   // NaN 与 "abc" 都判非法
        }
      }

      const box = document.createElement("div");
      box.className = "chart-box";
      host.innerHTML = "";
      host.appendChild(box);
      const inst = window.echarts.init(box);
      inst.setOption(buildOption(chart, type, cats, series));
      return true;
    } catch (e) {
      host.innerHTML = "";
      return false;
    }
  }

  /** 由 chart spec 生成 echarts option。 */
  function buildOption(chart, type, cats, series) {
    const unit = chart.unit || "";
    const title = chart.title || "";
    const axisLabel = { fontSize: 12, color: "#6b7280" };
    const grid = { left: 8, right: 16, top: title ? 40 : 28, bottom: 36, containLabel: true };

    if (type === "pie") {
      const names = cats.length
        ? cats
        : series[0].data.map(function (_, i) { return "项" + (i + 1); });
      const data = series[0].data.map(function (v, i) {
        return { name: names[i] != null ? String(names[i]) : "项" + (i + 1), value: Number(v) };
      });
      return {
        color: PALETTE,
        title: title ? { text: title, left: "center", textStyle: { fontSize: 14 } } : undefined,
        // pie 不重复标题：用 tooltip 携带 unit，不在 legend 里再写一遍
        tooltip: {
          trigger: "item",
          formatter: function (p) { return p.name + "：" + p.value + (unit ? " " + unit : ""); },
        },
        series: [{
          type: "pie", radius: ["38%", "66%"], center: ["50%", "52%"],
          data: data, label: { fontSize: 12, formatter: "{b}: {c}" },
        }],
      };
    }

    // bar / line：坐标轴图
    return {
      color: PALETTE,
      title: title ? { text: title, left: "center", textStyle: { fontSize: 14 } } : undefined,
      tooltip: {
        trigger: "axis",
        formatter: function (ps) {
          if (!Array.isArray(ps) || !ps.length) return "";
          let s = String(ps[0].axisValue) + "<br/>";
          ps.forEach(function (p) {
            s += p.marker + p.seriesName + "：" + p.value + (unit ? " " + unit : "") + "<br/>";
          });
          return s;
        },
      },
      legend: series.length > 1 ? { bottom: 0, textStyle: { fontSize: 12 } } : undefined,
      grid: grid,
      xAxis: {
        type: "category", data: cats,
        axisLabel: axisLabel, axisLine: { lineStyle: { color: "#cbd5e1" } },
      },
      yAxis: {
        type: "value", name: unit || undefined, nameTextStyle: { fontSize: 12 },
        axisLabel: axisLabel, splitLine: { lineStyle: { color: "#eef0f4" } },
      },
      series: series.map(function (s, i) {
        return {
          name: s.name || (gt("系列") + (i + 1)), type: type,
          data: (s.data || []).map(function (v) { return Number(v); }),
          label: { show: true, fontSize: 11, position: "top" },
          smooth: type === "line",
        };
      }),
    };
  }

  /** 可视化渲染（P1/P2）：diagram→mermaid，chart→echarts；失败一律回退要点。
   *  host 是卡片内一个空容器；slide 是归一化后的课件页对象。
   *  返回 "svg" | "canvas" | "fallback"（mermaid 异步时返回 Promise<"svg"|"fallback">）。 */
  function renderVisual(host, slide) {
    if (!host) return "fallback";
    try {
      // ① 新形态：后端已用确定性编译器把 typed IR 编成 SVG（Archify 式）。
      //    直接内联即可 —— 前端不再承担「把图形语言画出来」的责任，
      //    渲染结果与后端校验/回归测试看到的是同一份字节。
      if (slide && slide.kind === "diagram" && slide.diagram) {
        const svg = slide.diagram.svg;
        if (typeof svg === "string" && svg.indexOf("<svg") >= 0) {
          // 只接受我们自己编译器产出的 svg 根标签，避免任何非预期内容被注入
          const trimmed = svg.trim();
          if (trimmed.indexOf("<svg") === 0 && trimmed.indexOf("<script") < 0) {
            host.innerHTML = trimmed;
            if (host.querySelector("svg")) return "svg";
          }
        }
        // ② 旧形态：库里早先落下的讲义存的是 Mermaid 源码，保留老渲染路径（向后兼容）
        if (slide.diagram.code) {
          const r = renderMermaid(host, slide.diagram.code);
          if (r === true) return "svg";
          if (r && typeof r.then === "function") {
            return r.then(function (ok) { return ok ? "svg" : (fallbackHTML(host), "fallback"); });
          }
          fallbackHTML(host);
          return "fallback";
        }
      }
      if (slide && slide.kind === "chart" && slide.chart) {
        if (renderChart(host, slide.chart)) return "canvas";
        fallbackHTML(host);
        return "fallback";
      }
    } catch (e) {
      fallbackHTML(host);
      return "fallback";
    }
    fallbackHTML(host);
    return "fallback";
  }

  /** 课件特色页（P1）：对比表格 / 金句卡。纯 HTML，不用 vendor 库。
   *
   *  用 DOM API 逐节点构建（而非拼 innerHTML）——模型产出的内容一律走
   *  textContent，天然防注入。结构非法时直接返回 ""（该页退化为普通要点页）。
   *
   *  @returns "table" | "takeaway" | ""
   */
  function renderExtras(host, slide) {
    if (!host || !slide) return "";
    if (slide.kind === "table" && slide.table
        && Array.isArray(slide.table.columns) && Array.isArray(slide.table.rows)) {
      const t = slide.table;
      // 前端也兜一层结构校验：后端虽已规范化，但坏数据（行长度不齐）宁可整页
      // 跳过（bullets 仍显示）也不要渲染出一张缺列的表格。
      const need = t.columns.length;
      if (need < 2 || !t.rows.length
          || t.rows.some((r) => !Array.isArray(r) || r.length !== need)) {
        return "";
      }
      const box = document.createElement("div");
      box.className = "table-box";
      if (t.title) {
        const cap = document.createElement("div");
        cap.className = "table-title";
        cap.textContent = String(t.title);
        box.appendChild(cap);
      }
      const table = document.createElement("table");
      table.className = "viz-table";
      const thead = document.createElement("thead");
      const htr = document.createElement("tr");
      t.columns.forEach((c) => {
        const th = document.createElement("th");
        th.textContent = String(c);
        htr.appendChild(th);
      });
      thead.appendChild(htr);
      table.appendChild(thead);
      const tbody = document.createElement("tbody");
      t.rows.forEach((row) => {
        const tr = document.createElement("tr");
        (Array.isArray(row) ? row : []).forEach((cell) => {
          const td = document.createElement("td");
          td.textContent = String(cell);
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      box.appendChild(table);
      host.appendChild(box);
      return "table";
    }
    if (slide.kind === "takeaway" && slide.takeaway) {
      const box = document.createElement("div");
      box.className = "takeaway-box";
      box.textContent = String(slide.takeaway);
      host.appendChild(box);
      return "takeaway";
    }
    return "";
  }

  // 暴露给 lesson.js 与测试 harness（window.Viz 命名空间）。
  window.Viz = {
    render: renderVisual, renderMermaid: renderMermaid,
    renderChart: renderChart, renderExtras: renderExtras,
  };
})();
