/* 本地语音输入（麦克风 → 文字）：录音 + 调本地 ASR 接口 + 结果填入输入框。

设计要点
--------
1. **零外发**：音频只送到本机后端 `/api/asr/transcribe`（sherpa-onnx + SenseVoice，
   随绿色包内置），不经过任何第三方。这是项目的红线之一。
2. **通用**：不绑定任何页面。调用方只要给一个「按钮」和「目标输入框」即可，
   工作台与课堂页共用同一份实现，避免两处各写一份导致行为不一致。
3. **单例**：麦克风同一时刻只允许一个会话，因此用一个模块级变量保存当前会话。

用法::

    Asr.toggle(btnEl, textareaEl, { onResult: (text) => {} });

2026-09-13 新增：课堂页提问框也要语音输入，从 workbench.js 抽出。
*/
(function () {
  "use strict";

    // 当前录音会话（没有则为 null）。
    let session = null;
    // 「正在取麦克风」标志：getUserMedia 要等用户点授权、可能几秒，
    // 这段窗口里 session 还是 null —— 不挡住的话连点会开出第二个会话，
    // 第一个麦克风流再也没人停（指示灯常亮、两份录音同时跑）。
    let opening = false;

  function supported() {
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia
      && (window.AudioContext || window.webkitAudioContext));
  }

  /** Float32 采样块 → 16kHz 单声道 16bit WAV（本地 ASR 要求）。 */
  function encodeWav(chunkList, srcRate) {
    const all = [];
    let total = 0;
    chunkList.forEach((c) => { all.push(c); total += c.length; });
    const merged = new Float32Array(total);
    let off = 0;
    all.forEach((c) => { merged.set(c, off); off += c.length; });
    const target = 16000;
    const n = Math.max(1, Math.round(merged.length * target / srcRate));
    const idx = new Float32Array(n);
    for (let i = 0; i < n; i++) idx[i] = i * (merged.length - 1) / Math.max(1, n - 1);
    const out = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const p = idx[i], i0 = Math.floor(p), i1 = Math.min(merged.length - 1, i0 + 1), f = p - i0;
      out[i] = merged[i0] * (1 - f) + merged[i1] * f;
    }
    const buf = new ArrayBuffer(44 + n * 2);
    const v = new DataView(buf);
    const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    str(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); str(8, "WAVE");
    str(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, target, true); v.setUint32(28, target * 2, true);
    v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    str(36, "data"); v.setUint32(40, n * 2, true);
    for (let i = 0; i < n; i++) {
      const s = Math.max(-1, Math.min(1, out[i]));
      v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return buf;
  }

  function setBtn(btn, text, recording) {
    if (!btn) return;
    btn.textContent = text;
    btn.classList.toggle("recording", !!recording);
    btn.disabled = !!btn.dataset.busy;
  }

  /**
   * 开始 / 结束录音。
   *
   * @param {HTMLElement} btn     触发按钮（文案与样式由本模块接管）
   * @param {HTMLTextAreaElement|HTMLInputElement|(() => HTMLElement)} target
   *        接收识别结果的输入框；传函数可在每次用时重新取（页面重绘也不失效）。
   * @param {{idleText?: string, toast?: (msg: string, bad?: boolean) => void,
   *          onResult?: (text: string) => void}} opts
   */
  async function toggle(btn, target, opts) {
    opts = opts || {};
    const toast = opts.toast || function () { };
    const idle = opts.idleText || "🎙";
    const getEl = () => (typeof target === "function" ? target() : target);

      if (session) { const s = session; session = null; await s.stop(); return; }
      if (opening) return;              // 正在取麦克风：这次点击直接忽略（防并发开会话）
      if (!supported()) {
        toast("当前浏览器不支持录音；请用 Chrome / Edge 打开（本地地址或 https）", true);
        return;
      }

      let stream;
      opening = true;
      if (btn) btn.disabled = true;      // 等待授权期间先锁住按钮，给出「在动了」的反馈
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          audio: { channelCount: 1, echoCancellation: true },
        });
      } catch (e) {
        toast("无法访问麦克风：" + (e && e.message || e), true);
        if (btn) btn.disabled = false;
        return;
      } finally {
        opening = false;
      }

    const Ctx = window.AudioContext || window.webkitAudioContext;
    const ctx = new Ctx();
    const src = ctx.createMediaStreamSource(stream);
    const proc = ctx.createScriptProcessor(4096, 1, 1);
    const chunks = [];
    proc.onaudioprocess = (e) => chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
    src.connect(proc); proc.connect(ctx.destination);

    setBtn(btn, "⏹ 结束", true);
    if (opts.onStart) opts.onStart();

    session = {
      stop: async () => {
        setBtn(btn, "识别中…", false);
        btn.dataset.busy = "1"; btn.disabled = true;
        try {
          src.disconnect(); proc.disconnect();
          stream.getTracks().forEach((t) => t.stop());
          const wav = encodeWav(chunks, ctx.sampleRate);
          ctx.close();

          const fd = new FormData();
          fd.append("audio", new Blob([wav], { type: "audio/wav" }), "speech.wav");
          const d = await Api.upload("/api/asr/transcribe", fd);
          const el = getEl();
          if (el && d && d.text) {
            el.value = (el.value ? el.value.replace(/\s*$/, " ") : "") + d.text;
            el.focus();
          }
          toast(`识别完成（${((d && d.duration_ms || 0) / 1000).toFixed(1)}s / ${(d && d.latency_ms) || 0}ms）`);
          if (opts.onResult) opts.onResult((d && d.text) || "");
        } catch (e) {
          toast("识别失败：" + (e && e.message || e), true);
        } finally {
          delete btn.dataset.busy;
          setBtn(btn, idle, false);
          if (opts.onStop) opts.onStop();
        }
      },
    };
  }

  window.Asr = { supported, toggle, encodeWav };
})();
