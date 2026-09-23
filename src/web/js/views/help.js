/* 帮助页（顶栏「不会用，点这里」）：使用步骤 + 功能简介 + API 申请指引。
 *
 * 写作原则（用户拍板）：只写**基本不会变**的大致步骤，别把易变的细节写死 ——
 * 例如不写死模型名（DeepSeek 的模型迭代很快，写死了下次就得改），
 * 只写「预设选 DeepSeek，地址与模型名会自动填好」。 */
(function () {
  const gt = (k, v) => window.I18n ? window.I18n.t(k, v) : k;

  window.I18n && window.I18n.merge({ en: {
  } });
  "use strict";

  function render(host) {
    host.innerHTML = `
<div class="page" style="max-width:900px;margin:0 auto">
  <div class="card">
    <b style="font-size:16px">知伴怎么用</b>
    <p class="hint" style="margin:6px 0 0">
      知伴是装在你电脑上的学习软件：把材料拆成能「上课」的课程，并基于材料回答你的问题。
      全部数据都在本机（程序目录的 <code>data/</code> 文件夹），换电脑整个目录拷走即可。
    </p>

    <div class="sep"></div>

    <b>第一次用，其实只要做一件事：配一个模型接口</b>
    <p class="hint" style="margin:6px 0 0">
      知伴自己不含 AI 能力，需要你填一个「对话模型」的接口。推荐
      <b>DeepSeek</b> —— 便宜、够用，几元钱能用很久。
    </p>

    <div style="margin-top:14px">
      <b>① 去申请一个 API Key（约 5 分钟）</b>
      <ol style="margin:8px 0 0;padding-left:22px;line-height:2">
        <li>打开 DeepSeek 开放平台：<code>platform.deepseek.com</code></li>
        <li>注册并登录（手机号 / 邮箱都行）</li>
        <li>在左侧菜单找到 <b>API Keys</b>，点「创建 API key」，起个名字（随便写，比如 <code>zhiban</code>）</li>
        <li><b>立刻复制生成的 Key</b>（形如 <code>sk-xxxxxxxx</code>）——
            它<b>只显示这一次</b>，关掉就再也看不到了（丢了就删掉重建一个）</li>
        <li>充值：新账号一般有少量赠送额度够你试；用完后在充值页面充几元即可</li>
      </ol>
    </div>

    <div style="margin-top:14px">
      <b>② 填到知伴里（填一次，以后不用管）</b>
      <ol style="margin:8px 0 0;padding-left:22px;line-height:2">
        <li>点右上角 <b>设置</b></li>
        <li>在「对话模型」一栏，预设选 <b>DeepSeek</b> —— 会自动填好这两项：
            地址 <code>https://api.deepseek.com</code>、模型名 <code>deepseek-flash</code></li>
        <li>把刚才复制的 Key 粘贴到 <b>API Key</b> 输入框</li>
        <li>点 <b>测试连接</b>，提示成功就保存</li>
      </ol>
    </div>

    <p class="hint" style="margin-top:12px">
      用的是别的服务商、或单位/学校给的中转接口？那就把三项都换成它给你的值 ——
      只要它兼容 <b>OpenAI 的对话接口</b>（绝大多数服务都兼容），知伴就能接。
    </p>

    <p class="hint" style="margin-top:12px">
      Key 只保存在你自己电脑上（本地加密存储），不会上传到任何地方。
      万一日后 DeepSeek 更新了模型名，把「模型名」改成官网文档里列出的最新对话模型即可 ——
      地址和 Key 都不用动。
    </p>

    <div class="sep"></div>

    <b>关于语音朗读</b>
    <p class="hint" style="margin:6px 0 0">
      朗读有两档：<b>本地朗读</b>（随软件自带的小模型，不用配置、不联网，开箱即用）
      和 <b>云端朗读</b>（音色更自然，需要另外配一个「语音合成」服务的接口）。
    </p>
    <p class="hint" style="margin:6px 0 0">
      云端不是必须的 —— 不配也能正常用本地朗读。任何提供「文字转语音」HTTP 接口的
      服务都能接，这里以 <b>阶跃星辰（StepFun）</b> 为例走一遍流程。
    </p>

    <div style="margin-top:14px">
      <b>① 申请语音接口的密钥（约 5 分钟）</b>
      <ol style="margin:8px 0 0;padding-left:22px;line-height:2">
        <li>打开阶跃星辰开放平台：<code>platform.stepfun.com</code></li>
        <li>注册并登录（手机号 / 邮箱都行）</li>
        <li>进入控制台的 <b>账户管理 → 接口密钥</b>，点「创建密钥」，起个名字（随便写，比如 <code>zhiban-tts</code>）</li>
        <li><b>立刻复制生成的 Key</b>（形如 <code>sk-xxxxxxxx</code>）—— 它<b>只显示这一次</b>，
            关掉就再也看不到了（丢了就删掉重建一个）</li>
        <li>新账号一般有赠送额度；如需更多，在官网的充值/套餐页按需开通（以官网当前活动为准）</li>
      </ol>
    </div>

    <div style="margin-top:14px">
      <b>② 填到知伴里（设置 → 语音朗读）</b>
      <ol style="margin:8px 0 0;padding-left:22px;line-height:2">
        <li>点右上角 <b>设置</b>，找到「语音朗读」一栏</li>
        <li><b>接口地址</b>填 <code>https://api.stepfun.com/v1</code>（注意带 <code>/v1</code>，别多也别少）</li>
        <li><b>API Key</b> 粘贴刚才复制的 Key</li>
        <li><b>模型名</b>填语音合成模型（阶跃当前是 <code>stepaudio-2.5-tts</code>；
            以官网模型列表里列出的为准，换了就改成新的）</li>
        <li><b>音色</b>可以先用默认；想换就在官网文档的音色列表里挑一个名字填上</li>
        <li>点 <b>测试连接</b>，提示成功就保存 —— 之后上课朗读就走云端音色了</li>
      </ol>
    </div>

    <p class="hint" style="margin-top:12px">
      <b>同一个 Key 也能用于「对话模型」</b>：如果对话模型也想用阶跃，在设置页把
      地址填 <code>https://api.stepfun.com/v1</code>、模型名填官网列出的最新对话模型
      （例如 <code>step-3.7-flash</code>）即可 —— 地址和 Key 与上面语音填的是同一套。
    </p>

    <p class="hint" style="margin-top:12px">
      Key 只保存在你自己电脑上（本地加密存储），不会上传到任何地方。
    </p>

    <div class="sep"></div>

    <b>日常使用（四步）</b>
    <ol style="margin:8px 0 0;padding-left:22px;line-height:2">
      <li><b>放材料</b> —— 工作台点「＋ 上传」，支持 PDF / Word / PPT / Markdown / TXT / 网页。
          也可以完全不给材料：在新建课程时选「没有材料，我直接说想学什么」，让 AI 先把材料写出来。</li>
      <li><b>生成课程</b> —— 课程页「＋ 新建课程」→ 选课型和材料 → 生成大纲 →
          确认结构（结构和标题都能改）→ 按讲生成讲义。</li>
      <li><b>上课</b> —— 打开讲次，看白板讲义（可翻页），点朗读听讲；学完做随堂练习。</li>
      <li><b>提问</b> —— 工作台先勾选材料再提问，回答会带页码引用，点角标可看原文。
          知伴会记住你的偏好和盲区，可在「记忆」里查看或删除。</li>
    </ol>

    <div class="sep"></div>

    <b>三个常见疑问</b>
    <ul style="margin:8px 0 0;padding-left:22px;line-height:2">
      <li><b>要联网吗？</b>只有当知伴在「生成内容」和「回答问题」时需要联网（走你填的模型接口）；
          资料解析、检索、本地语音识别与本地朗读都在本机完成、不出网。</li>
      <li><b>我的材料会被传上去吗？</b>不会整份上传。生成时只会把<b>用得上的少量材料片段</b>
          随请求发给你配置的模型服务 —— 这也是为了让它有据可依、能给出页码引用。</li>
      <li><b>找不到某份材料怎么办？</b>先看工作台左侧「资料库」里的状态：
          只有解析完成的材料才能被检索和引用；解析失败的会标出来，点 ↻ 可以重新解析。</li>
    </ul>
  </div>
</div>`;
  }

  window.Views = window.Views || {};
  window.Views.help = { render };
})();
