/* API 封装：统一解析 Envelope，SSE 流式读取。 */
(function () {
  const gt = (k, v) => window.I18n ? window.I18n.t(k, v) : k;

  window.I18n && window.I18n.merge({ en: {
    "无法连接本地服务，请确认知伴正在运行": "Cannot reach the local service; make sure ZhiBan is running",
    "响应没有可读的流（HTTP ": "Response has no readable stream (HTTP ",
    "请求失败（HTTP ": "Request failed (HTTP ",
  } });
  "use strict";

  async function request(method, url, body, opts) {
    opts = opts || {};
    const init = { method, headers: {} };
    if (body !== undefined && !(body instanceof FormData)) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    } else if (body instanceof FormData) {
      init.body = body;
    }
    let resp;
    try {
      resp = await fetch(url, init);
    } catch (e) {
      throw new ApiError(0, gt("无法连接本地服务，请确认知伴正在运行"));
    }
    const ct = resp.headers.get("content-type") || "";
    if (!ct.includes("application/json")) {
      if (!resp.ok) throw new ApiError(resp.status, gt("请求失败（HTTP ") + resp.status + "）");
      return await resp.blob();
    }
    const json = await resp.json();
    if (json.code !== 0) throw new ApiError(json.code, json.message, json.error && json.error.detail);
    return json.data;
  }

  class ApiError extends Error {
    constructor(code, message, detail) {
      super(message);
      this.code = code;
      this.detail = detail;
    }
  }

  const api = {
    ApiError,
    get: (u) => request("GET", u),
    post: (u, b) => request("POST", u, b),
    put: (u, b) => request("PUT", u, b),
    patch: (u, b) => request("PATCH", u, b),
    del: (u) => request("DELETE", u),
    upload: (u, formData) => request("POST", u, formData),
    download: (u) => { window.open(u, "_blank"); },

    /** SSE 流式问答。handlers: {meta, delta, guided, citation, done, error} */
    stream(url, body, handlers) {
      const ctrl = new AbortController();
      (async () => {
        let evName = "";
        try {
            const resp = await fetch(url, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body),
              signal: ctrl.signal,
            });
            // ⚠️ 先判状态码：后端出错时返回的是 JSON 错误封套，**不是** SSE。
            // 直接当流解析会把「{"code":2001,"message":"模型调用失败"}」当正文逐行喂给
            // delta → 聊天气泡里混进一串 JSON 乱码（用户实测）。这里显式抛错，
            // 交给下面的 catch 统一走 handlers.error。
            if (!resp.ok) {
              let msg = "HTTP " + resp.status;
              try {
                const j = await resp.json();
                msg = (j && (j.message || j.detail)) || msg;
              } catch (e) { /* 不是 JSON 就用状态码 */ }
              throw new Error(msg);
            }
            if (!resp.body) throw new Error(gt("响应没有可读的流（HTTP ") + resp.status + "）");
            const reader = resp.body.getReader();
          const dec = new TextDecoder("utf-8");
          let buf = "";
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += dec.decode(value, { stream: true });
            const frames = buf.split("\n\n");
            buf = frames.pop();
            for (const frame of frames) {
              let name = "message", data = "";
              for (const line of frame.split("\n")) {
                if (line.startsWith("event: ")) name = line.slice(7).trim();
                else if (line.startsWith("data: ")) data += line.slice(6);
              }
              if (!data) continue;
              let payload;
              try { payload = JSON.parse(data); } catch (e) { continue; }
              if (handlers[name]) handlers[name](payload);
            }
          }
        } catch (e) {
          if (e.name !== "AbortError" && handlers.error) handlers.error({ code: 0, message: String(e) });
        } finally {
          if (handlers.close) handlers.close();
        }
      })();
      return { abort: () => ctrl.abort() };
    },
  };

  window.Api = api;
})();
