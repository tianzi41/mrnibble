/* 用户画像：题目定义 + 两种渲染（新手引导的一次一题问卷 / 设置页的平铺表单）。
 *
 * 选项 id 与后端 src/backend/services/profile.py 的 _VALUE_TEXT **一一对应**，
 * 增删题目或选项必须两边同步改（后端单测会盯注入文本）。
 */
(function () {
  "use strict";

  // HTML 转义（与各 views 相同的模块内定义；window.esc 并不存在，
  // 直接引用会抛 ReferenceError —— 2026-09-22 实测踩过）。
  const esc = (s) => String(s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // 条件题：when(answers) 为 false 时整题跳过（答案也一并清理，见 quiz 内逻辑）。
  const FIELDS = [
    { key: "age", q: "你处在哪个年龄段？", opts: [
      ["u18", "18 岁以下"], ["18-25", "18–25 岁"], ["26-35", "26–35 岁"],
      ["36-50", "36–50 岁"], ["50p", "50 岁以上"] ] },
    { key: "role", q: "你现在的身份是？", opts: [
      ["student", "学生"], ["worker", "职场人"], ["freelance", "自由职业"], ["other", "其他"] ] },
    { key: "stage", q: "就读阶段是？", opts: [
      ["primary", "小学"], ["junior", "初中"], ["senior", "高中"],
      ["university", "大学"], ["postgrad", "研究生"] ],
      when: (a) => a.role === "student" },
    { key: "grade", q: "具体哪个年级？", opts: [
      ["p1", "小学一年级"], ["p2", "小学二年级"], ["p3", "小学三年级"], ["p4", "小学四年级"],
      ["p5", "小学五年级"], ["p6", "小学六年级"],
      ["j1", "初一"], ["j2", "初二"], ["j3", "初三"],
      ["s1", "高一"], ["s2", "高二"], ["s3", "高三"],
      ["u1", "大一"], ["u2", "大二"], ["u3", "大三"], ["u4", "大四"], ["pg", "研究生"] ],
      when: (a) => a.role === "student"
        && ["primary", "junior", "senior"].indexOf(a.stage) >= 0 },
    { key: "purpose", q: "主要是为什么学？", opts: [
      ["exam", "应付考试"], ["cert", "考证 / 考研"], ["work", "提升工作技能"],
      ["interest", "兴趣拓展"], ["kids", "辅导孩子"] ] },
    { key: "style", q: "喜欢的讲法是？", opts: [
      ["analogy", "多举例、打比方"], ["rigor", "按公式严谨推导"],
      ["exampoints", "直接划考点"], ["quick", "快速过一遍"] ] },
    { key: "daily", q: "每天能投入多久？", opts: [
      ["d15", "15 分钟以内"], ["d30", "半小时左右"], ["d60", "1 小时左右"], ["flex", "不固定"] ] },
    { key: "fields", q: "常学哪些领域？（可多选，可跳过）", multi: true, opts: [
      ["code", "编程 / 计算机"], ["en", "英语"], ["math", "数学"], ["lit", "文史"],
      ["sci", "理化"], ["work", "职业技能"], ["art", "艺术 / 设计"], ["other", "其他"] ] },
    { key: "note", q: "还有什么想告诉 AI 的？（一句话，可跳过）", text: true },
  ];

  /** 当前可见题目（条件题按已答内容过滤）。 */
  function visible(answers) {
    return FIELDS.filter((f) => !f.when || f.when(answers || {}));
  }

  /** 从 GET /api/settings 的 profile 组读回答案对象。 */
  function fromSettings(cfg) {
    const p = (cfg && cfg.profile) || {};
    const out = {};
    FIELDS.forEach((f) => { out[f.key] = p[f.key] || ""; });
    return out;
  }

  /** 答案对象 → PUT /api/settings 的 profile 组（空值也传，允许清空）。 */
  function toPayload(answers) {
    const out = {};
    FIELDS.forEach((f) => { out[f.key] = (answers[f.key] || "").trim(); });
    return out;
  }

  /** 换身份/学段时，作废依赖它们的下游答案（避免「选了大学却留着高一」）。 */
  function cascadeClear(answers, changedKey) {
    const a = Object.assign({}, answers);
    if (changedKey === "role") { a.stage = ""; a.grade = ""; }
    if (changedKey === "stage") { a.grade = ""; }
    return a;
  }

  /**
   * 新手引导用：一次一题的问卷。
   * opts: { answers, onProgress(i, total), onDone(answers) }
   */
  function renderQuiz(host, opts) {
    let answers = Object.assign({}, opts.answers || {});
    let i = 0;

    function paint() {
      const list = visible(answers);
      if (i >= list.length) { opts.onDone(answers); return; }
      const f = list[i];
      if (opts.onProgress) opts.onProgress(i, list.length);
      const cur = answers[f.key] || "";
      const multiSel = f.multi && cur
        ? cur.split(",").filter(Boolean) : [];

      host.innerHTML = `
        <div class="quiz">
          <div class="quiz-q">${esc(f.q)}</div>
          <div class="quiz-opts${f.opts.length > 8 ? " two-col" : ""}">
            ${f.opts.map(([v, label]) => {
              const on = f.multi ? multiSel.indexOf(v) >= 0 : cur === v;
              return `<button type="button" class="opt${on ? " on" : ""}" data-v="${v}">${esc(label)}</button>`;
            }).join("")}
          </div>
          ${f.text ? `<textarea id="quiz-note" rows="2" placeholder="一句话就行，可跳过">${esc(cur)}</textarea>` : ""}
          <div class="quiz-foot">
            <span class="hint">第 ${i + 1} / ${list.length} 题</span>
            <span style="flex:1"></span>
            ${i > 0 ? '<button class="btn small" id="q-prev">上一题</button>' : ""}
            <button class="btn small" id="q-skip">跳过本题</button>
            <button class="btn small primary" id="q-next">${i === list.length - 1 ? "完成" : "下一题"}</button>
          </div>
        </div>`;

      host.querySelectorAll(".opt").forEach((b) => {
        b.onclick = () => {
          const v = b.dataset.v;
          if (f.multi) {
            const arr = multiSel.slice();
            const at = arr.indexOf(v);
            if (at >= 0) arr.splice(at, 1); else arr.push(v);
            answers[f.key] = arr.join(",");
            paint();
            return;
          }
          answers[f.key] = v;
          answers = cascadeClear(answers, f.key);
          i += 1;
          paint();   // 单选点完即进下一题（少一次点击）
        };
      });
      if (f.text) {
        const ta = host.querySelector("#quiz-note");
        ta.oninput = () => { answers[f.key] = ta.value; };
      }
      const prev = host.querySelector("#q-prev");
      if (prev) prev.onclick = () => { i -= 1; paint(); };
      host.querySelector("#q-skip").onclick = () => {
        answers[f.key] = "";
        i += 1;
        paint();
      };
      host.querySelector("#q-next").onclick = () => {
        if (f.text) answers[f.key] = (host.querySelector("#quiz-note").value || "").trim();
        i += 1;
        paint();
      };
    }
    paint();
  }

  /**
   * 设置页用：平铺表单（条件题随答案动态显隐），底部保存。
   * opts: { answers, onSave(answers), onClear() }
   */
  function renderEditor(host, opts) {
    let answers = Object.assign({}, opts.answers || {});

    function paint() {
      const list = visible(answers);
      host.innerHTML = list.map((f) => {
        const cur = answers[f.key] || "";
        const multiSel = cur ? cur.split(",").filter(Boolean) : [];
        const body = f.text
          ? `<textarea id="pf-note" rows="2" placeholder="一句话，可留空">${esc(cur)}</textarea>`
          : `<div class="quiz-opts${f.opts.length > 8 ? " two-col" : ""}">${f.opts.map(([v, label]) => {
              const on = f.multi ? multiSel.indexOf(v) >= 0 : cur === v;
              return `<button type="button" class="opt${on ? " on" : ""}" data-k="${f.key}" data-v="${v}">${esc(label)}</button>`;
            }).join("")}</div>`;
        return `<div class="pf-row"><div class="quiz-q" style="font-size:13px">${esc(f.q)}</div>${body}</div>`;
      }).join("") + `
        <div class="row" style="margin-top:10px">
          <button class="btn small" id="pf-clear">清空画像</button>
          <span style="flex:1"></span>
          <button class="btn small primary" id="pf-save">保存画像</button>
        </div>
        <p class="hint" style="margin-top:6px">这些信息会作为背景随每次 AI 生成一起提交，用于调整难度与讲法；只存在本机。</p>`;

      host.querySelectorAll(".opt").forEach((b) => {
        b.onclick = () => {
          const k = b.dataset.k, v = b.dataset.v;
          const f = FIELDS.filter((x) => x.key === k)[0];
          if (f.multi) {
            const arr = (answers[k] || "").split(",").filter(Boolean);
            const at = arr.indexOf(v);
            if (at >= 0) arr.splice(at, 1); else arr.push(v);
            answers[k] = arr.join(",");
          } else {
            answers[k] = v;
            answers = cascadeClear(answers, k);
          }
          paint();
        };
      });
      const ta = host.querySelector("#pf-note");
      if (ta) ta.oninput = () => { answers.note = ta.value; };
      host.querySelector("#pf-clear").onclick = () => {
        answers = {};
        paint();
        if (opts.onClear) opts.onClear();
      };
      host.querySelector("#pf-save").onclick = () => {
        if (opts.onSave) opts.onSave(answers);
      };
    }
    paint();
  }

  window.Profile = { FIELDS, visible, fromSettings, toPayload, renderQuiz, renderEditor };
})();
