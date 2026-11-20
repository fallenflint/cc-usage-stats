"""Live Claude Code usage stats: incremental collector into SQLite + dashboard + status line.

  py -3 ccstats.py collect        incremental: reads only new bytes of each transcript
  py -3 ccstats.py serve [port]   dashboard on http://127.0.0.1:8777 (button "Обновить" = collect)
  py -3 ccstats.py status         one line for a status line (today / subscription month)
  py -3 ccstats.py rebuild        drop the DB and read everything again

Data: transcripts in ~/.claude/projects and WSL's ~/.claude/projects (+ <sid>/subagents/*.jsonl).
Logic ported from the one-off report (collect.py): message.id dedup, Windows/WSL copies merged by
session id, headless auto-runs (ssh-<parent> folder or parent UUID in the first prompt), active time =
sum of gaps <= 15 min, days cut in the configured time zone. Money = API list prices, computed only from local transcripts.
"""
import glob, json, os, re, sqlite3, subprocess, sys, time
from datetime import datetime, timezone, timedelta

CODE = os.path.dirname(os.path.abspath(__file__))           # the repo: code + template
HOME = os.path.expanduser("~")
HERE = os.environ.get("CCSTATS_DIR") or os.path.join(HOME, ".claude", "usage-stats")   # personal data
os.makedirs(HERE, exist_ok=True)
DB = os.path.join(HERE, "usage.db")
LOCK = os.path.join(HERE, "collect.lock")
META_GLOB = os.path.join(HOME, "AppData/Local/Claude-Data/claude_*/claude-code-sessions/*/*/local_*.json")
IDLE_GAP = 15 * 60
STALE = 180            # status line kicks a background collect if data is older than this (s)


def load_config():
    """Personal settings live in <data dir>/config.json, never in the repo (see config.example.json)."""
    try:
        cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    except FileNotFoundError:
        cfg = {}
    local_tz = round((datetime.now().astimezone().utcoffset() or timedelta()).total_seconds() / 3600)
    return {"tz": cfg.get("tz", local_tz), "billing_day": cfg.get("billing_day", 1),
            "port": cfg.get("port", 8777), "extra_roots": cfg.get("extra_roots", []),
            "rules": [tuple(r) for r in cfg.get("rules", [])]}


CFG = load_config()
TZ = CFG["tz"]                     # hours from UTC used to cut days
BILLING_DAY = CFG["billing_day"]   # day of month the subscription renews
PORT = CFG["port"]

# $ per MTok: input, cache write 5m, cache write 1h, cache read, output (Anthropic API list prices, 2026-11)
PRICES = {
    "claude-opus-5-5":   (4.0, 5.0, 8.0, 0.20, 20.0),
    "claude-opus-5":     (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-8":   (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-7":   (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-fable-5-2":  (8.0, 10.0, 16.0, 0.20, 40.0),
    "claude-fable-5-1":  (10.0, 12.5, 20.0, 0.25, 50.0),
    "claude-fable-5":    (10.0, 12.5, 20.0, 1.00, 50.0),
    "claude-sonnet-5-5": (2.0, 2.5, 4.0, 0.10, 10.0),
    "claude-sonnet-5":   (2.0, 2.5, 4.0, 0.20, 10.0),
    "claude-sonnet-4-6": (3.0, 3.75, 6.0, 0.30, 15.0),
    "claude-haiku-5-5":  (0.10, 0.125, 0.20, 0.01, 0.50),   # prompts > 100K: x5 (see cost())
    "claude-haiku-4-5":  (1.0, 1.25, 2.0, 0.10, 5.0),
}
FAMILY = [("fable", "claude-fable-5-2"), ("mythos", "claude-fable-5-2"), ("opus", "claude-opus-5-5"),
          ("sonnet", "claude-sonnet-5-5"), ("haiku", "claude-haiku-5-5")]
PRICE_VER = "2026-11-20a"

RULES = CFG["rules"] + [(r"scratch-workspaces|scratch-\d{4}", "Без папки (scratch)")]
NOISE = re.compile(r"^(<command-|<local-command|<system-reminder>|Caveat:|\[Request interrupted|<task-notification|<side-sessions-event|<ci-monitor)")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def norm(cwd):
    c = (cwd or "").replace("\\", "/").lower()
    c = re.sub(r"/\.claude/worktrees/[^/]+", "", c)
    return c.rstrip("/")


def project_of(cwd):
    c = norm(cwd)
    for rx, name in RULES:
        if re.search(rx, c):
            return name
    return "Другое: " + (c.split("/")[-1] if c else "?")


def text_of(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def day_of(t):
    return datetime.fromtimestamp(t + TZ * 3600, timezone.utc).strftime("%Y-%m-%d")


def price_of(model):
    if model in PRICES:
        return PRICES[model]
    m = (model or "").lower()
    for k, v in FAMILY:
        if k in m:
            return PRICES[v]
    return PRICES["claude-opus-5-5"]


def cost(model, inp, cw5, cw1h, cr, out, speed):
    p = price_of(model)
    mult = 1.0
    if model == "claude-haiku-5-5" and inp + cw5 + cw1h + cr > 100_000:
        mult = 5.0
    if speed == "fast":
        mult *= 2.0
    return mult * (inp * p[0] + cw5 * p[1] + cw1h * p[2] + cr * p[3] + out * p[4]) / 1e6


# ---------------------------------------------------------------- DB
SCHEMA = """
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, sid TEXT, is_sub INT, size INT, mtime REAL, off INT);
CREATE TABLE IF NOT EXISTS sessions(sid TEXT PRIMARY KEY, cwd TEXT, title TEXT, branch TEXT, first_user TEXT,
  ssh_parent TEXT);
CREATE TABLE IF NOT EXISTS msgs(mid TEXT PRIMARY KEY, sid TEXT, is_sub INT, ts REAL, model TEXT,
  inp INT, cw5 INT, cw1h INT, cr INT, out INT, speed TEXT, cost REAL);
CREATE INDEX IF NOT EXISTS msgs_ts ON msgs(ts);
CREATE INDEX IF NOT EXISTS msgs_sid ON msgs(sid);
CREATE TABLE IF NOT EXISTS events(uid TEXT PRIMARY KEY, sid TEXT, ts REAL, kind TEXT, txt TEXT);
CREATE INDEX IF NOT EXISTS events_sid ON events(sid, ts);
CREATE TABLE IF NOT EXISTS meta(sid TEXT PRIMARY KEY, app_id TEXT, title TEXT, archived INT, spawned_by TEXT,
  scheduled INT, created REAL, last_act REAL, cwd TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


def connect():
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def kv_get(con, k, default=None):
    r = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def kv_set(con, k, v):
    con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


# ---------------------------------------------------------------- collect
UPSERT_MSG = """INSERT INTO msgs(mid,sid,is_sub,ts,model,inp,cw5,cw1h,cr,out,speed,cost) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(mid) DO UPDATE SET inp=max(inp,excluded.inp), cw5=max(cw5,excluded.cw5), cw1h=max(cw1h,excluded.cw1h),
 cr=max(cr,excluded.cr), out=max(out,excluded.out), cost=max(cost,excluded.cost)"""


def scan(con, path, sid, is_sub, start):
    """Read complete lines from byte offset `start`; return new offset."""
    sess = con.execute("SELECT cwd,title,branch,first_user FROM sessions WHERE sid=?", (sid,)).fetchone()
    cwd, title, branch, first_user = sess if sess else (None, None, None, None)
    pos = start
    with open(path, "rb") as f:
        f.seek(start)
        for raw in f:
            if not raw.endswith(b"\n"):
                break                      # line still being written
            pos += len(raw)
            if b'"type":"user"' in raw and b'"tool_result"' in raw:
                continue                   # tool output, never a prompt
            if b'"assistant"' not in raw and b'"user"' not in raw and b'custom-title' not in raw:
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue
            typ = d.get("type")
            if typ == "custom-title" and not is_sub:
                title = d.get("customTitle") or title
                continue
            t = d.get("timestamp")
            if not t:
                continue
            tt = ts(t)
            if d.get("cwd") and not cwd:
                cwd = d["cwd"]
            if d.get("gitBranch") and not is_sub:
                branch = d["gitBranch"]
            if typ == "assistant":
                msg = d.get("message") or {}
                u = msg.get("usage")
                model = msg.get("model") or "?"
                if not u or model == "<synthetic>":
                    continue
                mid = msg.get("id") or d.get("requestId") or d.get("uuid")
                inp = u.get("input_tokens") or 0
                cwt = u.get("cache_creation_input_tokens") or 0
                cc = u.get("cache_creation") or {}
                cw1h = cc.get("ephemeral_1h_input_tokens") or 0
                cw5 = max(cwt - cw1h, 0)
                cr = u.get("cache_read_input_tokens") or 0
                out = u.get("output_tokens") or 0
                speed = u.get("speed") or "standard"
                con.execute(UPSERT_MSG, (mid, sid, is_sub, tt, model, inp, cw5, cw1h, cr, out, speed,
                                         cost(model, inp, cw5, cw1h, cr, out, speed)))
                if not is_sub:
                    con.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,NULL)", ("a:" + mid, sid, tt, "a"))
            elif typ == "user" and not is_sub and not d.get("isMeta") and not d.get("isSidechain"):
                c = (d.get("message") or {}).get("content")
                if first_user is None:
                    first_user = c if isinstance(c, str) else json.dumps(c)[:3000]
                    first_user = first_user[:3000]
                txt = text_of(c).strip()
                if not txt or NOISE.match(txt):
                    continue
                con.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)",
                            ("p:" + (d.get("uuid") or t), sid, tt, "p", txt[:400]))
    con.execute("""INSERT INTO sessions(sid,cwd,title,branch,first_user) VALUES(?,?,?,?,?)
        ON CONFLICT(sid) DO UPDATE SET cwd=excluded.cwd,title=excluded.title,branch=excluded.branch,
        first_user=excluded.first_user""", (sid, cwd, title, branch, first_user))
    return pos


def running_distros():
    """WSL distros that are up right now. Asking does not boot WSL; touching \\wsl.localhost would."""
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(["wsl.exe", "-l", "--running", "-q"], capture_output=True, timeout=15,
                             creationflags=0x08000000).stdout  # CREATE_NO_WINDOW
        return [d for d in out.decode("utf-16le", "ignore").replace("\x00", "").split() if d]
    except Exception:
        return []


def roots():
    """Transcript folders: this OS's ~/.claude/projects, every running WSL distro's, and config extra_roots."""
    out = [os.path.join(HOME, ".claude", "projects")] + list(CFG["extra_roots"])
    for d in running_distros():            # a stopped distro is skipped; its files wait for the next run
        base = f"//wsl.localhost/{d}"
        out += glob.glob(base + "/home/*/.claude/projects") + glob.glob(base + "/root/.claude/projects")
    return out


def wsl_running(distro="ubuntu"):
    """True if the WSL distro is already up. Touching \\\\wsl.localhost would boot a stopped one."""
    try:
        out = subprocess.run(["wsl.exe", "-l", "--running", "-q"], capture_output=True, timeout=15,
                             creationflags=0x08000000 if os.name == "nt" else 0).stdout  # CREATE_NO_WINDOW
        names = out.decode("utf-16le", "ignore").replace("\x00", "").lower().split()
        return distro in names
    except Exception:
        return False


def discover():
    """Yield (path, sid, is_sub, ssh_parent) for every transcript."""
    for root in roots():
        try:
            if not os.path.isdir(root):
                continue
        except OSError:
            continue
        for path in glob.glob(os.path.join(root, "*", "*.jsonl")):
            sid = os.path.splitext(os.path.basename(path))[0]
            folder = os.path.basename(os.path.dirname(path))
            ssh = folder[4:] if folder.startswith("ssh-") and folder[4:] != sid else None
            yield path, sid, 0, ssh
        for path in glob.glob(os.path.join(root, "*", "*", "subagents", "*.jsonl")):
            sid = os.path.basename(os.path.dirname(os.path.dirname(path)))
            yield path, sid, 1, None


def load_meta(con):
    rows = []
    for f in glob.glob(META_GLOB):
        try:
            m = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for k in ("cliSessionId", "lastAssistantCliSessionId"):
            if m.get(k):
                rows.append((m[k], m.get("sessionId"), m.get("title"), 1 if m.get("isArchived") else 0,
                             m.get("parentSessionId") or m.get("forkedFromSessionId"),
                             1 if m.get("scheduledTaskId") else 0, (m.get("createdAt") or 0) / 1000 or None,
                             (m.get("lastActivityAt") or 0) / 1000 or None, m.get("cwd") or m.get("originCwd")))
    seen = set()
    for r in rows:
        if r[0] in seen:
            continue
        seen.add(r[0])
        con.execute("INSERT OR REPLACE INTO meta VALUES(?,?,?,?,?,?,?,?,?)", r)


def take_lock():
    try:
        if os.path.exists(LOCK) and time.time() - os.path.getmtime(LOCK) > 1800:
            os.remove(LOCK)          # stale lock from a crashed run
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def collect(verbose=True):
    if not take_lock():
        if verbose:
            print("another collect is running; skipped")
        return False
    try:
        t0 = time.time()
        con = connect()
        if kv_get(con, "price_ver") != PRICE_VER:
            recost(con)
        known = {r[0]: r[1:] for r in con.execute("SELECT path,size,mtime,off FROM files")}
        n_files = n_bytes = 0
        for path, sid, is_sub, ssh in discover():
            try:
                st = os.stat(path)
            except OSError:
                continue
            k = known.get(path)
            if k and k[0] == st.st_size and k[1] == st.st_mtime:
                continue
            off = k[2] if k else 0
            if st.st_size < off:
                off = 0                    # file was rewritten; dedup keys make re-reading safe
            try:
                new_off = scan(con, path, sid, is_sub, off)
            except OSError:
                continue
            if ssh:
                con.execute("UPDATE sessions SET ssh_parent=? WHERE sid=?", (ssh, sid))
            con.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?)",
                        (path, sid, is_sub, st.st_size, st.st_mtime, new_off))
            n_files += 1
            n_bytes += new_off - off
            if n_files % 50 == 0:
                con.commit()
        load_meta(con)
        kv_set(con, "last_collect", time.time())
        con.commit()
        write_outputs(con)
        con.close()
        if verbose:
            print(f"collect: {n_files} files, {n_bytes / 1e6:.1f} MB new, {time.time() - t0:.1f}s")
        return True
    finally:
        try:
            os.remove(LOCK)
        except OSError:
            pass


def recost(con):
    rows = con.execute("SELECT mid,model,inp,cw5,cw1h,cr,out,speed FROM msgs").fetchall()
    con.executemany("UPDATE msgs SET cost=? WHERE mid=?", [(cost(*r[1:]), r[0]) for r in rows])
    kv_set(con, "price_ver", PRICE_VER)


# ---------------------------------------------------------------- aggregate
def period_bounds(now=None):
    """Current subscription month in local time: [billing day 00:00, next billing day 00:00)."""
    loc = datetime.fromtimestamp((now or time.time()) + TZ * 3600, timezone.utc)
    y, m = loc.year, loc.month
    if loc.day < BILLING_DAY:
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    start = datetime(y, m, BILLING_DAY, tzinfo=timezone.utc)
    ny, nm = (y + (m == 12), 1 if m == 12 else m + 1)
    end = datetime(ny, nm, BILLING_DAY, tzinfo=timezone.utc)
    off = timedelta(hours=TZ)
    return (start - off).timestamp(), (end - off).timestamp()


def build_data(con):
    meta = {r[0]: r for r in con.execute("SELECT * FROM meta")}
    sess = {r[0]: r for r in con.execute("SELECT * FROM sessions")}
    # per session, per day: cost, total tokens, in, cw, cr, out, calls ; subagent cost
    agg = {}
    model_days = {}
    for sid, day, model, is_sub, c, inp, cw, cr, out, n in con.execute(
            f"""SELECT sid, date(ts+{TZ * 3600},'unixepoch') d, model, is_sub, sum(cost), sum(inp), sum(cw5+cw1h),
                sum(cr), sum(out), count(*) FROM msgs GROUP BY sid, d, model, is_sub"""):
        a = agg.setdefault(sid, {"days": {}, "models": {}, "sub": 0.0})
        dd = a["days"].setdefault(day, [0.0, 0, 0, 0, 0, 0, 0, 0.0])  # cost,tok,in,cw,cr,out,calls,active_h
        dd[0] += c; dd[1] += inp + cw + cr + out; dd[2] += inp; dd[3] += cw; dd[4] += cr; dd[5] += out; dd[6] += n
        a["models"][model] = a["models"].get(model, 0.0) + c
        if is_sub:
            a["sub"] += c
        md = model_days.setdefault(day, {}).setdefault(model, [0.0, 0])
        md[0] += c; md[1] += inp + cw + cr + out
    # events: prompts and active time
    prompts, first, last, samples = {}, {}, {}, {}
    prev = {}
    for sid, t, kind, txt in con.execute("SELECT sid, ts, kind, txt FROM events ORDER BY sid, ts"):
        a = agg.setdefault(sid, {"days": {}, "models": {}, "sub": 0.0})
        first.setdefault(sid, t)
        last[sid] = t
        if kind == "p":
            prompts[sid] = prompts.get(sid, 0) + 1
            if sid not in samples and txt:
                samples[sid] = txt[:200]
        p = prev.get(sid)
        if p is not None and t - p <= IDLE_GAP:
            dd = a["days"].setdefault(day_of(t), [0.0, 0, 0, 0, 0, 0, 0, 0.0])
            dd[7] += (t - p) / 3600
        prev[sid] = t
    all_ids = set(sess)
    rows = []
    for sid, a in agg.items():
        s = sess.get(sid) or (sid, None, None, None, None, None)
        m = meta.get(sid)
        cwd = s[1] or (m[8] if m else "") or ""
        parent = s[5]
        if not parent and not prompts.get(sid) and s[4]:
            c = [u for u in UUID.findall(s[4]) if u != sid and u in all_ids]
            parent = c[0] if c else None
        kind = "headless" if (not prompts.get(sid) and (parent or not m)) else "interactive"
        title = (m[2] if m and m[2] else None) or s[2] or samples.get(sid, "")[:80] or sid[:8]
        rows.append({"id": sid, "t": title, "p": project_of(cwd), "cwd": cwd, "k": kind, "par": parent,
                     "app": m[1] if m else None, "arch": bool(m and m[3]), "sched": bool(m and m[5]),
                     "s": first.get(sid), "e": last.get(sid), "pr": prompts.get(sid, 0), "sub": round(a["sub"], 4),
                     "models": {k: round(v, 4) for k, v in a["models"].items()},
                     "days": {d: [round(v[0], 4)] + v[1:7] + [round(v[7], 4)] for d, v in a["days"].items()}})
    by_id = {r["id"]: r for r in rows}
    for r in rows:   # auto-runs inherit the parent's project
        if r["k"] == "headless" and r["par"] in by_id:
            r["p"] = by_id[r["par"]]["p"]
            r["pt"] = by_id[r["par"]]["t"]
    ps, pe = period_bounds()
    return {"generated": time.time(), "last_collect": float(kv_get(con, "last_collect", 0) or 0),
            "tz": TZ, "period": [ps, pe], "billing_day": BILLING_DAY, "rows": rows,
            "model_days": {d: {m: [round(v[0], 4), v[1]] for m, v in ms.items()} for d, ms in model_days.items()}}


def summary(con):
    now = time.time()
    ps, pe = period_bounds(now)
    today = day_of(now)
    tstart = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() - TZ * 3600
    q = "SELECT coalesce(sum(cost),0), coalesce(sum(inp+cw5+cw1h+cr+out),0), coalesce(sum(out),0) FROM msgs WHERE ts>=? AND ts<?"
    d = con.execute(q, (tstart, tstart + 86400)).fetchone()
    p = con.execute(q, (ps, pe)).fetchone()
    return {"today": {"cost": d[0], "tokens": d[1], "out": d[2]},
            "period": {"cost": p[0], "tokens": p[1], "out": p[2], "start": ps, "end": pe,
                       "days": max(1, int((now - ps) // 86400) + 1)},
            "last_collect": float(kv_get(con, "last_collect", 0) or 0), "generated": now}


def write_outputs(con):
    data = build_data(con)
    tpl = open(os.path.join(CODE, "dashboard_template.html"), encoding="utf-8").read()
    html = tpl.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False))
    tmp = os.path.join(HERE, "dashboard.html.tmp")
    open(tmp, "w", encoding="utf-8").write(html)
    os.replace(tmp, os.path.join(HERE, "dashboard.html"))
    tmp = os.path.join(HERE, "summary.json.tmp")
    json.dump(summary(con), open(tmp, "w", encoding="utf-8"))
    os.replace(tmp, os.path.join(HERE, "summary.json"))


# ---------------------------------------------------------------- status line
def fmt_money(x):
    return f"${x:,.0f}" if x >= 100 else f"${x:,.2f}"


def status():
    try:
        sys.stdin.read() if not sys.stdin.isatty() else None   # Claude Code sends session JSON; unused
    except Exception:
        pass
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=2)
        s = summary(con)
        con.close()
    except Exception:
        print("CC: нет базы")
        return
    if time.time() - s["last_collect"] > STALE and not os.path.exists(LOCK):
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else sys.executable
        flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0   # DETACHED_PROCESS | NEW_PROCESS_GROUP
        try:
            subprocess.Popen([exe, os.path.abspath(__file__), "collect", "-q"], creationflags=flags,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True)
        except Exception:
            pass
    age = int((time.time() - s["last_collect"]) / 60)
    print(f"сегодня {fmt_money(s['today']['cost'])} · месяц {fmt_money(s['period']['cost'])}"
          f" ({s['period']['days']} дн)" + (f" · {age} мин назад" if age >= 15 else ""))


# ---------------------------------------------------------------- server
def serve(port=PORT):
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype):
            b = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self.send(200, open(os.path.join(CODE, "dashboard_template.html"), encoding="utf-8").read(),
                          "text/html; charset=utf-8")
            elif self.path.startswith("/api/data"):
                con = connect()
                try:
                    self.send(200, json.dumps(build_data(con), ensure_ascii=False), "application/json")
                finally:
                    con.close()
            elif self.path.startswith("/api/summary"):
                con = connect()
                try:
                    self.send(200, json.dumps(summary(con)), "application/json")
                finally:
                    con.close()
            else:
                self.send(404, "not found", "text/plain")

        def do_POST(self):
            if self.path.startswith("/api/refresh"):
                ok = collect(verbose=False)
                self.send(200, json.dumps({"ok": ok}), "application/json")
            else:
                self.send(404, "not found", "text/plain")

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"http://127.0.0.1:{port}/")
    srv.serve_forever()


if __name__ == "__main__":
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    cmd = sys.argv[1] if len(sys.argv) > 1 else "collect"
    if cmd == "collect":
        collect(verbose="-q" not in sys.argv)
    elif cmd == "serve":
        serve(int(sys.argv[2]) if len(sys.argv) > 2 else PORT)
    elif cmd == "status":
        status()
    elif cmd == "rebuild":
        for f in (DB, DB + "-wal", DB + "-shm"):
            if os.path.exists(f):
                os.remove(f)
        collect()
    else:
        print(__doc__)
