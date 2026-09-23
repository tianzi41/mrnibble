/* 界面语言：zh（默认）/ en。字典扁平 key，如 "nav.courses"。
 *
 * 切换 = window.I18n.switch(lang) → 写 localStorage → 重渲染当前路由。
 * 默认中文：老用户与全部现有测试（中文判据）零影响。
 *
 * 设计要点：
 * - DICT 是唯一语言真相源，单一文件。本期只收 main.js + 切换器自身用到的 key；
 *   后续视图改造按同一规范「只追加 key」，不与本期冲突。
 * - t(key, vars)：查字典；缺失时回退 key 本身（并在 console.warn 一次，方便抓漏翻）；
 *   支持 {name} 插值：gt("x", {name:"a"})。
 * - get()：读 localStorage，非 "en" 一律视作 zh（容错隐私模式 / 损坏值）。
 * - switch(lang)：写 localStorage；设 document.documentElement.lang；
 *   触发 window.dispatchEvent(new Event("zhiban-langchange")) 供调用方重渲染。
 * - merge(partial)：把视图级语言包并进 DICT（partial = {zh:{...}, en:{...}}），
 *   **已存在的 key 不覆盖**（先到先得）。视图语言包跟视图文件同体，
 *   多路并行改造互不抢同一文件；视图文件在 IIFE 内第一行调用即可。
 */
window.I18n = (function () {
  var KEY = "zhiban-lang";

  /* 字典最小集（本期）：只收 main.js + 切换器自身用到的 key。
     后续任务按需追加，不与本期冲突。 */
  var DICT = {
    zh: {
      // 应用标识（index.html 首屏静态 + main.js 运行期按语言刷新）
      "app.title": "知伴 · 本地 AI 学习伴侣",
      "app.tagline": "本地 AI 学习伴侣",

      // 顶栏导航（main.js ROUTES）
      "nav.courses": "课程",
      "nav.workbench": "工作台",
      "nav.generate": "资料生成",
      "nav.review": "闪卡复习",
      "nav.memory": "记忆",
      "nav.settings": "设置",
      "nav.help": "不会用，点这里",

      // 语言切换器自身
      "lang.label": "语言",

      // 模型徽标：朗读状态
      "badge.tts.off": "未设置",
      "badge.tts.cloud": "云端",
      "badge.tts.local": "本地",
      "badge.tts.system": "系统语音",
      // 模型徽标：状态文本
      "badge.no_model": "缺模型名",
      "badge.no_api": "未配置 API",
      "badge.disconnected": "服务未连接",
      // 模型徽标：正文「{model} · 朗读{tts}」
      "badge.read": "朗读{tts}",
      // 模型徽标：悬浮标题（\n 换行）
      "badge.title_ok": "已配置对话模型：{model}",
      "badge.title_no_model": "接口地址已填，但缺少模型名，无法提问 → 点击前往设置",
      "badge.title_no_api": "尚未配置 API（无法生成课程 / 无法提问）→ 点击前往设置",
      "badge.title_read": "朗读：{tts}（点击前往设置）",

      // 路由加载占位 / 视图渲染失败
      "loading": "加载中…",
      "view.error": "加载失败：",
    },
    en: {
      // App identity (static first paint in index.html; refreshed per language by main.js)
      "app.title": "ZhiBan · Local AI Learning Companion",
      "app.tagline": "Local AI Learning Companion",

      "nav.courses": "Courses",
      "nav.workbench": "Workbench",
      "nav.generate": "Materials",
      "nav.review": "Flashcards",
      "nav.memory": "Memory",
      "nav.settings": "Settings",
      "nav.help": "Help & how-to",

      "lang.label": "Language",

      "badge.tts.off": "Not set",
      "badge.tts.cloud": "Cloud",
      "badge.tts.local": "Local",
      "badge.tts.system": "System voice",
      "badge.no_model": "Model name missing",
      "badge.no_api": "API not configured",
      "badge.disconnected": "Service not connected",
      "badge.read": "Read: {tts}",
      "badge.title_ok": "Configured chat model: {model}",
      "badge.title_no_model": "API base URL set but model name missing — questions won't work → click to open Settings",
      "badge.title_no_api": "API not configured (can't generate courses / can't ask) → click to open Settings",
      "badge.title_read": "Read: {tts} (click to open Settings)",

      "loading": "Loading…",
      "view.error": "Failed to load: ",
    },
  };

  // 语言缓存：t() 每次调用都读 localStorage 是同步 IO，一页几百次会把
  // --dump-dom 的虚拟时间预算耗光（拍到异步渲染未完成的中间态）。
  var _lang = null;

  function get() {
    if (_lang) return _lang;
    try {
      _lang = localStorage.getItem(KEY) === "en" ? "en" : "zh";
    } catch (e) {
      _lang = "zh";
    }
    return _lang;
  }

  // 只 warn 一次的漏翻 key（避免刷屏）
  var _warned = {};

  function t(key, vars) {
    var lang = get();
    var dict = DICT[lang] || DICT.zh;
    var s = dict[key];
    if (s === undefined || s === null) {
      if (!_warned[key]) {
        _warned[key] = true;
        // eslint-disable-next-line no-console
        console.warn("[i18n] 漏翻 key:", key, "(lang=" + lang + ")");
      }
      return key; // 回退 key 本身，不抛异常
    }
    if (vars) {
      s = String(s).replace(/\{(\w+)\}/g, function (_m, k) {
        return (vars[k] === undefined || vars[k] === null) ? _m : String(vars[k]);
      });
    }
    return s;
  }

  /* merge(partial)：视图级语言包并入 DICT。partial = {zh:{...}, en:{...}}。
     已存在的 key 不覆盖（先到先得：核心字典优先，视图包只补新增）。
     并行改造时各视图包随自己的文件走，避免多路抢写同一文件。
     只有 en 的视图包（中文即 key 模式）：把 key 自身补进 DICT.zh，
     中文环境直接命中，不走「查不到 → warn」的慢路径。 */
  function merge(partial) {
    if (!partial || typeof partial !== "object") return;
    ["zh", "en"].forEach(function (lang) {
      var add = partial[lang];
      if (!add || typeof add !== "object") return;
      var dict = DICT[lang] || (DICT[lang] = {});
      Object.keys(add).forEach(function (k) {
        if (dict[k] === undefined) dict[k] = add[k];
      });
    });
    if (partial.en && !partial.zh) {
      var zhDict = DICT.zh;
      Object.keys(partial.en).forEach(function (k) {
        if (zhDict[k] === undefined) zhDict[k] = k;
      });
    }
  }

  function switchTo(lang) {
    lang = (lang === "en") ? "en" : "zh";
    _lang = lang;
    try {
      localStorage.setItem(KEY, lang);
    } catch (e) { /* 隐私模式等，忽略 */ }
    try {
      document.documentElement.setAttribute("lang", lang);
    } catch (e) { /* 极端环境，忽略 */ }
    // 通知所有调用方（含 main.js 的重渲染）做刷新
    try {
      window.dispatchEvent(new Event("zhiban-langchange"));
    } catch (e) { /* 不支持 Event 的浏览器忽略 */ }
  }

  return { get: get, t: t, switch: switchTo, merge: merge, KEY: KEY, DICT: DICT };
})();
