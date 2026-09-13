"""领域异常、错误码表与全局异常处理器（架构文档 §6.2 / §14.4）。

统一返回封套::

    成功：{"code": 0, "message": "ok", "data": {...}}
    失败：{"code": <非0>, "message": "<中文摘要>", "data": null, "error": {"detail": "..."}}

业务错误一律抛 :class:`AppError`，由本模块注册的处理器转成标准封套。
**注意**：``message`` / ``detail`` 中不得出现密钥或敏感绝对路径。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

__all__ = [
    "ERROR_TABLE",
    "AppError",
    "ok",
    "fail",
    "register_exception_handlers",
]

# code -> (http_status, 中文摘要)
ERROR_TABLE: dict[int, tuple[int, str]] = {
    0: (200, "成功"),
    1000: (400, "参数校验失败"),
    1001: (404, "资源不存在"),
    1002: (409, "状态不允许"),
    2000: (400, "模型未配置"),
    2001: (502, "模型调用失败"),
    2002: (502, "嵌入失败"),
    2003: (502, "连接测试失败"),
    3000: (415, "不支持的格式"),
    3001: (500, "解析失败"),
    3002: (413, "文件过大"),
    3003: (502, "网页抓取失败"),
    4000: (503, "ASR 模型不可用"),
    4001: (500, "ASR 识别失败"),
    4002: (400, "TTS 未配置"),
    4003: (502, "TTS 合成失败"),
    5000: (500, "生成失败"),
    5001: (500, "导出失败"),
    5002: (500, "课程生成失败"),
    5003: (500, "判分失败"),
    5004: (500, "单元总结生成失败"),
    9000: (500, "内部错误"),
}


class AppError(Exception):
    """业务领域异常，经全局处理器转成标准封套。

    Attributes:
        code: 业务错误码（见 :data:`ERROR_TABLE`）。
        message: 可直接展示的中文摘要。
        detail: 可选技术细节（不得含密钥/敏感路径）。
        http_status: 对应 HTTP 状态码。
    """

    def __init__(
        self,
        code: int,
        message: str | None = None,
        detail: str | None = None,
    ) -> None:
        http_status, default_msg = ERROR_TABLE.get(code, (500, "内部错误"))
        self.code: int = int(code)
        self.message: str = message or default_msg
        self.detail: str | None = detail
        self.http_status: int = http_status
        super().__init__(self.message)


def ok(data: Any = None, message: str = "ok") -> dict[str, Any]:
    """构造成功封套。

    Args:
        data: 业务数据（可为 ``None``）。
        message: 提示信息，默认 ``"ok"``。

    Returns:
        标准成功封套字典。
    """
    return {"code": 0, "message": message, "data": data}


def fail(
    code: int,
    message: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """构造失败封套。

    Args:
        code: 业务错误码。
        message: 中文摘要，默认取错误码表。
        detail: 可选技术细节。

    Returns:
        标准失败封套字典。
    """
    _, default_msg = ERROR_TABLE.get(code, (500, "内部错误"))
    payload: dict[str, Any] = {
        "code": int(code),
        "message": message or default_msg,
        "data": None,
    }
    if detail:
        payload["error"] = {"detail": detail}
    return payload


def register_exception_handlers(app: FastAPI) -> None:
    """向 FastAPI 应用注册全局异常处理器。

    覆盖：:class:`AppError`、请求校验错误、HTTP 异常、以及未捕获异常兜底。
    """

    @app.exception_handler(AppError)
    async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=fail(exc.code, exc.message, exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ()))
        detail = f"{loc}: {first.get('msg', '校验失败')}" if loc else "请求参数不合法"
        return JSONResponse(status_code=400, content=fail(1000, None, detail))

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(
        _: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # 404/405 等映射为 1001；其余按 9000 兜底。
        if exc.status_code == 404:
            return JSONResponse(status_code=404, content=fail(1001, "资源不存在"))
        default = ERROR_TABLE[9000][1]
        message = exc.detail if isinstance(exc.detail, str) and exc.detail else default
        return JSONResponse(status_code=exc.status_code, content=fail(9000, message))

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        # 兜底：绝不把原始异常文本直接暴露（可能含敏感信息），仅给出类型名。
        return JSONResponse(
            status_code=500,
            content=fail(9000, None, f"未处理异常：{type(exc).__name__}"),
        )
