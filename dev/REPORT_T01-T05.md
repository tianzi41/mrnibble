# 知伴（ZhiBan）第一阶段（T01~T05）自检报告

- 执行人：寇豆码（工程师）
- 环境：`Q:\xiaolongxia\xiaxia\hyperknow调研\zhiban`，Python 3.13.14（`.venv`）
- 启动：`PYTHONPATH=src .venv/Scripts/python.exe -m backend.main`（端口 8760，写 `data/runtime.json`）
- 结论：**IS_PASS: YES**

## 判据实测结果

| 任务 | 自检脚本 | 结果 |
|---|---|---|
| T01 骨架/健康/路径 | `dev/t01_t02_check.py` | ✅ `GET /api/health` → `{"code":0,...,"status":"ok","version":"0.1.0"}`；`resource_path()` 开发态与模拟冻结态（设 `sys._MEIPASS`）均定位 `web/` 与 `schema.sql` |
| T02 数据层/迁移 | `dev/t01_t02_check.py` | ✅ 首次启动自动建库；`chunks_fts/memories_fts` + 6 个触发器齐备；重复启动幂等；`schema_version=1`；中文 2/3/4 字与中英混排 FTS 命中，负例 0 |
| T03 密钥安全 | `dev/t03_check_secrets.py` | ✅ 假 Key `sk-test-1234567890abcdef`：DB 存 Fernet 密文；`GET /api/settings` 只回 `sk-****cdef` 且响应体不含明文；**`data/logs/` grep 明文命中 0 次**；错误端点测试 `code=2003` |
| T04 解析入库 | `dev/t04_check_ingest.py` | ✅ PDF/DOCX/PPTX(3页)/MD/TXT 全部 `status=ready`；PDF 切片 `page_no=[1..5]` 整数；PPTX `page_no=[1,2,3]`=幻灯片序号；DOCX 带 `section` 层级；扫描版 PDF `ready` + `warning` 且切片为 0；删除文档后 `chunks` 归零 |
| T05 嵌入/检索 | `dev/t05_check_retrieve.py` | ✅ 中文文档切片带 512 维向量；`POST /api/retrieve` 搜「洛必达法则」「极限」均命中；搜不存在词命中 0；英文 PDF 命中带整数 `page_no`；云端端点不可达时降级仍可用 |

## 全局一致性审查

1. 返回封套：所有 router 统一用 `errors.ok()` → `{"code","message","data"}`（16 处），失败走全局异常处理器。✅
2. 路径：业务代码无任何 `os.environ` / `__file__` 拼装，全部经 `paths.resource_path()` / `paths.data_path()`。✅
3. 错误码：仅使用 §6.2 表内码（1000/1001/2000/2002/2003/3000/3003/4003）。✅
4. 导入与编译：`compileall` 通过；38 个模块全量导入无失败。✅
5. OpenAPI：15 条路径，与 §6.3/6.4/6.5/6.10 契约一致。✅

## 本阶段踩坑与解决

- **启动顺序**：`_register_secret_masking()` 曾在 `db.migrate()` 之前执行，干净库上因 `settings` 表未建而启动失败；已改为「先迁移、后登记密钥」。该问题仅被“清空 data/ 冷启动”用例暴露。
- **中文检索**：采用主理人拍板的「逐字 CJK 索引 + unicode61 + 短语查询」，入库/查询共用 `textutil.build_index_text`，2 字词可靠命中。
- **负例召回**：纯向量通道对无关中文会返回低余弦候选，新增 `retrieval.min_vec_score`（默认 0.2）门槛，保证材料外问题召回为空。
- **jieba 缓存写 C 盘**：已把 `jieba.dt.tmp_dir` 重定向到 `data/cache`（Q 盘）。
- **PDF 夹具**：本机无 reportlab/fpdf，夹具改为手工构造合法多页 PDF（pypdf 可提取）；中文夹具走 MD/DOCX/PPTX（UTF-8）。

## 下一阶段（T06~T11）最大风险点

1. **引导式护栏的确定性**：必须靠产品层结构化拦截（首轮 `final_answer==""` + 正则结论句式 + 二次兜底模板），不能只靠提示词；弱模型下尤其要保证首轮永不给最终答案。
2. **引用不可伪造**：模型只允许输出 `[[c:N]]`，页码一律由 `chunk_id` 回填 DB 值；严禁把模型正文里的“第 N 页”当页码。
3. **材料边界**：`hits` 全为空或低分时必须原样回“材料中未提及”且不带引用，需与检索阈值联动（本阶段已铺垫 `min_vec_score`）。
4. **记忆硬删除**：召回一律 `JOIN memories` 主表，禁止任何模块直查 `memories_fts`；删除后 `memories_fts` 行数须为 0。
5. **本地 ASR**：sherpa-onnx + SenseVoice int8 首次加载耗内存与耗时；模型不随包，需在 T11 做好“未下载时的明确报错 + 下载脚本”，且 `/api/asr/*` 绝不发起外网请求。
6. **SSE 流式**：需保证 `meta/delta/citation/done` 顺序与异常中断时的错误事件，避免前端悬挂。
