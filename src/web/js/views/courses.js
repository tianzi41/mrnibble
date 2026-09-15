/* 课程：列表 + 创建向导（目标/基础/体量 → 大纲生成 → 结构确认）。 */
(function () {
  "use strict";

  const S = {
    courses: [],
    documents: [],
    courseId: null,
    course: null,       // 当前查看的课程详情
    creating: false,
    mindmap: null,      // 结构预览的 markmap 实例
  };

  const LEVELS = [["beginner", "零基础"], ["intermediate", "有基础"], ["advanced", "进阶"]];
  const DEPTHS = [["brief", "概览（快速过一遍）"], ["standard", "标准"], ["detailed", "深入（含推导与易错点）"]];

  const el = (tag, cls, html) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html !== undefined) n.innerHTML = html;
    return n;
  };
  const esc = (s) => String(s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /* ── 数据 ─────────────────────────────── */
  async function loadCourses() {
    const d = await Api.get("/api/courses");
    S.courses = d.items || [];
  }
  async function loadDocuments() {
    try {
      const d = await Api.get("/api/documents?page=1&page_size=200");
      S.documents = (d.items || []).filter((x) => x.status === "ready");
    } catch (e) { S.documents = []; }
  }
  async function loadCourse(id) {
    S.courseId = id;
    S.course = await Api.get("/api/courses/" + id);
  }

  /* ── 左侧课程列表 ─────────────────────── */
  function renderList() {
    const box = document.getElementById("course-list");
    box.innerHTML = "";
    box.appendChild(el("div", "panel-head",
      `<span>课程（${S.courses.length}）</span><button class="btn small primary" id="btn-new">＋ 新建课程</button>`));
    const body = el("div", "panel-body");
    if (!S.courses.length) {
      body.appendChild(el("div", "empty", "还没有课程。<br>点「＋ 新建课程」，选材料、写下学习目标即可生成。"));
    }
    S.courses.forEach((c) => {
      const p = c.progress || {};
      const item = el("div", "item" + (c.id === S.courseId ? " active" : ""));
      item.appendChild(el("span", "t", esc(c.title)));
      item.appendChild(el("small", null, `${p.done_lessons || 0}/${p.total_lessons || 0}`));
      const del = el("button", "x", "✕");
      del.title = "删除课程";
      del.onclick = async (ev) => {
        ev.stopPropagation();
        if (!confirm(`删除课程《${c.title}》？单元、讲次与练习记录会一并删除。`)) return;
        await Api.del("/api/courses/" + c.id);
        if (S.courseId === c.id) { S.courseId = null; S.course = null; }
        await loadCourses(); renderList(); renderMain();
      };
      item.appendChild(del);
      item.onclick = async () => { await loadCourse(c.id); renderList(); renderMain(); };
      body.appendChild(item);
    });
    box.appendChild(body);
    document.getElementById("btn-new").onclick = () => { S.creating = true; S.course = null; renderMain(); };
  }

  /* ── 主区分发 ─────────────────────────── */
  function renderMain() {
    const host = document.getElementById("course-main");
    host.innerHTML = "";
    if (S.creating) return renderCreate(host);
    if (!S.course) {
      host.appendChild(el("div", "empty",
        "从左侧选择一门课程，或新建一门。<br><br>课程会把你的材料拆成「单元 → 讲次」，" +
        "每节讲次有白板讲义，学完可以做随堂练习。"));
      return;
    }
    renderDetail(host);
  }

  /* ── 新建课程（创建向导）───────────────── */
  function renderCreate(host) {
    const card = el("div", "card");
    card.innerHTML = `
      <div class="row" style="justify-content:space-between">
        <b>新建课程</b>
        <span class="pill">第 1 步 / 共 2 步：填写学习目标</span>
      </div>
      <div class="sep"></div>
      <div class="field">
        <label>学习材料（不选则使用全部已解析材料）</label>
        <div id="pick-docs" class="doc-pick"></div>
      </div>
      <div class="field">
        <label>学习目标：学完想做到什么？
          <button class="btn small" id="f-suggest" style="margin-left:8px">✨ 帮我推荐</button>
        </label>
        <textarea id="f-goal" rows="3" placeholder="例如：学完能独立完成二重积分的换序与计算，并能在物理应用题里判断该不该换序"></textarea>
        <div id="f-goal-list" class="hint" style="margin-top:6px"></div>
      </div>
      <div class="row">
        <div class="field">
          <label>当前基础</label>
          <select id="f-level">${LEVELS.map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        </div>
        <div class="field">
          <label>内容深度</label>
          <select id="f-depth">${DEPTHS.map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        </div>
        <div class="field">
          <label>单元数量</label>
          <select id="f-units"><option value="" selected>自动（按材料定）</option>${[2, 3, 4, 5, 6].map((n) => `<option value="${n}">${n} 个单元</option>`).join("")}</select>
        </div>
        <div class="field">
          <label>实践环节</label>
          <select id="f-hands">
            <option value="1" selected>包含实操（可出真实操作类题目）</option>
            <option value="0">纯理论（不出实操题，讲稿也不布置操作任务）</option>
          </select>
        </div>
      </div>
      <div class="row" style="justify-content:flex-end">
        <button class="btn" id="f-cancel">取消</button>
        <button class="btn primary" id="f-go">生成大纲</button>
      </div>
      <div class="hint">生成过程会分阶段显示进度；生成完成后你可以先修改结构，再确认。</div>`;
    host.appendChild(card);

    const pick = document.getElementById("pick-docs");
    if (!S.documents.length) {
      pick.appendChild(el("div", "hint", "还没有已解析的材料，请先到「工作台」上传文件。"));
    }
    S.documents.forEach((d) => {
      const lab = el("label", "switch", `<input type="checkbox" value="${d.id}"> ${esc(d.title)}`);
      pick.appendChild(lab);
    });

    document.getElementById("f-cancel").onclick = () => { S.creating = false; renderMain(); };

    document.getElementById("f-suggest").onclick = async () => {
      const btn = document.getElementById("f-suggest");
      const box = document.getElementById("f-goal-list");
      if (!S.documents.length) return Toast("还没有已解析的材料", true);
      btn.disabled = true; btn.textContent = "分析中…";
      box.textContent = "正在分析材料，推荐学习目标…";
      try {
        const ids = [...pick.querySelectorAll("input:checked")].map((i) => i.value);
        const r = await Api.post("/api/courses/suggest-goals", { document_ids: ids });
        const goals = r.goals || [];
        box.innerHTML = "";
        if (!goals.length) { box.textContent = "没有分析出目标，请手动填写。"; return; }
        box.appendChild(el("div", "hint", "点一个目标，自动填入："));
        goals.forEach((g) => {
          const b = el("button", "btn small", esc(g));
          b.style.margin = "4px 6px 0 0";
          b.onclick = () => { document.getElementById("f-goal").value = g; Toast("已填入，可再修改"); };
          box.appendChild(b);
        });
        // 材料来源**永远可见**：推荐看的就是这几份材料，错配时一眼能发现
        // （曾经勾文言文材料却推荐出大模型目标，界面上毫无线索）。
        const srcs = r.sources || [];
        if (srcs.length) {
          box.appendChild(el("div", "hint",
            `推荐基于你勾选的：${srcs.map((t) => esc(t)).join("、")}`));
        }
      } catch (e) {
        box.textContent = "";
        Toast("推荐失败：" + e.message, true);
      } finally { btn.disabled = false; btn.textContent = "✨ 帮我推荐"; }
    };

    document.getElementById("f-go").onclick = async () => {
      const goal = document.getElementById("f-goal").value.trim();
      if (!goal) return Toast("请填写学习目标", true);
      const badge = document.getElementById("model-badge");
      if (!badge.dataset.ok) return Toast("请先在「设置」里配置对话模型", true);
      const ids = [...pick.querySelectorAll("input:checked")].map((i) => i.value);
      const btn = document.getElementById("f-go");
      btn.disabled = true; btn.textContent = "生成中…";
      try {
        const rawUnit = document.getElementById("f-units").value;
        const r = await Api.post("/api/courses", {
          goal, document_ids: ids,
          level: document.getElementById("f-level").value,
          depth: document.getElementById("f-depth").value,
          unit_count: rawUnit ? parseInt(rawUnit, 10) : null,
          hands_on: document.getElementById("f-hands").value === "1",
        });
        await pollOutline(r.course_id, r.job_id, host);
      } catch (e) {
        Toast(e.message, true);
        btn.disabled = false; btn.textContent = "生成大纲";
      }
    };
  }

  /** 轮询大纲任务，完成后进入结构确认。 */
  /**
   * 轮询大纲任务直到完成。
   *
   * ``onReady`` / ``onFail`` 由调用方注入收尾动作：
   * - 建课向导 / 编辑结构：默认跳到结构确认页（confirmOutline）；
   * - 课程详情页的「重新生成大纲」：**不能**把整个详情换成结构编辑器，
   *   要原地重置按钮并刷新详情（否则按钮停在「重新生成中…」，状态与
   *   右侧 stage 文案「已完成」不同步 —— 用户实测问题 1）。
   * 轮询中对网络抖动容错（连续失败 >20 次才放弃），总时长上限提到 10 分钟
   * （新提示词 + 8k 输出下，多单元大纲可能超过旧的 210 秒）。
   */
  async function pollOutline(courseId, jobId, host, stageBox, onReady, onFail) {
    let misses = 0;
    for (let i = 0; i < 600; i++) {
      let job;
      try {
        job = await Api.get("/api/courses/jobs/" + jobId);
        misses = 0;
      } catch (e) {
        if (++misses > 20) {
          Toast("与服务的连接不稳定，请稍后在课程页查看结果", true);
          if (onFail) { onFail(e); return; }
          return;
        }
        await new Promise((r) => setTimeout(r, 1000));
        continue;
      }
      if (stageBox) stageBox.textContent = job.stage || "";
      else showStage(host, job.stage);
      if (job.status === "ready") {
        if (onReady) { await onReady(job); return; }
        await loadCourses();
        await loadCourse(courseId);
        renderList();
        S.creating = false;
        return confirmOutline(host, courseId);
      }
      if (job.status === "failed") {
        if (onFail) { onFail(new Error(job.error || "生成失败")); return; }
        host.innerHTML = `<div class="card"><b>大纲生成失败</b><div class="hint">${esc(job.error || "")}</div>
          <div class="row" style="margin-top:12px"><button class="btn primary" id="retry">重新生成</button></div></div>`;
        document.getElementById("retry").onclick = () => { S.creating = true; renderMain(); };
        return;
      }
      await new Promise((r) => setTimeout(r, 700));
    }
    Toast("生成超时，请稍后在课程页查看结果", true);
    if (onFail) onFail(new Error("生成超时"));
  }

  function showStage(host, stage) {
    host.innerHTML = `<div class="card"><b>正在生成课程大纲…</b>
      <div class="hint" style="margin-top:8px">${esc(stage || "")}</div>
      <div class="bar"><i style="width:100%"></i></div>
      <div class="hint">时间取决于模型速度，通常十几秒到一分钟。</div></div>`;
  }

  /** 讲次教学设计 desc → 折叠展示块（只读；无 desc 返回空串）。 */
  function descHtml(d) {
    if (!d || typeof d !== "object") return "";
    const rows = [];
    if (d.outcomes) rows.push(`学习目标：${esc(d.outcomes.join("；"))}`);
    if (d.knowledge_points) rows.push(`知识点边界：${esc(d.knowledge_points.join("；"))}`);
    if (d.concepts) rows.push(`术语口径：${esc(d.concepts.join("、"))}`);
    if (d.operations) rows.push(`涉及操作：${esc(d.operations.join("；"))}`);
    const tr = d.transition || {};
    const seg = [];
    if (tr.prev) seg.push(`承接 ${esc(tr.prev)}`);
    if (tr.next) seg.push(`引向 ${esc(tr.next)}`);
    if (tr.avoid) seg.push(`避免展开 ${esc(tr.avoid)}`);
    if (seg.length) rows.push(`讲间衔接：${seg.join("；")}`);
    if (d.visual && d.visual !== "无") rows.push(`可视化提示：${esc(d.visual)}`);
    if (d.exercise_focus) rows.push(`考察点：${esc(d.exercise_focus.join("；"))}`);
    if (d.expected_mistakes) rows.push(`学生易错点：${esc(d.expected_mistakes.join("；"))}`);
    if (d.exercise_flow) rows.push(`题型安排：${esc(d.exercise_flow)}`);
    if (!rows.length) return "";
    return `<details class="lesson-desc"><summary>教学设计（AI 按此备课）</summary>
      <div class="hint" style="margin:6px 0 0">${rows.map((r) => `<div>· ${r}</div>`).join("")}</div></details>`;
  }

  /** 结构确认：可改标题/目标、删除讲次，确认后进入课程。 */
  function confirmOutline(host, courseId) {
    const c = S.course;
    const draft = JSON.parse(JSON.stringify(c.units ? { title: c.title, units: c.units } : { units: [] }));
    draft.units = draft.units.map((u) => ({
      title: u.title, summary: u.summary,
      // desc：大纲阶段生成的「本讲教学设计」，只读展示（改标题/目标不会重算它）
      lessons: u.lessons.map((l) => ({ title: l.title, objective: l.objective, kind: l.kind, desc: l.desc || null })),
    }));

    // 已有讲次 id 说明这门课已经落库过 → 本次是「编辑结构」而不是首次确认。
    const editing = (c.units || []).some((u) => (u.lessons || []).some((l) => l.id));
    const card = el("div", "card");
    card.innerHTML = `
      <div class="row" style="justify-content:space-between">
        <b>${editing ? "编辑课程结构" : "确认课程结构"}</b>
        <span class="pill">${editing ? "修改后保存即生效" : "第 2 步 / 共 2 步"}</span>
      </div>
      <div class="hint">左边改标题与学习目标、增删讲次；右边是同一份结构的思维导图，改完立即同步。</div>
      <div class="sep"></div>
      <div class="tree-layout">
        <div id="tree" class="tree-edit"></div>
        <div class="tree-map">
          <div class="row" style="justify-content:space-between">
            <span class="hint">结构预览（思维导图）</span>
            <button class="btn small" id="o-fit">适应窗口</button>
          </div>
          <div id="map"></div>
        </div>
      </div>
      <div class="field" style="margin-top:12px">
        <label>重新生成前，写下你的要求（可选）</label>
        <textarea id="o-regen-note" rows="2"
          placeholder="例如：单元再少一点，只保留 3 个；多放一些例题与易错点；先讲定义再讲计算"></textarea>
        <div class="hint">点「重新生成」时会把这段要求交给模型，新大纲会尽量按你的描述调整；留空则按当前目标重新生成。</div>
      </div>
      <div class="row" style="justify-content:flex-end">
        <button class="btn" id="o-regen">重新生成</button>
        <button class="btn primary" id="o-ok">${editing ? "保存结构" : "确认，开始学习"}</button>
      </div>`;
    host.appendChild(card);

    const tree = document.getElementById("tree");
    const paint = () => {
      tree.innerHTML = "";
      draft.units.forEach((u, ui) => {
        const box = el("div", "unit-box");
        box.appendChild(el("div", "row",
          `<input type="text" value="${esc(u.title)}" data-u="${ui}" style="flex:1">`));
        u.lessons.forEach((l, li) => {
          const row = el("div", "lesson-row");
          row.innerHTML = `<span class="pill">${l.kind === "practice" ? "练习" : "讲解"}</span>`;
          const t = el("input", null); t.type = "text"; t.value = l.title; t.style.flex = "1";
          t.oninput = () => { l.title = t.value; drawMap(); };
          const o = el("input", null); o.type = "text"; o.value = l.objective; o.style.flex = "1";
          o.placeholder = "学习目标";
          o.oninput = () => { l.objective = o.value; drawMap(); };
          const x = el("button", "btn small", "删除");
          x.onclick = () => { u.lessons.splice(li, 1); paint(); };
          row.appendChild(t); row.appendChild(o); row.appendChild(x);
          box.appendChild(row);
          const dd = descHtml(l.desc);
          if (dd) box.insertAdjacentHTML("beforeend", dd);
        });
        const add = el("button", "btn small", "＋ 讲次");
        add.onclick = () => {
          u.lessons.push({ title: "新的讲次", objective: "", kind: "lecture" });
          paint();
        };
        box.appendChild(add);
        box.querySelector(`[data-u="${ui}"]`).oninput = (e) => { u.title = e.target.value; drawMap(); };
        tree.appendChild(box);
      });
      drawMap();
    };

    /** 把当前草稿渲染为 markmap 思维导图（复用资料生成页的渲染方式）。 */
    function drawMap() {
      const box = document.getElementById("map");
      if (!box) return;
      const lines = [`# ${draft.title || "课程结构"}`, ""];
      draft.units.forEach((u) => {
        lines.push(`- ${u.title || "未命名单元"}`);
        u.lessons.forEach((l) => {
          const mark = l.kind === "practice" ? "（练习）" : "";
          lines.push(`  - ${l.title || "未命名讲次"}${mark}`);
        });
      });
      S.mindmap = MD.mindmap(box, lines.join("\n"));
    }
    paint();

    document.getElementById("o-fit").onclick = () => {
      try { S.mindmap && S.mindmap.fit(); } catch (e) { /* 忽略 */ }
    };

    document.getElementById("o-ok").onclick = async () => {
      const units = draft.units
        .filter((u) => u.lessons.length)
        .map((u) => ({
          title: u.title, summary: u.summary,
          lessons: u.lessons.map((l) => ({
            title: l.title, objective: l.objective, kind: l.kind, depth: l.depth || "standard",
          })),
        }));
      if (!units.length) return Toast("至少保留一个讲次", true);
      const btn = document.getElementById("o-ok");
      btn.disabled = true; btn.textContent = "保存中…";
      try {
        await Api.post(`/api/courses/${courseId}/outline:confirm`, { title: c.title, units });
        S.creating = false;
        await loadCourses();
        await loadCourse(courseId);
        renderList();
        if (editing) {
          // 清掉 ?confirm=，避免刷新后又跳回编辑器
          location.hash = "#/courses";
          renderMain();
          Toast("结构已保存");
        } else {
          renderMain();
          Toast("课程已就绪，开始学习吧");
        }
      } catch (e) {
        Toast(e.message, true);
        btn.disabled = false; btn.textContent = editing ? "保存结构" : "确认，开始学习";
      }
    };
    document.getElementById("o-regen").onclick = async () => {
      const btn = document.getElementById("o-regen");
      const noteEl = document.getElementById("o-regen-note");
      const note = (noteEl && noteEl.value || "").trim();
      btn.disabled = true; btn.textContent = "重新生成中…";
      // 立刻给出反馈，避免「点了没反应」的错觉。
      showStage(host, note ? "正在按你的要求重新组织大纲…" : "正在重新生成大纲…");
      try {
        const r = await Api.post(`/api/courses/${courseId}/outline:regenerate`, { note });
        await pollOutline(courseId, r.job_id, host);
      } catch (e) {
        Toast(e.message, true);
        btn.disabled = false; btn.textContent = "重新生成";
        renderMain();
      }
    };
  }

  /* ── 课程详情 ─────────────────────────── */
  function renderDetail(host) {
    // 详情页只承载一份内容：先清空再画。否则「重新生成大纲」完成后的
    // 原地刷新（onReady → renderDetail）会把新详情 append 在旧详情下面，
    // 出现新旧大纲上下并排（用户实测问题 1）。
    host.innerHTML = "";
    const c = S.course;
    const p = c.progress || {};
    const head = el("div", "card");
    head.innerHTML = `
      <div class="row" style="justify-content:space-between">
        <b style="font-size:16px">${esc(c.title)}</b>
        <span class="pill ${c.status === "ready" ? "ok" : ""}">${c.status === "ready" ? "已就绪" : c.status === "failed" ? "生成失败" : "生成中"}</span>
      </div>
      <div class="hint">目标：${esc(c.goal)}</div>
      <div class="hint">基础 ${esc(c.level_name)} · 深度 ${esc(c.depth_name)} · 共 ${c.units.length} 个单元</div>
      ${c.summary ? `<div class="hint" style="margin-top:6px">${esc(c.summary)}</div>` : ""}
      <div class="bar" style="margin-top:10px"><i style="width:${p.percent || 0}%"></i></div>
      <div class="hint">进度：${p.done_lessons || 0}/${p.total_lessons || 0} 节（${p.percent || 0}%）</div>
      ${c.error ? `<div class="hint" style="color:var(--bad)">${esc(c.error)}</div>` : ""}
      <div class="row" style="margin-top:10px">
        <button class="btn small" id="c-edit">编辑结构</button>
        <button class="btn small" id="c-regen">重新生成大纲</button>
        <span class="hint" id="c-stage"></span>
      </div>
      <div class="field" style="margin-top:10px">
        <label>重新生成前，写下你的要求（可选）</label>
        <textarea id="c-regen-note" rows="2"
          placeholder="例如：把单元拆得更细，每个单元只讲一个概念；多放一些典型例题"></textarea>
        <div class="hint">填写后点「重新生成大纲」，新大纲会按你的描述调整；留空则按当前目标重新生成。</div>
      </div>`;
    host.appendChild(head);
    document.getElementById("c-edit").onclick = () => {
      location.hash = "#/courses?confirm=" + c.id;
    };
    document.getElementById("c-regen").onclick = async () => {
      const stage = document.getElementById("c-stage");
      const btn = document.getElementById("c-regen");
      const noteEl = document.getElementById("c-regen-note");
      const note = (noteEl && noteEl.value || "").trim();
      btn.disabled = true; btn.textContent = "重新生成中…";
      if (stage) stage.textContent = note ? "正在按你的要求重新组织大纲…" : "正在重新生成大纲…";
      const resetBtn = () => {
        const b = document.getElementById("c-regen");
        if (b) { b.disabled = false; b.textContent = "重新生成大纲"; }
        const st = document.getElementById("c-stage");
        if (st) st.textContent = "";
      };
      try {
        const r = await Api.post(`/api/courses/${c.id}/outline:regenerate`, { note });
        // 详情页的重新生成：完成后**原地刷新详情**（新大纲/进度直接可见），
        // 按钮复位 —— 不跳结构编辑器，避免「按钮停在生成中、状态却已完成」的不同步。
        await pollOutline(c.id, r.job_id, host, stage,
          async () => {
            resetBtn();
            await loadCourses();
            await loadCourse(c.id);
            renderDetail(host);
            Toast("大纲已重新生成", false);
          },
          () => resetBtn());
      } catch (e) {
        Toast(e.message, true);
        btn.disabled = false; btn.textContent = "重新生成大纲";
        if (stage) stage.textContent = "";
      }
    };

    // 按单元顺序 × 单元内讲次顺序，找出第一个 status !== "done" 的讲次（接下来要上的那一讲）
    let nextId = null;
    for (const u0 of (c.units || [])) {
      for (const l0 of (u0.lessons || [])) {
        if (l0.status !== "done") { nextId = l0.id; break; }
      }
      if (nextId) break;
    }

    (c.units || []).forEach((u) => {
      const box = el("div", "card");
      const tag = u.status === "done" ? '<span class="pill ok">已完成</span>'
        : u.status === "active" ? '<span class="pill">进行中</span>' : '<span class="pill">未开始</span>';
      const st = u.stats || {};
      box.innerHTML = `<div class="row" style="justify-content:space-between">
          <b>第 ${u.ordinal} 单元 · ${esc(u.title)}</b>${tag}</div>
        ${u.summary ? `<div class="hint">${esc(u.summary)}</div>` : ""}
        ${st.questions
          ? `<div class="hint">练习：答对 ${st.correct}/${st.questions} · 得分 ${st.score}${
              st.errors ? ` · 错题 ${st.errors}` : ""}</div>`
          : ""}`;
      const list = el("div");
      (u.lessons || []).forEach((l) => {
        const isNext = (l.id === nextId);
        const row = el("div", "item lesson-row" + (isNext ? " next-lesson" : ""));
        const done = l.status === "done";
        row.innerHTML = `<span class="pill">${esc(l.kind_name)}</span>
          ${isNext ? '<span class="pill next-pill">接下来</span>' : ""}
          <span class="t">${l.ordinal}. ${esc(l.title)}</span>
          ${done ? '<span class="pill ok">已完成</span>' : ""}`;
        const go = el("button", "btn small" + (isNext ? " primary" : ""), done ? "再看一遍" : "开始");
        go.onclick = () => { location.hash = "#/lessons/" + l.id; };
        row.appendChild(go);
        list.appendChild(row);
      });
      box.appendChild(list);

      // 单元总结入口（P1：学完一个单元后的回顾与薄弱点）
      const lastLesson = (u.lessons || [])[(u.lessons || []).length - 1];
      const foot = el("div", "row");
      foot.style.marginTop = "8px";
      const sum = el("button", "btn small",
        u.summary_status === "ready" ? "查看单元总结" : "生成单元总结");
      sum.onclick = () => {
        if (!lastLesson) return Toast("这个单元还没有讲次", true);
        location.hash = "#/lessons/" + lastLesson.id + "?tab=summary";
      };
      foot.appendChild(sum);
      if (u.summary_status === "ready") {
        foot.appendChild(el("span", "pill ok", "总结已生成"));
      }
      box.appendChild(foot);
      host.appendChild(box);
    });
  }

  /* ── 装配 ─────────────────────────────── */
  /** 从 hash 查询串读取要编辑结构的课程 id（#/courses?confirm=xxx）。 */
  function confirmTarget() {
    const q = location.hash.split("?")[1] || "";
    const m = /(?:^|&)confirm=([0-9a-fA-F]+)/.exec(q);
    return m ? m[1] : "";
  }

  async function render(host) {
    await Promise.all([loadCourses(), loadDocuments()]);
    host.innerHTML = `
      <div class="cols">
        <div class="col col-side" id="course-list" style="display:flex;flex-direction:column"></div>
        <div class="col col-main page" id="course-main"></div>
      </div>`;
    renderList();
    if (S.courseId) { try { await loadCourse(S.courseId); } catch (e) { S.courseId = null; S.course = null; } }
    // 支持 #/courses?new=1 直接进入新建向导（便于 UI 冒烟测试）
    if (/\bnew=1\b/.test(location.hash.split("?")[1] || "")) { S.creating = true; }

    // 直接进入结构编辑（课程详情里的「编辑结构」入口，也可用链接直达）
    const target = confirmTarget();
    if (target) {
      try {
        await loadCourse(target);
        S.creating = false;
        renderList();
        return confirmOutline(document.getElementById("course-main"), target);
      } catch (e) {
        Toast("打开结构编辑失败：" + e.message, true);
      }
    }
    renderMain();
  }

  window.Views = window.Views || {};
  window.Views.courses = { render };
})();
