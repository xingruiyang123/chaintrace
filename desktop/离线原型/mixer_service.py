"""混币—法币出口识别的桥接服务：给 Electron 侧提供 run / export 两个动作。

写法照抄同目录的 service.py：同样是纯标准库、同样先校验再落盘、
同样在没有通过校验时一个文件都不写。

导出产物（契约 §4）：
    混币出口识别结果.json   完整结果，结构与输入契约 §3 一致
    断链归因.csv            一行一对候选，便于在表格里复核
    运行记录.json           参数、哈希、边界语，供审计
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from algo2 import graph, pipeline  # noqa: E402

SAMPLE_PATH = HERE / "样例" / "混币出口虚拟样例.json"

BOUNDARY = "本页全部数据为虚构模拟数据，仅用于展示原型能力；混币—法币的对应关系为算法候选，不表示资金转移已确认。"


class MixerInputError(ValueError):
    """面向人的中文错误。"""


def read_input(path: str | Path) -> dict:
    try:
        with Path(path).open(encoding="utf-8-sig") as handle:
            return json.load(handle)
    except OSError as exc:
        raise MixerInputError(f"无法读取混币样例文件：{path}（{exc}）") from exc
    except ValueError as exc:
        raise MixerInputError(f"混币样例不是合法 JSON：{path}（{exc}）") from exc


def run(path: str | Path) -> tuple[dict, dict]:
    """读入 → 校验 → 跑完整管道。返回（结果, 输入数据集）。

    结果里的 source_file_sha256 是这份文件的字节哈希；
    input_sha256 是解析后对象的规范化哈希。验收脚本比对字节时用前者。
    """
    raw = read_input(path)
    digest = graph.file_sha256(path)
    try:
        dataset = graph.load_dataset(raw)
    except graph.InputError as exc:
        raise MixerInputError(str(exc)) from exc
    result = pipeline.run_dataset(dataset, source_file_sha256=digest)
    return result, raw


def _write_failure(out_dir: str | Path, exc: Exception) -> None:
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    record = {
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "status": "rejected",
        "error": str(exc),
        "is_simulated": True,
    }
    (target / "运行失败.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _check(result: dict, dataset: dict | None) -> None:
    """落盘前先校验：结构不对、或输入被改过，就一个字都不写。"""
    if dataset is not None:
        try:
            checked = graph.load_dataset(dataset)
        except graph.InputError as exc:
            raise MixerInputError(f"输入数据集不合法：{exc}") from exc
        if graph.sha256(dataset) != result.get("input_sha256"):
            raise MixerInputError("输入数据集与识别结果的 input_sha256 不一致，无法导出。")
        problems = pipeline.validate_result(result, checked)
    else:
        problems = pipeline.validate_result(result)
    if problems:
        raise MixerInputError("识别结果未通过结构校验：" + "；".join(problems[:6]))


def _write_csv(result: dict, target: Path) -> int:
    rows = 0
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "案件",
                "链路编号",
                "池",
                "入金边",
                "出金边",
                "归因分",
                "档位",
                "排名",
                "金额",
                "时间",
                "面额",
                "链上位置",
                "批次共现",
                "费率",
                "构造真值命中",
                "解释摘要",
            ]
        )
        for case in result["cases"]:
            for link in case["links"]:
                features = link["features"]
                rows += 1
                writer.writerow(
                    [
                        case["case_id"],
                        link["link_id"],
                        link["pool_id"],
                        link["deposit_edge"],
                        link["withdrawal_edge"],
                        link["score"],
                        link["tier"],
                        link["rank"],
                        features["amount"]["value"],
                        features["time"]["value"],
                        features["denomination"]["value"],
                        features["path_rarity"]["value"],
                        features["cooccurrence"]["value"],
                        features["fee_fit"]["value"],
                        "是" if link.get("ground_truth_hit") else "否",
                        "；".join(link.get("explain", [])[:2]),
                    ]
                )
    return rows


def export(result: dict, out_dir: str | Path, dataset: dict | None = None) -> dict:
    """落盘三份产物并返回运行记录。校验不过就不写任何文件。"""
    _check(result, dataset)
    dest = Path(out_dir)
    dest.mkdir(parents=True, exist_ok=True)

    result_path = dest / "混币出口识别结果.json"
    csv_path = dest / "断链归因.csv"
    log_path = dest / "运行记录.json"

    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    link_rows = _write_csv(result, csv_path)

    log = {
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "status": "completed",
        "is_simulated": True,
        "boundary": BOUNDARY,
        "input_sha256": result.get("input_sha256"),
        "source_file_sha256": result.get("source_file_sha256"),
        "result_generated_at": result.get("generated_at"),
        "cases": len(result.get("cases", [])),
        "counts": {
            "cases": len(result.get("cases", [])),
            "mixer_pools": sum(len(c.get("mixer_pools", [])) for c in result.get("cases", [])),
            "links": sum(len(c.get("links", [])) for c in result.get("cases", [])),
            "offramps": sum(len(c.get("offramps", [])) for c in result.get("cases", [])),
            "paths": sum(len(c.get("paths", [])) for c in result.get("cases", [])),
        },
        "link_rows": link_rows,
        "parameters": result.get("parameters"),
        "boundaries": result.get("boundaries"),
        "output_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (result_path, csv_path)
        },
    }
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return log


def run_file(input_path: str | Path, out_dir: str | Path) -> tuple[dict, dict]:
    try:
        result, dataset = run(input_path)
        return result, export(result, out_dir, dataset)
    except (MixerInputError, graph.InputError, ValueError, TypeError, KeyError, OSError) as exc:
        _write_failure(out_dir, exc)
        raise


if __name__ == "__main__":
    # 手工跑：python mixer_service.py <输入文件> [输出目录]
    src = sys.argv[1] if len(sys.argv) > 1 else str(SAMPLE_PATH)
    out = sys.argv[2] if len(sys.argv) > 2 else str(HERE / "样例" / "导出")
    payload, record = run_file(src, out)
    print(json.dumps(record, ensure_ascii=False, indent=2))
