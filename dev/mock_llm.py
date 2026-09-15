"""本地 Mock LLM 服务（OpenAI 兼容，仅测试用，不进产品包）。

用途：本机没有可用的大模型端点，也没有真实 API Key，因此用一个可**确定性控制
输出行为**的假端点来驱动自动化测试——尤其是「故意违规」场景，用来证明护栏
是真的拦得住，而不是只在正常路径上碰巧通过。

启动：
    .venv/Scripts/python.exe dev/mock_llm.py            # 监听 127.0.0.1:8761

按 ``model`` 字段切换行为：
    mock-normal              普通问答：返回带 [[c:1]] [[c:2]] 的 Markdown（流式/非流式均支持）
    mock-good-guided         合规引导式输出（首轮 final_answer 为空、2 拆解 + 1 追问）
    mock-violate-first-turn  **故意违规**：首轮就把最终答案塞进 final_answer
    mock-bad-json            返回非法 JSON（验证解析失败兜底）
    mock-empty-hits          回答引用 [[c:1]]（用于验证越界/无引用表时被剔除）
    mock-cheatsheet          合规速查表 JSON（5 条）
    mock-flashcard           合规闪卡 JSON（3 张）
    mock-mindmap             合规思维导图 JSON
    mock-quiz                合规练习题 JSON
    其它任意名                原样回显输入
"""

from __future__ import annotations

import json
import math
import time
import zlib

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="ZhiBan Mock LLM")

# mock 嵌入的维度。
#
# 注意（2026-09-12 修正）：早期版本用 `(seed*(i+3))%97/97` 这种锯齿函数生成向量，
# 结果是**任意两段文本的余弦都偏高**，导致向量通道把无关问题也召回，进而让
# 「材料外 → 必须回『材料中未提及』+ 零引用」这条红线在测试里失真。
#
# 现在改为「字符二元组 / 三元组 的哈希词袋」：
# - 只用 n-gram 不用单字，避免「的 / 是」这类高频字造成假相似；
# - 维度取 4096，把哈希碰撞压到可忽略（256 维时碰撞本身就能凑出 0.2 的余弦）；
# - 用 zlib.crc32（跨进程稳定），绝不用内置 hash()（每进程加盐，不稳定）。
EMBED_DIM = 4096


def _quote_block() -> str:
    """普通回答正文（含两个合法引用角标）。"""
    return (
        "材料中给出的核心结论是：洛必达法则用于处理 0/0 或 ∞/∞ 型未定式 [[c:1]]，"
        "其适用前提是分子分母同时趋于零或无穷 [[c:2]]。\n\n"
        "另外材料还强调了使用前要先验证类型，避免误用 [[c:1]]。"
    )


def _guided_payload(violate: bool) -> dict:
    """构造引导式结构化输出。"""
    if violate:
        return {
            "mode": "explain",
            "final_answer": "洛必达法则的适用条件是分子分母同时趋于零或无穷。",
            "decomposition_steps": [{"step": 1, "title": "直接给结论", "hint": ""}],
            "follow_up_questions": [],
            "knowledge_gaps": [],
            "next_action": "conclude",
            "student_state": {"mastery": 1.0},
            "conclusion_allowed": True,
            "summary": "答案是：分子分母同时趋于零或无穷。",
            "citations_used": [1],
        }
    return {
        "mode": "explain",
        "final_answer": "",
        "decomposition_steps": [
            {"step": 1, "title": "确认未定式类型", "hint": "先判断是 0/0 还是 ∞/∞ [[c:1]]"},
            {"step": 2, "title": "分别对分子分母求导", "hint": "写出求导后的式子，先别代入数值"},
            {"step": 3, "title": "再求极限并核对前提", "hint": "检查结果是否仍为未定式"},
        ],
        "follow_up_questions": [
            "你先说说，这道题的分子和分母在 x→0 时各自趋于多少？",
        ],
        "knowledge_gaps": [],
        "next_action": "ask_follow_up",
        "student_state": {"mastery": 0.2, "confidence": 0.4},
        "conclusion_allowed": False,
        "summary": (
            "我们先把问题拆开，一步步来，先不急着得到结论。\n\n"
            "**推进顺序**\n1. 确认未定式类型\n2. 分别对分子分母求导\n3. 再求极限并核对前提\n\n"
            "材料里相关定义在 [[c:1]]。"
        ),
        "citations_used": [1],
    }


def _bad_json() -> str:
    """故意非法的 JSON（缺失右括号）。"""
    return '{"mode": "explain", "final_answer": "", "decomposition_steps": ['


def _lecture_payload() -> str:
    """合格的课堂内容包（课件 / 讲稿 / 讲义三套分离，讲稿是展开讲解而非照念）。"""
    return json.dumps({
        "summary": "本讲先建立直觉，再给出形式化定义 [[c:1]]。",
        "slides": [
            {"id": "slide-1", "kind": "concept", "title": "极限的直觉", "bullets": [
                "自变量靠近某点", "函数值靠近确定的数", "重点是趋势，不一定要取到该点"],
             "body": "", "citation_refs": [1]},
            {"id": "slide-2", "kind": "quote", "title": "材料中的关键表述", "bullets": [
                "洛必达法则用于处理未定式极限", "先判断类型，再考虑法则"],
             "body": "", "citation_refs": [2]},
            {"id": "slide-3", "kind": "example", "title": "使用前检查", "bullets": [
                "0/0 型", "∞/∞ 型", "不满足前提就不能直接用"],
             "body": "", "citation_refs": [2]},
        ],
        "scripts": [
            {"slide_id": "slide-1", "text": "这一页我们先建立极限的直觉。你只需要抓住两个动作：自变量在靠近，函数值也在靠近。材料中说，极限描述的是函数在某点附近的变化趋势 [[c:1]]，所以重点不是这个点本身能不能取到，而是靠近时的趋势。"},
            {"slide_id": "slide-2", "text": "接下来把这个直觉连接到洛必达法则。课件上只列了两点，但讲的时候要补一句：洛必达不是所有极限题的万能按钮，它主要服务于未定式极限。材料里提到它是求未定式极限的重要方法 [[c:2]]，这里的关键词就是未定式。"},
            {"slide_id": "slide-3", "text": "最后看使用前检查。拿到题目不要急着求导，先确认是不是零比零或无穷比无穷。如果这个前提没满足，直接套洛必达就可能把题做错。也就是说，先验证类型，再使用法则 [[c:2]]。"},
        ],
        "cards": [
            {"kind": "concept", "title": "极限的直觉 [[c:1]]",
             "body": "当自变量无限接近某点时，函数值无限接近某个确定的数 [[c:1]]。"},
            {"kind": "quote", "title": "材料原文 [[c:2]]",
             "body": "洛必达法则是求未定式极限的重要方法 [[c:2]]。"},
            {"kind": "example", "title": "一个例子", "body": "先验证类型，再使用法则 [[c:2]]。"},
            {"kind": "note", "title": "易错提醒", "body": "未验证类型就用法则会出错。"},
        ],
        "outline": ["建立直觉", "形式化定义", "验证使用前提"],
        "keypoints": [
            {"term": "未定式", "desc": "0/0 或 ∞/∞ 型 [[c:2]]"},
            {"term": "极限", "desc": "函数在某点附近的趋势 [[c:1]]"},
        ],
        "recap": "先判断类型，再决定方法 [[c:2]]。",
        # 材料标注意图：n 对应引用表编号，服务端据此回填真实页码。
        "marks": [
            {"n": 1, "kind": "highlight", "text": "这里定义了极限，是后面所有推导的基础"},
            {"n": 2, "kind": "circle", "text": "注意：使用前必须先验证类型"},
        ],
    }, ensure_ascii=False)


def _p1_lecture_payload() -> str:
    """P1 特色页契约测试包：对比表格（好/坏）+ 金句卡（超长）+ 加粗要点。

    坏 table 的行单元格数与列数不齐 → 服务端应剥掉 table 字段、页面退化为要点页；
    好 table 原样保留；takeaway 超长应被截断到 60 字。
    """
    long_takeaway = ("记住：这类问题的关键不是记住结论，而是先判断类型再选方法，"
                     "因为不同的类型对应完全不同的处理路径，选错了方法再熟练也会做错，"
                     "这是本讲唯一需要带走的判断习惯。")   # 明显 > 60 字
    return json.dumps({
        "summary": "本讲对照两种活动代码页设置，并给出收尾金句 [[c:1]]。",
        "slides": [
            {"id": "slide-1", "kind": "concept", "title": "两种编码设置", "bullets": [
                "**936** 是简体中文 GBK 代码页", "**65001** 是 UTF-8 代码页",
                "选错会导致乱码"], "body": "", "citation_refs": [1]},
            {"id": "slide-2", "kind": "table", "title": "两种代码页对照", "bullets": [
                "逐项对照", "结论见右列"],
             "table": {"title": "936 与 65001 对照",
                       "columns": ["项目", "936", "65001"],
                       "rows": [["编码", "GBK", "UTF-8"], ["中文占用", "2 字节", "3 字节"]]},
             "body": "", "citation_refs": [1]},
            {"id": "slide-3", "kind": "table", "title": "列数不齐的坏表格", "bullets": [
                "这页会被剥掉 table 字段"],
             "table": {"title": "坏表格", "columns": ["甲", "乙", "丙"],
                       "rows": [["只有两列", "少一列"]]},
             "body": "", "citation_refs": [1]},
            {"id": "slide-4", "kind": "takeaway", "title": "本讲金句", "bullets": [],
             "takeaway": long_takeaway, "body": "", "citation_refs": [1]},
            {"id": "slide-5", "kind": "takeaway", "title": "第二句金句（应被剥除）", "bullets": [],
             "takeaway": "这是第二句金句，超出每讲一页的上限。", "body": "",
             "citation_refs": [1]},
        ],
        "scripts": [
            {"slide_id": "slide-1", "text": "这一页先建立坐标：我们讨论的是命令行里那个活动代码页。材料说切换代码页用 chcp 命令 [[c:1]]，所以先记住两个数字：936 和 65001。数字本身不难记，难的是知道它们在什么场景下用。记住这点，后面看对照表就轻松了。"},
            {"slide_id": "slide-2", "text": "现在看这张对照表。请先看第一列，它告诉我们比较的维度有哪些；再看第二列和第三列，它们分别是两种设置下的表现。你会发现中文占用的字节数不同，这不是细节，而是判断乱码来源的直接依据 [[c:1]]。"},
            {"slide_id": "slide-3", "text": "这一页我们换个角度：如果不做对照、只凭印象选，会发生什么。想象一下你在记事本里存了中文，换台机器打开就全是问号——那通常就是代码页不匹配。所以对照的意义在于把「凭感觉」换成「按维度比较」 [[c:1]]。"},
            {"slide_id": "slide-4", "text": "最后留一句给你带走的话。课程里所有方法最终都会浓缩成一个判断习惯：遇到问题先分类，再选工具。这句话听起来简单，但真正难的是每次都做到。你可以在下一次遇到乱码时试着先问自己属于哪一类 [[c:1]]。"},
            {"slide_id": "slide-5", "text": "这一页我们再补充一层理解：为什么反复强调先分类。因为分类之后，你面对的不再是一团模糊的现象，而是几个有名字的情况，每种情况都有对应做法。这样一来，经验才能被复用，而不是每次都从头猜 [[c:1]]。"},
        ],
        "cards": [
            {"kind": "concept", "title": "活动代码页 [[c:1]]",
             "body": "chcp 命令用于查看或切换当前活动代码页 [[c:1]]。"},
            {"kind": "quote", "title": "材料原文 [[c:1]]",
             "body": "代码页决定非 ASCII 字符按哪种字符集解释 [[c:1]]。"},
            {"kind": "example", "title": "一个例子", "body": "同一份文本用不同代码页解释会得到不同结果。"},
            {"kind": "note", "title": "易错提醒", "body": "只看字符数量判断编码类型并不可靠。"},
        ],
        "outline": ["两种代码页", "逐项对照", "收尾金句"],
        "keypoints": [
            {"term": "chcp", "desc": "切换活动代码页的命令 [[c:1]]"},
            {"term": "65001", "desc": "UTF-8 的代码页编号 [[c:1]]"},
        ],
        "recap": "先分类，再选工具 [[c:1]]。",
        "marks": [{"n": 1, "kind": "highlight", "text": "这是本讲所有判断的基础"}],
    }, ensure_ascii=False)


_IR_WF = {
    "schema_version": 1, "diagram_type": "workflow",
    "meta": {"title": "chcp 设置流程"},
    "lanes": [{"id": "l1", "label": "准备"}, {"id": "l2", "label": "执行"}],
    "nodes": [{"id": "open", "type": "frontend", "label": "打开命令行", "lane": "l1"},
              {"id": "chcp", "type": "backend", "label": "运行 chcp", "lane": "l2"},
              {"id": "set", "type": "backend", "label": "切换代码页", "lane": "l2"}],
    "edges": [{"id": "e1", "from": "open", "to": "chcp", "label": "输入命令"},
              {"id": "e2", "from": "chcp", "to": "set", "label": "确认编号"}],
}

# 坏 IR：多出一条指向不存在节点的边（触发 graph/dangling-ref 回执）
_IR_WF_BAD = {**_IR_WF,
              "edges": [*_IR_WF["edges"],
                        {"id": "e9", "from": "open", "to": "ghost", "label": "幽灵"}]}


def _ir_lecture_payload(ir: dict) -> str:
    """把合格讲义包的第 2 页换成 diagram 页（kind=diagram + typed IR）。"""
    base = json.loads(_lecture_payload())
    base["slides"][1] = {
        "id": "slide-2", "kind": "diagram", "title": "chcp 设置流程",
        "bullets": ["先看命令行怎么进", "再看代码页怎么切"],
        "body": "", "citation_refs": [1], "diagram": {"ir": ir},
    }
    base["scripts"][1] = {
        "slide_id": "slide-2",
        "text": "这张图画的是从打开命令行到切换代码页的两步。注意箭头是单向的："
                "先运行 chcp 看到当前编号，才知道该不该切、往哪个值切。"
                "材料里说代码页决定非 ASCII 字符怎么解释 [[c:1]]，所以这一步的顺序不能颠倒。",
    }
    return json.dumps(base, ensure_ascii=False)


def _mirror_lecture() -> str:
    """**故意违规**的课堂内容包：讲稿就是课件文字原样念一遍。

    用来验证「讲稿不许照念课件」的结构护栏：服务端应判定雷同并触发重写。
    """
    base = json.loads(_lecture_payload())
    for sc in base["scripts"]:
        slide = next(s for s in base["slides"] if s["id"] == sc["slide_id"])
        sc["text"] = "。".join([slide["title"]] + list(slide["bullets"])) + "。"
    return json.dumps(base, ensure_ascii=False)


def _spy_dump(body: dict) -> None:
    """把收到的 messages 原样落到 ZHIBAN_MOCK_SPY 文件（测试用：看提示词注入实况）。"""
    import os
    path = os.environ.get("ZHIBAN_MOCK_SPY")
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(body.get("messages") or [], f, ensure_ascii=False)
    except OSError:
        pass


def _viz_lecture_payload() -> str:
    """可视化契约测试包：好/坏 diagram 与好/坏 chart 各一页。

    坏 diagram 含 classDef（样式指令，禁止）；坏 chart 的 data 混入字符串。
    服务端应剥掉两个坏字段（页面退化为要点页），保留两个好字段且数字转 float。
    """
    return json.dumps({
        "summary": "本讲围绕批处理文件的解析流程与耗时对比展开 [[c:1]]。",
        "slides": [
            {"id": "slide-1", "kind": "concept", "title": "为什么要看解析流程", "bullets": [
                "逐行读取", "先查编码再解析"], "body": "", "citation_refs": [1]},
            {"id": "slide-2", "kind": "diagram", "title": "解析流程", "bullets": [
                "两步流程", "先读后解析"],
             "diagram": {"lang": "mermaid",
                         "code": "flowchart TD\n  A[读取文件] --> B[逐行解析]"},
             "body": "", "citation_refs": [1]},
            {"id": "slide-3", "kind": "diagram", "title": "带样式指令的坏图", "bullets": [
                "这页会被剥掉 diagram 字段"],
             "diagram": {"lang": "mermaid",
                         "code": "flowchart TD\n  A[上传] --> B[解析]\n  classDef bad fill:#f9f"},
             "body": "", "citation_refs": [1]},
            {"id": "slide-4", "kind": "chart", "title": "耗时对比", "bullets": [
                "三种方式", "单位秒"],
             "chart": {"type": "bar", "title": "三种方式耗时对比", "unit": "秒",
                       "categories": ["方式A", "方式B", "方式C"],
                       "series": [{"name": "耗时", "data": [12, 30, 25]}]},
             "body": "", "citation_refs": [2]},
            {"id": "slide-5", "kind": "chart", "title": "坏数据图表", "bullets": [
                "这页会被剥掉 chart 字段"],
             "chart": {"type": "bar", "title": "坏数据", "categories": ["甲", "乙", "丙"],
                       "series": [{"name": "值", "data": ["abc", "12", "13"]}]},
             "body": "", "citation_refs": [2]},
        ],
        "scripts": [
            {"slide_id": "slide-1", "text": "这一讲我们先看整体：为什么要关心解析流程。材料里说批处理是逐行被解释执行的 [[c:1]]，这意味着顺序和编码都会影响结果。我们先建立一个整体印象，后面的图会把这个过程拆开来看。"},
            {"slide_id": "slide-2", "text": "现在看这张图：它画的是解析的两步。左边是读取文件，右边是逐行解析。大家注意箭头的方向是单向的——正因为是单向，前面一步出了问题，后面一定跟着错。这就是材料强调先确认编码的原因 [[c:1]]。"},
            {"slide_id": "slide-3", "text": "这一页我们把刚才的流程再走一遍，重点看每一步的输入是什么。读取这一步的输入是原始字节，解析这一步的输入已经是文本了。两者混在一起就会出错，这也是接下来对比耗时时要控制的前提 [[c:2]]。"},
            {"slide_id": "slide-4", "text": "这张柱状图把三种方式的耗时放在了一起。请先看最高的那根柱子，再看最矮的。差距来自哪里？材料里提到不同实现的处理路径不同 [[c:2]]，所以耗时不同。看图时先找最大值和最小值，再解释中间的。"},
            {"slide_id": "slide-5", "text": "最后我们把前面两页合起来：流程决定了哪一步最花时间，耗时数据又反过来验证流程分析。课后请按这张图自己复述一遍整个链路，能复述出来说明这一讲真的懂了 [[c:2]]。"},
        ],
        "cards": [
            {"kind": "concept", "title": "解析流程 [[c:1]]",
             "body": "批处理文件被逐行解释执行：先读取、再解析 [[c:1]]。顺序单向，前一步出错会传导。"},
            {"kind": "quote", "title": "材料原文 [[c:2]]",
             "body": "不同实现的处理路径不同，耗时也不同 [[c:2]]。"},
            {"kind": "example", "title": "耗时对比怎么读", "body": "先看最大最小，再看中间：三种方式的差距来自处理路径 [[c:2]]。"},
            {"kind": "note", "title": "易错提醒", "body": "不要把读取阶段的错误归咎于解析阶段。"},
        ],
        "outline": ["解析流程", "耗时对比"],
        "keypoints": [
            {"term": "逐行解析", "desc": "批处理的基本执行方式 [[c:1]]"},
            {"term": "耗时对比", "desc": "三种方式差距来自处理路径 [[c:2]]"},
        ],
        "recap": "先看流程，再看数据 [[c:1]]。",
        "marks": [
            {"n": 1, "kind": "highlight", "text": "这里说明了解析是逐行的"},
            {"n": 2, "kind": "circle", "text": "耗时差异的出处"},
        ],
    }, ensure_ascii=False)


def _viz3_lecture_payload() -> str:
    """三页全部是合法 diagram —— 验证「可视化页 ≤2」的上限剥除。"""
    base = json.loads(_viz_lecture_payload())
    slides = base["slides"]
    # slide-4/5 的 chart 换成合法 diagram，变成 3 页 diagram
    slides[3] = {"id": "slide-4", "kind": "diagram", "title": "流程补充", "bullets": ["第三张图"],
                 "diagram": {"lang": "mermaid", "code": "flowchart LR\n  C[校验] --> D[执行]"},
                 "body": "", "citation_refs": [2]}
    del slides[4]
    base["scripts"] = [sc for sc in base["scripts"] if sc["slide_id"] != "slide-5"]
    return json.dumps(base, ensure_ascii=False)


def _outline_payload() -> str:
    """课程大纲：2 个单元，每单元 2 讲解 + 1 练习。"""
    return json.dumps({
        "title": "极限与洛必达法则",
        "summary": "从极限定义出发，掌握洛必达法则的使用前提与典型题型。",
        "units": [
            {"title": "极限的基础", "summary": "建立极限的直觉与定义",
             "lessons": [
                 {"title": "极限是什么", "objective": "能用自己的话解释极限 [[c:1]]",
                  "kind": "lecture", "depth": "establish"},
                 {"title": "极限的运算法则", "objective": "会用四则运算求极限 [[c:1]]",
                  "kind": "lecture", "depth": "define"},
                 {"title": "基础练习", "objective": "能完成本节的基础练习", "kind": "practice",
                  "depth": "apply"},
             ]},
            {"title": "洛必达法则", "summary": "未定式的处理",
             "lessons": [
                 {"title": "适用前提", "objective": "能判断何时可用 [[c:2]]",
                  "kind": "lecture", "depth": "define"},
                 {"title": "典型例题", "objective": "会做 0/0 与 ∞/∞ 型 [[c:2]]",
                  "kind": "lecture", "depth": "derive"},
                 {"title": "随堂练习", "objective": "能检验本单元各讲目标是否达成", "kind": "practice",
                  "depth": "apply"},
             ]},
        ],
    }, ensure_ascii=False)


def _outline_dirty(kind: str) -> str:
    """**故意违规**的大纲：kind=obj → objective 带禁词；kind=depth → depth 用脏值。"""
    obj = json.loads(_outline_payload())
    for u in obj["units"]:
        for l in u["lessons"]:
            if kind == "depth" and l["kind"] == "lecture":
                l["depth"] = "deep"          # 不在四值枚举里
            if kind == "obj" and l["kind"] == "lecture":
                l["objective"] = "了解" + l["title"] + "的概念"   # 禁词开头
    return json.dumps(obj, ensure_ascii=False)


def _pick(body: dict) -> str:
    """按 model 名路由到对应行为，返回回复文本。"""
    model = str(body.get("model") or "")
    if model == "mock-normal":
        return _quote_block()
    if model == "mock-stall-guided":
        # 恒定返回同一份引导输出：模拟「模型原地打转、一直问同一个问题」。
        return json.dumps(_guided_payload(False), ensure_ascii=False)
    if model == "mock-echo-guided":
        # 引导式路径的回显：必须是**合法 JSON**（否则走兜底模板，看不到注入内容）。
        # 只取**最后一条** system（= build_guided_messages 拼的「材料检索结果」那一块）：
        # 首条 system 是护栏规则本身，里面列了「答案是」等反面示例词，
        # 原样回显会被结论句式判定正确拦下（护栏在正常工作，但我们就看不到注入了）。
        msgs = body.get("messages") or []
        systems = [str(m.get("content") or "") for m in msgs if m.get("role") == "system"]
        roles = ",".join(str(m.get("role") or "?") for m in msgs)
        ctx = systems[-1] if systems else ""
        # 脱敏：提示词里本来就会写「答案是」这类**反面示例词**，原样回显会被
        # 护栏的结论句式判定拦下（护栏工作正常，但我们就看不到注入内容了）。
        for bad in ("正确答案是", "正确答案为", "答案是", "答案为", "最终答案", "结果是"):
            ctx = ctx.replace(bad, "结论〇")
        payload = _guided_payload(False)
        payload["summary"] = f"CTX[roles={roles}]>>>" + ctx[:800]
        return json.dumps(payload, ensure_ascii=False)
    if model == "mock-echo-context":
        # 回显注入的 system 上下文：用来**直接看到**材料到底有没有进提示词。
        # 只适用于普通（非结构化）路径；引导式路径需要合法 JSON，请用 mock-good-guided。
        msgs = body.get("messages") or []
        systems = [str(m.get("content") or "") for m in msgs if m.get("role") == "system"]
        return "===MOCK-ECHO===\n" + "\n---\n".join(systems)
    if model == "mock-good-guided":
        return json.dumps(_guided_payload(False), ensure_ascii=False)
    if model == "mock-violate-first-turn":
        return json.dumps(_guided_payload(True), ensure_ascii=False)
    if model == "mock-bad-json":
        return _bad_json()
    if model == "mock-empty-hits":
        return "这里引用了一个不存在的编号 [[c:9]]，以及一个越界的 [[c:999]]。"
    if model == "mock-cheatsheet":
        items = [
            {"point": f"要点{i}", "detail": f"第{i}条的说明内容 [[c:1]]", "cite": [1]}
            for i in range(1, 6)
        ]
        return json.dumps({"items": items}, ensure_ascii=False)
    if model == "mock-flashcard":
        items = [
            {"question": f"问题{i}", "answer": f"答案{i} [[c:1]]"} for i in range(1, 4)
        ]
        return json.dumps({"items": items}, ensure_ascii=False)
    if model == "mock-mindmap":
        return json.dumps({
            "root": {"name": "极限", "children": [
                {"name": "定义", "children": [{"name": "ε-δ 定义"}]},
                {"name": "运算法则", "children": [{"name": "四则运算"}, {"name": "复合"}]},
                {"name": "连续", "children": [{"name": "左右连续"}]},
            ]},
        }, ensure_ascii=False)
    if model == "mock-outline":
        # 课程大纲：2 个单元，每单元 2 讲解 + 1 练习
        return _outline_payload()
    if model == "mock-outline-dirty-obj":
        # objective 带禁词：首轮违规触发重试，重试轮返回干净大纲
        if "上一次输出不符合 JSON 结构要求" in "\n".join(
                str(m.get("content") or "") for m in (body.get("messages") or [])):
            return _outline_payload()
        return _outline_dirty("obj")
    if model == "mock-outline-dirtydepth":
        # 讲次 depth 用脏值 "deep"：应被归一到四值枚举（不触发重试，直接落库）
        return _outline_dirty("depth")
    if model == "mock-spy-outline":
        _spy_dump(body)
        return _outline_payload()
    if model == "mock-spy-lecture":
        _spy_dump(body)
        return _lecture_payload()
    if model == "mock-lecture-p1":
        return _p1_lecture_payload()
    if model in ("mock-lecture-ir", "mock-lecture-ir-bad", "mock-lecture-ir-stubborn"):
        asked_rewrite = "课堂图示编辑" in "\n".join(
            str(m.get("content") or "") for m in (body.get("messages") or []))
        if model == "mock-lecture-ir-bad" and asked_rewrite:
            # 收到带诊断的回执 → 给出修好的 IR（验证「回执重写」这条路径真的通）
            return json.dumps({"diagram": {"ir": _IR_WF}}, ensure_ascii=False)
        if model == "mock-lecture-ir-stubborn":
            # 无论重写几次都坏 → 应退化为要点页
            return _ir_lecture_payload(_IR_WF_BAD)
        if model == "mock-lecture-ir-bad":
            return _ir_lecture_payload(_IR_WF_BAD)
        return _ir_lecture_payload(_IR_WF)
    if model == "mock-lecture-viz":
        return _viz_lecture_payload()
    if model == "mock-lecture-viz3":
        return _viz3_lecture_payload()
    if model == "mock-lecture":
        return _lecture_payload()
    if model in ("mock-lecture-mirror", "mock-lecture-stubborn"):
        # 「讲稿照念课件」护栏的两种测试路径：
        #   mock-lecture-mirror   → 收到定向重写指令后给出合格讲稿（验证「重写一次」）
        #   mock-lecture-stubborn → 无论重写几次都照念（验证「确定性扩写」兜底）
        asked_rewrite = "只是把课件文字念了一遍" in "\n".join(
            str(m.get("content") or "") for m in (body.get("messages") or [])
        )
        if asked_rewrite and model == "mock-lecture-mirror":
            return _lecture_payload()
        return _mirror_lecture()
    if model == "mock-summary":
        return json.dumps({
            "recap": "本单元先建立极限的直觉与定义，再进入洛必达法则的适用前提。",
            "mastered": ["能用自己的话解释极限", "能判断 0/0 与 ∞/∞ 型"],
            "weak_points": ["对「使用前必须验证类型」这一步还不够熟"],
            "next_steps": ["把易错点那节课重看一遍", "重做一次随堂练习"],
            "score_note": "练习整体正确率不错，主要是细节容易漏。",
        }, ensure_ascii=False)
    if model == "mock-practice":
        return json.dumps({"items": [
            {"type": "single", "stem": "洛必达法则适用于哪种未定式 [[c:2]]",
             "options": ["0/0 型", "1/0 型", "0·∞ 型", "∞-∞ 型"],
             "answer": 0, "explanation": "材料指出适用于 0/0 或 ∞/∞ 型 [[c:2]]"},
            {"type": "boolean", "stem": "使用前必须先验证类型 [[c:2]]",
             "options": ["正确", "错误"], "answer": 0,
             "explanation": "材料强调先验证 [[c:2]]"},
            {"type": "fill_in", "stem": "洛必达法则处理的两种未定式是 ____ 与 ____",
             "answer": ["0/0 型 与 ∞/∞ 型", "0/0和∞/∞"], "explanation": "见材料 [[c:2]]"},
            {"type": "open", "stem": "说说使用洛必达法则前要做什么",
             "answer": "先验证是否为 0/0 或 ∞/∞ 型未定式", "explanation": "要点：验证类型"},
            {"type": "single", "stem": "看图：图中划线部分讲的是什么 [[c:2]]",
             "options": ["极限的定义", "洛必达法则的适用前提", "连续性", "导数运算法则"],
             "answer": 1, "explanation": "见材料对应页 [[c:2]]", "image": {"n": 2}},
        ]}, ensure_ascii=False)
    if model == "mock-practice-hands":
        # 含 1 道回填式实操题（hands_on）：课程开关**开启**时保留、可判分；
        # 课程开关**关闭**时应在 _validate_practice 被兜底剥除（提示词已禁止，
        # 这里验证的是模型不听话时的第二道防线）。
        return json.dumps({"items": [
            # hands_on 放第一题：练习页一次只显示一题（无状态 dump 只能看到第 1 题），
            # 放到首位才能让「实操题徽章」的端到端断言看到它。
            {"type": "hands_on",
             "stem": "打开 CMD 输入 chcp 并回车，把你看到的活动代码页编号填进来",
             "answer": ["936", "65001"], "explanation": "材料提到 936 与 65001 [[c:1]]"},
            {"type": "single", "stem": "chcp 命令的作用是 [[c:1]]",
             "options": ["切换活动代码页", "复制文件", "查看磁盘", "结束进程"],
             "answer": 0, "explanation": "材料说明 chcp 用于切换活动代码页 [[c:1]]"},
            {"type": "fill_in", "stem": "UTF-8 对应的代码页编号是 ____",
             "answer": ["65001"], "explanation": "见材料 [[c:1]]"},
            {"type": "boolean", "stem": "代码页决定非 ASCII 字符的解释方式 [[c:1]]",
             "options": ["正确", "错误"], "answer": 0, "explanation": "材料原文 [[c:1]]"},
        ]}, ensure_ascii=False)
    if model == "mock-grade":
        return json.dumps({"correct": True, "score": 0.8,
                           "feedback": "答出了核心要点，建议补充「先验证类型」这一步。"},
                          ensure_ascii=False)
    if model == "mock-quiz":
        items = [
            {"stem": f"第{i}题：洛必达法则适用于哪种未定式？",
             "options": ["0/0", "1/0", "∞-∞", "0·∞"],
             "answer_index": 0, "explanation": f"解析{i} [[c:1]]"}
            for i in range(1, 4)
        ]
        return json.dumps({"items": items}, ensure_ascii=False)
    if model == "mock-goals":
        return json.dumps({
            "goals": [
                "能用自己的话解释极限的定义",
                "能判断一道题该不该用洛必达法则",
                "能独立完成 0/0 与 ∞/∞ 型的极限计算",
                "能说出使用洛必达法则前必须验证的两个前提",
            ],
        }, ensure_ascii=False)
    if model == "mock-spy-goals":
        # 落盘收到的 messages，供断言「推荐喂的是材料概览、且没越界到未勾选文档」
        _spy_dump(body)
        return json.dumps({
            "goals": [
                "能读懂《孔雀东南飞》的叙事脉络",
                "能翻译并背诵指定段落",
                "能辨析文中的偏义复词与古今异义",
                "能说明乐府诗与近体诗在形式上的差别",
            ],
        }, ensure_ascii=False)
    if model == "mock-outline-5":
        return json.dumps({
            "title": "极限与洛必达法则（五单元）",
            "summary": "极限定义、运算法则、洛必达法则、连续性、综合练习。",
            "units": [
                {"title": f"单元{i}", "summary": f"第{i}单元简介",
                 "lessons": [
                     {"title": f"单元{i}·讲解1", "objective": "能运用核心概念解题", "kind": "lecture", "depth": "define"},
                     {"title": f"单元{i}·讲解2", "objective": "能完成典型例题", "kind": "lecture", "depth": "derive"},
                     {"title": f"单元{i}·练习", "objective": "巩固本节内容", "kind": "practice", "depth": "apply"},
                 ]}
                for i in range(1, 6)
            ],
        }, ensure_ascii=False)
    if model == "mock-echo-flags":
        # 用于验证 LLM 请求参数（如 enable_thinking）是否被正确下发。
        return json.dumps({
            "goals": [f"enable_thinking={body.get('enable_thinking')}"],
        }, ensure_ascii=False)
    return f"[mock] 收到：{body.get('messages', [{}])[-1].get('content', '')[:200]}"


def _chunk_text(text: str, size: int = 12):
    """把文本切成增量块。"""
    for i in range(0, len(text), size):
        yield text[i:i + size]


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "mock-llm"}


# 真实 OpenAI 兼容端点都会暴露模型清单；补上它，才能验证
# 「模型名不在端点可用列表」这类提示（这正是用户实际踩到的坑）。
MOCK_MODELS = ("mock-normal", "mock-violate-first-turn", "mock-bad-json", "mock-empty-hits",
               "mock-echo-context", "mock-echo-guided", "mock-good-guided", "mock-stall-guided",
               "mock-outline", "mock-lecture", "mock-practice", "mock-grade",
               "mock-summary", "mock-goals", "mock-spy-goals", "mock-outline-5", "mock-echo-flags",
               "mock-lecture-p1", "mock-practice-hands",
               "mock-lecture-ir", "mock-lecture-ir-bad", "mock-lecture-ir-stubborn",
               "mock-lecture-mirror", "mock-lecture-stubborn")


@app.get("/v1/models")
def list_models() -> JSONResponse:
    """OpenAI 兼容模型列表。"""
    return JSONResponse({
        "object": "list",
        "data": [{"id": m, "object": "model", "owned_by": "mock"} for m in MOCK_MODELS],
    })


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI 兼容 chat/completions（支持 stream=true 的 SSE）。"""
    body = await request.json()
    text = _pick(body)
    created = int(time.time())
    model = str(body.get("model") or "mock")

    if body.get("stream"):
        def sse():
            for piece in _chunk_text(text):
                frame = {
                    "id": "chatcmpl-mock", "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"
            done = {
                "id": "chatcmpl-mock", "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream")

    return JSONResponse({
        "id": "chatcmpl-mock", "object": "chat.completion", "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": len(text), "total_tokens": 10 + len(text)},
    })


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    """OpenAI 兼容 embeddings（返回确定性伪向量，仅用于走通检索融合）。"""
    body = await request.json()
    inputs = body.get("input")
    inputs = inputs if isinstance(inputs, list) else [inputs]

    def vec(t: str) -> list[float]:
        """确定性「字符 n-gram」哈希词袋向量（已 L2 归一化）。

        语义上等价于词法相似度：字符片段重叠越多余弦越高，无重叠则接近 0。
        足以让「相关 → 召回」「材料外 → 召回为空」两类判定稳定复现。
        长度不足 2 的文本退化为整串哈希。
        """
        s = str(t)
        feats: list[str] = []
        for n in (2, 3):
            if len(s) >= n:
                feats.extend(s[i:i + n] for i in range(len(s) - n + 1))
        if not feats:
            feats = [s or " "]
        v = [0.0] * EMBED_DIM
        for f in feats:
            v[zlib.crc32(f.encode("utf-8")) % EMBED_DIM] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    data = [{"object": "embedding", "index": i, "embedding": vec(t)} for i, t in enumerate(inputs)]
    return JSONResponse({
        "object": "list", "data": data,
        "model": body.get("model") or "mock-embed",
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    })


if __name__ == "__main__":
    # 端口可用 ZHIBAN_MOCK_PORT 覆盖（多套自测并行时避免抢 8761）。
    import os

    uvicorn.run(
        app, host="127.0.0.1",
        port=int(os.environ.get("ZHIBAN_MOCK_PORT", "8761")),
        log_level="warning",
    )
