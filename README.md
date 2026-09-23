# 知伴 ZhiBan · 本地 AI 学习伴侣

> **单机本地的引导式 AI 学习软件。** 把你的课件、论文、教材放进本机，即可基于**自己的材料**
> 提问、生成课程与复习资料、被"像老师一样"引导着学。数据不出本机，填入你自己的 API Key 即可使用。

| 项 | 说明 |
|---|---|
| 运行平台 | Windows 10 / 11 x64 |
| 交付形态 | **免安装绿色包**：解压 → 双击 `知伴.exe`，无需安装 Python / Node / Docker |
| 使用方式 | 单机单人，无注册登录，全部数据存本机 |
| 数据位置 | 程序目录内 `data/`（拷走整包即完成迁移） |
| 许可证 | [MIT](LICENSE) |
| 技术栈 | Python 3.13 + FastAPI + SQLite(FTS5) + 零构建原生 JS 前端 |
| 测试 | 19 套自动化套件 / 896 项断言（含 48 项打包后冻结态验证），全部通过 |

---

## 一、快速开始（用户）

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
| **上课（字幕 + 逐页讲授）** | 屏幕中下方常驻**字幕**（与语音逐句同步）；**逐页连续讲授**，翻页时自动把新课件滚到顶部完整显示（不被字幕条/操作条遮挡），可随时「⏸ 暂停 / ▶ 继续」，点字幕条任意位置也能继续 |
| **课件特色页** | 除要点页外，课件还会按内容形态出现：**对比表格页**（两方案/多编码逐项对照）、**架构化图示页**（5 种图型：流程 / 时序 / 数据流 / 状态机 / 架构分层，由 AI 产出结构化 IR、程序确定性编译成图）、**图表页**（柱状/折线/饼图）、**金句卡**（讲末结论）；任何一步失败都会退回文字要点，**永不空白**；要点里的关键术语会加粗 |
| **实操题与「实践环节」开关** | 建课时可选是否包含实操：**开启**的课程会出「🖐 实操题」（题干要求真的动手做一次、把结果回填，判分同填空题）；**纯理论课**关掉即可，出题与讲稿都不再出现操作任务 —— 文言文/理论类课程适用 |
| **学习目标推荐** | 创建向导点「✨ 帮我推荐」会**按你勾选的材料**给出 4 个可检验目标，并在下方标明「推荐基于你勾选的：…」——材料来源永远可见 |
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
| **朗读（TTS，可选）** | 默认开启「本地系统语音」（Windows 自带音色，离线零 Key，即时出声）；可切"本地神经语音 MeloTTS"（更自然，模型随包，离线零 Key，但 CPU 合成约 40 秒/段且音量不稳）或"云端 API"（与对话模型**同站点**时端点与 Key 都可留空自动沿用；**异站点**不会误用对话模型的 Key）。设置页有 **测试连接 / 试听**（三种模式都可用）并实时显示"当前生效"引擎；另有 **音色 voice** 一栏（非 OpenAI 系服务商必须填它自己的音色名）；云端失败会**明确报错并给出实际请求 URL 与上游原始错误**，不会静默降级成本地朗读 |

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

## 四、常见问题

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

## 五、开发者指南

### 5.1 技术栈与结构

- **后端**：Python 3.13 + FastAPI + uvicorn，作为本机侧车服务（只绑 `127.0.0.1`，固定端口 8760，被占用顺延至 8770）
- **存储**：SQLite（WAL 模式）+ FTS5 全文检索（CJK 逐字分词）；密钥用 Fernet 加密后落库，日志脱敏
- **前端**：零构建原生 JS（IIFE 模块 + `<script>` 直载），无框架无打包器；第三方库存 `src/web/vendor/`
- **桌面外壳**：`src/launcher.py` 拉起后端 + 打开 Edge/Chrome 应用窗口，窗口存活即服务存活
- **构建**：PyInstaller 冻结态（`build/zhiban.spec`），随包分发 ASR + MeloTTS 模型
- **测试**：`dev/*_check.py` 全部自带 mock 模型服务，**无需网络与真实 Key**

```
zhiban/
├─ src/
│  ├─ backend/            # FastAPI 侧车服务
│  │  ├─ db/              # schema / migrations / connection
│  │  ├─ services/        # courses / retrieval / tts / asr / diagram / embedder …
│  │  ├─ routers/         # HTTP 端点
│  │  └─ main.py
│  ├─ web/                # 零构建前端（index.html + css + js，js/views/ 为页面模块）
│  └─ launcher.py         # 桌面外壳入口
├─ dev/                   # 自动化测试（19 套件，自带 mock）与 fixtures
├─ scripts/               # 模型下载 / vendor 拉取 / 打包 / 备份恢复
├─ build/                 # PyInstaller spec 与构建脚本
├─ docs/                  # PRD / 架构 / 用户手册 / 模型兼容列表 / 依赖许可
└─ requirements*.txt
```

### 5.2 克隆后必须补齐的内容

> 本仓库按 `.gitignore` 排除了**体积大或敏感**的东西，克隆后不能直接跑，需按下表补齐。
> 这些内容都是可重建的，不需要手动拷贝。

| 未入库的内容 | 为什么 | 怎么补 |
|---|---|---|
| `src/web/vendor/`（前端库 4MB） | 第三方库，避免 vendor 化 | `powershell -ExecutionPolicy Bypass -File scripts\fetch_vendor.ps1` |
| `src/web/vendor/pdfjs/` | 同上 | `.venv\Scripts\python scripts\fetch_pdfjs.py` |
| `models/asr/`（SenseVoice 229MB） | 模型权重过大 | `powershell -ExecutionPolicy Bypass -File scripts\download_models.ps1`（走 hf-mirror 国内镜像） |
| `models/tts/`（MeloTTS 77MB） | 同上 | `.venv\Scripts\python scripts\download_tts_model.py`（走 ModelScope 镜像，支持断点续传） |
| `models/embed/`（bge-small-zh INT8 25MB，可选） | 语义检索增强，默认关闭 | `.venv\Scripts\python scripts\download_bge_model.py` |
| `data/` | **含 `secret.key` 与用户资料库** | 首次启动自动创建，无需处理 |
| `.venv/`、`dist*/`、`build/*/` | 虚拟环境与构建产物 | 见下方步骤 1 与 4 |

### 5.3 从源码运行

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

# 4) 开发态运行（两种方式）
.venv\Scripts\python -m backend.main            # 仅后端，需设 PYTHONPATH=src，端口 8760
powershell -ExecutionPolicy Bypass -File scripts\run_dev.ps1   # 后端 + 浏览器窗口（完整桌面体验）
```

### 5.4 跑测试

19 个套件全部**自带 mock 模型服务**，无需网络与真实 Key。跑前建议 `taskkill /IM python.exe /F`
清残留测试进程（端口区间 8760–8770）：

| 套件 | 覆盖 | 项数 |
|---|---|---|
| `dev/t01_t02_check.py` | 建库 / FTS / schema 迁移 | 15 |
| `dev/t06_t11_check.py` | 后端主链路（设置/文档/检索/TTS/ASR） | 86 |
| `dev/course_check.py` | 课程生成全链路（大纲→课件→练习→总结） | 255 |
| `dev/course_ui_check.py` | 前端页面冒烟（含 CDP 真实浏览器交互，需 Chrome） | 201 |
| `dev/tts_fallback_check.py` | 语音回退 / 密钥不外泄 / 失败分类 | 20 |
| `dev/tts_prefetch_check.py` | 朗读预取与段间静音实测 | 17 |
| `dev/tts_voices_check.py` | 音色发现 / 探测缓存 / 能力裁剪 | 14 |
| `dev/tts_custom_check.py` | 自定义 TTS 适配器 | 5 |
| `dev/diagram_ir_check.py` | 图示 IR 校验与确定性编译 | 64 |
| `dev/viz_harness_check.py` | 课件可视化渲染链路 | 31 |
| `dev/embedder_check.py` | 本地 bge 嵌入（模型缺则自动跳过） | 6 |
| `dev/chrome_app_check.py` | Chrome `--app` 真窗冒烟 | 8 |
| `dev/launcher_liveness_check.py` | 桌面外壳存活判定（真建窗口并最小化） | 31 |
| `dev/hardening_check.py` | 上传预检 / 抓取上限 / 回环校验 / 安全头 | 18 |
| `dev/topic_goal_check.py` | AI 材料建课学习目标 | 17 |
| `dev/outline_units_check.py` | 大纲单元数量弹性 | 20 |
| `dev/course_done_check.py` | 结课收尾 | 9 |
| `dev/ui_fixes_check.py` | 前端改动端到端 | 31 |
| `dev/frozen_check.py` | **打包后冻结态**（用 `ZHIBAN_DIST` 指定被测包） | 48 |

```powershell
# 单套示例
.venv\Scripts\python dev\t06_t11_check.py

# 冻结态验证（务必指定独立数据目录，别污染真实数据）
$env:ZHIBAN_DIST="dist\知伴"; $env:ZHIBAN_DATA_DIR="$env:TEMP\zhiban-frozen"
.venv\Scripts\python dev\frozen_check.py
```

### 5.5 构建打包

```powershell
# 1) PyInstaller 冻结态构建（产物 dist\知伴\）
powershell -ExecutionPolicy Bypass -File build\build.ps1

# 2) 冻结态验证（见 5.4，48 项全过才继续）

# 3) 打免安装绿色包 zip（自动排除 data/ 与 secret.key，并做 testzip 校验）
.venv\Scripts\python scripts\make_package.py --dist dist\知伴 --out 知伴-免安装绿色包-v<版本>.zip
```

约定：`dist` 目录只增不改（每次新编号），构建用 `--noconfirm` 前必须确认目标目录不存在——
它会连 `data/`（用户库）一起删。

### 5.6 备份与恢复

```powershell
powershell -ExecutionPolicy Bypass -File scripts\backup.ps1    # 备份 data/（db + secret.key + files）
powershell -ExecutionPolicy Bypass -File scripts\restore.ps1   # 从备份恢复
```

---

## 六、文档索引

| 文档 | 内容 |
|---|---|
| `docs/03-用户手册.md` | 上传文档、问答、生成复习材料、语音的完整使用说明 |
| `docs/04-管理员手册.md` | 模型切换、日志、备份恢复、升级、数据迁移 |
| `docs/05-模型兼容列表.md` | 已验证可用的模型端点清单 |
| `docs/06-第三方依赖与许可.md` | 全部第三方依赖及其开源许可证 |
| `docs/01-PRD.md` | 产品需求文档（48 条需求与验收标准） |
| `docs/02-架构设计.md` | 系统架构、数据模型、接口契约（开发者向） |
| `dev/REPORT_T01-T05.md` | 第一阶段（骨架/数据层/密钥/解析/检索）自检报告 |

## 许可

本项目代码采用 MIT 许可，详见 `LICENSE`。第三方组件许可见 `docs/06-第三方依赖与许可.md`。
