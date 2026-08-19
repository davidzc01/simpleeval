"""统一错误处理模块"""

from fastapi import HTTPException
from .models import ErrorResponse, ErrorDetail


# 错误码常量
class ErrorCode:
    PROJECT_NOT_FOUND = "project_not_found"
    EVALSET_NOT_FOUND = "evalset_not_found"
    RUN_NOT_FOUND = "run_not_found"
    INVALID_CONFIG = "invalid_config"
    NO_ENABLED_CASES = "no_enabled_cases"
    IMPORT_FORMAT_ERROR = "import_format_error"
    MAPPING_INVALID = "mapping_invalid"
    TARGET_API_ERROR = "target_api_error"
    JUDGE_API_ERROR = "judge_api_error"
    NETWORK_ERROR = "network_error"
    INTERNAL_ERROR = "internal_error"


def raise_http_error(code: str, message: str, status_code: int = 404):
    """抛出 HTTP 错误"""
    raise HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message}}
    )


def project_not_found(project_id: str):
    """项目不存在"""
    raise_http_error(
        ErrorCode.PROJECT_NOT_FOUND,
        f"找不到项目：{project_id}",
        404
    )


def evalset_not_found(evalset_id: str):
    """评测集不存在"""
    raise_http_error(
        ErrorCode.EVALSET_NOT_FOUND,
        f"找不到评测集：{evalset_id}",
        404
    )


def run_not_found(run_id: str):
    """Run 不存在"""
    raise_http_error(
        ErrorCode.RUN_NOT_FOUND,
        f"找不到 run：{run_id}",
        404
    )


def invalid_config(message: str):
    """配置校验失败"""
    raise_http_error(
        ErrorCode.INVALID_CONFIG,
        message,
        422
    )


def no_enabled_cases():
    """无启用的 case"""
    raise_http_error(
        ErrorCode.NO_ENABLED_CASES,
        "评测集没有启用的 case",
        422
    )


def import_format_error(message: str):
    """导入格式错误"""
    raise_http_error(
        ErrorCode.IMPORT_FORMAT_ERROR,
        message,
        422
    )


def mapping_invalid(message: str):
    """映射无效"""
    raise_http_error(
        ErrorCode.MAPPING_INVALID,
        message,
        422
    )


def target_api_error(message: str):
    """目标 API 错误"""
    raise_http_error(
        ErrorCode.TARGET_API_ERROR,
        message,
        502
    )


def judge_api_error(message: str):
    """Judge API 错误"""
    raise_http_error(
        ErrorCode.JUDGE_API_ERROR,
        message,
        502
    )


def network_error(message: str):
    """网络错误"""
    raise_http_error(
        ErrorCode.NETWORK_ERROR,
        message,
        502
    )


def internal_error(message: str):
    """内部错误"""
    raise_http_error(
        ErrorCode.INTERNAL_ERROR,
        message,
        500
    )
