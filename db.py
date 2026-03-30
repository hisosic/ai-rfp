"""
PostgreSQL database module for RFP AI Analyzer.
Replaces JSON file-based persistence with a dedicated database.
"""
import os
import json
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

DB_URL = os.environ.get("DATABASE_URL", "postgresql://rfp:changeme@localhost:5432/rfpdb")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS rfps (
    id          TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    filepath    TEXT NOT NULL,
    text_length INTEGER NOT NULL DEFAULT 0,
    uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposals (
    id       TEXT PRIMARY KEY,
    rfp_id   TEXT,
    content  TEXT NOT NULL,
    created_at TEXT DEFAULT (now()::text)
);

CREATE TABLE IF NOT EXISTS knowledge_items (
    id         TEXT PRIMARY KEY,
    category   TEXT NOT NULL,
    title      TEXT NOT NULL,
    content    TEXT NOT NULL,
    tags       TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    id         SERIAL PRIMARY KEY,
    rfp_id     TEXT NOT NULL,
    version    INTEGER NOT NULL,
    content    TEXT NOT NULL,
    score      INTEGER DEFAULT 0,
    note       TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_members (
    id     TEXT PRIMARY KEY,
    rfp_id TEXT NOT NULL,
    name   TEXT NOT NULL,
    role   TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS team_sections (
    id        TEXT PRIMARY KEY,
    rfp_id    TEXT NOT NULL,
    title     TEXT NOT NULL,
    assignee  TEXT DEFAULT '',
    reason    TEXT DEFAULT '',
    status    TEXT DEFAULT '대기'
);

CREATE TABLE IF NOT EXISTS team_comments (
    id         SERIAL PRIMARY KEY,
    section_id TEXT NOT NULL,
    author     TEXT NOT NULL,
    text       TEXT NOT NULL,
    time       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_history (
    id        SERIAL PRIMARY KEY,
    rfp_id    TEXT NOT NULL,
    step      TEXT NOT NULL,
    version   INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    result    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipelines (
    rfp_id          TEXT PRIMARY KEY,
    completed_steps TEXT NOT NULL DEFAULT '{}',
    results         TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS activity_logs (
    id     SERIAL PRIMARY KEY,
    time   TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT DEFAULT ''
);
"""


def _connect():
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = False
    return conn


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(SCHEMA_SQL)
    migrate_from_json_if_needed()


def migrate_from_json_if_needed():
    json_path = Path("data") / "store.json"
    if not json_path.exists():
        return
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM rfps")
        if cur.fetchone()[0] > 0:
            return
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            return

        for rfp_id, meta in data.get("rfp_store_meta", {}).items():
            cur.execute("INSERT INTO rfps (id,filename,filepath,text_length,uploaded_at) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (rfp_id, meta.get("filename",""), meta.get("filepath",""), meta.get("text_length",0), meta.get("uploaded_at","")))

        for pid, p in data.get("proposal_store", {}).items():
            cur.execute("INSERT INTO proposals (id,rfp_id,content) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                        (pid, p.get("rfp_id"), p.get("content","")))

        for item in data.get("knowledge_store", []):
            cur.execute("INSERT INTO knowledge_items (id,category,title,content,tags,created_at) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (item["id"], item["category"], item["title"], item["content"], json.dumps(item.get("tags",[])), item.get("created_at","")))

        for rfp_id, versions in data.get("version_store", {}).items():
            for v in versions:
                cur.execute("INSERT INTO versions (rfp_id,version,content,score,note,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                            (rfp_id, v["version"], v["content"], v.get("score",0), v.get("note",""), v.get("created_at","")))

        for rfp_id, team in data.get("team_store", {}).items():
            for m in team.get("members", []):
                cur.execute("INSERT INTO team_members (id,rfp_id,name,role) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                            (m["id"], rfp_id, m["name"], m.get("role","")))
            for s in team.get("sections", []):
                cur.execute("INSERT INTO team_sections (id,rfp_id,title,assignee,reason,status) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                            (s["id"], rfp_id, s["title"], s.get("assignee",""), s.get("reason",""), s.get("status","대기")))
                for c in s.get("comments", []):
                    cur.execute("INSERT INTO team_comments (section_id,author,text,time) VALUES (%s,%s,%s,%s)",
                                (s["id"], c["author"], c["text"], c["time"]))

        for rfp_id, items in data.get("history_store", {}).items():
            for h in items:
                cur.execute("INSERT INTO analysis_history (rfp_id,step,version,timestamp,result) VALUES (%s,%s,%s,%s,%s)",
                            (rfp_id, h["step"], h["version"], h["timestamp"], h["result"]))

        for rfp_id, p in data.get("pipeline_store", {}).items():
            cur.execute("INSERT INTO pipelines (rfp_id,completed_steps,results) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                        (rfp_id, json.dumps(p.get("completed_steps",{})), json.dumps(p.get("results",{}))))

        for item in data.get("activity_log", []):
            cur.execute("INSERT INTO activity_logs (time,action,detail) VALUES (%s,%s,%s)",
                        (item["time"], item["action"], item.get("detail","")))

    json_path.rename(json_path.with_suffix(".json.bak"))
    print("[Migration] store.json -> PostgreSQL complete")


# ─── RFP CRUD ───

def insert_rfp(rfp_id, filename, filepath, text_length, uploaded_at):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO rfps (id,filename,filepath,text_length,uploaded_at) VALUES (%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET filename=%s,filepath=%s,text_length=%s,uploaded_at=%s",
                    (rfp_id, filename, filepath, text_length, uploaded_at, filename, filepath, text_length, uploaded_at))

def get_rfp_meta(rfp_id):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM rfps WHERE id=%s", (rfp_id,))
        return cur.fetchone()

def rfp_exists(rfp_id):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM rfps WHERE id=%s", (rfp_id,))
        return cur.fetchone() is not None

def list_rfps():
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM rfps ORDER BY uploaded_at DESC")
        return cur.fetchall()

def count_rfps():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM rfps")
        return cur.fetchone()[0]

def delete_rfp(rfp_id):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT filepath FROM rfps WHERE id=%s", (rfp_id,))
        row = cur.fetchone()
        # cascade deletes
        cur.execute("DELETE FROM team_comments WHERE section_id IN (SELECT id FROM team_sections WHERE rfp_id=%s)", (rfp_id,))
        cur.execute("DELETE FROM team_sections WHERE rfp_id=%s", (rfp_id,))
        cur.execute("DELETE FROM team_members WHERE rfp_id=%s", (rfp_id,))
        cur.execute("DELETE FROM versions WHERE rfp_id=%s", (rfp_id,))
        cur.execute("DELETE FROM analysis_history WHERE rfp_id=%s", (rfp_id,))
        cur.execute("DELETE FROM proposals WHERE rfp_id=%s", (rfp_id,))
        cur.execute("DELETE FROM pipelines WHERE rfp_id=%s", (rfp_id,))
        cur.execute("DELETE FROM rfps WHERE id=%s", (rfp_id,))
        return row[0] if row else None


# ─── Proposals ───

def insert_proposal(pid, rfp_id, content):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO proposals (id,rfp_id,content) VALUES (%s,%s,%s)", (pid, rfp_id, content))

def count_proposals():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM proposals")
        return cur.fetchone()[0]

def list_proposals_by_rfp(rfp_id):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM proposals WHERE rfp_id=%s", (rfp_id,))
        return cur.fetchall()


# ─── Knowledge ───

def insert_knowledge(item):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO knowledge_items (id,category,title,content,tags,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                    (item["id"], item["category"], item["title"], item["content"], json.dumps(item.get("tags",[])), item.get("created_at","")))

def list_knowledge(category=None):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if category:
            cur.execute("SELECT * FROM knowledge_items WHERE category=%s ORDER BY created_at DESC", (category,))
        else:
            cur.execute("SELECT * FROM knowledge_items ORDER BY created_at DESC")
        rows = cur.fetchall()
        for r in rows:
            try: r["tags"] = json.loads(r["tags"])
            except: r["tags"] = []
        return rows

def count_knowledge():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM knowledge_items")
        return cur.fetchone()[0]


# ─── Versions ───

def insert_version(rfp_id, version, content, score, note, created_at):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO versions (rfp_id,version,content,score,note,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                    (rfp_id, version, content, score, note, created_at))

def list_versions(rfp_id):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT rfp_id,version,content,score,note,created_at FROM versions WHERE rfp_id=%s ORDER BY version", (rfp_id,))
        return cur.fetchall()

def count_versions(rfp_id):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM versions WHERE rfp_id=%s", (rfp_id,))
        return cur.fetchone()[0]


# ─── Pipeline ───

def upsert_pipeline(rfp_id, completed_steps, results):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""INSERT INTO pipelines (rfp_id,completed_steps,results) VALUES (%s,%s,%s)
                       ON CONFLICT(rfp_id) DO UPDATE SET completed_steps=%s, results=%s""",
                    (rfp_id, json.dumps(completed_steps), json.dumps(results), json.dumps(completed_steps), json.dumps(results)))

def get_pipeline(rfp_id):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM pipelines WHERE rfp_id=%s", (rfp_id,))
        row = cur.fetchone()
        if not row:
            return {"completed_steps": {}, "results": {}}
        return {"completed_steps": json.loads(row["completed_steps"]), "results": json.loads(row["results"])}

def update_pipeline_step(rfp_id, step, result):
    p = get_pipeline(rfp_id)
    p["completed_steps"][step] = True
    p["results"][step] = result
    upsert_pipeline(rfp_id, p["completed_steps"], p["results"])

def list_pipelines():
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM pipelines")
        rows = cur.fetchall()
        result = {}
        for r in rows:
            result[r["rfp_id"]] = {"completed_steps": json.loads(r["completed_steps"]), "results": json.loads(r["results"])}
        return result

def count_pipelines():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM pipelines")
        return cur.fetchone()[0]


# ─── History ───

def insert_history(rfp_id, step, version, timestamp, result):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO analysis_history (rfp_id,step,version,timestamp,result) VALUES (%s,%s,%s,%s,%s)",
                    (rfp_id, step, version, timestamp, result))

def get_history(rfp_id, step=None):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if step:
            cur.execute("SELECT rfp_id,step,version,timestamp,result FROM analysis_history WHERE rfp_id=%s AND step=%s ORDER BY version", (rfp_id, step))
        else:
            cur.execute("SELECT rfp_id,step,version,timestamp,result FROM analysis_history WHERE rfp_id=%s ORDER BY id", (rfp_id,))
        return cur.fetchall()

def count_history_step(rfp_id, step):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM analysis_history WHERE rfp_id=%s AND step=%s", (rfp_id, step))
        return cur.fetchone()[0]


# ─── Activity Log ───

def insert_activity(time_str, action, detail=""):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO activity_logs (time,action,detail) VALUES (%s,%s,%s)", (time_str, action, detail))
        cur.execute("DELETE FROM activity_logs WHERE id NOT IN (SELECT id FROM activity_logs ORDER BY id DESC LIMIT 50)")

def get_recent_activities(limit=10):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT time,action,detail FROM activity_logs ORDER BY id DESC LIMIT %s", (limit,))
        return cur.fetchall()


# ─── Team ───

def get_team(rfp_id):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id,name,role FROM team_members WHERE rfp_id=%s", (rfp_id,))
        members = cur.fetchall()
        cur.execute("SELECT id,title,assignee,reason,status FROM team_sections WHERE rfp_id=%s", (rfp_id,))
        sections = cur.fetchall()
        for s in sections:
            cur.execute("SELECT author,text,time FROM team_comments WHERE section_id=%s ORDER BY id", (s["id"],))
            s["comments"] = cur.fetchall()
        return {"members": members, "sections": sections}

def add_team_member(rfp_id, member_id, name, role):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO team_members (id,rfp_id,name,role) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", (member_id, rfp_id, name, role))

def remove_team_member(rfp_id, member_id):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM team_members WHERE id=%s AND rfp_id=%s", (member_id, rfp_id))
        existed = cur.fetchone()[0] > 0
        cur.execute("DELETE FROM team_members WHERE id=%s AND rfp_id=%s", (member_id, rfp_id))
        return existed

def add_team_section(rfp_id, section_id, title, assignee="", reason="", status="대기"):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO team_sections (id,rfp_id,title,assignee,reason,status) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (section_id, rfp_id, title, assignee, reason, status))

def replace_team_sections(rfp_id, sections):
    """Replace all sections for rfp_id (used by auto-assign)."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM team_comments WHERE section_id IN (SELECT id FROM team_sections WHERE rfp_id=%s)", (rfp_id,))
        cur.execute("DELETE FROM team_sections WHERE rfp_id=%s", (rfp_id,))
        for s in sections:
            cur.execute("INSERT INTO team_sections (id,rfp_id,title,assignee,reason,status) VALUES (%s,%s,%s,%s,%s,%s)",
                        (s["id"], rfp_id, s["title"], s.get("assignee",""), s.get("reason",""), s.get("status","대기")))

def update_section_assignee(section_id, assignee):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE team_sections SET assignee=%s WHERE id=%s", (assignee, section_id))

def update_section_status(section_id, status):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE team_sections SET status=%s WHERE id=%s", (status, section_id))
        cur.execute("SELECT title FROM team_sections WHERE id=%s", (section_id,))
        row = cur.fetchone()
        return row[0] if row else ""

def add_section_comment(section_id, author, text, time_str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO team_comments (section_id,author,text,time) VALUES (%s,%s,%s,%s)", (section_id, author, text, time_str))

def get_team_members(rfp_id):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id,name,role FROM team_members WHERE rfp_id=%s", (rfp_id,))
        return cur.fetchall()
