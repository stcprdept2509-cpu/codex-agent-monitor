#!/usr/bin/env python3
"""Read-only local dashboard for Codex's agent / subagent / skill activity.

Reads ~/.codex/state_5.sqlite (threads + spawn edges) and tails each active
thread's rollout jsonl to detect status, the skill currently being read, and
turn timings. Nothing is written back to Codex's files. Every number the UI
shows is derived from real local data - nothing here is a placeholder value.
"""
import http.server
import json
import mimetypes
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
DB_PATH = Path(os.environ.get("CODEX_DB_PATH", CODEX_HOME / "state_5.sqlite")).expanduser()
CLAUDE_PROJECTS_DIR = Path(
    os.environ.get("CLAUDE_PROJECTS_DIR", Path.home() / ".claude" / "projects")
).expanduser()
PORT = int(os.environ.get("PORT", "8799"))
HOST = os.environ.get("HOST", "127.0.0.1")
WINDOW_MS = 24 * 3600 * 1000        # show threads touched in the last 24h
TAIL_ACTIVE_MS = 15 * 60 * 1000     # only tail rollout files for threads touched in the last 15 min
TAIL_BYTES = 300_000                # how much of the rollout file to read from the end

TOOL_LABELS = {
    "Bash": "コマンド実行",
    "Read": "ファイル確認",
    "Edit": "コード編集",
    "Write": "ファイル作成",
    "WebFetch": "Web調査",
    "WebSearch": "Web検索",
    "Agent": "サブエージェント稼働",
    "ToolSearch": "ツール検索",
    "Skill": "スキル実行",
    "NotebookEdit": "ノートブック編集",
    "TaskCreate": "タスク管理",
    "TaskUpdate": "タスク管理",
}


def friendly_tool_label(name):
    if not name:
        return None
    if name in TOOL_LABELS:
        return TOOL_LABELS[name]
    if name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1] if len(parts) > 1 else "外部ツール"
        action = parts[-1].replace("_", " ") if len(parts) > 2 else "実行"
        return f"{server}: {action}"
    return name

SKILL_RE = re.compile(r"skills/([^/\"]+)/SKILL\.md")

# Best-effort "what kind of work is this agent doing" label. Codex doesn't
# store a job title anywhere, so this is inferred from the skill file it
# last read (strong signal) or, failing that, keywords in the thread's own
# title (weak signal). It's a guess, not a guaranteed-accurate category.
SKILL_TO_JOB_TYPE = {
    "stc-sns-strategy-agents": "SNS・コンテンツ運用",
    "stc-note-infographic-director": "SNS・コンテンツ運用",
    "stc-natural-japanese-guard": "SNS・コンテンツ運用",
    "sns-content": "SNS・コンテンツ運用",
    "stc-tsujimura-invoice-workflow": "経理・請求書",
    "stc-monthly-expense-settlement": "経理・請求書",
    "indeed-recruiting-ops": "採用",
    "stc-sales-market-intelligence": "営業・新規事業",
    "stc-new-business-builder": "営業・新規事業",
    "codebase-design": "開発・エンジニアリング",
    "diagnosing-bugs": "開発・エンジニアリング",
    "tdd": "開発・エンジニアリング",
    "prototype": "開発・エンジニアリング",
    "anthropic-skill-creator": "開発・エンジニアリング",
    "stc-standard-slide-generation": "資料作成",
    "contextual-transcription": "文字起こし・議事録",
    "stc-ui-ux-human-design-guard": "デザインレビュー",
    "stc-secretary-orchestrator": "秘書業務",
    "stc-iwano-operating-canon": "秘書業務",
}

KEYWORD_JOB_TYPES = [
    (("SNS", "Threads", "Instagram", "note", "フォロワー", "リール", "投稿"), "SNS・コンテンツ運用"),
    (("請求書", "経費", "精算", "invoice"), "経理・請求書"),
    (("採用", "求人", "面接", "応募"), "採用"),
    (("営業", "新規事業", "競合", "案件"), "営業・新規事業"),
    (("設計書", "レビュー", "DB", "バグ", "実装", "コード"), "開発・エンジニアリング"),
    (("文字起こし", "議事録"), "文字起こし・議事録"),
    (("スライド", "資料"), "資料作成"),
]


def classify_job_type(headline_text, skill, automation):
    if skill and skill in SKILL_TO_JOB_TYPE:
        return SKILL_TO_JOB_TYPE[skill]
    for keywords, label in KEYWORD_JOB_TYPES:
        if any(kw in headline_text for kw in keywords):
            return label
    if automation:
        return "自動化(定型タスク)"
    return "その他"

THREAD_COLUMNS = """
    id, title, preview, agent_nickname, agent_role, model, cwd,
    created_at_ms, updated_at_ms, rollout_path, archived
"""


def db():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Codex database not found: {DB_PATH}")
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro&immutable=1", uri=True)


def fetch_recent_threads(conn, now_ms):
    cur = conn.execute(
        f"SELECT {THREAD_COLUMNS} FROM threads WHERE archived = 0 AND updated_at_ms > ? "
        "ORDER BY updated_at_ms DESC",
        (now_ms - WINDOW_MS,),
    )
    cols = [d[0] for d in cur.description]
    return {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}


def fetch_threads_by_ids(conn, ids):
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(f"SELECT {THREAD_COLUMNS} FROM threads WHERE id IN ({placeholders})", list(ids))
    cols = [d[0] for d in cur.description]
    return {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}


def fetch_spawn_edges(conn):
    return conn.execute("SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges").fetchall()


def headline(thread):
    text = (thread["title"] or thread["preview"] or "").strip()
    first_line = text.splitlines()[0] if text else "(無題)"
    if first_line.startswith("Automation:"):
        first_line = first_line[len("Automation:") :].strip()
    return first_line[:70] or "(無題)"


def is_automation(thread):
    return (thread["title"] or "").startswith("Automation:")


def parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


# A single verbose turn (lots of reasoning/tool output/token_count spam) can
# push far more than TAIL_BYTES of data after the last task_started marker,
# especially on long-lived sessions whose rollout file has grown huge. If a
# fixed-size tail misses every task_started/task_complete/turn_aborted event,
# a genuinely running session reads as falsely idle. So we escalate the read
# window until we actually find a status marker (or give up at the cap).
STATUS_TAIL_SIZES = [TAIL_BYTES, 1_000_000, 5_000_000, 20_000_000, 80_000_000]
STATUS_MARKERS = (b'"task_started"', b'"task_complete"', b'"turn_aborted"')


def read_tail_with_status_marker(path, size):
    data = b""
    for tail_bytes in STATUS_TAIL_SIZES:
        with path.open("rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()  # drop the partial line left by seeking mid-file
            data = f.read()
        if any(marker in data for marker in STATUS_MARKERS) or tail_bytes >= size:
            break
    return data.decode("utf-8", errors="ignore")


def analyze_rollout(rollout_path):
    """Tail a rollout jsonl and derive real status/timing/skill facts.

    Returns a dict with:
      status: "running" | "idle" | "error"
      skill: last skills/<name>/SKILL.md file read, if any
      turn_durations_ms: list of completed-turn durations found in the tail
      still_running_since_ms: epoch ms if a turn is currently open, else None
    """
    path = Path(rollout_path)
    if not path.exists():
        return {"status": "idle", "skill": None, "turn_durations_ms": [], "still_running_since_ms": None}
    try:
        size = path.stat().st_size
        data = read_tail_with_status_marker(path, size)
    except OSError:
        return {"status": "idle", "skill": None, "turn_durations_ms": [], "still_running_since_ms": None}

    pending_start = None
    last_completion_kind = None
    last_skill = None
    turn_durations_ms = []
    still_running_since_ms = None

    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        ts = parse_ts(entry.get("timestamp"))
        kind = entry.get("type")
        if kind == "event_msg":
            event_type = entry.get("payload", {}).get("type")
            if event_type == "task_started":
                pending_start = ts
            elif event_type in ("task_complete", "turn_aborted"):
                last_completion_kind = event_type
                if pending_start and ts:
                    turn_durations_ms.append((ts - pending_start).total_seconds() * 1000)
                pending_start = None
        elif kind == "response_item":
            payload = entry.get("payload", {})
            if payload.get("type") == "function_call" and payload.get("name") == "exec_command":
                match = SKILL_RE.search(payload.get("arguments", ""))
                if match:
                    last_skill = match.group(1)

    if pending_start:
        status = "running"
        still_running_since_ms = pending_start.timestamp() * 1000
    elif last_completion_kind == "turn_aborted":
        status = "error"
    else:
        status = "idle"

    return {
        "status": status,
        "skill": last_skill,
        "turn_durations_ms": turn_durations_ms,
        "still_running_since_ms": still_running_since_ms,
    }


def parse_claude_code_session(path):
    """Tail a Claude Code transcript jsonl and derive title/status facts.

    Claude Code has no explicit turn_started/turn_complete marker like Codex.
    Instead: every tool call the assistant makes is a "tool_use" block, and
    the result comes back as a "tool_result" block in a later user message.
    Whatever tool_use has no matching tool_result by end-of-file is still
    in flight - that's the "running" signal.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
                f.readline()
            data = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return None

    title = None
    fallback_title = None
    last_ts_ms = None
    created_ts_ms = None
    pending = {}
    agent_tool_count = 0

    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        parsed_ts = parse_ts(entry.get("timestamp"))
        ts_ms = parsed_ts.timestamp() * 1000 if parsed_ts else None
        if ts_ms:
            last_ts_ms = ts_ms
            if created_ts_ms is None:
                created_ts_ms = ts_ms

        etype = entry.get("type")
        if etype == "custom-title" and entry.get("customTitle"):
            title = entry["customTitle"]
        elif etype == "ai-title" and entry.get("aiTitle") and title is None:
            title = entry["aiTitle"]

        message = entry.get("message") or {}
        content = message.get("content")
        if etype == "user" and fallback_title is None and isinstance(content, str) and content.strip():
            fallback_title = content.strip().splitlines()[0][:70]
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    pending[block.get("id")] = {
                        "name": block.get("name"),
                        "input": block.get("input") or {},
                        "ts_ms": ts_ms,
                    }
                    if block.get("name") == "Agent":
                        agent_tool_count += 1
                elif block.get("type") == "tool_result":
                    pending.pop(block.get("tool_use_id"), None)

    if last_ts_ms is None:
        return None

    return {
        "title": title or fallback_title or "(無題)",
        "last_ts_ms": last_ts_ms,
        "created_ts_ms": created_ts_ms or last_ts_ms,
        "pending": pending,
        "agent_tool_count": agent_tool_count,
    }


def fetch_claude_code_agents(now_ms):
    """Build agent-shaped nodes from local Claude Code session transcripts."""
    agents = []
    if not CLAUDE_PROJECTS_DIR.exists():
        return agents

    for path in CLAUDE_PROJECTS_DIR.glob("*/*.jsonl"):
        try:
            mtime_ms = path.stat().st_mtime * 1000
        except OSError:
            continue
        if now_ms - mtime_ms > WINDOW_MS:
            continue

        parsed = parse_claude_code_session(path)
        if not parsed:
            continue

        age_ms = now_ms - (parsed["last_ts_ms"] or 0)
        pending_items = sorted(parsed["pending"].values(), key=lambda p: p["ts_ms"] or 0, reverse=True)
        running = age_ms < TAIL_ACTIVE_MS and bool(pending_items)
        current = pending_items[0] if running else None
        current_tool_name = current["name"] if current else None
        current_skill = None
        current_label = None
        if current_tool_name == "Skill":
            current_skill = (current["input"] or {}).get("skill")
        elif current_tool_name:
            current_label = friendly_tool_label(current_tool_name)

        children = []
        if current and current_tool_name == "Agent":
            sub_input = current["input"] or {}
            sub_headline = (sub_input.get("description") or "サブエージェント稼働中")[:70]
            children.append({
                "id": path.stem + "-subagent",
                "headline": sub_headline,
                "nickname": sub_input.get("subagent_type"),
                "role": None,
                "model": None,
                "automation": False,
                "job_type": "調査・サブエージェント",
                "status": "running",
                "skill": None,
                "current_tool": "サブエージェント稼働",
                "turn_count": None,
                "avg_turn_ms": None,
                "running_since_ms": current["ts_ms"],
                "created_at_ms": current["ts_ms"],
                "updated_at_ms": current["ts_ms"],
                "source": "claude-code",
                "children": [],
            })

        job_type = classify_job_type(parsed["title"], None, False)
        if job_type == "その他":
            job_type = "コーディング・開発"

        agents.append({
            "id": path.stem,
            "headline": parsed["title"],
            "nickname": None,
            "role": None,
            "model": "Claude Code",
            "automation": False,
            "job_type": job_type,
            "status": "running" if running else "idle",
            "skill": current_skill,
            "current_tool": current_label,
            "turn_count": None,
            "avg_turn_ms": None,
            "running_since_ms": current["ts_ms"] if running else None,
            "created_at_ms": parsed["created_ts_ms"],
            "updated_at_ms": parsed["last_ts_ms"],
            "source": "claude-code",
            "children": children,
        })

    return agents


def build_agents(threads_by_id, children_map, root_ids, now_ms, analysis_cache):
    def node(thread_id):
        thread = threads_by_id[thread_id]
        analysis = analysis_cache.get(thread_id)
        status = analysis["status"] if analysis else "idle"
        skill = analysis["skill"] if analysis else None
        turn_count = len(analysis["turn_durations_ms"]) if analysis else None
        avg_turn_ms = (
            sum(analysis["turn_durations_ms"]) / len(analysis["turn_durations_ms"])
            if analysis and analysis["turn_durations_ms"]
            else None
        )
        thread_headline = headline(thread)
        automation = is_automation(thread)
        return {
            "id": thread_id,
            "headline": thread_headline,
            "nickname": thread["agent_nickname"],
            "role": thread["agent_role"],
            "model": thread["model"],
            "automation": automation,
            "job_type": classify_job_type(thread_headline, skill, automation),
            "status": status,
            "skill": skill,
            "current_tool": None,
            "turn_count": turn_count,
            "avg_turn_ms": avg_turn_ms,
            "running_since_ms": analysis["still_running_since_ms"] if analysis else None,
            "created_at_ms": thread["created_at_ms"],
            "updated_at_ms": thread["updated_at_ms"],
            "source": "codex",
            "children": [node(c) for c in children_map.get(thread_id, []) if c in threads_by_id],
        }

    ordered_roots = sorted(root_ids, key=lambda i: threads_by_id[i]["updated_at_ms"] or 0, reverse=True)
    return [node(rid) for rid in ordered_roots]


def flatten_agents(agents, rows=None):
    if rows is None:
        rows = []
    for agent in agents:
        rows.append(agent)
        if agent.get("children"):
            flatten_agents(agent["children"], rows)
    return rows


def build_summary_and_activity(flat_agents):
    running = idle = error = 0
    weighted_duration_total = 0.0
    weighted_duration_count = 0
    skills_seen = set()
    activity = []

    for agent in flat_agents:
        status = agent["status"]
        if status == "running":
            running += 1
        elif status == "error":
            error += 1
        else:
            idle += 1

        if agent.get("avg_turn_ms") is not None and agent.get("turn_count"):
            weighted_duration_total += agent["avg_turn_ms"] * agent["turn_count"]
            weighted_duration_count += agent["turn_count"]
        current_activity = agent.get("skill") or agent.get("current_tool")
        if current_activity and status == "running":
            skills_seen.add(current_activity)

        name = agent["nickname"] or agent["headline"]
        if status == "running":
            label = f"{name} が作業中"
            if current_activity:
                label += f"（{current_activity}）"
            event_ts = agent.get("running_since_ms") or agent["updated_at_ms"]
        elif status == "error":
            label = f"{name} でエラーが発生"
            event_ts = agent["updated_at_ms"]
        else:
            label = f"{name} が完了"
            event_ts = agent["updated_at_ms"]
        activity.append({"ts_ms": event_ts, "label": label, "status": status, "source": agent.get("source")})

    activity.sort(key=lambda e: e["ts_ms"] or 0, reverse=True)

    summary = {
        "total": len(flat_agents),
        "running": running,
        "idle": idle,
        "error": error,
        "avg_turn_ms": (weighted_duration_total / weighted_duration_count) if weighted_duration_count else None,
        "skill_count": len(skills_seen),
    }
    return summary, activity[:8]


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/api/state":
            self._send_state()
        elif self.path == "/api/health":
            self._send_health()
        elif self.path in ("/", "/index.html"):
            self._send_file("index.html", "text/html")
        elif self.path.startswith("/assets/"):
            self._send_static_asset()
        else:
            self.send_error(404)

    def _send_file(self, name, content_type):
        content = (Path(__file__).parent / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _send_static_asset(self):
        root = (Path(__file__).parent / "assets").resolve()
        raw_path = unquote(urlparse(self.path).path.lstrip("/"))
        requested = (Path(__file__).parent / raw_path).resolve()
        if root not in requested.parents and requested != root:
            self.send_error(403)
            return
        if not requested.is_file():
            self.send_error(404)
            return
        content = requested.read_bytes()
        content_type = mimetypes.guess_type(str(requested))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_health(self):
        self._send_json(
            {
                "ok": True,
                "codex_db_exists": DB_PATH.exists(),
                "claude_projects_dir_exists": CLAUDE_PROJECTS_DIR.exists(),
                "codex_db_path": str(DB_PATH),
                "claude_projects_dir": str(CLAUDE_PROJECTS_DIR),
            }
        )

    def _send_state(self):
        now_ms = int(time.time() * 1000)
        warnings = []
        threads_by_id = {}
        edges = []
        if DB_PATH.exists():
            conn = db()
            try:
                threads_by_id = fetch_recent_threads(conn, now_ms)
                all_edges = fetch_spawn_edges(conn)
                # Only pull in a spawn edge if it touches something already in the
                # recent window - otherwise month-old subagent trees would keep
                # resurrecting just because they once had a parent/child link.
                edges = [(p, c) for p, c in all_edges if p in threads_by_id or c in threads_by_id]
                missing_ids = {p for p, c in edges if p not in threads_by_id}
                missing_ids |= {c for p, c in edges if c not in threads_by_id}
                if missing_ids:
                    threads_by_id.update(fetch_threads_by_ids(conn, missing_ids))
            finally:
                conn.close()
        else:
            warnings.append(f"Codex database not found: {DB_PATH}")

        children_map = {}
        child_ids = set()
        for parent, child in edges:
            if parent in threads_by_id and child in threads_by_id:
                children_map.setdefault(parent, []).append(child)
                child_ids.add(child)

        analysis_cache = {}
        for thread_id, thread in threads_by_id.items():
            age_ms = now_ms - (thread["updated_at_ms"] or 0)
            if age_ms < TAIL_ACTIVE_MS and thread["rollout_path"]:
                analysis_cache[thread_id] = analyze_rollout(thread["rollout_path"])

        root_ids = [tid for tid in threads_by_id if tid not in child_ids]
        codex_agents = build_agents(threads_by_id, children_map, root_ids, now_ms, analysis_cache)
        claude_agents = fetch_claude_code_agents(now_ms)
        agents = codex_agents + claude_agents
        agents.sort(key=lambda a: a["updated_at_ms"] or 0, reverse=True)
        summary, activity = build_summary_and_activity(flatten_agents(agents))

        self._send_json(
            {
                "generated_at_ms": now_ms,
                "summary": summary,
                "activity": activity,
                "agents": agents,
                "warnings": warnings,
            }
        )


def main():
    server = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Codex Agent Monitor -> http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
