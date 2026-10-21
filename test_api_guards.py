"""接口级业务守卫回归测试（不调用真实模型）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from function_tools import guard_function_stage


def test_side_functions_require_stage():
    assert guard_function_stage({"_completed_stage": None}, "pre_survey")
    assert guard_function_stage({"_completed_stage": "initiation"}, "pre_survey") is None
    assert guard_function_stage({"_completed_stage": "field"}, "seek_opinion")
    assert guard_function_stage({"_completed_stage": "report"}, "seek_opinion") is None
    assert guard_function_stage({"_completed_stage": "field"}, "overdue_supervise")
    assert guard_function_stage({"_completed_stage": "report"}, "overdue_supervise") is None
    assert guard_function_stage({"_completed_stage": "report"}, "file_materials")
    assert guard_function_stage({"_completed_stage": "archive"}, "file_materials") is None


def test_unknown_function_is_not_blocked_by_stage_guard():
    assert guard_function_stage({"_completed_stage": None}, "unknown") is None


if __name__ == "__main__":
    test_side_functions_require_stage()
    test_unknown_function_is_not_blocked_by_stage_guard()
    print("api guard tests passed")
