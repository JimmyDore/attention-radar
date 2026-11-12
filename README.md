# attention-radar

A macOS menu bar inbox of the Claude Code sessions in [Paradev](https://superset.sh) (formerly Superset) that are waiting for you.

<img src="docs/screenshot.png" alt="The attention-radar menu: sessions grouped by project, with the hovered one offering Open and Snooze" width="720">

## Why

With ten worktrees open, the Paradev sidebar shows a lot of sessions as idle. Some of them really wait for an answer from you. Others look idle but are waiting on a background subagent, a shell, a `Monitor`, a scheduled wakeup or another agent. You can only tell them apart by opening each one.

attention-radar reads each session's state and keeps only the ones where the next move is yours. The number in the menu bar is how many there are. The goal is inbox zero. A row stays until the session runs again, even after you've looked at it.

## What it counts

A Claude Code session running in a Paradev terminal counts unless something is still running:

| Signal | Source | Effect |
|---|---|---|
| Open dialog: Paradev `blocked`, status `waiting`, or an unanswered `AskUserQuestion` or plan approval | snapshot, session file, transcript | 🔴 top of the list, even if the session is otherwise busy |
| Last turn failed | Paradev `host.db` | 🔴 top of the list |
| Status `busy` (thinking, tool, background subagent) or `shell` (background shell, `Monitor`) | `~/.claude/sessions/<pid>.json` | Hidden |
| Status `shell`, but the turn ended on a question, or every background shell is a server listening on a TCP port | transcript, `ps` and `lsof` | Counted: a dev server never ends, so the session is waiting for you |
| `ScheduleWakeup` or one-shot cron still to come | transcript | Hidden |
| Recurring cron not deleted | transcript | Hidden |
| Worker whose turn ended on a report to its orchestrator (opt-in, see below) | transcript, orchestrator records | Hidden, counted in the orchestrator's reports |
| Topic session whose status file for this turn says `quiet` or `working` | status file | Hidden: an orchestrator tick that found nothing for you |
| Anything else | | Counted, oldest first in its project |
| Terminal gone but the session never closed (shutdown, crash) | Paradev `host.db` | ↻ "Interrupted" at the top, counted, with **Resume** |
| Session you paused | radar state + Paradev `host.db` | "Paused" submenu, not counted, with **Resume** |

Each row starts with what the session wants from you: `decisions N`, `approve`, `question`, `review` or `failed`, and hovering it shows the first question or next step. A topic session's status file (see below) says it outright. For any other session the radar reads the last message: numbered questions (`❓ **Q1**`, or `1.` lines ending on a question mark) are decisions, a closing "go…?" or "approve" asks for approval, a published artifact link asks for a review, and anything else is a question.

Rows are grouped by project. The dot shows how long a session has been waiting: 🟠 under 1 h, 🟡 1 to 24 h, ⚪ over 24 h (probably finished: delete the workspace).

A hidden session that has shown no activity for 60 minutes moves to "To check" at the bottom of the menu, without raising the count. That catches a stuck shell or a subagent that hung.

A session that ended its turn on "I'm waiting for X" with nothing running does count. No local signal separates it from a real question.

## Using it

- **→ Next** at the top of the menu says where to go now: the oldest session waiting in the project you last opened one in, so you finish a project before switching, else the oldest anywhere (in the focused project, if there is one). A dialog or a failed turn comes first. Click it to open that session; the next one takes its place.
- Hover a row (or Next), then **Skip, come back later**: the row goes to the end of its group, out of Next, until the session writes again. Nothing opens.
- Hover a row to open its workspace or snooze it.
- Click a row to open its workspace in Paradev, on the right pane.
- A row you opened moves to the end of its group until the session writes again, so you don't open it twice while the radar catches up.
- Hover, then **Snooze**: 1 h, 2 h, until noon or 18:00, or tomorrow morning. New activity in the session brings it back sooner.
- After a shutdown or a crash, the sessions that were open show under **Interrupted**. Click one to resume it on the same conversation, in the pane it died in, or **Resume all** to bring them back one every two seconds. The radar asks Paradev's host service to resume it (`terminalAgents.resume`), which is what Paradev does when you look at the dead pane: the pane picks the new terminal up, and a session can't be started twice. **Open** works too: showing the dead pane makes Paradev resume it. A row leaves once a live terminal holds its session again.
- Too much open at once? Hover a row, then **Pause** to stop that Claude and free its memory, or hover a project's header for **Pause all**. Only a session whose turn is over can be paused. The radar ends the terminal the way a crash does, so Paradev keeps the session resumable: it waits in the **Paused** submenu, out of the count like a snoozed row and grouped by project (**Resume all** per project), and resumes in its own pane when you click **Resume** or look at that pane. A paused pane that is on screen in Paradev resumes right away.
- **Focus on a project**, then a duration: only that project shows and counts in the menu bar; the others fold into one "Other projects" submenu until the focus ends or you stop it.
- **Ignore a project**, then a duration: the opposite of focus. That project leaves the list and the count, and waits in an "Ignored projects" submenu until the time is up or you pick **Stop ignoring**. Several projects can be ignored at once. To skip a project for good, use `ignore_projects` in the config.
- `!` in the menu bar means the radar is broken. It never shows zero in that case.

The menu never deletes a workspace and never types into a session. Resume is the one thing it starts: a Claude on a conversation that already existed, and only when you click it. Pause is the one thing it stops, also only when you click it.

## Install

You need macOS, Paradev with Claude Code in its terminals, and [SwiftBar](https://swiftbar.app). The radar uses the Python 3 that ships with macOS and has no dependencies.

```bash
brew install --cask swiftbar
git clone https://github.com/JimmyDore/attention-radar.git
cd attention-radar
./install.sh
```

`install.sh` links the plugin into SwiftBar's plugin folder, setting one up if SwiftBar has none. It then runs `doctor`, which prints what the radar can see:

```
config file      ~/.config/attention-radar/config.json (not found, defaults)
paradev cli      ~/.paradev/bin/paradev
claude dir       ~/.claude
host databases   ~/.paradev/host/…/host.db
orchestrators    off
snooze file      ~/.local/state/attention-radar/snoozes.json
agent terminals  12, 12 linked to a Claude session
```

To update, run `git pull`. The plugin is a link, so there's nothing to reinstall.

The menu refreshes every 10 seconds. To change that, rename the link in the plugin folder, for example to `attention-radar.30s.py`.

## Configuration

Everything has a default. To change something, create `~/.config/attention-radar/config.json` with the keys you want ([config.example.json](config.example.json) lists them all):

```json
{
  "language": "fr",
  "stale_after_minutes": 30,
  "ignore_projects": ["sandbox"]
}
```

| Key | Default | What it does |
|---|---|---|
| `language` | `"en"` | Menu language: `en` or `fr` |
| `stale_after_minutes` | `60` | How long a running session can stay silent before it goes to "To check" |
| `morning_hour` | `9` | When "Snooze until tomorrow" ends |
| `snooze_hours` | `[12, 18]` | Times offered as "Until …" in the snooze menu |
| `ignore_projects` | `[]` | Paradev project names the radar skips |
| `paradev_cli` | found automatically | Path to the `paradev` CLI |
| `claude_dir` | `$CLAUDE_CONFIG_DIR` or `~/.claude` | Where Claude Code keeps its sessions and transcripts |
| `host_dbs` | `~/.paradev/host/*/host.db`, `~/.superset/host/*/host.db` | Paradev host databases, read for failed turns and interrupted sessions |
| `orchestrators_dirs` | `[]` | See below. `orchestrators_dir`, a single folder, still works |
| `status_dirs` | `["~/.cache/topic-orchestrator/*/status"]` | Folders of topic status files, see below |
| `demo` | `false` | Show made-up sessions instead of yours, for screenshots |

An unknown key or broken JSON shows `!` in the menu bar with the reason, so a typo can't fail silently.

### Orchestrators (optional)

Some sessions are orchestrators: they start workers in other workspaces, and the workers report back to them. Without help the radar shows them all side by side, the workers that only wait on their orchestrator included. List the folders that describe who reports to whom in `orchestrators_dirs`, and the radar keeps one row per orchestrator plus the workers that really wait on you:

```
ACME  ·  3
🟡  billing-orchestrator  ·  2 report(s)
🟠  ticket-42-grill  ·  ↳ billing     a worker talking to you: answer it there
🟠  fix-login                         a session of its own
```

- A worker whose last action before it stopped was a report to its orchestrator waits on the orchestrator. It leaves the menu and isn't counted: its report shows on the orchestrator's row. That holds only while the orchestrator's workspace has a live Claude to read the report.
- A worker that stopped without reporting (a grill, a skill prompt), or kept working after its report and stopped later, is talking to you. It shows as an ordinary row, named after its topic, and is counted. So does a `waiting` report, which points you at the worker's terminal.
- The orchestrator shows how many reports it hasn't read.
- Topic sessions also write `~/.cache/topic-orchestrator/<topic>/status/<workspace id>.json` each turn: `{"label", "count", "next", "at", "terminal", …}`. A status written since the turn's prompt gives the row its label and hover text, and `quiet` or `working` keeps the session out of the count. Set the folders with `status_dirs`.

Each folder is a topic or holds one folder per topic:

```
<topic>/
  orchestrator.json     {"workspace": "<its Paradev workspace id>", "terminal": "<its terminal id>"}
  inbox/<any>.json      a report the orchestrator hasn't read yet, with the worker's "workspace"
  handled/<any>.json    a report it has read
  workers/<any>.json    optional, {"workspace": "<a worker's Paradev workspace id>"}
```

A worker is any workspace listed in `workers/` or named by a report. The radar spots a report in a worker's transcript as a Bash command running `orch.py report`, and reads `--kind waiting` from it. Your orchestration tooling writes these files. The radar only reads them.

## Limits

- `~/.claude/sessions` and `host.db` are internal formats, so an update to Claude Code or Paradev can break the radar. If no session matches at all, the menu bar shows `!` instead of zero.
- Sessions are matched to terminals through the terminal id that Paradev puts in its environment (`SUPERSET_TERMINAL_ID` today), so only Claude Code started in a Paradev terminal is counted.
- A workspace without Claude running isn't counted, so a Claude you exit before it finishes drops off the radar. Sessions killed with their terminal (shutdown, crash) are the exception: they show as Interrupted.

## Privacy

The radar runs locally and makes no network calls, apart from Resume asking Paradev's host service on `127.0.0.1`. It reads the Paradev CLI's snapshot, Paradev's host database, Claude Code's session files, the end of each transcript, and the process list and listening ports (`ps`, `lsof`) when a session has a background shell. It writes only its own state files (snoozes, opened rows, last opened project, focus, paused sessions), starts a Claude only when you click Resume, and stops one only when you click Pause.

## Development

```bash
/usr/bin/python3 -B -m unittest discover -s tests
/usr/bin/python3 -B attention_radar.py demo      # the menu SwiftBar would draw, with made-up sessions
/usr/bin/python3 -B attention_radar.py           # the same with your real sessions
```

## License

MIT
