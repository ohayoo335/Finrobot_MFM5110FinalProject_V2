"""Local research history and explicit user notes, without model calls.

Conversation text is stored verbatim. Only completed turns are recalled as
research evidence; a new session never implicitly reads another session's turns.
Context is a bounded projection of this store, not a source of current prices.
"""

import contextlib
import hashlib
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
from uuid import uuid4


MAX_CONTEXT_CHARS = 12000
MAX_EVIDENCE_CHARS = 6000
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_STOCK_CODE = re.compile(r"(?<!\d)\d{6}(?!\d)")
_IDENTITY_FIELDS = {"experiment_id", "artifact_id", "symbol", "ticker", "data_sha256", "clean_sha256"}
_DATE_FIELDS = {
    "as_of", "requested_as_of", "data_date", "date", "start", "end",
    "start_date", "end_date", "actual_start", "actual_end", "requested_start",
    "requested_end", "first_date", "last_date", "evaluation_start", "fetched_at",
}
_VALUE_FIELDS = {
    "ok", "status", "audit_ok", "strategy", "source_tool", "provider", "source",
    "endpoint", "adjust", "price_adjustment", "unit", "formula", "value",
    "fast", "slow", "initial_cash", "max_position", "lot_size", "commission",
    "min_commission", "slippage", "annual_periods", "risk_free_rate",
    "evaluation_rows", "warmup_rows", "bar_count", "entry_budget_limit",
    "maximum_observed_close_weight", "continuous_position_cap_enforced",
}
_CONTAINERS = {"config", "metadata", "source", "metrics", "factors", "audit",
               "strategy", "buy_and_hold", "csi300", "data_window", "result", "data"}
_STATS_CONTAINERS = {"metrics", "factors", "strategy", "buy_and_hold", "csi300"}
_SECRET_FIELD = re.compile(r"key|token|password|secret|authorization|llm_config", re.I)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _limit(value):
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    return value


def _extract_rules(question, answer, chats):
    """Keep validated JSON rules separately from text previews and numeric evidence."""
    found = {}

    def collect(value, source):
        pending = [value]
        for _ in range(128):
            if not pending:
                break
            item = pending.pop()
            if isinstance(item, str) and len(item) <= 16000:
                try:
                    pending.append(json.loads(item))
                except (ValueError, RecursionError):
                    pass
            elif isinstance(item, dict):
                if "signal" in item:
                    try:
                        from ..functional.momentum_backtest import validate_strategy_rule
                        rule = validate_strategy_rule(item)
                        encoded = _json(rule)
                        if len(encoded) <= 16000:
                            digest = hashlib.sha256(json.dumps(rule, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
                            found[digest] = {"rule": rule, "source": source, "rule_sha256": digest}
                    except (ValueError, TypeError, RecursionError, OverflowError):
                        pass
                else:
                    keys = sorted(item, key=lambda key: key in {"rule", "rule_json"})
                    pending.extend(item[key] for key in keys if not _SECRET_FIELD.search(str(key))
                                   and (isinstance(item[key], (dict, list)) or key in {"rule", "rule_json"}))
            elif isinstance(item, list):
                pending.extend(reversed(item[:128]))

    decoder = json.JSONDecoder()
    for text, source in ((question, "user_question"), (answer, "assistant_answer")):
        # Scan both ends of unusually long text; never evaluate its contents.
        text = text if len(text) <= 65536 else text[:32768] + "\n" + text[-32768:]
        position = 0
        for _ in range(128):
            start = text.find("{", position)
            if start < 0:
                break
            try:
                value, consumed = decoder.raw_decode(text[start:])
                collect(value, source)
                position = start + consumed
            except (ValueError, RecursionError):
                position = start + 1
    for chat in chats:
        for message in chat.get("history", []):
            for call in message.get("tool_calls") or []:
                collect(call.get("function", {}).get("arguments", ""), "tool_argument")
            if message.get("role") in {"tool", "function"}:
                for response in message.get("tool_responses") or [message]:
                    collect(response.get("content"), "tool_result")
    return list(found.values())


def summarize_turn(original, budget):
    """Prefer whole rules, then evidence references, then short text previews."""
    row = {key: value for key, value in original.items() if key not in {"question", "answer", "evidence", "rules"}}
    row.update(question="", answer="", evidence=[], rules=[])
    if len(_json(row)) > budget - 100:
        return None
    rules = original.get("rules", [])
    for rule in reversed(rules):
        if len(_json({**row, "rules": [rule, *row["rules"]]})) <= budget - 100:
            row["rules"].insert(0, rule)
    if len(row["rules"]) != len(rules):
        row.update(rules_omitted=len(rules) - len(row["rules"]), truncated=True)
    available = max(2, budget - len(_json(row)) - 1800)
    row["evidence"] = _bounded_evidence(original["evidence"], available)
    if not row["evidence"] and original["evidence"]:
        row["evidence"] = _bounded_evidence([_references(value) for value in original["evidence"]],
                                            max(2, budget - len(_json(row)) - 200))
    if row["evidence"] != original["evidence"]:
        row["truncated"] = True
    for key, maximum in (("question", 500), ("answer", 1000)):
        text = original[key]
        if rules and "{" in text:
            text = text[:text.index("{")] + "（完整 JSON 规则见 rules 字段；原文可用 /recall 查看。）"
        row[key] = text[:maximum]
        if len(text) > maximum or text != original[key]:
            row[key] += "…"
            row["truncated"] = True
    for key in ("answer", "question"):
        while len(_json(row)) > budget and row[key]:
            row[key] = row[key][:-250]
            row["truncated"] = True
    return row if len(_json(row)) <= budget else None


def _is_path(key):
    return key == "path" or key.endswith(("_path", "_directory"))


def _project_result(data, *, stats=False, depth=0):
    """Keep small structured tool facts, never raw code, tables or credentials."""
    if not isinstance(data, dict) or depth > 5:
        return {}
    output = {}
    # References precede descriptive metrics, so a large tool result keeps IDs.
    keys = sorted(data, key=lambda key: (key not in _IDENTITY_FIELDS, str(key)))
    for key in keys:
        if not isinstance(key, str) or len(key) > 80 or _SECRET_FIELD.search(key):
            continue
        value = data[key]
        if isinstance(value, dict) and (key in _CONTAINERS or stats):
            projected = _project_result(value, stats=stats or key in _STATS_CONTAINERS, depth=depth + 1)
            if projected:
                output[key] = projected
        elif key in _IDENTITY_FIELDS | _DATE_FIELDS | _VALUE_FIELDS or _is_path(key) or stats:
            if value is None or isinstance(value, bool):
                output[key] = value
            elif isinstance(value, (int, float)) and math.isfinite(value):
                output[key] = value
            elif isinstance(value, str):
                maximum = 2048 if _is_path(key) else 512
                # Never cut identifiers or paths into misleading partial values.
                if len(value) <= maximum and (not stats or key in _VALUE_FIELDS | _DATE_FIELDS | _IDENTITY_FIELDS or _is_path(key)):
                    output[key] = value
        if len(output) >= 24:
            break
    return output


def _references(data):
    """A smaller projection retaining whole references, dates and source labels."""
    output = {}
    for key, value in data.items():
        if isinstance(value, dict):
            nested = _references(value)
            if nested:
                output[key] = nested
        elif key in _IDENTITY_FIELDS | _DATE_FIELDS | {"tool", "role", "ok", "status", "source_tool"} or _is_path(key):
            output[key] = value
    return output


def _bounded_evidence(rows, budget):
    selected = []
    for row in reversed(rows):
        candidate = row
        if len(_json([candidate, *selected])) > budget:
            candidate = _references(row)
        if len(_json([candidate, *selected])) <= budget:
            selected.insert(0, candidate)
        if len(selected) >= 12:
            break
    return selected


def _extract_evidence(chats):
    rows, seen = [], set()
    for chat in chats:
        names = {}
        for message in chat.get("history", []):
            for call in message.get("tool_calls") or []:
                names[call.get("id")] = call.get("function", {}).get("name", "")
            if message.get("role") not in {"tool", "function"}:
                continue
            responses = message.get("tool_responses") or [message]
            for response in responses:
                content = response.get("content")
                if isinstance(content, str):
                    try:
                        data = json.loads(content)
                    except (ValueError, RecursionError):
                        # Report tools can return the actual filename as plain text.
                        path = content.strip()
                        data = {"result_path": path} if "\n" not in path and len(path) <= 2048 and re.search(r"\.(pdf|png|csv|json|html)$", path, re.I) else {}
                else:
                    data = content
                projected = _project_result(data)
                if not projected:
                    continue
                row = {"role": str(chat.get("role", ""))[:128],
                       "tool": str(response.get("name") or names.get(response.get("tool_call_id")) or message.get("name") or "")[:128],
                       **projected}
                identity = _json(row)
                if identity not in seen:
                    seen.add(identity)
                    rows.append(row)
    return _bounded_evidence(rows, MAX_EVIDENCE_CHARS)


def _terms(query):
    text = query.strip()
    if not text:
        return []
    stocks = _STOCK_CODE.findall(text)
    if stocks:
        return list(dict.fromkeys(stocks))[:8]
    terms = [text[:256]]
    for token in re.findall(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", text):
        terms.append(token)
        if len(token) > 4 and re.fullmatch(r"[\u4e00-\u9fff]+", token):
            terms.extend(token[i:i + 2] for i in range(len(token) - 1)
                         if token[i:i + 2] not in {"之前", "上次", "关于", "帮我", "一下", "记得", "我的"})
    return list(dict.fromkeys(terms))[:12]


def _turn(row):
    rules = json.loads(row["rules"]) if "rules" in row.keys() else []
    return {"id": row["id"], "session_id": row["session_id"],
            "question": row["question"], "answer": row["answer"],
            "roles": json.loads(row["roles"]), "status": row["status"],
            "created_at": row["created_at"], "evidence": json.loads(row["evidence"]),
            "rules": rules or _extract_rules(row["question"], row["answer"], [])}


class ResearchMemory:
    """Persistent CLI research sessions; user notes are explicitly cross-session."""

    def __init__(self, path, session_id=None, mode="auto", role=None):
        if mode not in {"auto", "single"}:
            raise ValueError("mode must be auto or single")
        if role is not None and (not isinstance(role, str) or not role.strip() or len(role) > 128):
            raise ValueError("role must be a nonempty role name")
        self.session_id = uuid4().hex if session_id is None else session_id
        if not isinstance(self.session_id, str) or not _SESSION_ID.fullmatch(self.session_id):
            raise ValueError("session_id must contain 1-128 letters, digits, dots, underscores or hyphens")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, mode TEXT NOT NULL, role TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, title TEXT NOT NULL DEFAULT '')")
            db.execute("CREATE TABLE IF NOT EXISTS turns (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id), question TEXT NOT NULL, answer TEXT NOT NULL, roles TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('completed', 'partial', 'failed')), created_at TEXT NOT NULL, evidence TEXT NOT NULL)")
            if "rules" not in {row["name"] for row in db.execute("PRAGMA table_info(turns)")}:
                db.execute("ALTER TABLE turns ADD COLUMN rules TEXT NOT NULL DEFAULT '[]'")
            db.execute("CREATE INDEX IF NOT EXISTS turns_session_id ON turns(session_id, id DESC)")
            db.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, created_at TEXT NOT NULL)")
            existing = db.execute("SELECT mode, role FROM sessions WHERE id = ?", (self.session_id,)).fetchone()
            if existing is not None:
                if (existing["mode"], existing["role"]) != (mode, role):
                    raise ValueError("The saved session uses a different mode or role")
            else:
                now = _now()
                db.execute("INSERT INTO sessions (id, mode, role, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                           (self.session_id, mode, role, now, now))

    @contextlib.contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=NORMAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def recent_turns(self, limit=3):
        """Return this session's recent turns, including failure status, oldest first."""
        with self._db() as db:
            rows = db.execute("SELECT * FROM turns WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                              (self.session_id, _limit(limit))).fetchall()
        return [_turn(row) for row in reversed(rows)]

    def save_turn(self, question, answer, roles, chats, status="completed"):
        if not isinstance(question, str) or not isinstance(answer, str):
            raise ValueError("question and answer must be strings")
        if status not in {"completed", "partial", "failed"}:
            raise ValueError("Unknown turn status")
        if isinstance(roles, str) or not isinstance(roles, (list, tuple)) or len(roles) > 10 or any(not isinstance(role, str) or not role or len(role) > 128 for role in roles):
            raise ValueError("roles must be a list of role names")
        if status == "completed" and not answer.strip():
            raise ValueError("A completed turn must have an answer")
        evidence = _json(_extract_evidence(chats))
        rules = _json(_extract_rules(question, answer, chats))
        now = _now()
        with self._db() as db:
            cursor = db.execute("INSERT INTO turns (session_id, question, answer, roles, status, created_at, evidence, rules) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                (self.session_id, question, answer, _json(list(roles)), status, now, evidence, rules))
            db.execute("UPDATE sessions SET updated_at = ?, title = CASE WHEN title = '' THEN ? ELSE title END WHERE id = ?",
                       (now, question[:80], self.session_id))
            return cursor.lastrowid

    def remember(self, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("A note must contain text")
        now = _now()
        with self._db() as db:
            cursor = db.execute("INSERT INTO notes (text, created_at) VALUES (?, ?)", (text, now))
            return {"id": cursor.lastrowid, "text": text, "created_at": now}

    def forget(self, note_id):
        if type(note_id) is not int or note_id < 1:
            raise ValueError("note_id must be a positive integer")
        with self._db() as db:
            return db.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount > 0

    def recall(self, query, limit=5):
        """Explicit cross-session lookup by stock code or literal keywords."""
        if not isinstance(query, str):
            raise ValueError("query must be text")
        limit = _limit(limit)
        terms = _terms(query)
        with self._db() as db:
            note_filter = " AND (" + " OR ".join("instr(lower(text), lower(?)) > 0" for _ in terms) + ")" if terms else ""
            turn_filter = " AND (" + " OR ".join("instr(lower(question || ' ' || answer || ' ' || evidence), lower(?)) > 0" for _ in terms) + ")" if terms else ""
            notes = db.execute("SELECT id, text, created_at FROM notes WHERE 1=1" + note_filter + " ORDER BY id DESC LIMIT ?", (*terms, limit)).fetchall()
            turns = db.execute("SELECT * FROM turns WHERE status = 'completed'" + turn_filter + " ORDER BY id DESC LIMIT ?", (*terms, limit)).fetchall()
        return {"notes": [dict(row) for row in notes], "turns": [_turn(row) for row in turns]}

    def sessions(self, limit=10):
        with self._db() as db:
            rows = db.execute("SELECT id, mode, role, updated_at, title FROM sessions ORDER BY updated_at DESC, id DESC LIMIT ?",
                              (_limit(limit),)).fetchall()
        return [dict(row) for row in rows]

    def context(self, question, include_recent=True):
        """Build <=12,000 JSON characters, preserving full evidence references.

        Dates on turns/notes describe when they were recorded. Data dates remain
        inside the original tool evidence; neither means prices are current.
        """
        if not isinstance(question, str):
            raise ValueError("question must be text")
        with self._db() as db:
            notes = db.execute("SELECT id, text, created_at FROM notes ORDER BY id DESC LIMIT 20").fetchall()
            turns = db.execute("SELECT * FROM turns WHERE session_id = ? AND status = 'completed' ORDER BY id DESC LIMIT 3",
                               (self.session_id,)).fetchall() if include_recent else []
        result = {"session_id": self.session_id, "notes": [], "recent_turns": []}
        # Notes are explicit user preferences, so they need not mention a ticker.
        for row in notes:
            note = dict(row)
            if len(note["text"]) > 500:
                note["text"] = note["text"][:500] + "…"
                note["truncated"] = True
            if len(_json([*result["notes"], note])) > 3000:
                break
            result["notes"].append(note)
        budget = (MAX_CONTEXT_CHARS - len(_json(result)) - 100) // max(1, len(turns))
        for raw in reversed(turns):
            turn = summarize_turn(_turn(raw), budget)
            if turn is not None:
                result["recent_turns"].append(turn)
        return result
