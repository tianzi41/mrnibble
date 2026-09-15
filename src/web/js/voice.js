/* 朗读（本地系统语音）与 Markdown → 朗读文本的清洗。

说明：这块逻辑原先写在 workbench 里，P1 的课堂页也要用（朗读讲义），
因此抽成公共模块，避免两处各写一份导致行为不一致。

2026-09-12 升级：支持长文本分段朗读（不再被 1500 字符硬截断），
用 ``gen`` 计数器保证 ``stop()`` 能中断整批队列。
*/
(function () {
  "use strict";

  // 语音列表是**异步**加载的：首次 getVoices() 常返回空数组，
  // 必须先等 onvoiceschanged，否则挑不到中文音色。
  function ensureVoices() {
    if (!window.speechSynthesis) return;
    const load = () => { speechSynthesis.getVoices(); };
    load();
    if (!speechSynthesis.getVoices().length) {
      speechSynthesis.onvoiceschanged = load;
    }
  }
  ensureVoices();

  function pickZhVoice() {
    const vs = (window.speechSynthesis && speechSynthesis.getVoices()) || [];
    return vs.find((v) => /zh[-_]?CN|cmn|Huihui|Yaoyao|Xiaoxiao|Kangkang|晓晓|慧慧/i.test(v.name + " " + v.lang))
      || vs.find((v) => /^zh/i.test(v.lang));
  }

  /** Markdown → 适合朗读的纯文本（去掉角标、代码块与符号）。 */
  function plainText(md) {
    return String(md || "")
      .replace(/\[\[c:\d+\]\]/g, "")
      .replace(/\[\d+\]/g, "")
      .replace(/```[\s\S]*?```/g, "（此处是代码块）")
      .replace(/`([^`]*)`/g, "$1")
      .replace(/[#*>`_~|]/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  /** 把长文本切成适合单次 utterance 的块（按句子边界）。 */
  function splitChunks(text, maxLen) {
    if (text.length <= maxLen) return [text];
    const chunks = [];
    let i = 0;
    while (i < text.length) {
      if (i + maxLen >= text.length) { chunks.push(text.slice(i)); break; }
      // 从末尾往前找句末标点或空格
      let j = i + maxLen;
      while (j > i && !/[。！？.\n ]/.test(text[j])) j--;
      if (j === i) j = i + maxLen; //  fallback：硬切
      chunks.push(text.slice(i, j + 1));
      i = j + 1;
    }
    return chunks;
  }

  /**
   * 按**句子边界**切块，相邻短句合并到不超过 ``maxLen``。
   *
   * 用于字幕同步：``chunkChars`` 模式下每次 ``onChunk`` 触发恰好对应
   * 一句（或两句）话，屏幕上的字幕就能跟着语音一句句更新，
   * 而不是一次把整页讲稿全铺出来。
   */
  function splitBySentence(text, maxLen) {
    const s = String(text || "");
    const parts = [];
    let start = 0;
    for (let i = 0; i < s.length; i++) {
      if ("。！？!?；;".indexOf(s[i]) >= 0) {
        const piece = s.slice(start, i + 1).trim();
        if (piece) parts.push(piece);
        start = i + 1;
      }
    }
    const tail = s.slice(start).trim();
    if (tail) parts.push(tail);
    if (!parts.length) return [s];

    const out = [];
    let buf = "";
    parts.forEach((p) => {
      if (p.length > maxLen) {
        if (buf) { out.push(buf); buf = ""; }
        for (let i = 0; i < p.length; i += maxLen) out.push(p.slice(i, i + maxLen));
        return;
      }
      if (buf && (buf + p).length > maxLen) { out.push(buf); buf = p; }
      else buf += p;
    });
    if (buf) out.push(buf);
    return out;
  }

  // 代数计数：++gen 可让上一批的回调全部失效（stop() 时用）。
  let _gen = 0;

  // 本地引擎：system=浏览器系统语音（默认，零依赖）；melo=后端 MeloTTS 神经语音；cloud=云端 OpenAI 兼容 /audio/speech。
  let _engine = "system";
  // cloud 模式下「为何没真正走云端」的原因（缺配置/失败）。供 UI 显示，绝不静默换成别的引擎。
  let _cloudReason = "";
  // melo 模式下正在播放的 Audio（stop() 时要掐掉）。
  let _audio = null;
  // 合成结果缓存（跨页预取）：key=原文文本，value={ok,v} 或 promise。
  // 命中即秒回，页间切换不再等第一段合成（朗读偶发停顿的主源）。
  const _prime = new Map();
  const PRIME_MAX = 32;

  function fetchCached(text, fetcher) {
    if (_prime.has(text)) return _prime.get(text);
    const pr = fetcher(text).then(
      (v) => ({ ok: true, v }),
      (e) => ({ ok: false, e }));
    _prime.set(text, pr);
    if (_prime.size > PRIME_MAX) {
      const first = _prime.keys().next().value;
      _prime.delete(first);
    }
    return pr;
  }

  /** 预热：把一段讲稿按 speak 相同的规则切块并提前合成（不播放）。
   *  lesson.js 在朗读第 i 页时对第 i+1 页调用，页间切换即命中缓存。 */
  function prime(text, opts) {
    try {
      opts = opts || {};
      // ⚠️ 切块规则必须与 speak() **逐字一致**（detailed 由 chunkChars 推导，
      // 不让调用方传——曾经 prime 恒用 splitChunks、speak 字幕模式用
      // splitBySentence，缓存永远不命中，页间冷启动合成就是「段落间停 2 秒」）。
      const hardMax = _engine === "system" ? 1200 : (_engine === "melo" ? 120 : 1200);
      const detailed = Number(opts.chunkChars) > 0;
      const chunkMax = detailed ? Math.min(Number(opts.chunkChars), hardMax) : hardMax;
      // 预热总量上限：melo 是 CPU 合成，预热太多会抢正在播放页的合成资源；
      // splitBySentence 贪心顺序切分对前缀稳定，前 N 字的切块结果与全文一致。
      const capTotal = _engine === "melo" ? 1200 : 4000;
      const capped = String(text || "").slice(0, capTotal);
      const pieces = detailed ? splitBySentence(capped, chunkMax) : splitChunks(capped, chunkMax);
      const fetchOne = _engine === "cloud"
        ? requestCloud
        : (t) => requestLocal(t).then((b) => ({ blob: b, mime: "audio/wav" }));
      pieces.forEach((pc) => { try { fetchCached(pc, fetchOne); } catch (e) { /* 忽略 */ } });
    } catch (e) { /* 预热失败不影响播放 */ }
  }
  // 用户主动暂停：melo 在两段合成之间有间隙，此时 _audio 为 null，
  // 只 pause 当前 Audio 会漏掉间隙，所以要用 _hold 让「下一段」先等一等。
  let _hold = false;

  function configure(opts) {
    opts = opts || {};
    if (opts.engine === "system" || opts.engine === "melo") _engine = opts.engine;
  }

  /** 当前引擎（供界面显示）。 */
  function engine() { return _engine; }

  /**
   * 从后端读取朗读设置并据此选择引擎。
   *
   * - 云端模式：引擎就是 cloud。缺配置时**只记原因、不换引擎**——静默改用系统
   *   语音正是用户这次抱怨的根源（降级后用户分不清是配置错还是软件 bug）。
   * - 本地模式：仅当「melo 引擎 + 模型就位」才用 MeloTTS，否则系统语音。
   */
  async function syncFromServer() {
    try {
      const st = (await Api.get("/api/tts/status")) || {};
      if (st.enabled && st.mode === "cloud") {
        // 云端模式：引擎就是 cloud。缺配置时**只记原因，不换引擎**——
        // 静默改用系统语音正是用户这次抱怨的根源。
        _engine = "cloud";
        _cloudReason = st.cloud_configured ? "" : "云端朗读尚未配置（缺少语音端点或模型名）";
      } else if (st.enabled && st.mode === "local"
                 && st.local_engine === "melo" && st.local_model_available) {
        _engine = "melo"; _cloudReason = "";
      } else {
        _engine = "system"; _cloudReason = "";
      }
    } catch (e) {
      _engine = "system"; _cloudReason = "";
    }
    return _engine;
  }

  /** 当前云端未生效的原因（供 UI 显示）。云端未启用时返回空串。 */
  function cloudIssue() { return _cloudReason; }

  /** 调后端本地合成（MeloTTS），返回 WAV 的 ArrayBuffer。 */
  async function requestLocal(text) {
    const resp = await fetch("/api/tts/local", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    return await resp.arrayBuffer();
  }

  /** 调后端云端合成（OpenAI 兼容 /audio/speech），返回 {blob, mime}。 */
  async function requestCloud(text) {
    const resp = await fetch("/api/tts/speech", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!resp.ok) {
      // 后端错误封套是 {code, message, data, error:{detail}}：上游端点的真实原因
      // （如「语音端点返回 HTTP 404」）在 **error.detail** 里，message 只是
      // 「TTS 合成失败」这类摘要。两个都要带上，否则用户查不出是配置错还是软件问题。
      let summary = "", detail = "";
      try {
        const j = (await resp.json()) || {};
        summary = j.message || "";
        detail = (j.error && j.error.detail) || j.detail || "";
      } catch (e) { /* 非 JSON 响应 */ }
      const extra = [summary, detail].filter(Boolean).join(" —— ");
      throw new Error("HTTP " + resp.status + (extra ? "：" + extra : ""));
    }
    const mime = (resp.headers.get("content-type") || "audio/mpeg").split(";")[0].trim();
    return { blob: await resp.blob(), mime };
  }

  /**
   * 播放一段音频：src 可以是 ArrayBuffer 或 Blob；mime 指定 MIME（默认 audio/wav）。
   * 播放结束或出错都走同一个 done() 并 resolve（不卡住队列）。
   * 语义保持：设 _audio、_audio === a 时才清空、URL.revokeObjectURL。
   */
  function playAudio(src, mime) {
    mime = mime || "audio/wav";
    return new Promise((resolve) => {
      try {
        const blob = (src instanceof Blob) ? src : new Blob([src], { type: mime });
        const url = URL.createObjectURL(blob);
        const a = new Audio(url);
        _audio = a;
        const done = () => {
          URL.revokeObjectURL(url);
          if (_audio === a) _audio = null;
          resolve();
        };
        a.onended = done;
        a.onerror = done;
        a.play().catch(done);
      } catch (e) { resolve(); }
    });
  }

  /**
   * 朗读一段文本。
   * - ``system`` 引擎：浏览器 speechSynthesis（零外发）
   * - ``melo`` 引擎：后端本地 MeloTTS 合成 WAV 后逐段播放（同样是零外发）
   *
   * @param {string} text Markdown 或纯文本
   * @param {{onError?: (msg: string) => void, onWarn?: (msg: string) => void,
   *          onChunk?: (text: string, index: number, total: number) => void,
   *          onEnd?: () => void, chunkChars?: number}} opts
   *
   * ``onChunk`` 在每一段开始朗读前触发（可用于「实时显示正在讲什么」）；
   * ``onEnd`` 在整段读完时触发（可被上层用来推进到下一页课件）。
   * 被 ``stop()`` 中断时两者都不会再触发。
   *
   * ``chunkChars`` 指定时改用**按句子切块**，让 ``onChunk`` 的粒度落在
   * 一句话上（课堂字幕需要「说一句、显示一句」），不指定则沿用大块切分。
   */
  async function speak(text, opts) {
    opts = opts || {};
    const warn = opts.onWarn || function () { };
    const fail = opts.onError || function () { };
    const clean = plainText(text);
    if (!clean) return;
    // 总长 20000 字为安全上限（避免误塞过长文本）
    const capped = clean.length > 20000 ? clean.slice(0, 20000) + " …（内容较长，后续省略）" : clean;

    const myGen = ++_gen;

    // 单块上限：melo 后端单段上限 300 字，这里切得更保守；system/cloud 单次 1200 字。
    const hardMax = _engine === "system" ? 1200 : (_engine === "melo" ? 120 : 1200);
    const detailed = Number(opts.chunkChars) > 0;
    const chunkMax = detailed ? Math.min(Number(opts.chunkChars), hardMax) : hardMax;
    const cut = (t) => (detailed ? splitBySentence(t, chunkMax) : splitChunks(t, chunkMax));

    // ── 需要「先请求合成、再播放」的引擎（melo / cloud）走同一条**预取流水线** ──
    // 串行「合成完再播、播完再合成」会把整段合成耗时原样暴露成段间停顿
    // （实测云端单句合成 ~2.2–2.5s，用户听到的「停顿 2.5 秒」就是它）。
    // 改成：边播第 i 段，边提前把 i+1 / i+2 段合成好，
    // 段间停顿 ≈ max(0, 合成耗时 − 本段播放耗时)，正常情况为 0。
    if (_engine === "melo" || _engine === "cloud") {
      const isCloud = _engine === "cloud";
      if (isCloud && _cloudReason) {
        fail(_cloudReason + "。请到「设置 → 语音」填写语音端点与模型名，"
             + "点「测试连接」确认可用后再试。");
        return;
      }
      const chunks = cut(capped);
      // 预取深度：云端单句合成 2~3s、有抖动，深度 2 时偶尔跟不上播放
      //（实测表现为连续几段后停 2~3 秒、字幕回退到「准备开始…」）。
      // 云端提到 4 路并行（合成速率 ≈ 播放速率 2 倍）；本地 melo 是 CPU 合成，
      // 3 路已足够且避免抢主线程。
      const PREFETCH = isCloud ? 4 : 3;
      const fetchOne = isCloud
        ? requestCloud
        : (t) => requestLocal(t).then((b) => ({ blob: b, mime: "audio/wav" }));
      // 包成「永不 reject」的形状：预取中的请求若失败，在有人 await 它之前
      // 不会产生未处理的 Promise 拒绝。
      const inflight = new Map();
      const kick = (i) => {
        if (i < 0 || i >= chunks.length || inflight.has(i)) return;
        inflight.set(i, fetchOne(chunks[i]).then(
          (v) => ({ ok: true, v }),
          (e) => ({ ok: false, e })));
      };
      for (let k = 0; k < PREFETCH; k++) kick(k);
      for (let i = 0; i < chunks.length; i++) {
        if (myGen !== _gen) return;
        const got = await inflight.get(i);
        inflight.delete(i);
        // 先补上后面的预取，再等暂停/播放 —— 让网络请求与播放真正并行
        kick(i + PREFETCH);
        if (!got.ok) {
          if (myGen !== _gen) return;
          fail((isCloud ? "云端朗读失败：" : "本地语音合成失败：") + got.e.message);
          return;                              // 明确失败，不降级
        }
        while (_hold && myGen === _gen) await new Promise((r) => setTimeout(r, 120));
        if (myGen !== _gen) return;
        if (opts.onChunk) opts.onChunk(chunks[i], i, chunks.length);
        await playAudio(got.v.blob, got.v.mime);
      }
      if (myGen === _gen && opts.onEnd) opts.onEnd();
      return;
    }

    if (!window.speechSynthesis) {
      fail("当前浏览器不支持本地朗读，可在「设置 → 语音」改用云端朗读");
      return;
    }
    const chunks = cut(capped);
    speechSynthesis.cancel();

    function speakNext(idx) {
      if (myGen !== _gen) return; // 已被 stop()
      if (idx >= chunks.length) { if (opts.onEnd) opts.onEnd(); return; }
      if (opts.onChunk) opts.onChunk(chunks[idx], idx, chunks.length);
      const u = new SpeechSynthesisUtterance(chunks[idx]);
      u.lang = "zh-CN";
      u.rate = 1.0;
      u.volume = 1.0;
      const v = pickZhVoice();
      if (v) u.voice = v;
      u.onerror = (e) => {
        if (myGen !== _gen) return;
        const err = (e && e.error) || "未知原因";
        if (err !== "interrupted" && err !== "canceled") fail("本地朗读失败：" + err);
      };
      u.onend = () => {
        if (myGen === _gen) speakNext(idx + 1);
      };
      speechSynthesis.speak(u);
      if (!v) {
        warn("未找到中文系统语音，朗读可能无声或发音不准；请在 Windows「设置 → 时间和语言 → 语音」添加中文语音包");
      }
    }
    speakNext(0);
  }

  function stop() {
    _prime.clear();   // 停止时清预热缓存，避免换讲次后误用旧内容
    _gen++;
    _hold = false;   // 停止时清掉暂停标志，否则下次朗读会被卡住
    // melo 引擎：掐掉正在播放的音频；system 引擎：取消语音队列。
    if (_audio) {
      try { _audio.pause(); _audio.currentTime = 0; } catch (e) { /* 忽略 */ }
      _audio = null;
    }
    if (window.speechSynthesis) speechSynthesis.cancel();
  }

  /**
   * 用户主动暂停朗读（区别于互动检查点）。
   * - system 引擎：speechSynthesis.pause / resume；
   * - melo 引擎：暂停当前 Audio，并用 :data:`_hold` 让「下一段」在恢复前不开始。
   */
  function pause() {
    _hold = true;
    // cloud 与 melo 都走 <audio> 播放，pause/resume 语义一致。
    if (_engine !== "system") {
      if (_audio) { try { _audio.pause(); } catch (e) { /* 忽略 */ } }
      return;
    }
    if (window.speechSynthesis) speechSynthesis.pause();
  }

  function resume() {
    _hold = false;
    if (_engine !== "system") {
      if (_audio) { try { _audio.play(); } catch (e) { /* 忽略 */ } }
      return;
    }
    if (window.speechSynthesis) speechSynthesis.resume();
  }

  /** 是否正在朗读（用于「离开课堂再回来」时判断该不该续讲）。 */
  function isSpeaking() {
    if (_engine !== "system") return !!_audio;
    return !!(window.speechSynthesis && (speechSynthesis.speaking || speechSynthesis.pending));
  }

  window.Voice = {
    ensureVoices, pickZhVoice, plainText, speak, stop,
    configure, engine, syncFromServer, cloudIssue, pause, resume, isSpeaking,
    prime,
  };
})();
