/* 随堂练习：逐题作答（选项或文字输入）→ 提交判分 → 成绩与解析。 */
(function () {
  "use strict";

  const S = {
    lesson: null,
    questions: [],
    answers: {},      // questionId -> 值
    checked: {},      // questionId -> 单题判定结果（对错/参考答案/解析），答完即填
    index: 0,
    result: null,
    checking: false,
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
    S.checked = {};
    S.index = 0;
    S.result = null;
    S.checking = false;
  }

  /** 是否已作答：未选 / 空字符串 / 纯空白都算没答。 */
  function isAnswered(q) {
    const a = S.answers[q.id];
    if (a === undefined || a === null) return false;
    if (typeof a === "string") return a.trim().length > 0;
    return true;
  }

  /**
   * 单题即时判定：把作答发给后端换回对错、参考答案与解析，**立刻**展示。
   *
   * 判定接口不写库（作答记录仍由最终提交统一落库），所以这里可以放心
   * 反复调用而不会污染成绩与错题本。
   */
  async function checkOne(host, q) {
    if (!isAnswered(q) || S.checking) return;
    S.checking = true;
    try {
      S.checked[q.id] = await Api.post("/api/courses/lessons/" + S.lesson.id + "/check", {
        question_id: q.id,
        answer: S.answers[q.id],
      });
    } catch (e) {
      Toast("判定失败：" + e.message, true);
    } finally {
      S.checking = false;
      renderBody(host);
    }
  }

  /* ── 题目区 ─────────────────────────────── */
  function renderQuestion(host) {
    const q = S.questions[S.index];
    if (!q) return;
    const res = S.checked[q.id] || null;
    const box = el("div", "card");
    box.appendChild(el("div", "hint",
      `第 ${S.index + 1} / ${S.questions.length} 题 · ${typeName(q.type)}`
      + (res ? (res.correct ? " · 已答对" : " · 已作答") : "")));
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
    const objective = q.type === "single" || q.type === "boolean";

    if (objective) {
      (q.options || []).forEach((opt, i) => {
        const picked = S.answers[q.id] === i;
        let cls = "opt-row";
        if (picked) cls += " picked";
        if (res) {
          // 判定后锁定：正确项标绿、选错的项标红，不再允许改选（避免看到答案后改）。
          cls += " locked";
          if (res.expected_index === i) cls += " correct";
          else if (picked) cls += " wrong";
        }
        const row = el("div", cls);
        row.innerHTML = `<span class="opt-key">${String.fromCharCode(65 + i)}</span><span>${esc(opt)}</span>`;
        if (!res) {
          // 选择题点选即作答 → 立刻判定并展示解析，不用再点一次按钮。
          row.onclick = () => {
            S.answers[q.id] = i;
            renderBody(host);
            checkOne(host, q);
          };
        }
        zone.appendChild(row);
      });
      if (!res) zone.appendChild(el("div", "hint", "点选答案后会立刻显示对错与解析。"));
    } else if (q.type === "fill_in" || q.type === "hands_on") {
      // 实操题（hands_on）与填空题共用「单行输入 + 归一化匹配」的作答形式，
      // 区别只在题干上方的「🖐 实操题」徽章与操作指引文案。
      if (q.type === "hands_on") {
        zone.appendChild(el("div", "hands-on-badge", "🖐 实操题 · 请先按题干实际操作，再回填结果"));
      }
      const inp = el("input", "fill-input");
      inp.type = "text";
      inp.placeholder = q.type === "hands_on"
        ? "把你实际操作看到的结果填进来" : "填入答案（不区分大小写与标点）";
      inp.value = S.answers[q.id] || "";
      inp.disabled = !!res;
      inp.oninput = () => { S.answers[q.id] = inp.value; };
      inp.onkeydown = (e) => {
        if (e.key === "Enter") { e.preventDefault(); checkOne(host, q); }
      };
      zone.appendChild(inp);
      if (!res) {
        zone.appendChild(el("div", "hint", q.type === "hands_on"
          ? "做完后把结果填进来，点「确认作答」给你判定。"
          : "填好后点「确认作答」，会立刻显示对错与解析。"));
      }
    } else {
      const ta = el("textarea", "fill-input");
      ta.rows = 5;
      ta.placeholder = "用自己的话作答（开放题由模型按参考答案评分）";
      ta.value = S.answers[q.id] || "";
      ta.disabled = !!res;
      ta.oninput = () => { S.answers[q.id] = ta.value; };
      zone.appendChild(ta);
      if (!res) zone.appendChild(el("div", "hint", "写完后点「确认作答」，会立刻给出评分与解析。"));
    }
    box.appendChild(zone);

    // 非选择题不给「选择即判定」的时机，需要一个明确的确认动作。
    if (!objective && !res) {
      const cbtn = el("button", "btn small primary", "✅ 确认作答");
      cbtn.style.marginTop = "10px";
      cbtn.onclick = () => checkOne(host, q);
      box.appendChild(cbtn);
    }

    // 即时反馈：对错 → 参考答案 → 解析（紧跟在作答之后）
    if (res) {
      const fb = el("div", "q-feedback " + (res.correct ? "ok" : "bad"));
      fb.appendChild(el("div", "fb-head", res.correct ? "✅ 回答正确" : "❌ 回答不正确"));
      if (res.expected && !res.correct) {
        fb.appendChild(el("div", "hint", `参考答案：${esc(res.expected)}`));
      }
      const text = String(res.explanation || res.feedback || "").trim();
      if (text) {
        const md = el("div", "md fb-body");
        MD.mount(md, text);
        fb.appendChild(md);
      }
      box.appendChild(fb);
    }

    // 未作答不允许进入下一题（用户反馈：以前没选也能点「下一步」）
    const answered = isAnswered(q);
    const last = S.index === S.questions.length - 1;
    const nav = el("div", "row");
    nav.style.marginTop = "14px";
    const prev = el("button", "btn small", "上一题");
    prev.id = "q-prev";
    prev.disabled = S.index === 0;
    prev.onclick = () => { S.index--; renderBody(host); };
    const next = el("button", "btn small primary", last ? "提交并查看成绩" : "下一题");
    next.id = "q-next";
    next.disabled = !answered;
    next.title = answered ? "" : "请先作答本题";
    next.onclick = async () => {
      if (!isAnswered(q)) return Toast("请先作答本题，再进入下一题", true);
      if (!last) { S.index++; renderBody(host); return; }
      await submit(host);
    };
    nav.appendChild(prev);
    nav.appendChild(el("span", null,
      `<span class="hint">已答 ${S.questions.filter(isAnswered).length}/${S.questions.length}</span>`));
    if (!answered) nav.appendChild(el("span", "hint", "未作答时无法进入下一题"));
    nav.appendChild(next);
    box.appendChild(nav);
    host.appendChild(box);
  }

  function typeName(t) {
    return { single: "单选题", boolean: "判断题", fill_in: "填空题",
             hands_on: "实操题", open: "开放题" }[t] || t;
  }

  async function submit(host) {
    const missing = S.questions.filter((q) => !isAnswered(q));
    if (missing.length) {
      return Toast(`还有 ${missing.length} 题没作答，请全部答完再提交`, true);
    }
    const answers = S.questions.map((q) => ({ question_id: q.id, answer: S.answers[q.id] }));
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
      S.checked = {};
      S.index = 0;
      S.result = null;
      renderBody(host);
    };
    document.getElementById("p-finish").onclick = async () => {
      const btn = document.getElementById("p-finish");
      btn.disabled = true;
      try {
        await Api.put("/api/courses/lessons/" + S.lesson.id, { complete: true });
        const course = await Api.get("/api/courses/" + S.lesson.course_id);
        const all = [];
        (course.units || []).forEach((u) => (u.lessons || []).forEach((l) => all.push(l)));
        // 最后一讲完成 = 整门课结课：回课堂页看「🏆 已学完」横幅，
        // 而不是甩回课程列表 + 一句就消失的提示（用户实测：以为没学完）。
        if (all.length && all.every((l) => l.status === "done")) {
          Toast("🏆 恭喜，这门课学完了！");
          location.hash = "#/lessons/" + S.lesson.id;
          return;
        }
        location.hash = "#/courses";
      } catch (e) { Toast(e.message, true); btn.disabled = false; }
    };
    document.getElementById("p-next").onclick = async () => {
      // 跳到下一节未完成讲次；全部完成则回课堂页看结课横幅。
      const course = await Api.get("/api/courses/" + S.lesson.course_id);
      const all = [];
      (course.units || []).forEach((u) => (u.lessons || []).forEach((l) => all.push(l)));
      const nextLesson = all.find((l) => l.status !== "done");
      if (nextLesson) {
        location.hash = "#/lessons/" + nextLesson.id;
        return;
      }
      Toast("🏆 恭喜，这门课学完了！");
      location.hash = "#/lessons/" + S.lesson.id;
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
        <div class="hint">讲完这一讲会自动生成随堂测验；也可以回到课堂，在右侧点「📝 去测验」生成。</div>
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
