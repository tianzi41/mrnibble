/* 新手引导（首次启动三步）：认识界面 → 用户画像 → 引导配 API。
 *
 * 触发与路由见 main.js：guide.done 非真时，除 #/welcome 外的页面都会被送到这里。
 * 三步都能单独跳过；guide.step 记录断点，刷新/重开从当前步继续。
 * 素材：src/web/assets/intro.png（带红框的界面图）+ welcome.mp3（预录解说）。
 */
(function () {
  "use strict";

  const STEPS = ["intro", "profile", "api"];
  let step = "intro";
  let answers = {};

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
    <b>欢迎使用知伴</b>
    <span class="welcome-step">第 1 步 / 共 3 步 · 认识界面</span>
  </div>
  <p class="hint" style="margin:0">
    知伴把你的资料变成一堂课：上传文件 → 自动生成课件 → 像老师一样讲给你听。
    下图是上课界面，四个红框分别是：
  </p>
  <img class="welcome-img" src="/static/assets/intro.png" alt="上课界面">
  <div class="welcome-audio">
    <button class="btn small" id="w-play">🔊 播放解说</button>
    <span class="hint" id="w-audio-hint">解说不自动播放时，点这个按钮</span>
  </div>
  <div class="welcome-foot">
    <button class="btn" id="w-skip">跳过引导</button>
    <button class="btn primary" id="w-next">下一步</button>
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
          if (hint) hint.textContent = "浏览器拦了自动播放：点页面任意位置即开始解说";
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
    <b>花一分钟了解你</b>
    <span class="welcome-step">第 2 步 / 共 3 步 · 用户画像</span>
  </div>
  <p class="hint" style="margin:0">
    回答几个选择题，之后生成的课程会按你的情况调整难度、举例和讲法。
    每一题都能跳过，也可以整段跳过。
  </p>
  <div id="w-quiz" class="welcome-quiz"></div>
  <div class="welcome-foot">
    <button class="btn" id="w-skip-all">整段跳过</button>
    <button class="btn" id="w-back">上一步</button>
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
    host.innerHTML = `
<div class="welcome">
  <div class="welcome-head">
    <b>最后一步：给知伴配一把「钥匙」</b>
    <span class="welcome-step">第 3 步 / 共 3 步 · 配置接口</span>
  </div>
  <div class="card">
    <b>知伴自己不含 AI 能力，生成课程、回答问题都要调用云端大模型</b>
    <p class="hint" style="margin:8px 0 0">
      所以需要一个「API Key」——相当于你在大模型服务商那里的账号钥匙。
      各家都有免费额度，几元钱能用很久，全程约 5 分钟。
    </p>
    <ol style="margin:10px 0 0;padding-left:22px;line-height:2">
      <li>推荐 <b>DeepSeek</b>（便宜、够用）：打开 <code>platform.deepseek.com</code> 注册，
          在「API Keys」里创建并<b>立即复制</b>（只显示一次）</li>
      <li>想用更自然的云端朗读，再配一个<b>阶跃星辰</b>的语音 Key
          （步骤见顶栏「不会用，点这里」帮助页）</li>
      <li>回到知伴<b>设置</b>页，把地址、模型名、Key 填进去，点「测试连接」</li>
    </ol>
    <p class="hint" style="margin-top:10px">
      不配也能用：内置朗读、资料解析、检索都在本机完成；只有「生成 / 提问」需要钥匙。
      之后随时可以在设置页补上。
    </p>
  </div>
  <div class="welcome-foot">
    <button class="btn" id="w-later">暂时跳过，我自己去配</button>
    <button class="btn primary" id="w-go">去设置页配置</button>
  </div>
</div>`;

    host.querySelector("#w-go").onclick = () => {
      saveStep("api", false).then(() => {
        location.hash = "#/settings?focus=api";
      });
    };
    host.querySelector("#w-later").onclick = finish;
  }

  window.Views = window.Views || {};
  window.Views.welcome = { render };
})();
