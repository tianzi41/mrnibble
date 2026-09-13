/* 随堂练习：逐题作答（选项或文字输入）→ 提交判分 → 成绩与解析。 */
(function () {
  "use strict";

  const S = {
    lesson: null,
    questions: [],
    answers: {},      // questionId -> 值
    index: 0,
    result: null,
  };

  const el = (tag, cls, html) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html !== undefined) n.innerHTML = html;
    return n;
  };
  const esc = (s) => String(s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  async function load(lessonId) {
    S.lesson = await Api.get("/api/courses/lessons/" + lessonId);
    const d = await Api.get("/api/courses/lessons/" + lessonId + "/practice");
    S.questions = d.items || [];
    S.answers = {};
    S.index = 0;
    S.result = null;
  }

  /* ── 题目区 ─────────────────────────────── */
  function renderQuestion(host) {
    const q = S.questions[S.index];
    if (!q) return;
    const box = el("div", "card");
    box.appendChild(el("div", "hint",
      `第 ${S.index + 1} / ${S.questions.length} 题 · ${typeName(q.type)}`));
    const stem = el("div", "stem md");
    MD.mount(stem, q.stem);
    box.appendChild(stem);

    // 图片题：用内置 pdf.js 渲染材料对应页（服务端出图需 PyMuPDF/AGPL，默认不启用）
    if (q.image) {
      const fig = el("div", "q-figure");
      const canvas = document.createElement("canvas");
      fig.appendChild(canvas);
      const cap = el("div", "hint",
        `《${esc(q.image.document_title || "材料")}》第 ${q.image.page_no} 页`);
      fig.appendChild(cap);
      box.appendChild(fig);
      if (!window.PdfView) {
        fig.innerHTML = '<div class="hint">pdf.js 未加载，无法显示材料页。</div>';
      } else {
        // 组件是 ES module，可能仍在加载：等它就绪再渲染。
        window.PdfView.ready().then((ok) => {
          if (!ok) {
            fig.innerHTML = '<div class="hint">pdf.js 加载失败，无法显示材料页。</div>';
            return null;
          }
          return window.PdfView.renderPage(q.image.document_id, q.image.page_no, canvas, 1.1);
        }).catch((e) => {
          fig.innerHTML = `<div class="hint">材料页渲染失败：${esc(e.message)}</div>`;
        });
      }
    }

    const zone = el("div");

    if (q.type === "single" || q.type === "boolean") {
      (q.options || []).forEach((opt, i) => {
        const row = el("div", "opt-row" + (S.answers[q.id] === i ? " picked" : ""));
        row.innerHTML = `<span class="opt-key">${String.fromCharCode(65 + i)}</span><span>${esc(opt)}</span>`;
        row.onclick = () => { S.answers[q.id] = i; renderBody(host); };
        zone.appendChild(row);
      });
    } else if (q.type === "fill_in") {
      const inp = el("input", "fill-input");
      inp.type = "text";
      inp.placeholder = "填入答案（不区分大小写与标点）";
      inp.value = S.answers[q.id] || "";
      inp.oninput = () => { S.answers[q.id] = inp.value; };
      zone.appendChild(inp);
      zone.appendChild(el("div", "hint", "用简短词语作答即可，例如一个术语或数值。"));
    } else {
      const ta = el("textarea", "fill-input");
      ta.rows = 5;
      ta.placeholder = "用自己的话作答（开放题由模型按参考答案评分）";
      ta.value = S.answers[q.id] || "";
      ta.oninput = () => { S.answers[q.id] = ta.value; };
      zone.appendChild(ta);
    }
    box.appendChild(zone);

    const nav = el("div", "row");
    nav.style.marginTop = "14px";
    const prev = el("button", "btn small", "上一题");
    prev.disabled = S.index === 0;
    prev.onclick = () => { S.index--; renderBody(host); };
    const next = el("button", "btn small", S.index === S.questions.length - 1 ? "提交并判分" : "下一题");
    next.className = "btn small primary";
    next.onclick = async () => {
      if (S.index < S.questions.length - 1) { S.index++; renderBody(host); return; }
      await submit(host);
    };
    nav.appendChild(prev);
    nav.appendChild(el("span", null, `<span class="hint">已答 ${Object.keys(S.answers).length}/${S.questions.length}</span>`));
    nav.appendChild(next);
    box.appendChild(nav);
    host.appendChild(box);
  }

  function typeName(t) {
    return { single: "单选题", boolean: "判断题", fill_in: "填空题", open: "开放题" }[t] || t;
  }

  async function submit(host) {
    const answers = S.questions
      .filter((q) => S.answers[q.id] !== undefined && S.answers[q.id] !== "")
      .map((q) => ({ question_id: q.id, answer: S.answers[q.id] }));
    if (!answers.length) return Toast("至少回答一题再提交", true);
    try {
      S.result = await Api.post("/api/courses/lessons/" + S.lesson.id + "/grade", { answers });
      renderBody(host);
    } catch (e) { Toast(e.message, true); }
  }

  /* ── 成绩区 ─────────────────────────────── */
  function renderResult(host) {
    const r = S.result;
    const box = el("div", "card");
    box.innerHTML = `
      <div class="row" style="justify-content:space-between">
        <b style="font-size:16px">练习结果</b>
        <span class="pill ${r.percent >= 60 ? "ok" : "warn"}">${r.percent}%</span>
      </div>
      <div class="hint">答对 ${r.correct_count}/${r.total} 题 · 得分 ${r.score}${
        r.attempt_no > 1 ? ` · 第 ${r.attempt_no} 次作答` : ""}</div>
      <div class="row" style="margin-top:12px">
        <button class="btn small" id="p-back">返回课堂</button>
        <button class="btn small" id="p-retry">再做一次</button>
        <button class="btn small" id="p-finish">完成本讲</button>
        <button class="btn small primary" id="p-next">继续下一节</button>
      </div>`;
    host.appendChild(box);

    r.results.forEach((x) => {
      const item = el("div", "card");
      const tag = x.correct ? '<span class="pill ok">正确</span>'
        : '<span class="pill bad">不正确</span>';
      const stem = el("div", "stem md");
      MD.mount(stem, x.stem);
      item.appendChild(el("div", "row", `${tag}<span class="hint">第 ${x.ordinal} 题 · ${typeName(x.type)}</span>`));
      item.appendChild(stem);
      item.appendChild(el("div", "hint", `参考答案：${esc(x.expected || "—")}`));
      if (x.feedback) {
        const fb = el("div", "md feedback");
        MD.mount(fb, x.feedback);
        item.appendChild(fb);
      }
      host.appendChild(item);
    });

    document.getElementById("p-back").onclick = () => { location.hash = "#/lessons/" + S.lesson.id; };
    // 再做一次：清空作答与结果，用同一套题重做（服务端记录为下一次作答）。
    document.getElementById("p-retry").onclick = () => {
      S.answers = {};
      S.index = 0;
      S.result = null;
      renderBody(host);
    };
    document.getElementById("p-finish").onclick = async () => {
      const btn = document.getElementById("p-finish");
      btn.disabled = true;
      try {
        await Api.put("/api/courses/lessons/" + S.lesson.id, { complete: true });
        location.hash = "#/courses";
      } catch (e) { Toast(e.message, true); btn.disabled = false; }
    };
    document.getElementById("p-next").onclick = async () => {
      // 跳到下一节未完成讲次；全部完成则回课程页。
      const course = await Api.get("/api/courses/" + S.lesson.course_id);
      const all = [];
      (course.units || []).forEach((u) => (u.lessons || []).forEach((l) => all.push(l)));
      const nextLesson = all.find((l) => l.status !== "done");
      location.hash = nextLesson ? "#/lessons/" + nextLesson.id : "#/courses";
    };
  }

  function renderBody(host) {
    host.innerHTML = "";
    if (S.result) { renderResult(host); return; }
    renderQuestion(host);
  }

  /* ── 装配 ─────────────────────────────── */
  async function render(host, lessonId) {
    await load(lessonId);
    if (!S.questions.length) {
      host.innerHTML = `<div class="page"><div class="card"><b>这一节还没有题目</b>
        <div class="hint">回到课堂点「做随堂练习」即可生成。</div>
        <div class="row" style="margin-top:12px"><button class="btn small" id="p-back2">返回课堂</button></div></div></div>`;
      document.getElementById("p-back2").onclick = () => { location.hash = "#/lessons/" + lessonId; };
      return;
    }
    host.innerHTML = `<div class="page" style="max-width:820px;margin:0 auto;width:100%">
      <div class="row" style="justify-content:space-between">
        <b style="font-size:16px">随堂练习</b>
        <span class="hint">${esc(S.lesson.title)}</span>
      </div>
      <div id="p-body"></div></div>`;
    renderBody(document.getElementById("p-body"));
  }

  window.Views = window.Views || {};
  window.Views.practice = { render };
})();
