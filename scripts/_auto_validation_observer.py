"""Read-only observer copied to a private sitecustomize during online validation.

No response, input or tool implementation is replaced. Only selected completed
calls are recorded; credentials and model client configuration are never saved.
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

_directory = os.environ.get("FINROBOT_AUTO_OBSERVATIONS")


def observe(frame, event, returned):
    if event != "return" or frame.f_code.co_name not in {"get_a_share_factors", "_run_v2_backtest", "_reply"}:
        return
    filename = frame.f_code.co_filename.replace("\\", "/")
    name = frame.f_code.co_name
    if not (filename.endswith("/functional/factors.py") or
            filename.endswith("/functional/momentum_backtest.py") or
            filename.endswith("/agents/research_router.py")):
        return
    folder = Path(_directory)
    try:
        turn = json.loads((folder.parent / "current_turn.json").read_text(encoding="utf-8"))["turn"]
        identity = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + "_" + uuid4().hex[:8]
        record = {"turn": turn, "function": name, "recorded_at": datetime.now(timezone.utc).isoformat()}
        if name == "_reply":
            agent = frame.f_locals.get("agent")
            if agent is None:
                return
            usage = agent.assistant.client.actual_usage_summary or {}
            record.update(agent=agent.assistant.name, reply_ok=isinstance(returned, dict) and "answer" in returned,
                          usage={model: {key: values.get(key, 0) for key in
                                 ("prompt_tokens", "completion_tokens", "total_tokens")}
                                 for model, values in usage.items() if isinstance(values, dict)})
        else:
            if returned is None:
                return
            bars = frame.f_locals.get("bars")
            if bars is None:
                return
            data_file = identity + ".csv"
            bars.to_csv(folder / data_file, index_label="Date", float_format="%.17g")
            record.update(symbol=frame.f_locals["symbol"], data_file=data_file,
                          data_sha256=hashlib.sha256((folder / data_file).read_bytes()).hexdigest(),
                          rows=len(bars), source={key: bars.attrs.get(key) for key in
                                                 ("source", "endpoint", "adjust", "volume_unit")})
            if name == "get_a_share_factors":
                record.update(as_of=frame.f_locals["as_of"], result=json.loads(returned))
            else:
                record.update(start=frame.f_locals["start"], end=frame.f_locals["end"], result=returned)
        (folder / (identity + ".json")).write_text(
            json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    except Exception as exc:
        with (folder / "observer_errors.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"function": name, "error_type": type(exc).__name__}) + "\n")


if _directory:
    Path(_directory).mkdir(parents=True, exist_ok=True)
    sys.setprofile(observe)
