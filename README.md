# 知伴 ZhiBan · 本地 AI 学习伴侣

> 复刻 Hyperknow 核心能力的**单机单人**本地软件。把课件、论文、教材放进本机，
> 即可基于**自己的材料**提问、生成复习资料、被"像老师一样"引导式教学。
> 数据不出本机，填入你自己的 API Key 即可使用。

| 项 | 说明 |
|---|---|
| 运行平台 | Windows 10 / 11 x64 |
| 交付形态 | **免安装绿色包**：解压 → 双击 `知伴.exe`，无需安装 Python / Node / Docker |
| 使用方式 | 单机单人，无注册登录，全部数据存本机 |
| 数据位置 | 程序目录内 `data/`（拷走整包即完成迁移） |

---

## 一、30 秒上手

1. 解压 `知伴.zip` 到任意目录（**建议放在空间充足的盘**，如 `D:\知伴\`）。
2. 双击 `知伴.exe`，会自动打开应用窗口。
3. 进入 **设置 → 对话模型**，填三样东西：
   - `base_url`（OpenAI 兼容端点，可用顶部"快速填充"）
   - `模型名`
   - `API Key`
4. 点 **测试连接**，看到 ✅ 即配置完成。
5. 回到 **工作台**，上传一份课件（PDF / DOCX / PPTX / MD / TXT），开始提问。

> 第一次使用语音输入会自动下载本地语音模型（约 228MB，来自 HuggingFace 国内镜像），
> 下载完成后**永久离线可用**。

---

## 二、核心功能

| 功能 | 说明 |
|---|---|
| **课程学习（把资料学成一门课）** | 选材料 + 写目标 → 生成课程大纲 → 思维导图式结构编辑与确认 → 单元/讲次逐节学习 → 随堂练习 → 单元总结与进度 |
| **白板讲义** | 每节讲次生成结构化讲义（概念 / 例子 / 公式 / 材料原文 / 补充 + 要点 + 关键术语 + 回顾），可导出 PNG / Markdown；上课时展示的是独立的**逐页课件**，朗读的是独立的**讲师讲稿**，三者互不混用 |
| **材料标注** | 在材料原页上高亮 / 圈注，右侧写旁注并自动连线指向对应位置；AI 也会在讲义里标出重点位置。仅分页材料（PDF）支持 |
| **课堂提问** | **默认开放回答**：既用材料，也允许围绕疑问补充材料外知识（用到时标注「材料外回答」）；自动带「当前课件第 n 页」上下文 |
| **课堂提问** | **默认开放回答**：既用材料，也允许围绕疑问补充材料外知识（用到时标注「材料外回答」）；自动带「当前课件第 n 页」上下文 |
| **上课（字幕 + 互动）** | 屏幕中下方常驻**字幕**（与语音逐句同步，默认开启）；每 2 页在字幕正上方弹出互动选择（继续/重讲/提问/直接讲完） |
| **随堂练习** | 一讲讲完**自动出题并进入答题**（5 题，以单选为主，部分带材料页配图）；**每答完一题立刻给对错与解析**，未作答不能跳到下一题；错题进课程错题本，可重做 |
| **讲稿防雷同** | 讲师讲稿与课件是两份内容；系统用文本相似度强制「讲稿不许照念课件」，雷同则重写、仍雷同则确定性扩写 |
| **单元总结** | 学完一个单元后生成：已掌握 / 待巩固 / 下一步 + 练习统计 |
| **文档问答 + 引用溯源** | 回答带页码/章节，点角标查看原文片段。**页码由系统按检索片段回填，模型没有写页码的通道**，结构上不可能伪造 |
| **材料边界严谨** | 材料里没有的内容，明确回复"材料中未提及"，不编造引用；确需补充会显式标注"（材料外回答）" |
| **五类资料生成** | 速查表 / 学习笔记 / 思维导图 / 练习题 / 闪卡，均可预览与导出 |
| **导出** | 闪卡 → Anki `.apkg` / Markdown / CSV；思维导图 → Markdown / PNG |
| **长期记忆** | 记住你的偏好、学习进度与知识盲区；可查看 / 编辑 / 删除 / 导出。**删除是物理删除**，之后不会再被召回 |
| **引导式教学** | 打开工作台底部"🎓 引导式学习"开关后，AI 先追问与拆解，不直接给答案；答错会记入知识盲区 |
| **语音输入（ASR）** | 完全本地识别（SenseVoice），**不发任何网络请求**，断网可用 |
| **朗读（TTS，可选）** | 默认关闭；可选"本地系统语音"（Windows 自带音色，离线零 Key）、"本地神经语音 MeloTTS"（更自然，模型随包，离线零 Key）或"云端 API" |

---

## 三、配置 API（BYOK）

支持任何 **OpenAI 兼容**端点，切换无需改代码。常见服务商见「设置 → 对话模型 → 快速填充」，
完整兼容列表见 `docs/05-模型兼容列表.md`。

**完全离线方案（不填任何 Key）**：

1. 安装并启动 [Ollama](https://ollama.com/)，拉一个模型，如 `ollama pull qwen2.5:7b`
2. 知伴「设置 → 对话模型」中 `base_url` 填 `http://127.0.0.1:11434/v1`，模型名 `qwen2.5:7b`，Key 留空
3. 断网后依然可以：上传文档 → 检索 → 问答 → 生成资料 → 语音输入 → 本地朗读

**隐私说明**：你上传的文档**只会**发送到**你自己配置**的模型端点。未配置端点时，
问答/生成功能直接提示"模型未配置"，绝不会把材料上传到任何第三方。

---

## 四、目录结构

```
知伴/
├─ 知伴.exe            ← 双击启动
├─ _internal/          ← 程序与随包资源（只读，请勿改动）
│  ├─ web/             ← 前端（含本地第三方库，离线可用）
│  └─ models/          ← 本地语音模型（若打包时已包含）
└─ data/               ← 你的全部数据（可写）
   ├─ zhiban.db        ← SQLite 主库（文档、切片、会话、记忆、课程与练习记录）
   ├─ files/           ← 上传的原始文档
   ├─ exports/         ← 导出的 apkg / md / csv / png
   ├─ secret.key       ← 本机加密主密钥（勿删，删了已存的 Key 将无法解密）
   └─ logs/zhiban.log  ← 运行日志（已脱敏，不含 Key）
```

**迁移 / 备份**：把整个 `知伴/` 文件夹拷走即可。也可用 `scripts/backup.ps1` / `restore.ps1`。

---

## 五、常见问题

<details>
<summary><b>「材料标注」页打开是空的 / 图片题没有配图？</b></summary>

材料标注与图片题依赖**分页材料**：只有 PDF 的切片带页码。用 MD / TXT 建的课程
没有「页」的概念，因此不会产生标注与配图（课程、讲义、练习其余功能都正常）。
需要标注时，把原始 PDF 一起上传后再建课程即可。
</details>

<details>
<summary><b>双击后窗口没弹出来？</b></summary>

看 `data/logs/zhiban.log` 与控制台窗口的报错。最常见原因：端口被占用
（程序会自动从 8760 顺延到 8770，再不行会提示）；杀毒软件拦截（把程序目录加入白名单）。
</details>

<details>
<summary><b>问答报"模型未配置"或"连接失败"？</b></summary>

检查三点：`base_url` 是否含 `/v1` 结尾、模型名是否正确、Key 是否有效。
点「设置 → 测试连接」可快速定位。国内网络访问 OpenAI 官方端点通常需要代理，
此时程序会自动使用系统代理；本地端点（Ollama）则始终直连、不走代理。
</details>

<details>
<summary><b>回答里说"材料中未提及"，但材料里明明有？</b></summary>

多半是检索没命中。先确认该文档状态为"已解析"；再换更接近原文的措辞提问；
仍不行可在工作台勾选"允许材料外回答"先拿到答案，再核对原文。
</details>

<details>
<summary><b>语音输入没反应？</b></summary>

浏览器会请求麦克风权限，请允许。若提示"模型不可用"，运行
`scripts/download_models.ps1` 手动下载语音模型（约 228MB），完成后即离线可用。
</details>

<details>
<summary><b>扫描版（纯图片）PDF 无法检索？</b></summary>

本版本不做 OCR。这类文件会标记为"已解析但无可提取文本层"。请改用带文字层的 PDF，
或先用其它工具做 OCR。
</details>

<details>
<summary><b>如何彻底卸载？</b></summary>

删除整个 `知伴/` 文件夹即可，系统内不留任何残留（数据都在这个文件夹里）。
</details>

---

## 六、从源码运行 / 构建（仅开发者需要）

> ⚠️ 本仓库按 `.gitignore` 排除了**体积大或敏感**的东西，克隆后不能直接跑，需按下表补齐。
> 这些内容都是可重建的，不需要手动拷贝。

| 未入库的内容 | 为什么 | 怎么补 |
|---|---|---|
| `src/web/vendor/`（前端库 4MB） | 第三方库，避免 vendor 化 | `powershell -ExecutionPolicy Bypass -File scripts\fetch_vendor.ps1` |
| `src/web/vendor/pdfjs/` | 同上 | `.venv\Scripts\python scripts\fetch_pdfjs.py` |
| `models/asr/`（SenseVoice 229MB） | 模型权重过大 | `powershell -ExecutionPolicy Bypass -File scripts\download_models.ps1`（走 hf-mirror 国内镜像） |
| `models/tts/`（MeloTTS 77MB） | 同上 | `.venv\Scripts\python scripts\download_tts_model.py`（走 ModelScope 镜像，支持断点续传） |
| `data/` | **含 `secret.key` 与用户资料库** | 首次启动自动创建，无需处理 |
| `.venv/`、`dist*/`、`build/*/` | 虚拟环境与构建产物 | 见下方步骤 1 与 4 |

```powershell
# 1) 建虚拟环境并装依赖（国内建议清华源）
python -m venv .venv
.venv\Scripts\python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt -r requirements-dev.txt

# 2) 补齐前端第三方库（否则界面起不来）
powershell -ExecutionPolicy Bypass -File scripts\fetch_vendor.ps1
.venv\Scripts\python scripts\fetch_pdfjs.py

# 3) 下载本地模型（ASR 必需；TTS 可选，不下载会自动回退系统语音）
powershell -ExecutionPolicy Bypass -File scripts\download_models.ps1
.venv\Scripts\python scripts\download_tts_model.py

# 4) 开发态直接跑（8760 端口），或一键打包
.venv\Scripts\python -m backend.main            # 需设 PYTHONPATH=src
powershell -ExecutionPolicy Bypass -File build\build.ps1
# 打包产物：dist\知伴\
```

运行自动化测试（自带 mock 模型服务，**无需网络与真实 Key**）：

```powershell
.venv\Scripts\python dev\t06_t11_check.py     # 后端主链路 81 项
.venv\Scripts\python dev\course_check.py      # 课程链路 116 项
.venv\Scripts\python dev\course_ui_check.py   # 前端页面冒烟 44 项（需本机 Chrome）
# 打包后冻结态（46 项；务必用独立数据目录，别污染真实数据）
#   ZHIBAN_DIST=dist10 ZHIBAN_DATA_DIR=<临时目录> .venv\Scripts\python dev\frozen_check.py
```

---

## 七、文档索引

| 文档 | 内容 |
|---|---|
| `docs/03-用户手册.md` | 上传文档、问答、生成复习材料、语音的完整使用说明 |
| `docs/04-管理员手册.md` | 模型切换、日志、备份恢复、升级、数据迁移 |
| `docs/05-模型兼容列表.md` | 已验证可用的模型端点清单 |
| `docs/06-第三方依赖与许可.md` | 全部第三方依赖及其开源许可证 |
| `docs/02-架构设计.md` | 系统架构、数据模型、接口契约（开发者向） |

## 许可

本项目代码采用 MIT 许可，详见 `LICENSE`。第三方组件许可见上文清单。
