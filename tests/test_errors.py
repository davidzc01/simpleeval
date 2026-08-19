"""errors 模块单元测试"""

import pytest
from fastapi import HTTPException

from app.errors import (
    raise_http_error,
    project_not_found,
    evalset_not_found,
    run_not_found,
    invalid_config,
    no_enabled_cases,
    import_format_error,
    mapping_invalid,
    target_api_error,
    judge_api_error,
    network_error,
    internal_error,
)


class TestRaiseHttpError:
    """HTTP 错误抛出测试"""

    def test_raise_http_error(self):
        """测试 raise_http_error"""
        with pytest.raises(HTTPException) as exc_info:
            raise_http_error("test_error", "测试错误消息", 400)

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == {
            "error": {
                "code": "test_error",
                "message": "测试错误消息"
            }
        }

    def test_raise_http_error_default_status(self):
        """默认状态码 404"""
        with pytest.raises(HTTPException) as exc_info:
            raise_http_error("not_found", "资源不存在")

        assert exc_info.value.status_code == 404


class TestProjectErrors:
    """项目相关错误测试"""

    def test_project_not_found(self):
        """项目不存在错误"""
        with pytest.raises(HTTPException) as exc_info:
            project_not_found("proj-123")

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail["error"]["code"] == "project_not_found"
        assert "proj-123" in exc_info.value.detail["error"]["message"]


class TestEvalSetErrors:
    """评测集相关错误测试"""

    def test_evalset_not_found(self):
        """评测集不存在错误"""
        with pytest.raises(HTTPException) as exc_info:
            evalset_not_found("evalset-456")

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail["error"]["code"] == "evalset_not_found"
        assert "evalset-456" in exc_info.value.detail["error"]["message"]


class TestRunErrors:
    """Run 相关错误测试"""

    def test_run_not_found(self):
        """Run 不存在错误"""
        with pytest.raises(HTTPException) as exc_info:
            run_not_found("run-789")

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail["error"]["code"] == "run_not_found"
        assert "run-789" in exc_info.value.detail["error"]["message"]


class TestConfigErrors:
    """配置相关错误测试"""

    def test_invalid_config(self):
        """配置校验失败"""
        with pytest.raises(HTTPException) as exc_info:
            invalid_config("base_url 不能为空")

        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["error"]["code"] == "invalid_config"
        assert "base_url 不能为空" in exc_info.value.detail["error"]["message"]


class TestEvalSetCaseErrors:
    """评测集 case 相关错误测试"""

    def test_no_enabled_cases(self):
        """无启用 case"""
        with pytest.raises(HTTPException) as exc_info:
            no_enabled_cases()

        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["error"]["code"] == "no_enabled_cases"

    def test_import_format_error(self):
        """导入格式错误"""
        with pytest.raises(HTTPException) as exc_info:
            import_format_error("CSV 第 3 行格式错误")

        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["error"]["code"] == "import_format_error"
        assert "CSV 第 3 行格式错误" in exc_info.value.detail["error"]["message"]

    def test_mapping_invalid(self):
        """映射无效"""
        with pytest.raises(HTTPException) as exc_info:
            mapping_invalid("$.data.missing 路径不存在")

        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["error"]["code"] == "mapping_invalid"


class TestAPIErrors:
    """API 相关错误测试"""

    def test_target_api_error(self):
        """目标 API 错误"""
        with pytest.raises(HTTPException) as exc_info:
            target_api_error("401 Unauthorized")

        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["error"]["code"] == "target_api_error"

    def test_judge_api_error(self):
        """Judge API 错误"""
        with pytest.raises(HTTPException) as exc_info:
            judge_api_error("403 Forbidden")

        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["error"]["code"] == "judge_api_error"

    def test_network_error(self):
        """网络错误"""
        with pytest.raises(HTTPException) as exc_info:
            network_error("连接超时")

        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["error"]["code"] == "network_error"

    def test_internal_error(self):
        """内部错误"""
        with pytest.raises(HTTPException) as exc_info:
            internal_error("未知错误")

        assert exc_info.value.status_code == 500
        assert exc_info.value.detail["error"]["code"] == "internal_error"
