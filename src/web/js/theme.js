/* 外观（主题）：默认浅色 / 米黄（护眼）/ 豆绿（护眼）/ 暗色 + 跟随系统。
 *
 * 为什么用 data-theme 属性而不是逐处改样式：app.css 里所有颜色都已收敛为 CSS 变量，
 * 主题只覆盖同名变量即可，组件样式一行都不用动。
 *
 * 首屏防闪由 index.html 的 <head> 里那段内联脚本负责（在样式加载前设好 data-theme）；
 * 这里负责「下拉联动 + 持久化 + 跟随系统变化」，并在页面加载后再应用一次（幂等）。
 * 双语：主题名语言包随本文件走；语言切换时由 main.js 的 langchange 处理器调 refresh()。
 */
(function () {
  const gt = (k, v) => window.I18n ? window.I18n.t(k, v) : k;

  window.I18n && window.I18n.merge({
    zh: {
      "theme.default": "默认（浅色）",
      "theme.sepia": "米黄（护眼）",
      "theme.green": "豆绿（护眼）",
      "theme.dark": "暗色",
      "theme.auto": "跟随系统",
    },
    en: {
      "theme.default": "Default (light)",
      "theme.sepia": "Sepia (eye-care)",
      "theme.green": "Bean green (eye-care)",
      "theme.dark": "Dark",
      "theme.auto": "Follow system",
    },
  });

  const KEY = "zhiban-theme";
  // value → 语言包 key
  const OPTS = [
    ["", "theme.default"],
    ["sepia", "theme.sepia"],
    ["green", "theme.green"],
    ["dark", "theme.dark"],
  ];
  const mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function stored() {
    try { return localStorage.getItem(KEY) || "auto"; } catch (e) { return "auto"; }
  }
  function resolve(v) {
    if (v === "auto") return mq && mq.matches ? "dark" : "";
    return v || "";
  }
  function apply(v) {
    const t = resolve(v);
    if (t) document.documentElement.setAttribute("data-theme", t);
    else document.documentElement.removeAttribute("data-theme");
  }

  function paintSel() {
    const sel = document.getElementById("theme-sel");
    if (!sel) return;
    sel.innerHTML = OPTS.map(([v, k]) => `<option value="${v}">${gt(k)}</option>`).join("")
      + `<option value="auto">${gt("theme.auto")}</option>`;
    sel.value = stored();
  }

  const sel = document.getElementById("theme-sel");
  if (sel) {
    paintSel();
    sel.onchange = () => {
      try { localStorage.setItem(KEY, sel.value); } catch (e) { /* 隐私模式等，忽略 */ }
      apply(sel.value);
    };
  }

  // 「跟随系统」时要跟着系统切换（用户显式选过主题则不打扰）
  if (mq) {
    const onSys = () => { if (stored() === "auto") apply("auto"); };
    if (mq.addEventListener) mq.addEventListener("change", onSys);
    else if (mq.addListener) mq.addListener(onSys);
  }

  apply(stored());
  window.Theme = {
    apply, current: stored, refresh: paintSel,
    options: OPTS.map(([v]) => v),
  };
})();
