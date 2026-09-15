"""评测专用后端启动器：拦截图示编译器，把**真实回执**落盘。

为什么需要它：`_compile_diagrams` 用 `logger.info(..., extra={"extra_fields": {...}})`
记录失败规则码，但控制台格式化器不打印 extras，跑评测时看不到「到底违反了哪条规则」。
本启动器在进程内 wrap `diagram.compile_ir`，把每次调用的回执（含失败诊断与 IR 全文）
追加写进 `E2E_RECEIPTS` 指定的 JSONL。

**只用于测试，不进产品包**（放在 dev 之外的 .tmp 里，也不改任何产品代码）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import backend.services.diagram as D  # noqa: E402

_OUT = Path(os.environ.get("E2E_RECEIPTS") or (ROOT / ".tmp" / "receipts.jsonl"))
_orig = D.compile_ir


def _patched(ir):  # type: ignore[no-untyped-def]
    svg, rec = _orig(ir)
    try:
        with _OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ok": rec.get("ok"),
                "stage": rec.get("stage"),
                "diagram_type": rec.get("diagram_type"),
                "diagnostics": rec.get("diagnostics"),
                "supportedFixes": rec.get("supportedFixes"),
                "failed_ir": None if rec.get("ok") else ir,
                "svg_bytes": len(svg) if svg else 0,
            }, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass
    return svg, rec


D.compile_ir = _patched

from backend.main import run_server  # noqa: E402

if __name__ == "__main__":
    run_server(reload=False)
