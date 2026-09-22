/* 应用入口：hash 路由 + 顶栏 + 全局 Toast。 */
(function () {
  "use strict";

  // 课程相关页面带参数（#/courses / #/lessons/{id} / #/practice/{id}），
  // 因此路由解析按「前缀匹配」，而不是整串相等。
  const ROUTES = [
    ["#/courses", "课程", "courses"],
    ["#/workbench", "工作台", "workbench"],
    ["#/generate", "资料生成", "generate"],
    ["#/review", "闪卡复习", "review"],
    ["#/memory", "记忆", "memory"],
    ["#/settings", "设置", "settings"],
  ["#/help", "不会用，点这里", "help"],
  ];

  // 带参数的课程子页：#/lessons/{id}、#/practice/{id}
  const PARAM_ROUTES = [
    [/^#\/lessons\/([^/?#]+)/, "lesson"],
    [/^#\/practice\/([^/?#]+)/, "practice"],
  ];

  function resolve(hash) {
    for (const [re, view] of PARAM_ROUTES) {
      const m = re.exec(hash || "");
      if (m) return { view, arg: m[1] };
    }
    const found = (ROUTES.find(([h]) => (hash || "").startsWith(h))) || ROUTES[0];
    return { view: found[2], arg: null };
  }

  const Toast = (msg, isErr) => {
    const t = document.getElementById("toast");
    t.textContent = msg;
    t.className = "toast show" + (isErr ? " err" : "");
    clearTimeout(t._t);
    t._t = setTimeout(() => { t.className = "toast"; }, isErr ? 4200 : 2400);
  };
  window.Toast = Toast;

  function buildNav() {
    const nav = document.getElementById("nav");
    nav.innerHTML = "";
    ROUTES.forEach(([hash, label]) => {
      const b = document.createElement("button");
      b.textContent = label;
      b.dataset.hash = hash;
      b.onclick = () => { location.hash = hash; };
      nav.appendChild(b);
    });
  }

  async function refreshModelBadge() {
    const badge = document.getElementById("model-badge");
    const dot = document.getElementById("conn-dot");
    try {
      const cfg = await Api.get("/api/settings");
      // 新手引导完成度：route() 的首次启动拦截据此判断（拿不到就不拦，
      // 避免服务异常时把用户锁在向导外进不去主界面）。
      _guideDone = !!((cfg.guide || {}).done);
      const base = (cfg.llm.base_url || "").trim();
      const model = (cfg.llm.model || "").trim();
      const ok = !!(base && model);
      // 区分「完全没配」与「只差模型名」——后者最容易让人以为已经配好了。
      const missing = !base ? "base_url" : (!model ? "model" : "");
      // 朗读状态一并显示：不必切到设置页才知道会不会出声。
      const tts = cfg.tts || {};
      const ttsLabel = !tts.enabled ? "未设置"
        : (tts.mode === "cloud" ? "云端"
          : (tts.local_engine === "melo" ? "本地" : "系统语音"));
      badge.textContent = (ok ? model : (missing === "model" ? "缺模型名" : "未配置 API"))
        + ` · 朗读${ttsLabel}`;
      badge.dataset.ok = ok ? "1" : "";
      badge.dataset.missing = missing;
      // 未配 API 用红色（原来是 warn 琥珀，不够「一眼看出就是不能用」）
      badge.style.color = ok ? "var(--ok)" : "var(--bad)";
      badge.style.cursor = "pointer";
      badge.title = (ok
        ? "已配置对话模型：" + model
        : (missing === "model"
          ? "接口地址已填，但缺少模型名，无法提问 → 点击前往设置"
          : "尚未配置 API（无法生成课程 / 无法提问）→ 点击前往设置"))
        + "\n朗读：" + ttsLabel + "（点击前往设置）";
      badge.onclick = () => { location.hash = "#/settings"; };
      // 小圆点跟随「到底能不能用」：未配 API 时保持红色，不再无条件点亮。
      dot.classList.toggle("on", ok);
    } catch (e) {
      badge.textContent = "服务未连接";
      badge.dataset.ok = "";
      badge.dataset.missing = "service";
      dot.classList.remove("on");
    }
  }
  window.Main = { refreshModelBadge };

  // 新手引导完成度（null=还没查到；true=已完成不拦；false=未完成，拦去向导）。
  let _guideDone = null;

  async function route() {
    const hash = location.hash || "#/courses";
    const found = resolve(hash);
    [...document.querySelectorAll("#nav button")].forEach((b) =>
      b.classList.toggle("active", (hash || "").startsWith(b.dataset.hash)));
    const host = document.getElementById("view");

    // 新手引导：未完成时，除向导自身与设置页外的页面都送去 #/welcome
    // （不重入向导，防死循环）。设置页放行：第 3 步「去设置页配置」的出口，
    // 用户也可能在引导中途主动去配 API。
    const isWelcome = hash.indexOf("#/welcome") === 0;
    const isSettings = hash.indexOf("#/settings") === 0;
    if (!isWelcome && !isSettings && _guideDone === false) {
      location.hash = "#/welcome";
      return;
    }
    host.innerHTML = '<div class="empty" style="width:100%">加载中…</div>';
    try {
      // 向导页不在 ROUTES 里（不进导航栏），显式分支渲染。
      if (isWelcome) {
        await window.Views.welcome.render(host);
        return;
      }
      await window.Views[found.view].render(host, found.arg);
    } catch (e) {
      // data-view-error：给自动化冒烟测试一个稳定的「视图渲染失败」标记，
      // 避免用中文文案做子串匹配（源码注释里也可能出现同样的词）。
      host.innerHTML = `<div class="empty" data-view-error="1" style="width:100%;text-align:left;max-width:900px;margin:0 auto">加载失败：${String(e.message || "").replace(/</g, "&lt;")}
        <pre style="white-space:pre-wrap;font-size:12px;color:#888">${String(e.stack || "").replace(/</g, "&lt;")}</pre></div>`;
    }
  }

  buildNav();
  window.addEventListener("hashchange", route);
  refreshModelBadge().then(route);

  // 页面心跳：桌面启动器据此判断应用窗口是否仍然打开。
  // （Edge 首开可能把 URL 转交给已有实例后立刻退出，启动器不能再以浏览器
  //  进程的存活来判断窗口；没有心跳 ≈ 页面已关闭，服务随之退出。）
  // ⚠️ 心跳不是唯一判据：窗口最小化时浏览器会节流隐藏页的定时器，心跳可能被
  //  拉长到十几秒，所以启动器还会独立探测应用窗口是否存在（见 src/launcher.py）。
  function ping() {
    fetch("/api/heartbeat", { method: "POST" }).catch(function () { /* 忽略瞬时失败 */ });
  }
  ping();
  setInterval(ping, 5000);

  // 真正关窗 / 导航离开时，用 sendBeacon 发一次「告别」：启动器据此立刻停机，
  // 不用等心跳宽限。（sendBeacon 在页面卸载时也能可靠发出，fetch 则可能被取消。）
  window.addEventListener("pagehide", function () {
    try {
      navigator.sendBeacon("/api/heartbeat?bye=1");
    } catch (e) { /* 老浏览器忽略 */ }
  });
})();
