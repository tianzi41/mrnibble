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
      const base = (cfg.llm.base_url || "").trim();
      const model = (cfg.llm.model || "").trim();
      const ok = !!(base && model);
      // 区分「完全没配」与「只差模型名」——后者最容易让人以为已经配好了。
      const missing = !base ? "base_url" : (!model ? "model" : "");
      badge.textContent = ok ? model
        : (missing === "model" ? "缺模型名" : "未配置模型");
      badge.dataset.ok = ok ? "1" : "";
      badge.dataset.missing = missing;
      badge.style.color = ok ? "var(--ok)" : "var(--warn)";
      badge.style.cursor = "pointer";
      badge.title = ok
        ? "已配置对话模型：" + model
        : (missing === "model"
          ? "接口地址已填，但缺少模型名，无法提问 → 点击前往设置"
          : "尚未配置对话模型 → 点击前往设置");
      badge.onclick = () => { location.hash = "#/settings"; };
      dot.classList.add("on");
    } catch (e) {
      badge.textContent = "服务未连接";
      badge.dataset.ok = "";
      badge.dataset.missing = "service";
      dot.classList.remove("on");
    }
  }
  window.Main = { refreshModelBadge };

  async function route() {
    const hash = location.hash || "#/courses";
    const found = resolve(hash);
    [...document.querySelectorAll("#nav button")].forEach((b) =>
      b.classList.toggle("active", (hash || "").startsWith(b.dataset.hash)));
    const host = document.getElementById("view");
    host.innerHTML = '<div class="empty" style="width:100%">加载中…</div>';
    try {
      await window.Views[found.view].render(host, found.arg);
    } catch (e) {
      // data-view-error：给自动化冒烟测试一个稳定的「视图渲染失败」标记，
      // 避免用中文文案做子串匹配（源码注释里也可能出现同样的词）。
      host.innerHTML = `<div class="empty" data-view-error="1" style="width:100%;text-align:left;max-width:900px;margin:0 auto">加载失败：${e.message}
        <pre style="white-space:pre-wrap;font-size:12px;color:#888">${String(e.stack || "").replace(/</g, "&lt;")}</pre></div>`;
    }
  }

  buildNav();
  window.addEventListener("hashchange", route);
  refreshModelBadge().then(route);

  // 页面心跳：桌面启动器据此判断应用窗口是否仍然打开。
  // （Edge 首开可能把 URL 转交给已有实例后立刻退出，启动器不能再以浏览器
  //  进程的存活来判断窗口；没有心跳 ≈ 页面已关闭，服务随之退出。）
  function ping() {
    fetch("/api/heartbeat", { method: "POST" }).catch(function () { /* 忽略瞬时失败 */ });
  }
  ping();
  setInterval(ping, 5000);
})();
