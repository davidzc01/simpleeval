#!/usr/bin/env python3
"""validation_set.xlsx → simpleEval 可导入评测集 JSON

字段映射：
- case_name = "{领导}-{企业}"
- input = "{领导}-{企业}\n\n{新闻全文}"（single-input 承载三变量，见方案 A）
- eval_type = llm_judge（content 内嵌 JSON 无法用规则类精确判定，先走 Judge 绕行）
- output_requirement = 按"预期结果"生成，让 Judge 判定是否符合预期
"""
import json
import sys

import pandas as pd

SRC = "/Users/davidchen/Desktop/模型Eval/走访提取Eval/validation_set.xlsx"
DST = "/Users/davidchen/Documents/code/career/david/projects/simpleeval/examples/zoufang-tiqu-evalset.json"


def build_cases(df: pd.DataFrame) -> list[dict]:
    cases = []
    for _, row in df.iterrows():
        leader = str(row["领导名称"]).strip()
        enterprise = str(row["企业名称"]).strip()
        content = str(row["新闻内容"]).strip()
        expected = bool(row["预期结果"])

        expected_word = "true" if expected else "false"
        cases.append({
            "case_name": f"{leader}-{enterprise}",
            "input": f"{leader}-{enterprise}\n\n{content}",
            "expected_output": None,
            "output_requirement": (
                f"输出 JSON 的 result 字段应为 {expected_word}。"
                f"若 result 为 {expected_word} 回答 1，否则回答 0。"
            ),
            "eval_type": "llm_judge",
            "eval_params": {},
        })
    return cases


def main():
    df = pd.read_excel(SRC)
    cases = build_cases(df)
    with open(DST, "w", encoding="utf-8") as f:
        json.dump(cases, f, ensure_ascii=False, indent=2)
    print(f"生成 {len(cases)} 条 case → {DST}")
    print(f"预期 true: {sum(1 for c in cases if 'true' in c['output_requirement'][:30])}, "
          f"预期 false: {sum(1 for c in cases if 'false' in c['output_requirement'][:30])}")


if __name__ == "__main__":
    main()
