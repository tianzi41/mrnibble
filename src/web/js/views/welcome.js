/* 新手引导（首次启动三步）：认识界面 → 用户画像 → 引导配 API。
 *
 * 触发与路由见 main.js：guide.done 非真时，除 #/welcome 外的页面都会被送到这里。
 * 三步都能单独跳过；guide.step 记录断点，刷新/重开从当前步继续。
 * 素材：src/web/assets/intro.png（带红框的界面图）+ welcome.mp3（预录解说）。
 * 双语：语言包随本文件走（画像题干在 profile_fields.js）。
 */
(function () {
  "use strict";

  const STEPS = ["intro", "profile", "api"];
  let step = "intro";
  let answers = {};
  let _cfg = null;            // 最近一次 GET /api/settings 的快照（paintApi 据此判断 API 是否已配好）

  // 仅用于把状态提示里的 model 名做 HTML 转义，避免渲染进 innerHTML 时出问题。
  const esc = (s) => String(s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const t = (k, v) => window.I18n ? window.I18n.t(k, v) : k;

  window.I18n && window.I18n.merge({
    zh: {
      "welcome.intro.title": "欢迎使用知伴",
      "welcome.intro.step": "第 {n} 步 / 共 3 步 · 认识界面",
      "welcome.intro.desc": "知伴把你的资料变成一堂课：上传文件 → 自动生成课件 → 像老师一样讲给你听。下图是上课界面，四个红框分别是：",
      "welcome.intro.img_alt": "上课界面",
      "welcome.intro.play": "🔊 播放解说",
      "welcome.intro.hint": "解说不自动播放时，点这个按钮",
      "welcome.intro.hint.blocked": "浏览器拦了自动播放：点页面任意位置即开始解说",
      "welcome.intro.skip": "跳过引导",
      "welcome.intro.next": "下一步",
      "welcome.profile.title": "花一分钟了解你",
      "welcome.profile.step": "第 {n} 步 / 共 3 步 · 用户画像",
      "welcome.profile.desc": "回答几个选择题，之后生成的课程会按你的情况调整难度、举例和讲法。每一题都能跳过，也可以整段跳过。",
      "welcome.profile.skip_all": "整段跳过",
      "welcome.profile.back": "上一步",
      "welcome.api.title": "最后一步：给知伴配一把「钥匙」",
      "welcome.api.step": "第 {n} 步 / 共 3 步 · 配置接口",
      "welcome.api.lead": "知伴自己不含 AI 能力，生成课程、回答问题都要调用云端大模型",
      "welcome.api.desc": "所以需要一个「API Key」——相当于你在大模型服务商那里的账号钥匙。各家都有免费额度，几元钱能用很久，全程约 5 分钟。",
      "welcome.api.step1": "推荐 <b>DeepSeek</b>（便宜、够用）：打开 <code>platform.deepseek.com</code> 注册，在「API Keys」里创建并<b>立即复制</b>（只显示一次）",
      "welcome.api.step2": "想用更自然的云端朗读，再配一个<b>阶跃星辰</b>的语音 Key（步骤见顶栏「不会用，点这里」帮助页）",
      "welcome.api.step3": "回到知伴<b>设置</b>页，把地址、模型名、Key 填进去，点「测试连接」",
      "welcome.api.note": "不配也能用：内置朗读、资料解析、检索都在本机完成；只有「生成 / 提问」需要钥匙。之后随时可以在设置页补上。",
      "welcome.api.detected": "✅ 检测到 API 已配置好：<b>{model}</b>（设置页可随时修改）",
      "welcome.api.go_settings": "去设置页配置",
      "welcome.api.edit_settings": "去设置页修改",
      "welcome.api.skip": "暂时跳过，我自己去配",
      "welcome.api.finish": "完成引导，进入工作台",
    },
    en: {
      "welcome.intro.title": "Welcome to ZhiBan",
      "welcome.intro.step": "Step {n} of 3 · Meet the interface",
      "welcome.intro.desc": "ZhiBan turns your materials into a lesson: upload files → auto-generate slides → explained to you like a teacher. Below is the classroom interface; the four red boxes are:",
      "welcome.intro.img_alt": "Classroom interface",
      "welcome.intro.play": "🔊 Play narration",
      "welcome.intro.hint": "If the narration doesn't start automatically, click this button",
      "welcome.intro.hint.blocked": "Autoplay was blocked by the browser: click anywhere to start the narration",
      "welcome.intro.skip": "Skip onboarding",
      "welcome.intro.next": "Next",
      "welcome.profile.title": "One minute to know you",
      "welcome.profile.step": "Step {n} of 3 · Learner profile",
      "welcome.profile.desc": "Answer a few multiple-choice questions; generated courses will then tune difficulty, examples, and teaching style to you. Every question can be skipped, or skip the whole section.",
      "welcome.profile.skip_all": "Skip all",
      "welcome.profile.back": "Back",
      "welcome.api.title": "Last step: give ZhiBan a \"key\"",
      "welcome.api.step": "Step {n} of 3 · Configure API",
      "welcome.api.lead": "ZhiBan has no built-in AI — generating courses and answering questions both call a cloud LLM",
      "welcome.api.desc": "So you need an \"API Key\" — the key to your account at an LLM provider. Every provider has a free quota; a few yuan lasts a long time. About 5 minutes total.",
      "welcome.api.step1": "Recommended: <b>DeepSeek</b> (cheap and capable): open <code>platform.deepseek.com</code>, sign up, create a key under \"API Keys\" and <b>copy it right away</b> (shown only once)",
      "welcome.api.step2": "For more natural cloud read-aloud, also add a <b>StepFun</b> voice key (steps in the \"Help & how-to\" page in the top bar)",
      "welcome.api.step3": "Back in ZhiBan's <b>Settings</b> page, fill in the URL, model name, and key, then click \"Test connection\"",
      "welcome.api.note": "It works without a key too: built-in read-aloud, document parsing, and search all run locally; only \"generate / ask\" needs the key. You can add it in Settings anytime later.",
      "welcome.api.detected": "✅ API detected: <b>{model}</b> (editable in Settings anytime)",
      "welcome.api.go_settings": "Open Settings to configure",
      "welcome.api.edit_settings": "Open Settings to edit",
      "welcome.api.skip": "Skip for now, I'll configure it myself",
      "welcome.api.finish": "Finish onboarding, go to Workbench",
    },
  });

  // ── 介绍解说音频（模块级持有，任何离开介绍的路径都能立刻停掉）─────────
  // 用户 2026-09-22 实测两个问题：① 打开不自动播（Chromium 默认禁无手势
  // 自动播放，启动器已加 --autoplay-policy=no-user-gesture-required 放行，
  // 这里仍保留手势兜底）；② 点「下一步」后解说还在后台放（切步骤/切页面
  // 没有停止逻辑）—— stopNarration 在 go/finish/离开 #/welcome 时都会调。
  const NARR_SRC = "/static/assets/welcome.mp3";
  let _narr = null;            // 当前 Audio 元素（null = 没在放）
  let _narrGestureArmed = false;

  function stopNarration() {
    const a = _narr;
    _narr = null;
    if (!a) return;
    try { a.pause(); a.currentTime = 0; } catch (e) { /* 忽略 */ }
    try { a.removeAttribute("src"); a.load(); } catch (e) { /* 释放解码器，忽略 */ }
  }

  // 自动播放被浏览器策略拦下时（开发态/普通浏览器窗口）：挂一次性手势，
  // 用户第一次点击或按键时接着放，不用非得找到播放按钮。
  function armNarrGestureFallback() {
    if (_narrGestureArmed) return;
    _narrGestureArmed = true;
    const kick = () => {
      document.removeEventListener("pointerdown", kick, true);
      document.removeEventListener("keydown", kick, true);
      if (_narr) { try { _narr.play().catch(() => {}); } catch (e) { /* 忽略 */ } }
    };
    document.addEventListener("pointerdown", kick, true);
    document.addEventListener("keydown", kick, true);
  }

  // 离开向导（hash 不再指向 #/welcome）→ 立即停掉解说，不在后台继续放。
  window.addEventListener("hashchange", () => {
    if ((location.hash || "").indexOf("#/welcome") !== 0) stopNarration();
  });

  async function render(host) {
    stopNarration();          // 重入向导：先停掉上一轮的解说，避免重叠
    step = "intro";
    answers = {};
    try {
      const cfg = await Api.get("/api/settings");
      _cfg = cfg;
      const g = (cfg && cfg.guide) || {};
      if (STEPS.indexOf(g.step) >= 0) step = g.step;
      answers = window.Profile.fromSettings(cfg);
    } catch (e) { /* 读不到设置就从第一步开始，不阻塞 */ }
    paint(host);
  }

  function saveStep(next, done) {
    return Api.put("/api/settings", {
      guide: { step: next || "", done: !!done },
    }).then(() => window.Main.refreshModelBadge())
      .catch(() => { /* 存断点失败不阻塞流程 */ });
  }

  function go(host, next) {
    stopNarration();          // 切步骤：立即停掉未读完的解说（用户 2026-09-22 实测）
    step = next;
    saveStep(next, false);   // 断点落库：刷新/重开从当前步继续
    paint(host);
  }

  function finish() {
    stopNarration();
    saveStep("", true).then(() => { location.hash = "#/workbench"; });
  }

  function paint(host) {
    if (step === "intro") paintIntro(host);
    else if (step === "profile") paintProfile(host);
    else paintApi(host);
  }

  /* ── 第 1 步：认识界面（截图 + 预录解说）──────────────────── */
  function paintIntro(host) {
    host.innerHTML = `
<div class="welcome">
  <div class="welcome-head">
    <b>${t("welcome.intro.title")}</b>
    <span class="welcome-step">${t("welcome.intro.step", { n: 1 })}</span>
  </div>
  <p class="hint" style="margin:0">
    ${t("welcome.intro.desc")}
  </p>
  <img class="welcome-img" src="/static/assets/intro.png" alt="${t("welcome.intro.img_alt")}">
  <div class="welcome-audio">
    <button class="btn small" id="w-play">${t("welcome.intro.play")}</button>
    <span class="hint" id="w-audio-hint">${t("welcome.intro.hint")}</span>
  </div>
  <div class="welcome-foot">
    <button class="btn" id="w-skip">${t("welcome.intro.skip")}</button>
    <button class="btn primary" id="w-next">${t("welcome.intro.next")}</button>
  </div>
</div>`;

    // 预录解说：进入本步即自动播放（启动器已放行自动播放策略）；
    // 被拦下时挂一次性手势兜底，并保留手动按钮。
    const hint = host.querySelector("#w-audio-hint");
    const ensure = () => {
      if (!_narr) {
        _narr = new Audio(NARR_SRC);
        _narr.preload = "auto";
      }
      return _narr;
    };
    const play = () => {
      let a;
      try { a = ensure(); } catch (e) { return; }
      try {
        a.currentTime = 0;
        const p = a.play();
        if (p && p.catch) p.catch(() => {
          armNarrGestureFallback();
          if (hint) hint.textContent = t("welcome.intro.hint.blocked");
        });
      } catch (e) { /* 老浏览器忽略 */ }
    };
    play();
    host.querySelector("#w-play").onclick = play;
    host.querySelector("#w-skip").onclick = finish;
    host.querySelector("#w-next").onclick = () => go(host, "profile");
  }

  /* ── 第 2 步：用户画像（一次一题，全可跳过）────────────────── */
  function paintProfile(host) {
    host.innerHTML = `
<div class="welcome">
  <div class="welcome-head">
    <b>${t("welcome.profile.title")}</b>
    <span class="welcome-step">${t("welcome.profile.step", { n: 2 })}</span>
  </div>
  <p class="hint" style="margin:0">
    ${t("welcome.profile.desc")}
  </p>
  <div id="w-quiz" class="welcome-quiz"></div>
  <div class="welcome-foot">
    <button class="btn" id="w-skip-all">${t("welcome.profile.skip_all")}</button>
    <button class="btn" id="w-back">${t("welcome.profile.back")}</button>
  </div>
</div>`;

    window.Profile.renderQuiz(host.querySelector("#w-quiz"), {
      answers,
      onDone: (a) => {
        answers = a;
        // 画像落库（逐题跳过的项传空串，不清空用户此前在设置页填过的值——
        // 空串在 update 里会覆盖，所以这里只传本次答了的项）。
        const payload = {};
        Object.keys(a).forEach((k) => {
          if (a[k]) payload[k] = a[k];   // 只传非空，未答/跳过的不覆盖
        });
        Api.put("/api/settings", { profile: payload })
          .catch(() => { /* 存盘失败不阻塞，进下一步 */ });
        go(host, "api");
      },
    });
    host.querySelector("#w-skip-all").onclick = () => go(host, "api");
    host.querySelector("#w-back").onclick = () => go(host, "intro");
  }

  /* ── 第 3 步：引导配 API（说明 + 跳设置页）──────────────────── */
  function paintApi(host) {
    // 已配好 API（base_url 与 model 都非空）→ 放行：直接完成引导 + 提供「去设置页修改」。
    // 未配好 → 保持原样（两按钮 id 不变：w-go 去设置页、w-later 暂时跳过）。
    const llm = (_cfg && _cfg.llm) || {};
    const ok = !!((llm.base_url || "").trim() && (llm.model || "").trim());
    const apiNote = ok
      ? `<p class="hint" style="margin-top:10px">${t("welcome.api.detected", { model: esc(llm.model) })}</p>`
      : "";
    host.innerHTML = `
<div class="welcome">
  <div class="welcome-head">
    <b>${t("welcome.api.title")}</b>
    <span class="welcome-step">${t("welcome.api.step", { n: 3 })}</span>
  </div>
  <div class="card">
    <b>${t("welcome.api.lead")}</b>
    <p class="hint" style="margin:8px 0 0">
      ${t("welcome.api.desc")}
    </p>
    <ol style="margin:10px 0 0;padding-left:22px;line-height:2">
      <li>${t("welcome.api.step1")}</li>
      <li>${t("welcome.api.step2")}</li>
      <li>${t("welcome.api.step3")}</li>
    </ol>
    <p class="hint" style="margin-top:10px">
      ${t("welcome.api.note")}
    </p>
    ${apiNote}
  </div>
  <div class="welcome-foot">
    <button class="btn" id="w-later">${ok ? t("welcome.api.edit_settings") : t("welcome.api.skip")}</button>
    <button class="btn primary" id="w-go">${ok ? t("welcome.api.finish") : t("welcome.api.go_settings")}</button>
  </div>
</div>`;

    if (ok) {
      // 已配好：主按钮直接完成引导；副按钮去设置页修改（沿用原 w-go 的跳转逻辑）。
      host.querySelector("#w-go").onclick = finish;
      host.querySelector("#w-later").onclick = () => {
        saveStep("api", false).then(() => {
          location.hash = "#/settings?focus=api";
        });
      };
    } else {
      host.querySelector("#w-go").onclick = () => {
        saveStep("api", false).then(() => {
          location.hash = "#/settings?focus=api";
        });
      };
      host.querySelector("#w-later").onclick = finish;
    }
  }

  window.Views = window.Views || {};
  window.Views.welcome = { render };
})();
