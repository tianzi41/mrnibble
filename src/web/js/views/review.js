/* 闪卡复习页：翻面 + 记住了/忘记了（答错回写知识盲区）。 */
(function () {
  "use strict";

  const S = { cards: [], idx: 0, flipped: false, generationId: null, stats: { good: 0, again: 0 } };

  async function render(host) {
    host.innerHTML = "";
    const page = document.createElement("div");
    page.className = "page";
    page.innerHTML = `
      <div class="row" style="margin-bottom:14px">
        <div class="field" style="max-width:340px"><label>卡组</label><select id="rv-deck"></select></div>
        <label class="switch" style="margin-top:18px"><input type="checkbox" id="rv-due"> 只看到期的</label>
        <span style="flex:1"></span>
        <span class="pill" id="rv-progress"></span>
      </div>
      <div class="card" id="rv-body"></div>`;
    host.appendChild(page);

    const decks = await Api.get("/api/generations?type=flashcard");
    const sel = document.getElementById("rv-deck");
    sel.innerHTML = `<option value="">全部闪卡</option>` +
      (decks.items || []).map((g) => `<option value="${g.id}">${g.title}</option>`).join("");
    sel.onchange = () => { S.generationId = sel.value || null; load(); };
    document.getElementById("rv-due").onchange = load;
    await load();
  }

  async function load() {
    const due = document.getElementById("rv-due").checked;
    const q = new URLSearchParams();
    if (S.generationId) q.set("generation_id", S.generationId);
    if (due) q.set("due_only", "true");
    const d = await Api.get("/api/flashcards" + (q.toString() ? "?" + q : ""));
    S.cards = d.items || [];
    S.idx = 0; S.flipped = false; S.stats = { good: 0, again: 0 };
    paint();
  }

  function paint() {
    const box = document.getElementById("rv-body");
    document.getElementById("rv-progress").textContent =
      `${Math.min(S.idx + 1, S.cards.length)} / ${S.cards.length} · 答对 ${S.stats.good} · 答错 ${S.stats.again}`;
    box.innerHTML = "";
    if (!S.cards.length) {
      box.innerHTML = '<div class="empty">没有可复习的卡片。先在「资料生成」里生成一组闪卡。</div>';
      return;
    }
    const c = S.cards[Math.min(S.idx, S.cards.length - 1)];
    const card = document.createElement("div");
    card.className = "flashcard" + (S.flipped ? " flip" : "");
    card.textContent = S.flipped ? c.answer : c.question;
    card.onclick = () => { S.flipped = !S.flipped; paint(); };
    box.appendChild(card);

    const hint = document.createElement("div");
    hint.className = "hint";
    hint.style.textAlign = "center";
    hint.textContent = S.flipped ? "想一想：真的记住了吗？" : "点击卡片显示答案";
    box.appendChild(hint);

    const bar = document.createElement("div");
    bar.className = "row";
    bar.style.cssText = "justify-content:center;margin-top:14px";
    if (S.flipped) {
      [["again", "忘记了", "danger"], ["hard", "有点模糊", ""], ["good", "记住了", "primary"], ["easy", "很简单", ""]].forEach(([k, label, cls]) => {
        const b = document.createElement("button");
        b.className = "btn " + cls;
        b.textContent = label;
        b.onclick = () => review(c, k);
        bar.appendChild(b);
      });
    } else {
      const b = document.createElement("button");
      b.className = "btn primary"; b.textContent = "显示答案";
      b.onclick = () => { S.flipped = true; paint(); };
      bar.appendChild(b);
    }
    box.appendChild(bar);
  }

  async function review(card, result) {
    try {
      await Api.post(`/api/flashcards/${card.id}/review`, { result });
      if (result === "again") S.stats.again++; else S.stats.good++;
      S.idx++; S.flipped = false;
      if (S.idx >= S.cards.length) {
        document.getElementById("rv-body").innerHTML =
          `<div class="empty">🎉 本轮复习完成（答对 ${S.stats.good} / 答错 ${S.stats.again}）。<br>答错的已记入知识盲区，可在「记忆」里查看。</div>`;
        document.getElementById("rv-progress").textContent = "完成";
        return;
      }
      paint();
    } catch (e) { Toast(e.message, true); }
  }

  window.Views = window.Views || {};
  window.Views.review = { render };
})();
