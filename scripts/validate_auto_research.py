"""Opt-in real CLI validation, with private snapshots and resumable offline auditing."""
import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / "artifacts/agent_chats/auto_validation"
JSON_HINT = "收益率、回撤和波动率均使用工具原始小数，均量使用手，不换算百分比或万手。最终只输出要求的JSON，随后另起一行单独输出TERMINATE。"
SCENARIOS = [
    {"name": "factor", "roles": ["A_Share_Factor_Researcher"], "tools": ["get_a_share_factors"],
     "prompt": "计算000651截至2024-12-31的20日动量、60日动量、20日年化波动率和20日平均成交量。JSON字段为symbol、data_date、factors，其中factors包含momentum_20d、momentum_60d、volatility_20d、avg_volume_20d四个原始数值。" + JSON_HINT},
    {"name": "ideation", "roles": ["A_Share_Factor_Ideator"], "tools": [],
     "prompt": "为我设计一个价量因子，持有条件严格为20日动量大于0且5日平均成交量高于20日平均成交量；否则空仓。只提出这一个候选，不增加其他过滤条件，不执行回测。JSON字段为name、hypothesis、rule_json（规则对象）、risk_notes、validation_status（未回测）。" + JSON_HINT},
    {"name": "rule_backtest", "roles": ["A_Share_Factor_Ideator"], "tools": ["backtest_a_share_strategy"],
     "prompt": "回测刚才这个因子规则：股票000651，2023-01-01至2024-12-31，规则保持不变，资金与费用沿用工具默认配置。JSON字段为symbol、start、end、rule_json、strategy_total_return、buy_hold_total_return、max_drawdown、sharpe、audit_ok、csi300_available。不可把无法取得的基准填成数值。" + JSON_HINT},
    {"name": "switch_symbol", "roles": ["A_Share_Factor_Ideator"], "tools": ["backtest_a_share_strategy"],
     "prompt": "换成600031，2025-01-01至2025-12-31，继续用刚才同一个规则重新计算，规则和默认费用保持不变。JSON字段仍为symbol、start、end、rule_json、strategy_total_return、buy_hold_total_return、max_drawdown、sharpe、audit_ok、csi300_available。" + JSON_HINT},
    {"name": "multiple_roles", "roles": ["A_Share_Factor_Researcher", "Quant_Analyst"],
     "tools": ["get_a_share_factors", "read_backtest_result"],
     "prompt": "请分开完成并汇总两项：计算000651截至2024-12-31的价量因子；读取原V2固定实验000001、2021-2025年的人工SMA及买入持有已保存指标，不用交互股票替代固定样本。最终JSON含factor和fixed_reference。factor含symbol、data_date、momentum_20d、momentum_60d、volatility_20d、avg_volume_20d；fixed_reference含symbol、sma_total_return、sma_max_drawdown、buy_hold_total_return、buy_hold_max_drawdown。两个来源与日期不可混用。" + JSON_HINT},
]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def answer_json(answer):
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", answer):
        try:
            value, _ = decoder.raw_decode(answer[match.start():])
            if isinstance(value, dict):
                return value
        except ValueError:
            continue
    raise ValueError("No JSON object in the final answer")


def close_number(actual, expected):
    if isinstance(actual, dict):
        actual = actual["value"]
    if expected is None:
        assert actual is None, (actual, expected)
    else:
        assert not isinstance(actual, bool)
        assert math.isfinite(float(actual)) and math.isclose(float(actual), float(expected), rel_tol=1e-7, abs_tol=1e-7), (actual, expected)


def rule_signal(rule):
    if isinstance(rule, str):
        rule = json.loads(rule)
    signal = rule["signal"]
    parts = []
    for part in signal["all"]:
        normalized = dict(part)
        normalized["value"] = float(normalized.get("value", 0))
        parts.append(json.dumps(normalized, sort_keys=True))
    return sorted(parts)


def tool_calls(record):
    return [call["function"] for chat in record.get("chats", []) for message in chat["history"]
            for call in (message.get("tool_calls") or [])]


def observed(run, turn, function):
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in (run / "observed").glob("*.json")]
    return sorted((row for row in rows if row["turn"] == turn and row["function"] == function),
                  key=lambda row: row["recorded_at"])


def audit_factor(run, turn, answer):
    import pandas as pd
    row = observed(run, turn, "get_a_share_factors")[-1]
    path = run / "observed" / row["data_file"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == row["data_sha256"]
    bars = pd.read_csv(path, index_col="Date", parse_dates=True).sort_index()
    close, volume = bars.Close, bars.Volume
    expected = {"momentum_20d": close.iloc[-1] / close.iloc[-21] - 1,
                "momentum_60d": close.iloc[-1] / close.iloc[-61] - 1,
                "volatility_20d": close.pct_change(fill_method=None).tail(20).std(ddof=1) * math.sqrt(252),
                "avg_volume_20d": volume.tail(20).mean()}
    assert row["symbol"] == answer["symbol"] == "000651"
    assert row["as_of"] == "2024-12-31"
    assert answer["data_date"] == row["result"]["data_date"]
    values = answer.get("factors", answer)
    for name, value in expected.items():
        close_number(row["result"]["factors"][name]["value"], value)
        close_number(values[name], value)
    return {"symbol": row["symbol"], "data_date": answer["data_date"], "rows": len(bars),
            "data_sha256": row["data_sha256"], "source": row["source"], "independent_numeric_checks": 8}


def audit_turn(run, turn, previous):
    scenario = SCENARIOS[turn - 1]
    checkpoint = json.loads((run / "checkpoint.json").read_text(encoding="utf-8"))
    record = json.loads((ROOT / checkpoint["records"][str(turn)]).read_text(encoding="utf-8"))
    assert record["ok"], record.get("results")
    assert record["roles"] == scenario["roles"], record["roles"]
    calls = tool_calls(record)
    names = [call["name"] for call in calls]
    assert set(scenario["tools"]).issubset(names), names
    answer = answer_json(record["answer"])
    result = {"turn": turn, "scenario": scenario["name"], "ok": True,
              "roles": record["roles"], "tool_calls": names}
    if turn == 1:
        result["numeric_evidence"] = audit_factor(run, turn, answer)
    elif turn == 2:
        assert not calls, names
        expected = [{"indicator": "momentum", "window": 20, "operator": ">", "value": 0},
                    {"indicator": "average_volume_ratio", "short_window": 5, "long_window": 20,
                     "operator": ">", "value": 1}]
        assert rule_signal(answer["rule_json"]) == rule_signal({"signal": {"all": expected}})
        result["rule_json"] = answer["rule_json"]
    elif turn in (3, 4):
        import pandas as pd
        sys.path.insert(0, str(ROOT / "finrobot_autogen"))
        from finrobot.functional.quantitative import run_signal_backtest, audit_backtest
        row = observed(run, turn, "_run_v2_backtest")[-1]
        actual = row["result"]
        assert rule_signal(actual["rule"]) == rule_signal(previous[1]["rule_json"])
        assert rule_signal(answer["rule_json"]) == rule_signal(actual["rule"])
        symbol, start, end = ("000651", "2023-01-01", "2024-12-31") if turn == 3 else ("600031", "2025-01-01", "2025-12-31")
        assert (row["symbol"], row["start"], row["end"]) == (symbol, start, end)
        assert (answer["symbol"], answer["start"], answer["end"]) == (symbol, start, end)
        path = run / "observed" / row["data_file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row["data_sha256"]
        bars = pd.read_csv(path, index_col="Date", parse_dates=True)
        signal = ((bars.Close / bars.Close.shift(20) - 1 > 0) &
                  (bars.Volume.rolling(5).mean() / bars.Volume.rolling(20).mean() > 1)).astype(int)
        cfg = {**actual["cost_assumptions"], "evaluation_start": start}
        replay = run_signal_backtest(bars, signal, cfg)
        hold = run_signal_backtest(bars, pd.Series(1, index=bars.index), cfg)
        assert audit_backtest(replay, cfg, bars)["ok"] and actual["audit_ok"] is True and answer["audit_ok"] is True
        for name in ("total_return", "max_drawdown", "sharpe"):
            close_number(actual["strategy"][name], replay["metrics"][name])
        close_number(actual["buy_and_hold"]["total_return"], hold["metrics"]["total_return"])
        for key, value in {"strategy_total_return": replay["metrics"]["total_return"],
                           "buy_hold_total_return": hold["metrics"]["total_return"],
                           "max_drawdown": replay["metrics"]["max_drawdown"],
                           "sharpe": replay["metrics"]["sharpe"]}.items():
            close_number(answer[key], value)
        assert answer["csi300_available"] == bool(actual["csi300"] and actual["csi300"].get("available"))
        result.update(symbol=symbol, start=start, end=end, data_sha256=row["data_sha256"],
                      source=row["source"], rule_preserved=True, ledger_audit_ok=True,
                      strategy_total_return=actual["strategy"]["total_return"],
                      buy_hold_total_return=actual["buy_and_hold"]["total_return"],
                      csi300_available=answer["csi300_available"], independent_numeric_checks=8)
    else:
        result["numeric_evidence"] = audit_factor(run, turn, answer["factor"])
        assert answer["fixed_reference"]["symbol"] == "000001"
        for name, prefix in (("sma", "sma"), ("buy_hold", "buy_hold")):
            saved = json.loads((ROOT / "artifacts/quant_v2" / name / "result.json").read_text(encoding="utf-8"))["metrics"]
            for metric in ("total_return", "max_drawdown"):
                close_number(answer["fixed_reference"][prefix + "_" + metric], saved[metric])
        assert any(chat["role"] == "Research_Synthesizer" for chat in record["chats"])
        for chat in record["chats"]:
            allowed = {"get_a_share_factors"} if chat["role"] == "A_Share_Factor_Researcher" else {"get_data_summary", "read_backtest_result"}
            if chat["role"] != "Research_Synthesizer":
                used = {call["function"]["name"] for msg in chat["history"] for call in (msg.get("tool_calls") or [])}
                assert used <= allowed, (chat["role"], used)
        result["synthesis_checks"] = 4
    return result


def audit_run(run):
    checkpoint = json.loads((run / "checkpoint.json").read_text(encoding="utf-8"))
    results = []
    planned = checkpoint.get("planned_turns", list(range(1, len(SCENARIOS) + 1)))
    for turn in planned:
        if str(turn) not in checkpoint["records"]:
            break
        try:
            result = audit_turn(run, turn, results)
        except Exception as exc:
            result = {"turn": turn, "scenario": SCENARIOS[turn - 1]["name"], "ok": False,
                      "error_type": type(exc).__name__, "error": str(exc)}
        results.append(result)
    console = (run / "console.txt").read_text(encoding="utf-8") if (run / "console.txt").exists() else ""
    noise = re.findall(r"EXECUTING FUNCTION|Suggested tool call|autogen\.oai\.client|\x1b\[", console)
    usage = [json.loads(p.read_text(encoding="utf-8")) for p in (run / "observed").glob("*.json")]
    totals = {key: sum(model.get(key, 0) for row in usage if row["function"] == "_reply"
                       for model in row["usage"].values()) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    visible_once = True
    for relative in checkpoint["records"].values():
        path = ROOT / relative
        record = json.loads(path.read_text(encoding="utf-8"))
        visible_once = visible_once and console.count(record["answer"]) == 1 and path.with_suffix(".log").exists()
        visible_once = visible_once and path.with_suffix(".md").read_text(encoding="utf-8").rstrip("\n") == record["answer"].rstrip("\n")
    summary = {"ok": len(results) == len(planned) and all(r["ok"] for r in results) and not noise and visible_once,
               "scope": "Real CLI, real model calls, real tool data; observer only records inputs/outputs. Recomputations are offline.",
               "planned_turns": planned, "all_saved_answers_visible_once": visible_once,
               "turns": results, "console_noise_found": noise, "tokens_reported_by_sdk": totals,
               "role_completions": sum(row["function"] == "_reply" for row in usage),
               "record_count": len(checkpoint["records"]), "cli_exit_code": checkpoint.get("cli_exit_code"),
               "observer_errors": (run / "observed/observer_errors.jsonl").exists()}
    summary["ok"] = summary["ok"] and not summary["observer_errors"] and checkpoint.get("cli_exit_code") == 0
    write_json(run / "summary.json", summary)
    return summary


def run_online(run, model, turn_timeout, planned):
    run.mkdir(parents=True, exist_ok=False)
    observations = run / "observed"
    observations.mkdir()
    instrumentation = run / "instrumentation"
    instrumentation.mkdir()
    shutil.copy2(ROOT / "scripts/_auto_validation_observer.py", instrumentation / "sitecustomize.py")
    env = {**os.environ, "DEBUG": "false", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "NLTK_DATA": str(ROOT / ".cache/nltk_data"), "FINROBOT_AUTO_OBSERVATIONS": str(observations),
           "PYTHONPATH": str(instrumentation)}
    command = [sys.executable, "-X", "utf8", str(ROOT / "finrobot_autogen/run_agent.py"), "--mode", "auto", "--model", model]
    checkpoint = {"model": model, "entrypoint": "run_agent.py --mode auto", "records": {},
                  "planned_turns": planned, "pid": None, "started_at": datetime.now(timezone.utc).isoformat()}
    write_json(run / "checkpoint.json", checkpoint)
    existing_records = set((ROOT / "artifacts/agent_chats").glob("*.json"))
    process = subprocess.Popen(command, cwd=ROOT / "finrobot_autogen", env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               encoding="utf-8", bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    checkpoint["pid"] = process.pid
    captured = []
    def collect():
        with (run / "console.txt").open("w", encoding="utf-8") as output:
            while True:
                char = process.stdout.read(1)
                if not char:
                    break
                captured.append(char)
                output.write(char)
                if char == "\n":
                    output.flush()
    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    try:
        for turn in planned:
            scenario = SCENARIOS[turn - 1]
            write_json(run / "current_turn.json", {"turn": turn})
            print(json.dumps({"turn": turn, "scenario": scenario["name"], "status": "calling real CLI"}), flush=True)
            process.stdin.write(scenario["prompt"] + "\n")
            process.stdin.flush()
            started = time.monotonic()
            match = None
            while time.monotonic() - started < turn_timeout:
                for path in (ROOT / "artifacts/agent_chats").glob("*.json"):
                    if path in existing_records:
                        continue
                    try:
                        record = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if record.get("question") == scenario["prompt"] and path.with_suffix(".md").exists() and str(path.with_suffix(".md")) in "".join(captured):
                        match = path
                if match or process.poll() is not None:
                    break
                time.sleep(.25)
            if match is None:
                raise TimeoutError(f"CLI turn {turn} did not finish; process exit={process.poll()}")
            checkpoint["records"][str(turn)] = str(match.relative_to(ROOT))
            existing_records.add(match)
            write_json(run / "checkpoint.json", checkpoint)
            previous = [json.loads((run / f"turn_{n}_audit.json").read_text(encoding="utf-8")) for n in planned if n < turn]
            result = audit_turn(run, turn, previous)
            write_json(run / f"turn_{turn}_audit.json", result)
            print(json.dumps(result, ensure_ascii=True), flush=True)
        process.stdin.write("exit\n")
        process.stdin.flush()
        process.wait(timeout=30)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=20)
        reader.join(timeout=10)
        checkpoint["cli_exit_code"] = process.returncode
        write_json(run / "checkpoint.json", checkpoint)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--audit", type=Path, help="Audit a saved private run without new requests")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--only", choices=["factor", "multiple_roles"], help="Run one independent scenario; default runs all five")
    parser.add_argument("--turn-timeout", type=int, default=480)
    args = parser.parse_args()
    if args.online == bool(args.audit):
        parser.error("Choose --online or --audit RUN_DIRECTORY")
    if args.audit:
        run = args.audit.resolve()
    else:
        run = PRIVATE / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        try:
            planned = [next(i for i, case in enumerate(SCENARIOS, 1) if case["name"] == args.only)] if args.only else list(range(1, len(SCENARIOS) + 1))
            run_online(run, args.model, args.turn_timeout, planned)
        except Exception as exc:
            write_json(run / "failure.json", {"error_type": type(exc).__name__, "error": str(exc)})
            print(json.dumps({"run": str(run.relative_to(ROOT)), "error_type": type(exc).__name__, "error": str(exc)}), flush=True)
    summary = audit_run(run)
    print(json.dumps({"ok": summary["ok"], "run": str(run.relative_to(ROOT)),
                      "completed_turns": summary["record_count"], "tokens": summary["tokens_reported_by_sdk"]}), flush=True)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
