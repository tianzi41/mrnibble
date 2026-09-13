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

  // 本地引擎：system=浏览器系统语音（默认，零依赖）；melo=后端 MeloTTS 神经语音。
  let _engine = "system";
  // melo 模式下正在播放的 Audio（stop() 时要掐掉）。
  let _audio = null;
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
   * 只有「已启用 + mode=local + engine=melo + 模型就位」四个条件都满足才用
   * MeloTTS，否则一律回退系统语音——保证模型没下载时功能不残废。
   */
  async function syncFromServer() {
    try {
      const st = await Api.get("/api/tts/status");
      const useMelo = !!(st && st.enabled && st.mode === "local"
        && st.local_engine === "melo" && st.local_model_available);
      _engine = useMelo ? "melo" : "system";
    } catch (e) {
      _engine = "system";
    }
    return _engine;
  }

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

  /** 播放一段 WAV；播放结束或出错都 resolve（不卡住队列）。 */
  function playWav(buf) {
    return new Promise((resolve) => {
      try {
        const url = URL.createObjectURL(new Blob([buf], { type: "audio/wav" }));
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

    // 单块上限：melo 后端单段上限 300 字，这里切得更保守；system 引擎单次 1200 字。
    const hardMax = _engine === "melo" ? 120 : 1200;
    const detailed = Number(opts.chunkChars) > 0;
    const chunkMax = detailed ? Math.min(Number(opts.chunkChars), hardMax) : hardMax;
    const cut = (t) => (detailed ? splitBySentence(t, chunkMax) : splitChunks(t, chunkMax));

    if (_engine === "melo") {
      // 边合成边播、延迟更低。
      const chunks = cut(capped);
      for (let i = 0; i < chunks.length; i++) {
        if (myGen !== _gen) return;   // 已被 stop()
        let buf;
        try {
          buf = await requestLocal(chunks[i]);
        } catch (e) {
          if (myGen !== _gen) return;
          fail("本地语音合成失败：" + e.message);
          return;
        }
        if (myGen !== _gen) return;
        // 用户暂停：在两段之间的合成间隙里 _audio 为 null，pause() 摁不住，
        // 所以这里要等 _hold 解除再继续播（否则会趁暂停偷偷往下讲）。
        while (_hold && myGen === _gen) await new Promise((r) => setTimeout(r, 120));
        if (myGen !== _gen) return;
        if (opts.onChunk) opts.onChunk(chunks[i], i, chunks.length);
        await playWav(buf);
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
    if (_engine === "melo") {
      if (_audio) { try { _audio.pause(); } catch (e) { /* 忽略 */ } }
      return;
    }
    if (window.speechSynthesis) speechSynthesis.pause();
  }

  function resume() {
    _hold = false;
    if (_engine === "melo") {
      if (_audio) { try { _audio.play(); } catch (e) { /* 忽略 */ } }
      return;
    }
    if (window.speechSynthesis) speechSynthesis.resume();
  }

  /** 是否正在朗读（用于「离开课堂再回来」时判断该不该续讲）。 */
  function isSpeaking() {
    if (_engine === "melo") return !!_audio;
    return !!(window.speechSynthesis && (speechSynthesis.speaking || speechSynthesis.pending));
  }

  window.Voice = {
    ensureVoices, pickZhVoice, plainText, speak, stop,
    configure, engine, syncFromServer, pause, resume, isSpeaking,
  };
})();
