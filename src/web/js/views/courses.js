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
        <span class="pill">第 1 步 / 共 2 步：选课型</span>
      </div>
      <div class="sep"></div>
      <div class="field">
        <label>学习材料（不选则使用全部已解析材料）</label>
        <div id="pick-docs" class="doc-pick"></div>
      </div>
      <div class="field">
        <label>你想要什么课？<span class="hint" style="margin-left:6px">（决定单元怎么分、按什么顺序讲）</span>
          <button class="btn small" id="f-analyze" style="margin-left:8px">✨ 分析材料并预选</button>
        </label>
        <div id="intent-cards" class="intent-cards"></div>
        <div id="intent-hint" class="hint" style="margin-top:6px"></div>
      </div>
      <div class="field">
        <label>学习目标（一句话；AI 按课型写，可改、可清空）</label>
        <textarea id="f-goal" rows="2" placeholder="选好课型后会自动写一句；也可以自己改，或清空让系统按课型决定"></textarea>
        <div class="row" style="margin-top:6px">
          <button class="btn small" id="f-gen-goal">按课型生成</button>
          <button class="btn small" id="f-clear-goal">清空</button>
          <span class="hint" id="f-goal-hint"></span>
        </div>
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
          <select id="f-units">
            <option value="" selected>自动（按材料定）</option>
            ${[2, 3, 4, 5, 6].map((n) => `<option value="${n}">${n} 个单元</option>`).join("")}
            <option value="custom">自定义…</option>
          </select>
          <input type="number" id="f-units-custom" min="1" max="12" placeholder="1 ~ 12"
                 style="display:none;margin-top:6px;max-width:110px">
        </div>
        <div class="field">
          <label>实践环节</label>
          <select id="f-hands">
            <option value="auto" selected>自动（按材料决定）</option>
            <option value="1">包含实操（可出真实操作类题目）</option>
            <option value="0">纯理论（不出实操题，讲稿也不布置操作任务）</option>
          </select>
        </div>
      </div>
      <details class="adv">
        <summary>高级选项（辅助课型 / 补充要求）</summary>
        <div class="field">
          <label>辅助课型（可选，最多一个）</label>
          <select id="f-assist"></select>
          <div class="hint" id="f-assist-hint">在满足主课型结构的前提下，额外加强某一块（例如主课型「精读」+ 辅助「考点」→ 逐句精讲并加强考点）。</div>
        </div>
        <div class="field">
          <label>补充要求（可选，一句话）</label>
          <input type="text" id="f-note" placeholder="例如：学生初三 / 2 课时 / 必须包含背诵默写">
        </div>
      </details>
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
      // 标题包一层 span：多列网格下超长文件名用省略号，完整名放 title 里
      const lab = el("label", "switch",
        `<input type="checkbox" value="${d.id}">` +
        `<span class="t" title="${esc(d.title)}">${esc(d.title)}</span>`);
      pick.appendChild(lab);
    });
    // 已选计数：材料多时一眼知道选了几份（不选 = 用全部材料）
    const pickHint = el("div", "hint", "");
    pick.parentElement.appendChild(pickHint);
    const paintPicked = () => {
      const n = pick.querySelectorAll("input:checked").length;
      pickHint.textContent = n ? `已选 ${n} 份材料` : "";
    };
    pick.addEventListener("change", paintPicked);
    paintPicked();

    document.getElementById("f-cancel").onclick = () => { S.creating = false; renderMain(); };

    // 「单元数量 → 自定义…」时展开数字输入（1~12；留空 = 自动）
    const unitsSel = document.getElementById("f-units");
    const unitsCustom = document.getElementById("f-units-custom");
    unitsSel.onchange = () => {
      const custom = unitsSel.value === "custom";
      unitsCustom.style.display = custom ? "" : "none";
      if (custom) unitsCustom.focus();
    };

    // ── 课型（学习意图）：主课型必选；辅助课型在「高级选项」里可选、最多一个 ──
    // 课型库只有后端一份（GET /api/courses/intents），这里只负责渲染与选择。
    const intentBox = document.getElementById("intent-cards");
    const assistSel = document.getElementById("f-assist");
    const goalEl = document.getElementById("f-goal");
    const hintEl = document.getElementById("f-goal-hint");
    let intents = [];
    const chosen = { primary: "", assist: "", note: "" };

    const paintIntents = () => {
      intentBox.innerHTML = "";
      if (!intents.length) {
        intentBox.textContent = "课型库加载失败，请刷新页面重试。";
        return;
      }
      intents.forEach((t) => {
        const c = el("button", "intent-card" + (chosen.primary === t.id ? " on" : ""));
        c.type = "button";
        c.innerHTML = `<b>${esc(t.name)}</b><div class="fit">${esc(t.fit)}</div>
          <div class="pace">${esc(t.pace || "")}</div>`;
        c.onclick = async () => {
          chosen.primary = t.id;
          if (chosen.assist === t.id) chosen.assist = "";   // 辅助不能与主相同
          paintIntents(); paintAssist();
          await autoGoal();                                 // 选课型即自动写目标
        };
        intentBox.appendChild(c);
      });
    };

    const paintAssist = () => {
      assistSel.innerHTML = '<option value="">不用辅助课型</option>' + intents
        .filter((t) => t.id !== chosen.primary)
        .map((t) => `<option value="${t.id}">${esc(t.name)} —— ${esc(t.fit)}</option>`)
        .join("");
      // 下拉会因「换主课型」把旧选项过滤掉：内部值必须同步清空，
      // 否则会出现「界面显示不用辅助、提交却带着旧辅助课型」的错位。
      if (chosen.assist && (chosen.assist === chosen.primary
        || !intents.some((t) => t.id === chosen.assist))) {
        chosen.assist = "";
      }
      assistSel.value = chosen.assist || "";
      assistSel.onchange = () => { chosen.assist = assistSel.value || ""; };
    };

    const setGoalHint = (s) => { hintEl.textContent = s || ""; };

    /** 按「材料 + 课型」写一句目的句（用户可改、可清空；清空后后端按课型兜底）。
     *
     * `goalSeq` 是**迟到响应防线**：连点不同课型卡时，先发出的请求可能后返回，
     * 不加判别就会把「上一个课型的目标」写进输入框，甚至覆盖用户刚手改的文本。
     * 规则：每次发起自增；用户手改 / 清空也自增（作废在途请求）；返回时序号不符即丢弃。
     */
    let goalSeq = 0;
    const autoGoal = async () => {
      if (!chosen.primary) return;
      const noteEl = document.getElementById("f-note");
      chosen.note = (noteEl && noteEl.value || "").trim();
      const seq = ++goalSeq;
      setGoalHint("正在按课型写目标…");
      try {
        const ids = [...pick.querySelectorAll("input:checked")].map((i) => i.value);
        const r = await Api.post("/api/courses/suggest-goal", {
          document_ids: ids, primary: chosen.primary,
          assist: chosen.assist || null, note: chosen.note || null,
        });
        if (seq !== goalSeq) return;                   // 已被更新的一次选择 / 手改取代
        goalEl.value = r.goal || "";
        setGoalHint(r.goal ? "已按课型生成，可自由修改或清空" : "");
      } catch (e) {
        if (seq !== goalSeq) return;
        setGoalHint("生成失败，可自己写一句，或留空让系统按课型决定");
      }
    };
    // 用户手改目标 → 作废在途的自动写目标（程序赋值不触发 input，不会误伤自己）
    goalEl.addEventListener("input", () => { goalSeq++; });

    document.getElementById("f-analyze").onclick = async () => {
      const btn = document.getElementById("f-analyze");
      const hint = document.getElementById("intent-hint");
      if (!S.documents.length) return Toast("还没有已解析的材料", true);
      btn.disabled = true; btn.textContent = "分析中…";
      hint.textContent = "正在看材料的体裁与内容，判断适合什么课…";
      try {
        const ids = [...pick.querySelectorAll("input:checked")].map((i) => i.value);
        const r = await Api.post("/api/courses/suggest-intents", { document_ids: ids });
        chosen.primary = r.primary || "";
        if (chosen.assist === chosen.primary) chosen.assist = "";  // 推荐结果可能撞上已选辅助
        paintIntents(); paintAssist();
        const name = (intents.find((t) => t.id === chosen.primary) || {}).name || "";
        const alts = (r.alternatives || [])
          .map((id) => (intents.find((t) => t.id === id) || {}).name).filter(Boolean);
        // 材料来源与理由**永远可见**：曾经「勾文言文却推荐出大模型目标」时界面上毫无线索。
        hint.textContent = `推荐「${name}」${r.reason ? "：" + r.reason : ""}`
          + (alts.length ? `（也可以试试：${alts.join("、")}）` : "")
          + ((r.sources || []).length ? ` ｜ 基于：${r.sources.map(esc).join("、")}` : "");
        await autoGoal();
      } catch (e) {
        hint.textContent = "";
        Toast("分析失败：" + e.message, true);
      } finally { btn.disabled = false; btn.textContent = "✨ 分析材料并预选"; }
    };

    document.getElementById("f-gen-goal").onclick = () => {
      if (!chosen.primary) return Toast("请先选一个课型", true);
      autoGoal();
    };
    document.getElementById("f-clear-goal").onclick = () => {
      goalSeq++;                       // 清空同样要作废在途请求，否则会被迟到的响应写回
      goalEl.value = "";
      setGoalHint("已清空 —— 留空时系统按课型决定这门课怎么讲");
    };

    (async () => {
      try {
        const r = await Api.get("/api/courses/intents");
        intents = r.items || [];
      } catch (e) { intents = []; }
      paintIntents(); paintAssist();
      document.getElementById("intent-hint").textContent =
        "点一张卡选课型；拿不准就点「分析材料并预选」。";
    })();

    document.getElementById("f-go").onclick = async () => {
      if (!chosen.primary) return Toast("请先选一个课型", true);
      // 目标允许留空（用户可一键清空）：后端会按所选课型兜底出一句「目的」。
      const goal = goalEl.value.trim();
      const badge = document.getElementById("model-badge");
      if (!badge.dataset.ok) return Toast("请先在「设置」里配置对话模型", true);
      const ids = [...pick.querySelectorAll("input:checked")].map((i) => i.value);
      const btn = document.getElementById("f-go");
      btn.disabled = true; btn.textContent = "生成中…";
      try {
        const rawUnit = document.getElementById("f-units").value;
        const customUnit = parseInt((document.getElementById("f-units-custom") || {}).value || "", 10);
        const unitCount = rawUnit === "custom"
          ? (Number.isFinite(customUnit) ? customUnit : null)
          : (rawUnit ? parseInt(rawUnit, 10) : null);
        const rawHands = document.getElementById("f-hands").value;   // auto | 1 | 0
        const noteEl = document.getElementById("f-note");
        const r = await Api.post("/api/courses", {
          goal, document_ids: ids,
          level: document.getElementById("f-level").value,
          depth: document.getElementById("f-depth").value,
          unit_count: unitCount,
          // 「自动」传 null → 后端存 NULL，由模型按材料判断要不要出实操题
          hands_on: rawHands === "auto" ? null : rawHands === "1",
          intent: {
            primary: chosen.primary,
            assist: chosen.assist || null,
            note: ((noteEl && noteEl.value) || "").trim() || null,
          },
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
      // desc / depth：大纲阶段生成的字段，**只读展示但必须原样带回**。
      // desc 跟着讲次对象走 → 删一讲、加两讲之后，各讲的教学设计仍挂在自己身上
      // （后端按序号位置复用行，若这里不带 desc 就会整体错位到别的讲次上）。
      lessons: u.lessons.map((l) => ({
        id: l.id || null,
        title: l.title, objective: l.objective, kind: l.kind,
        depth: l.depth, desc: l.desc || null,
      })),
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
            // 讲次 id：编辑已有课程时回带，后端按 id 复用行（内容跟着讲次走）。
            // 新建讲次没有 id → null → 后端按新增处理。
            id: l.id || null,
            title: l.title, objective: l.objective, kind: l.kind,
            // desc 必须带上：后端按讲次**对象**复用行，缺了它，被删讲次之后
            // 的每一讲都会继承前一讲的 desc（静默错位）。depth 只在新增讲次时被后端采用。
            depth: l.depth || "standard",
            desc: l.desc || null,
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
    // 讲次教学设计 desc：旧课程（功能上线前建的）为 null，详情页给一条「只补 desc」
    // 的入口 —— 用户若走「重新生成大纲」会重建讲次、把已生成好的讲义一起丢掉。
    const allLessons = [];
    (c.units || []).forEach((u) => (u.lessons || []).forEach((l) => allLessons.push(l)));
    const needDesc = allLessons.length > 0 && allLessons.some((l) => !l.desc);
    const head = el("div", "card");
    head.innerHTML = `
      <div class="row" style="justify-content:space-between">
        <b style="font-size:16px">${esc(c.title)}</b>
        <span class="pill ${c.status === "ready" ? "ok" : ""}">${c.status === "ready" ? "已就绪" : c.status === "failed" ? "生成失败" : "生成中"}</span>
      </div>
      <div class="hint">目标：${esc(c.goal)}</div>
      <div class="hint">基础 ${esc(c.level_name)} · 深度 ${esc(c.depth_name)} · 共 ${c.units.length} 个单元</div>
      ${c.intent ? `<div class="hint">课型：${esc(c.intent.primary_name)}${c.intent.assist_name ? " ＋ " + esc(c.intent.assist_name) + "（辅助）" : ""}${c.intent.note ? " ｜ " + esc(c.intent.note) : ""}</div>` : ""}
      ${c.summary ? `<div class="hint" style="margin-top:6px">${esc(c.summary)}</div>` : ""}
      <div class="bar" style="margin-top:10px"><i style="width:${p.percent || 0}%"></i></div>
      <div class="hint">进度：${p.done_lessons || 0}/${p.total_lessons || 0} 节（${p.percent || 0}%）</div>
      ${c.error ? `<div class="hint" style="color:var(--bad)">${esc(c.error)}</div>` : ""}
      <div class="row" style="margin-top:10px">
        <button class="btn small" id="c-edit">编辑结构</button>
        <button class="btn small" id="c-regen">重新生成大纲</button>
        ${needDesc ? '<button class="btn small" id="c-desc">补写教学设计</button>' : ""}
        <span class="hint" id="c-stage"></span>
      </div>
      ${needDesc ? `<div class="hint">本课建立时还没有「教学设计」功能，各讲课件只能看到标题。
        点「补写教学设计」会按现有结构为每讲补一份（不改结构、不影响已生成的讲义）。</div>` : ""}
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

    // 「补写教学设计」：只为旧课程补 desc，不重建讲次、不动已生成的讲义。
    const descBtn = document.getElementById("c-desc");
    if (descBtn) {
      descBtn.onclick = async () => {
        const stage = document.getElementById("c-stage");
        descBtn.disabled = true; descBtn.textContent = "补写中…";
        if (stage) stage.textContent = "正在为每讲补写教学设计…";
        const reset = () => {
          const b = document.getElementById("c-desc");
          if (b) { b.disabled = false; b.textContent = "补写教学设计"; }
          const st = document.getElementById("c-stage");
          if (st) st.textContent = "";
        };
        try {
          const r = await Api.post(`/api/courses/${c.id}/desc:rebuild`);
          await pollOutline(c.id, r.job_id, host, stage,
            async () => {
              await loadCourse(c.id);
              renderDetail(host);
              Toast("教学设计已补写完成，展开各讲即可查看", false);
            },
            (err) => { Toast((err && err.message) || "补写失败，请稍后重试", true); reset(); });
        } catch (e) {
          Toast(e.message, true);
          reset();
        }
      };
    }

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
        // 本讲教学设计（大纲阶段生成，只读）：与结构确认页/讲义页同格式的折叠块。
        // 放在讲次行**之外**——.item 是 flex 行，塞进去会打乱 pill/标题/按钮的排布。
        const d = descHtml(l.desc);
        if (d) {
          const dw = el("div", "lesson-desc-row");
          dw.innerHTML = d;
          list.appendChild(dw);
        }
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
    // 支持 #/courses?new=1 直接进入新建向导（便于 UI 冒烟测试）。
    // 反向也要管住：向导状态只由「本次进入是否带 new=1」决定 —— 否则用户不走「取消」
    // 而是切到别的页再回到课程页时，残留的 S.creating=true 会把他**又**丢进新建向导。
    if (/\bnew=1\b/.test(location.hash.split("?")[1] || "")) { S.creating = true; }
    else { S.creating = false; }

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
