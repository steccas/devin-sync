"""Tests for devin-sync on synthetic Devin data (no real Devin install needed).

Run: python3 -m unittest discover -s tests -v
"""
import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "devin-sync"

# Subset of the real Devin CLI schema (v17) with the shapes that matter for the merge:
# TEXT session ids, AUTOINCREMENT row ids, composite per-session keys, a global table.
SCHEMA = """
CREATE TABLE refinery_schema_history(version int4 PRIMARY KEY, name VARCHAR(255),
    applied_on VARCHAR(255), checksum VARCHAR(255));
CREATE TABLE sessions (id TEXT PRIMARY KEY, working_directory TEXT NOT NULL, backend_type TEXT NOT NULL,
    model TEXT NOT NULL, agent_mode TEXT NOT NULL, created_at INTEGER NOT NULL,
    last_activity_at INTEGER NOT NULL, title TEXT, main_chain_id INTEGER, hidden INTEGER NOT NULL DEFAULT 0);
CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
    node_id INTEGER NOT NULL, parent_node_id INTEGER, chat_message TEXT NOT NULL, created_at INTEGER NOT NULL,
    UNIQUE(session_id, node_id));
CREATE TABLE prompt_history (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL,
    timestamp INTEGER NOT NULL, session_id TEXT NOT NULL);
CREATE TABLE tool_call_state (session_id TEXT NOT NULL, tool_call_id TEXT NOT NULL, tool_call_json TEXT,
    PRIMARY KEY (session_id, tool_call_id));
CREATE TABLE app_state (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL);
INSERT INTO refinery_schema_history VALUES (17, 'v17', '', '');
PRAGMA journal_mode=WAL;
"""


def make_home(root, name):
    home = Path(root) / name
    (home / ".local/share/devin/cli").mkdir(parents=True)
    (home / ".config/Devin/User/globalStorage").mkdir(parents=True)
    (home / ".config/Devin/User/acp-messages").mkdir(parents=True)
    db = sqlite3.connect(home / ".local/share/devin/cli/sessions.db")
    db.executescript(SCHEMA)
    db.close()
    vs = sqlite3.connect(home / ".config/Devin/User/globalStorage/state.vscdb")
    vs.executescript("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);")
    vs.execute("INSERT INTO ItemTable VALUES ('secret://login', ?)", (f"{name}-secret",))
    vs.commit()
    vs.close()
    return home


def add_session(home, sid, ts, title, messages=2):
    db = sqlite3.connect(home / ".local/share/devin/cli/sessions.db")
    with db:
        db.execute("DELETE FROM message_nodes WHERE session_id=?", (sid,))
        db.execute("DELETE FROM sessions WHERE id=?", (sid,))
        db.execute("INSERT INTO sessions (id, working_directory, backend_type, model, agent_mode, created_at,"
                   " last_activity_at, title, main_chain_id) VALUES (?, '/repo', 'windsurf', 'm', 'code', 1, ?, ?, ?)",
                   (sid, ts, title, messages - 1))
        for n in range(messages):
            db.execute("INSERT INTO message_nodes (session_id, node_id, parent_node_id, chat_message, created_at)"
                       " VALUES (?, ?, ?, ?, 1)", (sid, n, n - 1 if n else None, f"{sid} msg {n}"))
        db.execute("INSERT INTO prompt_history (content, timestamp, session_id) VALUES (?, 1, ?)", (title, sid))
    db.close()
    vs = sqlite3.connect(home / ".config/Devin/User/globalStorage/state.vscdb")
    with vs:
        vs.execute("INSERT INTO ItemTable VALUES (?, ?)",
                   (f"windsurf.acp.sessioninfo.session.acp/devin-cli/{sid}", json.dumps({"title": title})))
    vs.close()


def sessions(home):
    db = sqlite3.connect(home / ".local/share/devin/cli/sessions.db")
    try:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return {sid: (title, n) for sid, title, n in db.execute(
            "SELECT id, title, (SELECT count(*) FROM message_nodes m WHERE m.session_id = s.id) FROM sessions s")}
    finally:
        db.close()


def index_value(home, key):
    vs = sqlite3.connect(home / ".config/Devin/User/globalStorage/state.vscdb")
    try:
        row = vs.execute("SELECT value FROM ItemTable WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        vs.close()


class DevinSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sync = self.root / "sync"
        self.a = make_home(self.root, "A")
        self.b = make_home(self.root, "B")

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, home, host):
        """Fresh module instance bound to a fake HOME and hostname."""
        env = {"HOME": str(home), "DEVIN_SYNC_DIR": str(self.sync), "DEVIN_SYNC_NO_NOTIFY": "1",
               "LANG": "C"}
        saved = {k: os.environ.get(k) for k in [*env, "XDG_CONFIG_HOME", "XDG_STATE_HOME"]}
        os.environ.update(env)
        os.environ.pop("XDG_CONFIG_HOME", None)
        os.environ.pop("XDG_STATE_HOME", None)
        try:
            loader = importlib.machinery.SourceFileLoader(f"devin_sync_{host}", str(SCRIPT))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            mod = importlib.util.module_from_spec(spec)
            loader.exec_module(mod)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        mod.HOST = host
        mod.devin_pids = lambda: []
        mod.LOCK_WAIT = 0
        mod.SNAPSHOT_WAIT = 0
        mod.CFG["sync_dir"] = str(self.sync)
        return mod

    def sync_pc(self, home, host):
        mod = self.load(home, host)
        mod.acquire_lock(False)
        mod.pull()
        self.assertTrue(mod.push())
        mod.release_lock()
        return mod

    def test_union_of_two_pcs(self):
        add_session(self.a, "from-a", 100, "A chat")
        add_session(self.b, "from-b", 200, "B chat", messages=3)
        self.sync_pc(self.a, "pc-a")
        self.sync_pc(self.b, "pc-b")
        self.sync_pc(self.a, "pc-a")
        expected = {"from-a": ("A chat", 2), "from-b": ("B chat", 3)}
        self.assertEqual(sessions(self.a), expected)
        self.assertEqual(sessions(self.b), expected)

    def test_newer_session_wins(self):
        add_session(self.a, "shared", 100, "old")
        self.sync_pc(self.a, "pc-a")
        self.sync_pc(self.b, "pc-b")
        add_session(self.b, "shared", 300, "continued on B", messages=5)
        add_session(self.a, "shared", 150, "stale edit on A")
        self.sync_pc(self.b, "pc-b")
        self.sync_pc(self.a, "pc-a")
        self.assertEqual(sessions(self.a)["shared"], ("continued on B", 5))
        key = "windsurf.acp.sessioninfo.session.acp/devin-cli/shared"
        self.assertEqual(json.loads(index_value(self.a, key))["title"], "continued on B")

    def test_login_secret_never_synced(self):
        add_session(self.a, "x", 1, "x")
        self.sync_pc(self.a, "pc-a")
        self.sync_pc(self.b, "pc-b")
        self.assertEqual(index_value(self.b, "secret://login"), "B-secret")
        index = json.loads((self.sync / "data/index.json").read_text())
        self.assertFalse(any(k.startswith("secret") for k in index))

    def test_similar_session_names_not_confused(self):
        add_session(self.a, "laced-look", 100, "one")
        add_session(self.b, "laced-look-2", 50, "two")
        self.sync_pc(self.a, "pc-a")
        self.sync_pc(self.b, "pc-b")
        key = "windsurf.acp.sessioninfo.session.acp/devin-cli/laced-look-2"
        self.assertEqual(json.loads(index_value(self.b, key))["title"], "two")

    def test_snapshot_has_no_sidecars(self):
        add_session(self.a, "x", 1, "x")
        self.sync_pc(self.a, "pc-a")
        self.sync_pc(self.b, "pc-b")  # reads the snapshot
        data = self.sync / "data"
        self.assertEqual(sorted(p.name for p in data.glob("sessions.db*")), ["sessions.db"])
        with closing(sqlite3.connect(data / "sessions.db")) as c:
            self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "delete")

    def test_schema_mismatch_skips_and_keeps_newer_snapshot(self):
        add_session(self.a, "a", 1, "a")
        self.sync_pc(self.a, "pc-a")
        snap = self.sync / "data/sessions.db"
        with closing(sqlite3.connect(snap)) as c, c:
            c.execute("INSERT INTO refinery_schema_history VALUES (18, 'v18', '', '')")
        meta = json.loads((self.sync / "data/meta.json").read_text())
        mod = self.load(self.b, "pc-b")
        meta["sha256"] = mod.sha256(snap)
        (self.sync / "data/meta.json").write_text(json.dumps(meta))
        before = mod.sha256(snap)
        self.sync_pc(self.b, "pc-b")
        self.assertEqual(sessions(self.b), {})  # not imported
        self.assertEqual(mod.sha256(snap), before)  # older PC did not overwrite

    def test_foreign_lock_blocks(self):
        self.sync.mkdir(parents=True)
        (self.sync / "lock.json").write_text(json.dumps({"host": "other-pc", "since": "now"}))
        mod = self.load(self.a, "pc-a")
        with self.assertRaises(SystemExit) as cm:
            mod.acquire_lock(False)
        self.assertEqual(cm.exception.code, 2)
        mod.acquire_lock(True)  # --force
        self.assertEqual(json.loads((self.sync / "lock.json").read_text())["host"], "pc-a")

    def test_incomplete_download_is_not_imported(self):
        add_session(self.a, "a", 1, "a")
        self.sync_pc(self.a, "pc-a")
        meta = json.loads((self.sync / "data/meta.json").read_text())
        meta["sha256"] = "0" * 64  # as if sessions.db were still downloading
        (self.sync / "data/meta.json").write_text(json.dumps(meta))
        mod = self.load(self.b, "pc-b")
        mod.pull()
        self.assertEqual(sessions(self.b), {})

    def test_install_rewrites_launcher(self):
        entry = self.root / "devin-desktop.desktop"
        entry.write_text("[Desktop Entry]\nName=Devin\nExec=/opt/devin-desktop/devin-desktop %F\n"
                         "[Desktop Action new-empty-window]\nExec=/opt/devin-desktop/devin-desktop --new-window %F\n")
        binary = self.root / "devin-desktop"
        binary.write_text("")
        mod = self.load(self.a, "pc-a")
        mod.DESKTOP_ENTRY_CANDIDATES = [str(entry)]
        mod.CFG["desktop_bin"] = str(binary)
        mod._refresh_menus = lambda: None
        mod.cmd_install(str(self.sync))
        execs = [l for l in mod.LAUNCHER.read_text().splitlines() if l.startswith("Exec=")]
        self.assertEqual(execs, [f"Exec={mod.SHIM} desktop %F", f"Exec={mod.SHIM} desktop --new-window %F"])
        self.assertEqual(json.loads(mod.CONFIG_FILE.read_text())["sync_dir"], str(self.sync.resolve()))
        mod.cmd_uninstall()
        self.assertFalse(mod.LAUNCHER.exists() or mod.SHIM.exists())

    def test_install_from_command_line(self):
        """`install --sync-dir X` must reach cmd_install (argparse REMAINDER trap)."""
        entry = self.root / "devin-desktop.desktop"
        entry.write_text("[Desktop Entry]\nExec=/x/devin-desktop %F\n")
        binary = self.root / "devin-desktop"
        binary.write_text("")
        env = {**os.environ, "HOME": str(self.a), "DEVIN_DESKTOP_BIN": str(binary), "DEVIN_SYNC_NO_NOTIFY": "1"}
        env.pop("DEVIN_SYNC_DIR", None)
        env.pop("XDG_CONFIG_HOME", None)
        # Point the entry lookup at our fake file through a tiny wrapper script.
        runner = (f"import runpy, sys; sys.argv = {[str(SCRIPT), 'install', '--sync-dir', str(self.sync)]!r};"
                  f"g = runpy.run_path({str(SCRIPT)!r}, run_name='devin_sync');"
                  f"g['DESKTOP_ENTRY_CANDIDATES'][:] = [{str(entry)!r}]; g['_refresh_menus'] = lambda: None;"
                  f"g['main'].__globals__.update(g); g['main']()")
        r = subprocess.run([sys.executable, "-c", runner], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        cfg = json.loads((self.a / ".config/devin-sync/config.json").read_text())
        self.assertEqual(cfg["sync_dir"], str(self.sync.resolve()))


if __name__ == "__main__":
    unittest.main()
