"""Opt-in live CLI acceptance across actual process restarts and local memory."""
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import validate_auto_research as auto

PRIVATE = ROOT / "artifacts/agent_chats/memory_validation"
HINT = "数值用原始小数，不换算百分比。JSON之后另起一行单独输出TERMINATE。"


def expected_rule(name):
    return {"name": name, "signal": {"all": [
        {"indicator": "momentum", "window": 17, "operator": ">", "value": .0125},
        {"indicator": "average_volume_ratio", "short_window": 6, "long_window": 23,
         "operator": ">", "value": 1.08},
        {"indicator": "moving_average_cross", "field": "close", "short_window": 9,
         "long_window": 37, "operator": ">", "value": 0}]}, "execution": "next_open"}


def canonical_rule(value):
    sys.path.insert(0, str(ROOT / "finrobot_autogen"))
    from finrobot.functional.momentum_backtest import validate_strategy_rule
    return validate_strategy_rule(json.loads(value) if isinstance(value, str) else value)


def returned_rule(answer):
    """Both a named rule_json field and the tool's direct rule object are valid."""
    value = auto.answer_json(answer)
    if "rule_json" in value:
        return canonical_rule(value["rule_json"]), "rule_json_wrapper"
    return canonical_rule(value), "direct_rule_object"


def find_rule(text):
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
            if isinstance(value, dict) and "rule_json" in value:
                return canonical_rule(value["rule_json"]), match.start()
        except (ValueError, TypeError, RecursionError):
            continue
    raise ValueError("No complete rule_json object in the answer")


def db_rows(run, query, params=()):
    path = (run / "research.sqlite3").resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(query, params)]


def calls_from_history(chats):
    return [call["function"] for chat in chats for msg in chat["history"]
            for call in (msg.get("tool_calls") or [])]


def invoke(run, number, prompt, session=None, role=None, no_memory=False, model="deepseek-flash"):
    auto.write_json(run / "current_turn.json", {"turn": number})
    env = {**os.environ, "DEBUG": "false", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "NLTK_DATA": str(ROOT / ".cache/nltk_data"),
           "FINROBOT_AUTO_OBSERVATIONS": str(run / "observed"),
           "PYTHONPATH": str(run / "instrumentation")}
    command = [sys.executable, "-X", "utf8", str(ROOT / "finrobot_autogen/run_agent.py"), prompt,
               "--mode", "single" if role else "auto", "--model", model,
               "--memory-db", str(run / ("disabled.sqlite3" if no_memory else "research.sqlite3"))]
    if session:
        command += ["--session", session]
    if role:
        command += ["--role", role]
    if no_memory:
        command += ["--no-memory"]
    before = set((ROOT / "artifacts/agent_chats").glob("*.json"))
    process = subprocess.Popen(command, cwd=ROOT / "finrobot_autogen", env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        output, _ = process.communicate(timeout=480)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate()
        (run / f"stage_{number}_console.txt").write_text(output, encoding="utf-8")
        raise TimeoutError(f"Stage {number} CLI exceeded its timeout")
    (run / f"stage_{number}_console.txt").write_text(output, encoding="utf-8")
    record_path = None
    answer = None
    chats = []
    for path in set((ROOT / "artifacts/agent_chats").glob("*.json")) - before:
        if path.with_suffix(".md").exists() and str(path.with_suffix(".md")) in output:
            record = json.loads(path.read_text(encoding="utf-8"))
            answer = path.with_suffix(".md").read_text(encoding="utf-8").rstrip("\n")
            chats = record["chats"] if isinstance(record, dict) else [{"role": role, "history": record}]
            record_path = str(path.relative_to(ROOT))
    result = {"number": number, "pid": process.pid, "exit_code": process.returncode,
              "session": session, "role": role, "prompt": prompt,
              "record_path": record_path, "answer": answer, "chats": chats}
    if record_path:
        rows = db_rows(run, "SELECT * FROM turns WHERE session_id=? ORDER BY id DESC LIMIT 1", (session,))
        result["stored_turn"] = rows[0] if rows else None
    auto.write_json(run / f"stage_{number}.json", result)
    assert process.returncode == 0, f"Stage {number}: CLI exit {process.returncode}; inspect private console"
    return result


def audit_backtest(run, stage, answer, rule, symbol, start, end):
    import pandas as pd
    sys.path.insert(0, str(ROOT / "finrobot_autogen"))
    from finrobot.functional.quantitative import run_signal_backtest, audit_backtest as audit_ledger
    observed = auto.observed(run, stage["number"], "_run_v2_backtest")[-1]
    actual = observed["result"]
    assert (observed["symbol"], observed["start"], observed["end"]) == (symbol, start, end)
    assert (answer["symbol"], answer["start"], answer["end"]) == (symbol, start, end)
    assert canonical_rule(actual["rule"]) == rule == canonical_rule(answer["rule_json"])
    path = run / "observed" / observed["data_file"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == observed["data_sha256"]
    bars = pd.read_csv(path, index_col="Date", parse_dates=True)
    signal = ((bars.Close / bars.Close.shift(17) - 1 > .0125) &
              (bars.Volume.rolling(6).mean() / bars.Volume.rolling(23).mean() > 1.08) &
              (bars.Close.rolling(9).mean() > bars.Close.rolling(37).mean())).astype(int)
    config = {**actual["cost_assumptions"], "evaluation_start": start}
    replay = run_signal_backtest(bars, signal, config)
    hold = run_signal_backtest(bars, pd.Series(1, index=bars.index), config)
    assert audit_ledger(replay, config, bars)["ok"] and actual["audit_ok"] and answer["audit_ok"]
    for metric in ("total_return", "max_drawdown", "sharpe"):
        auto.close_number(actual["strategy"][metric], replay["metrics"][metric])
    auto.close_number(actual["buy_and_hold"]["total_return"], hold["metrics"]["total_return"])
    for key, value in {"strategy_total_return": replay["metrics"]["total_return"],
                       "buy_hold_total_return": hold["metrics"]["total_return"],
                       "max_drawdown": replay["metrics"]["max_drawdown"],
                       "sharpe": replay["metrics"]["sharpe"]}.items():
        auto.close_number(answer[key], value)
    return {"symbol": symbol, "start": start, "end": end, "data_sha256": observed["data_sha256"],
            "rule_preserved": True, "ledger_ok": True, "numeric_checks": 8,
            "strategy_total_return": replay["metrics"]["total_return"],
            "buy_hold_total_return": hold["metrics"]["total_return"]}


def audit_stage(run, number):
    plan = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    stage = json.loads((run / f"stage_{number}.json").read_text(encoding="utf-8"))
    assert stage["exit_code"] == 0 and stage["answer"]
    turn = stage["stored_turn"]
    assert turn["question"] == stage["prompt"] and turn["answer"] == stage["answer"] and turn["status"] == "completed"
    console = (run / f"stage_{number}_console.txt").read_text(encoding="utf-8")
    assert console.count(stage["answer"]) == 1
    assert not re.search(r"EXECUTING FUNCTION|Suggested tool call|autogen\.oai\.client|\x1b\[", console)
    calls = calls_from_history(stage["chats"])
    names = [call["name"] for call in calls]
    rule = plan["expected_rule"]
    result = {"stage": number, "ok": True, "process_id": stage["pid"], "tool_calls": names,
              "completed_turn_saved": True, "answer_visible_once": True, "console_quiet": True}
    if number == 1:
        value, offset = find_rule(stage["answer"])
        assert value == rule and offset > 1000, (offset, value)
        assert not names, names
        saved = json.loads(turn["rules"])
        assert any(row["rule"] == rule and row["source"] == "assistant_answer" for row in saved)
        result.update(rule_after_preview_limit=True, rule_stored_whole=True, rule_json_offset=offset)
    elif number in (2, 3):
        answer = auto.answer_json(stage["answer"])
        assert "backtest_a_share_strategy" in names
        expected = ("000651", "2023-01-01", "2024-12-31") if number == 2 else ("600031", "2025-01-01", "2025-12-31")
        result.update(audit_backtest(run, stage, answer, rule, *expected))
        assert stage["session"] == plan["sessions"]["auto"]
        assert stage["pid"] != json.loads((run / "stage_1.json").read_text(encoding="utf-8"))["pid"]
        result["new_process_restored_session"] = True
    elif number == 4:
        answer = auto.answer_json(stage["answer"])
        assert answer["rule_known"] is False and answer["rule_json"] is None
        assert not names, names
        result["new_session_did_not_receive_other_turns"] = True
    elif number in (5, 8):
        answer = auto.answer_json(stage["answer"])
        assert "recall_research" in names and set(names) == {"recall_research"}
        recovered, shape = returned_rule(stage["answer"])
        assert recovered == rule
        result["answer_shape"] = shape
        if number == 5:
            assert answer["source_session_id"] == plan["sessions"]["auto"]
        result["explicit_cross_session_recall_ok"] = True
    elif number in (6, 7):
        answer = auto.answer_json(stage["answer"])
        assert not names, names
        assert answer["note_present"] is (number == 6)
        assert answer["note_tag"] == (plan["note_tag"] if number == 6 else None)
        result["canonical_note_state_seen_by_model"] = True
    elif number == 9:
        recovered, shape = returned_rule(stage["answer"])
        assert recovered == rule and not names
        assert stage["session"] == plan["sessions"]["single"]
        assert stage["pid"] != json.loads((run / "stage_8.json").read_text(encoding="utf-8"))["pid"]
        result["answer_shape"] = shape
        result["single_role_restart_rule_restored"] = True
    return result


def audit_local_controls(run):
    """Check local CLI controls and role restoration without calling a model."""
    sys.path.insert(0, str(ROOT / "finrobot_autogen"))
    from finrobot.agents.memory import ResearchMemory
    from finrobot.agents.research_router import AutoResearch
    plan = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    sessions = plan["sessions"]
    memory = ResearchMemory(run / "research.sqlite3", session_id=sessions["auto"])
    router = AutoResearch({}, memory=memory)
    disabled = run / "stage_no_memory.json"
    return {
        "no_memory_skips_database": disabled.exists()
            and json.loads(disabled.read_text(encoding="utf-8"))["exit_code"] == 0
            and not (run / "disabled.sqlite3").exists(),
        "explicit_note_deleted": "note_id" in plan and not db_rows(
            run, "SELECT id FROM notes WHERE id=?", (plan.get("note_id"),)),
        "old_verbatim_answer_retained_after_note_deletion": bool(db_rows(
            run, "SELECT id FROM turns WHERE session_id=? AND instr(answer,?)>0",
            (sessions["note_present"], plan["note_tag"]))),
        "automatic_role_restored": router.plan("继续") == ("A_Share_Factor_Ideator",),
    }


def audit_run(run):
    results = []
    for number in range(1, 10):
        if not (run / f"stage_{number}.json").exists():
            break
        try:
            results.append(audit_stage(run, number))
        except Exception as exc:
            results.append({"stage": number, "ok": False, "error_type": type(exc).__name__, "error": str(exc)})
    events = [json.loads(p.read_text(encoding="utf-8")) for p in (run / "observed").glob("*.json")]
    tokens = {key: sum(model.get(key, 0) for row in events if row["function"] in {"_reply", "ask"}
                       for model in row["usage"].values())
              for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    try:
        controls = audit_local_controls(run)
    except (KeyError, OSError, ValueError, sqlite3.Error):
        controls = {"local_control_audit_completed": False}
    auto.write_json(run / "controls.json", controls)
    result = {"ok": len(results) == 9 and all(r["ok"] for r in results) and all(controls.values())
                    and bool(controls) and not (run / "observed/observer_errors.jsonl").exists(),
              "stages": results, "local_controls": controls, "tokens_reported_by_sdk": tokens,
              "scope": "Real model calls through real CLI processes, isolated acceptance database, actual tool data. No mock responses; the observer records without changing results."}
    auto.write_json(run / "summary.json", result)
    return result


def run_online(run, model):
    run.mkdir(parents=True, exist_ok=False)
    (run / "observed").mkdir()
    (run / "instrumentation").mkdir()
    shutil.copy2(ROOT / "scripts/_auto_validation_observer.py", run / "instrumentation/sitecustomize.py")
    nonce = uuid4().hex[:12]
    sessions = {key: key + "_" + nonce for key in ("auto", "isolated", "note_present", "note_deleted", "single")}
    plan = {"model": model, "sessions": sessions, "note_tag": "MEMORY_ACCEPT_" + uuid4().hex[:16],
            "expected_rule": expected_rule("memory_rule_" + nonce),
            "started_at": datetime.now(timezone.utc).isoformat()}
    auto.write_json(run / "plan.json", plan)
    prompts = {
        1: f"为000651设计一个可执行价量因子，策略名称必须为{plan['expected_rule']['name']}。持有条件严格为：17日动量大于0.0125，6日均量除以23日均量大于1.08，9日收盘均线高于37日收盘均线，三个条件同时满足才持有，否则空仓。不要增加条件，不执行交易计算。先用超过1200个中文字符、至少八段文字分析逻辑、失效场景和成本风险，正文不放任何JSON或代码块；最后才输出一个包含rule_json的JSON对象。规则JSON仅含name、signal、execution=next_open。随后另起一行TERMINATE。",
        2: "回测刚才同一个因子规则：000651，2023-01-01至2024-12-31，原样使用已经保存的完整规则，默认资金和成本。返回JSON字段symbol、start、end、rule_json、strategy_total_return、buy_hold_total_return、max_drawdown、sharpe、audit_ok。" + HINT,
        3: "换成600031，2025-01-01至2025-12-31，继续使用刚才同一个规则重新计算，不改策略名或任何条件，默认资金与费用。JSON字段同前。" + HINT,
        4: "这是新会话。仅根据当前memory_context.recent_turns判断本会话是否已经保存过因子规则；禁止查询其他会话，禁止调用任何工具，不按示例猜测。只返回JSON：rule_known、rule_json；无本会话记录则为false和null。" + HINT,
        5: "现在显式跨会话查询本地历史研究中的000651因子规则，必须调用recall_research，不执行新计算。只返回JSON：rule_json（完整对象）、source_session_id（检索结果中原记录所属session_id，不是本会话ID）。" + HINT,
        6: "只检查memory_context.notes中的显式因子研究笔记，不查询历史问答，不调用任何工具。若有以MEMORY_ACCEPT_开头的标签，在JSON中返回note_present=true和note_tag原样标签；否则false和null。" + HINT,
        7: "只检查memory_context.notes中的显式因子研究笔记，不查询历史问答，不调用任何工具。若有以MEMORY_ACCEPT_开头的标签，在JSON中返回note_present=true和note_tag原样标签；否则false和null。" + HINT,
        8: "显式查询本地历史研究中的000651因子规则，必须调用recall_research，不执行新计算。只返回JSON：rule_json（完整对象）。" + HINT,
        9: "直接使用本会话memory_context中刚才的完整规则，原样返回JSON：rule_json。禁止调用工具，不执行新计算。" + HINT,
    }
    stage_sessions = {1: "auto", 2: "auto", 3: "auto", 4: "isolated", 5: "isolated",
                      6: "note_present", 7: "note_deleted", 8: "single", 9: "single"}
    for number in range(1, 10):
        if number == 6:
            invoke(run, "remember", "/remember 因子验收标签 " + plan["note_tag"], sessions["auto"], model=model)
            notes = db_rows(run, "SELECT id,text FROM notes")
            assert len(notes) == 1 and plan["note_tag"] in notes[0]["text"]
            plan["note_id"] = notes[0]["id"]
            auto.write_json(run / "plan.json", plan)
        if number == 7:
            invoke(run, "forget", "/forget " + str(plan["note_id"]), sessions["auto"], model=model)
            assert not db_rows(run, "SELECT id FROM notes WHERE id=?", (plan["note_id"],))
        print(json.dumps({"stage": number, "status": "calling real model in a new CLI process"}), flush=True)
        role = "A_Share_Factor_Ideator" if number in (8, 9) else None
        invoke(run, number, prompts[number], sessions[stage_sessions[number]], role, model=model)
        result = audit_stage(run, number)
        auto.write_json(run / f"stage_{number}_audit.json", result)
        print(json.dumps(result, ensure_ascii=True), flush=True)
    invoke(run, "no_memory", "/memory", no_memory=True, model=model)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--model", default="deepseek-flash")
    args = parser.parse_args()
    if args.online == bool(args.audit):
        parser.error("Choose --online or --audit RUN_DIRECTORY")
    if args.audit:
        run = args.audit.resolve()
    else:
        run = PRIVATE / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        try:
            run_online(run, args.model)
        except Exception as exc:
            auto.write_json(run / "failure.json", {"error_type": type(exc).__name__, "error": str(exc)})
            print(json.dumps({"run": str(run.relative_to(ROOT)), "error_type": type(exc).__name__, "error": str(exc)}), flush=True)
    summary = audit_run(run)
    print(json.dumps({"ok": summary["ok"], "run": str(run.relative_to(ROOT)), "stages": len(summary["stages"]),
                      "tokens": summary["tokens_reported_by_sdk"]}), flush=True)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
