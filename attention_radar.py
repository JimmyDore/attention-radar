"""Menu bar inbox: Claude Code sessions in Paradev (formerly Superset) that wait for you.

Reads only: the Paradev CLI and host database, Claude Code session files and transcripts,
and optional orchestrator records. Writes only: the snooze file. Never sends anything to a session.
"""
import datetime as dt
import glob
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field, fields
from typing import Dict, List, Optional

HOME = os.path.expanduser("~")
CONFIG_FILE = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"),
                           "attention-radar", "config.json")
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state"),
                         "attention-radar")
CLI_CANDIDATES = ["~/.paradev/bin/paradev", "~/.paradev/bin/superset", "~/.superset/bin/superset"]
CLI_NAMES = ["paradev", "superset"]

TAIL_BYTES = 512 * 1024
EXCERPT_CHARS = 90
DIALOG_TOOLS = {"AskUserQuestion", "ExitPlanMode"}
PLACEHOLDER_NAMES = {"local", "main"}
RUNNING_STATUSES = {"busy", "shell"}

NEEDS = "needs"
CHECK = "check"
HIDDEN = "hidden"


@dataclass
class Config:
    language: str = "en"
    paradev_cli: str = ""
    claude_dir: str = ""
    host_dbs: List[str] = field(default_factory=lambda: ["~/.paradev/host/*/host.db", "~/.superset/host/*/host.db"])
    stale_after_minutes: int = 60
    morning_hour: int = 9
    snooze_hours: List[int] = field(default_factory=lambda: [12, 18])
    ignore_projects: List[str] = field(default_factory=list)
    orchestrators_dir: str = ""
    snooze_file: str = os.path.join(STATE_DIR, "snoozes.json")
    demo: bool = False

    @property
    def claude(self):
        return os.path.expanduser(self.claude_dir or os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")

    @property
    def cli(self):
        if self.paradev_cli:
            return os.path.expanduser(self.paradev_cli)
        for path in map(os.path.expanduser, CLI_CANDIDATES):
            if os.access(path, os.X_OK):
                return path
        found = [shutil.which(name) for name in CLI_NAMES]
        return next((path for path in found if path), "paradev")


def load_config(path=None):
    """Defaults, overridden by the JSON file if there is one. A bad file raises, so the menu shows it."""
    path = path or CONFIG_FILE
    if not os.path.exists(path):
        return Config()
    with open(path) as handle:
        data = json.load(handle)
    if "superset_cli" in data:  # the key's name before the app became Paradev
        data.setdefault("paradev_cli", data.pop("superset_cli"))
    known = {f.name for f in fields(Config)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(f"unknown key(s) in {path}: {', '.join(unknown)}")
    return Config(**data)


TEXTS = {
    "en": {
        "waiting": "{project}  ·  waiting for {ago}",
        "open": "Open workspace",
        "snooze": "Snooze",
        "until": "Until {hour}:00",
        "tomorrow": "Tomorrow {hour}:00",
        "inbox_zero": "Inbox zero",
        "check": "TO CHECK  ·  no activity for {minutes} min",
        "snoozed": "Snoozed ({count})",
        "legend": "Legend",
        "no_text": "(no text)",
        "no_match": "No Claude session matched a terminal: did an internal format change?",
        "broken": "Radar broken: {error}",
        "snapshot_failed": "paradev snapshot failed",
        "units": ("min", "h", "d"),
        "urgency": ("blocked: dialog or failure", "under 1 h", "1 to 24 h", "over 24 h: finished?"),
    },
    "fr": {
        "waiting": "{project}  ·  attend depuis {ago}",
        "open": "Ouvrir le workspace",
        "snooze": "Snoozer",
        "until": "Jusqu'à {hour} h",
        "tomorrow": "Demain {hour} h",
        "inbox_zero": "Inbox zéro",
        "check": "À VÉRIFIER  ·  plus d'activité depuis {minutes} min",
        "snoozed": "Snoozées ({count})",
        "legend": "Légende",
        "no_text": "(pas de texte)",
        "no_match": "Aucune session Claude reliée : format interne changé ?",
        "broken": "Radar en panne : {error}",
        "snapshot_failed": "paradev snapshot a échoué",
        "units": ("min", "h", "j"),
        "urgency": ("bloquée : dialogue ou échec", "moins d'1 h", "1 à 24 h", "plus de 24 h : finie ?"),
    },
}

CONFIG = Config()


def configure(config):
    global CONFIG
    CONFIG = config


def text(key, **values):
    table = TEXTS.get(CONFIG.language, TEXTS["en"])
    value = table[key]
    return value.format(**values) if values else value


@dataclass
class Transcript:
    last_ts: Optional[float] = None
    last_ts_raw: str = ""
    last_text: str = ""
    dialog_text: str = ""
    open_dialog: bool = False
    wake_at: Optional[float] = None
    recurring_cron: bool = False


@dataclass
class Verdict:
    bucket: str
    rank: int = 1
    kind: str = ""


@dataclass
class Row:
    workspace_id: str
    terminal_id: str
    label: str
    project: str
    verdict: Verdict
    since: Optional[float]
    excerpt: str
    activity: str = ""
    session_id: Optional[str] = None
    detail: str = ""
    pane_id: str = ""


@dataclass
class Radar:
    rows: List[Row] = field(default_factory=list)
    snoozed: List[Row] = field(default_factory=list)
    warning: str = ""


def classify(state, status, transcript, failed, now, activity_at=None):
    """Decide where a session goes. `status` is None when no session file matched."""
    if state == "blocked" or status == "waiting" or transcript.open_dialog:
        return Verdict(NEEDS, 0, "dialog")
    if status is None:
        return Verdict(HIDDEN) if state in ("working", "starting") else Verdict(NEEDS)
    if status in RUNNING_STATUSES or transcript.recurring_cron:
        seen = [t for t in (activity_at, transcript.last_ts) if t]
        stale = seen and now - max(seen) > CONFIG.stale_after_minutes * 60
        if stale and not (transcript.wake_at and transcript.wake_at > now):
            return Verdict(CHECK)
        return Verdict(HIDDEN)
    if transcript.wake_at and transcript.wake_at > now:
        return Verdict(HIDDEN)
    if failed:
        return Verdict(NEEDS, 0, "failed")
    return Verdict(NEEDS)


def parse_transcript(lines):
    info = Transcript()
    open_ids = set()
    crons = set()
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("isSidechain") or entry.get("type") not in ("user", "assistant"):
            continue
        stamp = entry.get("timestamp")
        if stamp:
            info.last_ts_raw = stamp
            info.last_ts = _parse_iso(stamp)
        result = entry.get("toolUseResult")
        if isinstance(result, dict) and result.get("scheduledFor"):
            info.wake_at = result["scheduledFor"] / 1000
        if isinstance(result, dict) and result.get("nextFireAt"):
            if result.get("recurring"):
                crons.add(result.get("id"))
            else:
                info.wake_at = result["nextFireAt"] / 1000
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            kind = block.get("type") if isinstance(block, dict) else None
            if kind == "text" and entry["type"] == "assistant" and block.get("text", "").strip():
                info.last_text = block["text"]
            elif kind == "tool_use" and block.get("name") == "CronDelete":
                crons.discard((block.get("input") or {}).get("id"))
            elif kind == "tool_use" and block.get("name") in DIALOG_TOOLS:
                open_ids.add(block.get("id"))
                questions = (block.get("input") or {}).get("questions") or [{}]
                info.dialog_text = questions[0].get("question", "") if isinstance(questions[0], dict) else ""
            elif kind == "tool_result":
                open_ids.discard(block.get("tool_use_id"))
    info.open_dialog = bool(open_ids)
    info.recurring_cron = bool(crons)
    return info


def _parse_iso(value):
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def excerpt(content, limit=EXCERPT_CHARS):
    """End of the last paragraph: that is where the ask sits."""
    paragraphs = [p for p in re.split(r"\n\s*\n", content) if p.strip()]
    tail = paragraphs[-1] if paragraphs else ""
    tail = re.sub(r"[*`#|]", "", " ".join(tail.split())).strip()
    return "…" + tail[-limit:].lstrip() if len(tail) > limit else tail


def without_closing_sentence(content):
    """Drop the sign-off sentence: it pushes "Open" further down for no context.
    A closing question stays, since that is the ask."""
    if content.endswith("?"):
        return content
    ends = list(re.finditer(r"(?<=[.!…])\s+(?=\S)", content))
    return content[:ends[-1].start()] if ends else content


def clean_title(title):
    return re.sub(r"^[^\w«\"']+", "", html.unescape(title or "")).strip()


def duration(seconds):
    minute, hour, day = text("units")
    minutes = int(max(seconds, 0) // 60)
    if minutes < 60:
        return f"{minutes} {minute}"
    if minutes < 48 * 60:
        return f"{minutes // 60} {hour}"
    return f"{minutes // 1440} {day}"


def next_morning(now):
    tomorrow = dt.datetime.fromtimestamp(now).date() + dt.timedelta(days=1)
    return dt.datetime.combine(tomorrow, dt.time(CONFIG.morning_hour)).timestamp()


def is_snoozed(snooze, activity, now):
    return bool(snooze) and now < snooze.get("until", 0) and snooze.get("activity") == activity


def load_snoozes(path=None):
    try:
        with open(path or CONFIG.snooze_file) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def snooze_choices(now):
    """(label, until) pairs still in the future: 1 h, 2 h, the configured hours today, tomorrow morning."""
    today = dt.datetime.fromtimestamp(now).date()
    choices = [("1 h", now + 3600), ("2 h", now + 7200)]
    for hour in CONFIG.snooze_hours:
        moment = dt.datetime.combine(today, dt.time(hour)).timestamp()
        if moment > now + 3600:
            choices.append((text("until", hour=hour), moment))
    return choices + [(text("tomorrow", hour=CONFIG.morning_hour), next_morning(now))]


def save_snooze(terminal_id, activity, now, path=None, until=None):
    path = path or CONFIG.snooze_file
    snoozes = {k: v for k, v in load_snoozes(path).items() if v.get("until", 0) > now}
    snoozes[terminal_id] = {"until": until or next_morning(now), "activity": activity}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w") as handle:
        json.dump(snoozes, handle)
    os.replace(temp, path)


def load_sessions():
    """Live sessions keyed by Paradev terminal id."""
    files = {}
    for path in glob.glob(os.path.join(CONFIG.claude, "sessions", "*.json")):
        try:
            with open(path) as handle:
                data = json.load(handle)
            files[int(data["pid"])] = data
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if not files:
        return {}
    output = subprocess.run(
        ["/bin/ps", "eww", "-o", "pid=,command=", "-p", ",".join(map(str, files))],
        capture_output=True, text=True, timeout=10,
    ).stdout
    sessions = {}
    for line in output.splitlines():
        match = re.match(r"\s*(\d+)\s", line)
        terminal = re.search(r"\b(?:PARADEV|SUPERSET)_TERMINAL_ID=(\S+)", line)
        if not (match and terminal):
            continue
        session = files.get(int(match.group(1)))
        current = sessions.get(terminal.group(1))
        if session and (not current or session.get("startedAt", 0) > current.get("startedAt", 0)):
            sessions[terminal.group(1)] = session
    return sessions


def host_db_paths():
    return sorted({os.path.realpath(path) for pattern in CONFIG.host_dbs for path in glob.glob(os.path.expanduser(pattern))})


def load_failed_terminals():
    failed = set()
    for path in host_db_paths():
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
            try:
                rows = connection.execute(
                    "select terminal_id from terminal_agent_bindings"
                    " where last_event_type = 'Failed' and ended_at is null"
                ).fetchall()
            finally:
                connection.close()
            failed.update(row[0] for row in rows)
        except sqlite3.Error:
            continue
    return failed


def load_orchestrators(root=None):
    """Orchestrator terminal -> its worker workspaces and unread report count.

    Layout, one folder per topic: orchestrator.json {"terminal": ...}, workers/*.json {"workspace": ...},
    inbox/*.json for reports the orchestrator has not read yet.
    """
    root = os.path.expanduser(root if root is not None else CONFIG.orchestrators_dir)
    if not root:
        return {}
    orchestrators = {}
    for pointer in glob.glob(os.path.join(root, "*", "orchestrator.json")):
        topic = os.path.dirname(pointer)
        try:
            with open(pointer) as handle:
                terminal = json.load(handle)["terminal"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        workers = set()
        for path in glob.glob(os.path.join(topic, "workers", "*.json")):
            try:
                with open(path) as handle:
                    workers.add(json.load(handle)["workspace"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
        inbox = len(glob.glob(os.path.join(topic, "inbox", "*.json")))
        orchestrators[terminal] = {"workers": workers, "inbox": inbox}
    return orchestrators


def running_workspaces(snapshot, sessions):
    """Workspaces where at least one Claude is working: session busy or shell, or Paradev says working."""
    running = set()
    for workspace in snapshot.get("workspaces", []):
        for terminal in workspace.get("terminals", []):
            agent = terminal.get("agent")
            if not agent:
                continue
            session = sessions.get(terminal["id"])
            status = session.get("status") if session else None
            if status in RUNNING_STATUSES or (status is None and agent.get("state") in ("working", "starting")):
                running.add(workspace["id"])
    return running


def waits_on_worker(terminal_id, orchestrators, running):
    """An orchestrator with no unread report whose worker is still running has nothing for you yet."""
    orchestrator = orchestrators.get(terminal_id)
    return bool(orchestrator) and not orchestrator["inbox"] and bool(orchestrator["workers"] & running)


def session_dir(session):
    cwd = re.sub(r"[^A-Za-z0-9]", "-", session.get("cwd", ""))
    return os.path.join(CONFIG.claude, "projects", cwd, session.get("sessionId", ""))


def read_transcript(session):
    try:
        with open(session_dir(session) + ".jsonl", "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - TAIL_BYTES))
            data = handle.read()
    except OSError:
        return Transcript()
    lines = data.decode("utf-8", "replace").splitlines()
    return parse_transcript(lines[1:] if size > TAIL_BYTES else lines)


def newest_subagent_write(session):
    times = []
    for path in glob.glob(os.path.join(session_dir(session), "subagents", "*.jsonl")):
        try:
            times.append(os.path.getmtime(path))
        except OSError:
            continue
    return max(times) if times else None


def pane_ids(workspace):
    """terminal id -> pane id, across the main area and the right sidebar."""
    layout = workspace.get("layout") or {}
    tabs = list(layout.get("tabs") or []) + list((layout.get("sidebar") or {}).get("tabs") or [])
    return {
        pane.get("terminalId"): pane.get("id")
        for tab in tabs for pane in tab.get("panes") or [] if pane.get("terminalId")
    }


def build(snapshot, sessions, failed, snoozes, now, read=read_transcript, subagents=newest_subagent_write,
          orchestrators=None):
    radar = Radar()
    orchestrators = orchestrators or {}
    running = running_workspaces(snapshot, sessions) if orchestrators else set()
    ignored = set(CONFIG.ignore_projects)
    agents = matched = 0
    seen_sessions = set()
    for workspace in snapshot.get("workspaces", []):
        if workspace.get("project") in ignored:
            continue
        panes = pane_ids(workspace)
        shared = sum(1 for t in workspace.get("terminals", []) if t.get("agent")) > 1
        for terminal in workspace.get("terminals", []):
            agent = terminal.get("agent")
            if not agent:
                continue
            agents += 1
            session = sessions.get(terminal["id"])
            matched += bool(session)
            session_id = session.get("sessionId") if session else None
            if session_id and session_id in seen_sessions:
                continue
            seen_sessions.add(session_id)
            transcript = read(session) if session else Transcript()
            status = session.get("status") if session else None
            activity_at = None
            if status in RUNNING_STATUSES:
                updated = session.get("statusUpdatedAt")
                writes = subagents(session)
                activity_at = max([t for t in (updated and updated / 1000, writes) if t], default=None)
            verdict = classify(agent.get("state"), status, transcript, terminal["id"] in failed, now, activity_at)
            if verdict.bucket == NEEDS and verdict.rank and waits_on_worker(terminal["id"], orchestrators, running):
                verdict = Verdict(HIDDEN)
            if verdict.bucket == HIDDEN:
                continue
            name = workspace.get("name") or ""
            label = html.unescape(name)
            title = clean_title(terminal.get("title"))
            if name in PLACEHOLDER_NAMES and not shared:
                label = title or name
            elif shared and title:
                label = f"{label} › {title}"
            since = transcript.last_ts or (session and (session.get("statusUpdatedAt") or 0) / 1000) or None
            shown = transcript.dialog_text if transcript.open_dialog and transcript.dialog_text else transcript.last_text
            row = Row(
                workspace["id"], terminal["id"], label, workspace.get("project") or "", verdict,
                since, excerpt(shown), transcript.last_ts_raw, session_id,
            )
            row.pane_id = panes.get(terminal["id"], "")
            row.detail = without_closing_sentence(excerpt(shown, 420))
            if verdict.bucket == NEEDS and is_snoozed(snoozes.get(terminal["id"]), row.activity, now):
                radar.snoozed.append(row)
            else:
                radar.rows.append(row)
    if agents and not matched:
        radar.warning = text("no_match")
    radar.rows.sort(key=lambda r: (r.verdict.rank, r.since or 0))
    return radar


def _safe(content):
    return content.replace("|", " ").replace("\n", " ")


def _params(plugin, *args):
    parts = [f"bash=\"{plugin}\""]
    parts += [f"param{i + 1}=\"{value}\"" for i, value in enumerate(args)]
    return " ".join(parts) + " terminal=false"


FLAGS = "emojize=false symbolize=false trim=true"
GREY = "#8E8E93"
# Emoji, because SwiftBar draws SF Symbols white in a dropdown whatever their colour.
MARKS = ("🔴", "🟠", "🟡", "⚪")


def urgency(row, now):
    if row.verdict.rank == 0:
        return 0
    age = now - row.since if row.since else float("inf")
    return 1 if age < 3600 else (2 if age < 86400 else 3)


def wrap(content, width=60):
    lines, current = [], ""
    for word in content.split():
        if current and len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    return lines + ([current] if current else [])


def _ago(row, now):
    return duration(now - row.since) if row.since else "?"


def _row_lines(row, plugin, now, mark):
    """One short line; hovering opens the full text, Open, and Snooze."""
    opener = _params(plugin, "open", row.workspace_id, row.pane_id)
    # SwiftBar 2.1 drops the submenu routing of an action-less row when it patches the menu in
    # place, which greys the row out. An action on the row itself keeps it enabled.
    lines = [f"{mark}  {_safe(row.label)} | {opener} refresh=false length=55 {FLAGS}"]
    header = text("waiting", project=_safe(row.project), ago=_ago(row, now))
    lines.append(f"--{header} | size=11 color={GREY} {FLAGS}")
    lines += [f"--{_safe(part)} | size=13 {FLAGS}" for part in wrap(row.detail or row.excerpt or text("no_text"))]
    lines.append("-----")
    lines.append(f"--{text('open')} | sfimage=arrow.up.forward.app {opener} refresh=false {FLAGS}")
    if row.verdict.bucket == NEEDS:
        lines.append(f"--{text('snooze')} | sfimage=moon.zzz {FLAGS}")
        for label, until in snooze_choices(now):
            action = _params(plugin, "snooze", row.terminal_id, row.activity, int(until))
            lines.append(f"----{label} | {action} refresh=true {FLAGS}")
    return lines


def render(radar, plugin, now):
    needs = [r for r in radar.rows if r.verdict.bucket == NEEDS]
    checks = [r for r in radar.rows if r.verdict.bucket == CHECK]
    if radar.warning:
        lines = ["! | sfimage=exclamationmark.triangle", "---", f"{_safe(radar.warning)} | {FLAGS}"]
    elif needs:
        lines = [f"{len(needs)} | sfimage=tray.full", "---"]
    else:
        lines = ["0 | sfimage=checkmark.circle", "---", f"{text('inbox_zero')} | {FLAGS}"]
    groups = {}
    for row in sorted(needs, key=lambda r: (urgency(r, now), r.since or 0)):
        groups.setdefault(row.project, []).append(row)
    for index, (project, rows) in enumerate(groups.items()):
        if index:
            lines.append("---")
        lines.append(f"{_safe(project.upper())}  ·  {len(rows)} | size=11 color={GREY} {FLAGS}")
        for row in rows:
            lines += _row_lines(row, plugin, now, MARKS[urgency(row, now)])
    if checks:
        heading = text("check", minutes=CONFIG.stale_after_minutes)
        lines += ["---", f"{heading} | size=11 color={GREY} {FLAGS}"]
        for row in checks:
            lines += _row_lines(row, plugin, now, "⏳")
    if radar.snoozed:
        lines += ["---", f"{text('snoozed', count=len(radar.snoozed))} | sfimage=moon.zzz {FLAGS}"]
        for row in radar.snoozed:
            opener = _params(plugin, "open", row.workspace_id, row.pane_id)
            lines.append(f"--{_safe(row.label)} | {opener} refresh=false {FLAGS}")
    lines += ["---", f"{text('legend')} | size=11 color={GREY} {FLAGS}"]
    lines += [f"--{mark}  {label} | {FLAGS}" for mark, label in zip(MARKS, text("urgency"))]
    return "\n".join(lines)


DEMO = [
    # (workspace, project, bucket, rank, minutes ago, last text)
    ("fix-login-redirect", "web-app", NEEDS, 0, 3, "Keep the session cookie, or move to a short-lived token?"),
    ("add-billing-page", "web-app", NEEDS, 1, 12, "The billing page is done and the tests pass. Should I open the PR?"),
    ("postgres-16-upgrade", "api", NEEDS, 1, 190, "The migration script is ready. Run it on staging now?"),
    ("rate-limit-webhooks", "api", NEEDS, 1, 75, "Two options for the limit: per customer or per endpoint. Which one?"),
    ("refactor-auth-module", "api", NEEDS, 1, 3000, "Done: the PR is merged and the old module is deleted."),
    ("nightly-docs-sync", "docs", CHECK, 1, 95, "Watching the docs build in a background shell."),
]


def demo_radar(now):
    """Made-up sessions, for a screenshot that shows nothing of your own work."""
    radar = Radar()
    for index, (name, project, bucket, rank, minutes, last) in enumerate(DEMO):
        row = Row(f"demo-{index}", f"demo-{index}", name, project, Verdict(bucket, rank), now - minutes * 60,
                  excerpt(last), "demo", detail=last)
        radar.rows.append(row)
    radar.snoozed.append(Row("demo-s", "demo-s", "update-dependencies", "web-app", Verdict(NEEDS), now - 600, ""))
    return radar


def error_output(message):
    return "\n".join(["! | sfimage=exclamationmark.triangle", "---", f"{_safe(message)} | {FLAGS}"])


def load_snapshot():
    done = subprocess.run([CONFIG.cli, "snapshot", "--json"], capture_output=True, text=True, timeout=20)
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip()[:200] or text("snapshot_failed"))
    return json.loads(done.stdout)


def doctor():
    """Print what the radar found, for a first install."""
    print(f"config file      {CONFIG_FILE} ({'found' if os.path.exists(CONFIG_FILE) else 'not found, defaults'})")
    print(f"paradev cli      {CONFIG.cli}")
    print(f"claude dir       {CONFIG.claude}")
    print(f"host databases   {', '.join(host_db_paths()) or 'none found (failed turns will not show)'}")
    print(f"orchestrators    {CONFIG.orchestrators_dir or 'off'}")
    print(f"snooze file      {CONFIG.snooze_file}")
    try:
        snapshot = load_snapshot()
    except Exception as error:
        print(f"snapshot         FAILED: {error}")
        return 1
    sessions = load_sessions()
    terminals = [t for w in snapshot.get("workspaces", []) for t in w.get("terminals", []) if t.get("agent")]
    linked = sum(1 for t in terminals if t["id"] in sessions)
    print(f"agent terminals  {len(terminals)}, {linked} linked to a Claude session")
    return 0 if linked or not terminals else 1


def main(argv, plugin):
    now = dt.datetime.now().timestamp()
    try:
        configure(load_config())
    except (OSError, ValueError, TypeError) as error:
        print(error_output(f"attention-radar config: {error}"))
        return 0
    if argv[:1] == ["doctor"]:
        return doctor()
    if CONFIG.demo or argv[:1] == ["demo"]:
        print(render(demo_radar(now), plugin, now))
        return 0
    if argv[:1] == ["open"] and not argv[1].startswith("demo-"):
        subprocess.run([CONFIG.cli, "workspaces", "open", "--local", argv[1]], timeout=20)
        if len(argv) > 2 and argv[2]:
            subprocess.run([CONFIG.cli, "panes", "focus", "--workspace", argv[1], "--pane", argv[2]], timeout=20)
        return 0
    if argv[:1] in (["open"], ["snooze"]) and CONFIG.demo:
        return 0
    if argv[:1] == ["snooze"]:
        until = float(argv[3]) if len(argv) > 3 and argv[3] else None
        save_snooze(argv[1], argv[2] if len(argv) > 2 else "", now, until=until)
        return 0
    try:
        radar = build(load_snapshot(), load_sessions(), load_failed_terminals(), load_snoozes(), now,
                      orchestrators=load_orchestrators())
    except Exception as error:  # a broken radar must never read as inbox zero
        print(error_output(text("broken", error=error)))
        return 0
    print(render(radar, plugin, now))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.path.realpath(sys.argv[0])))
