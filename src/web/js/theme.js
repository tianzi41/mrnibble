/* 外观（主题）：默认浅色 / 米黄（护眼）/ 豆绿（护眼）/ 暗色 + 跟随系统。
 *
 * 为什么用 data-theme 属性而不是逐处改样式：app.css 里所有颜色都已收敛为 CSS 变量，
 * 主题只覆盖同名变量即可，组件样式一行都不用动。
 *
 * 首屏防闪由 index.html 的 <head> 里那段内联脚本负责（在样式加载前设好 data-theme）；
 * 这里负责「下拉联动 + 持久化 + 跟随系统变化」，并在页面加载后再应用一次（幂等）。
 */
(function () {
  const KEY = "zhiban-theme";
  const OPTS = [
    ["", "默认（浅色）"],
    ["sepia", "米黄（护眼）"],
    ["green", "豆绿（护眼）"],
    ["dark", "暗色"],
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

  const sel = document.getElementById("theme-sel");
  if (sel) {
    sel.innerHTML = OPTS.map(([v, n]) => `<option value="${v}">${n}</option>`).join("")
      + '<option value="auto">跟随系统</option>';
    sel.value = stored();
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
  window.Theme = { apply, current: stored, options: OPTS.map(([v]) => v) };
})();
