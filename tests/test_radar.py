import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import attention_radar as ar  # noqa: E402

NOW = 1_800_000_000.0


def t(**kw):
    return ar.Transcript(**kw)


def line(kind, content, ts="2026-11-05T10:00:00.000Z", **extra):
    return json.dumps({"type": kind, "timestamp": ts, "message": {"content": content}, **extra})


class Base(unittest.TestCase):
    def setUp(self):
        ar.configure(ar.Config())


class Classify(Base):
    def verdict(self, state="idle", status="idle", transcript=None, failed=False, activity=None, serving=False):
        return ar.classify(state, status, transcript or t(last_ts=NOW - 10), failed, NOW, activity, serving)

    def test_idle_with_nothing_running_needs_you(self):
        self.assertEqual(self.verdict().bucket, ar.NEEDS)

    def test_background_shell_or_subagent_is_hidden_even_if_paradev_says_idle(self):
        self.assertEqual(self.verdict(status="shell").bucket, ar.HIDDEN)
        self.assertEqual(self.verdict(status="busy").bucket, ar.HIDDEN)

    def test_background_shell_with_a_question_or_a_server_needs_you(self):
        self.assertEqual(self.verdict(status="shell", transcript=t(last_ts=NOW - 10, asks=True)).bucket, ar.NEEDS)
        self.assertEqual(self.verdict(status="shell", serving=True).bucket, ar.NEEDS)

    def test_busy_or_a_coming_wakeup_beats_the_question(self):
        self.assertEqual(self.verdict(status="busy", transcript=t(asks=True), serving=True).bucket, ar.HIDDEN)
        waking = t(last_ts=NOW - 10, asks=True, wake_at=NOW + 60)
        self.assertEqual(self.verdict(status="shell", transcript=waking).bucket, ar.HIDDEN)

    def test_dialog_beats_busy_and_ranks_first(self):
        for verdict in (
            self.verdict(state="blocked", status="busy"),
            self.verdict(status="waiting"),
            self.verdict(transcript=t(open_dialog=True)),
        ):
            self.assertEqual((verdict.bucket, verdict.rank), (ar.NEEDS, 0))

    def test_future_wakeup_is_hidden_past_wakeup_is_not(self):
        self.assertEqual(self.verdict(transcript=t(wake_at=NOW + 60)).bucket, ar.HIDDEN)
        self.assertEqual(self.verdict(transcript=t(wake_at=NOW - 60)).bucket, ar.NEEDS)

    def test_silent_running_session_goes_to_check_at_60_minutes(self):
        old = t(last_ts=NOW - 3601)
        self.assertEqual(self.verdict(status="shell", transcript=old).bucket, ar.CHECK)
        self.assertEqual(self.verdict(status="shell", transcript=t(last_ts=NOW - 3500)).bucket, ar.HIDDEN)

    def test_recent_subagent_write_keeps_it_hidden(self):
        old = t(last_ts=NOW - 7200)
        self.assertEqual(self.verdict(status="busy", transcript=old, activity=NOW - 30).bucket, ar.HIDDEN)

    def test_recurring_cron_is_hidden_until_it_goes_silent(self):
        live = t(last_ts=NOW - 600, recurring_cron=True)
        self.assertEqual(self.verdict(transcript=live).bucket, ar.HIDDEN)
        silent = t(last_ts=NOW - 3601, recurring_cron=True)
        self.assertEqual(self.verdict(transcript=silent).bucket, ar.CHECK)

    def test_failed_ranks_first_only_when_stopped(self):
        self.assertEqual(self.verdict(failed=True).kind, "failed")
        self.assertEqual(self.verdict(status="busy", failed=True).bucket, ar.HIDDEN)

    def test_without_session_file_falls_back_on_paradev_state(self):
        self.assertEqual(self.verdict(state="working", status=None).bucket, ar.HIDDEN)
        self.assertEqual(self.verdict(state="idle", status=None).bucket, ar.NEEDS)


class ParseTranscript(Base):
    def test_last_assistant_text_and_timestamp(self):
        info = ar.parse_transcript([
            line("assistant", [{"type": "text", "text": "first"}]),
            line("assistant", [{"type": "text", "text": "second"}], ts="2026-11-05T11:00:00.000Z"),
        ])
        self.assertEqual(info.last_text, "second")
        self.assertEqual(info.last_ts_raw, "2026-11-05T11:00:00.000Z")

    def test_subagent_lines_and_other_entry_types_are_ignored(self):
        info = ar.parse_transcript([
            line("assistant", [{"type": "text", "text": "mine"}]),
            line("assistant", [{"type": "text", "text": "subagent"}], isSidechain=True),
            json.dumps({"type": "cost-state", "timestamp": "2030-01-01T00:00:00Z"}),
            "not json",
        ])
        self.assertEqual(info.last_text, "mine")
        self.assertNotIn("2030", info.last_ts_raw)

    def test_dialog_is_open_until_it_has_a_result(self):
        ask = line("assistant", [{"type": "tool_use", "id": "x", "name": "AskUserQuestion"}])
        self.assertTrue(ar.parse_transcript([ask]).open_dialog)
        answer = line("user", [{"type": "tool_result", "tool_use_id": "x"}])
        self.assertFalse(ar.parse_transcript([ask, answer]).open_dialog)

    def test_open_dialog_keeps_its_question(self):
        ask = line("assistant", [{"type": "tool_use", "id": "x", "name": "AskUserQuestion",
                                  "input": {"questions": [{"question": "red or blue?"}]}}])
        self.assertEqual(ar.parse_transcript([ask]).dialog_text, "red or blue?")

    def test_turn_that_ends_on_a_question_asks(self):
        ask = line("assistant", [{"type": "text", "text": "Done.\n\n**Ça te va comme ça ?**\n\n➡️ Oui."}])
        self.assertTrue(ar.parse_transcript([ask]).asks)
        tool = line("assistant", [{"type": "tool_use", "id": "b", "name": "Bash"}])
        self.assertFalse(ar.parse_transcript([ask, tool]).asks)
        self.assertFalse(ar.parse_transcript([ask, line("user", "go")]).asks)

    def test_question_marks_in_code_or_mid_line_do_not_ask(self):
        self.assertFalse(ar.asks_question("Server on http://x/?a=1 is up.\n```\nwhat?\n```"))
        self.assertTrue(ar.asks_question("Je lance ?\n"))

    def test_wakeup_time_is_read_in_seconds(self):
        info = ar.parse_transcript([line("user", [], toolUseResult={"scheduledFor": 1793812380000})])
        self.assertEqual(info.wake_at, 1793812380.0)


    def test_recurring_cron_is_active_until_deleted(self):
        made = line("user", [], toolUseResult={"id": "c1", "recurring": True, "nextFireAt": 1793902316628.74})
        self.assertTrue(ar.parse_transcript([made]).recurring_cron)
        gone = line("assistant", [{"type": "tool_use", "name": "CronDelete", "input": {"id": "c1"}}])
        self.assertFalse(ar.parse_transcript([made, gone]).recurring_cron)

    def test_one_shot_cron_is_a_wakeup(self):
        made = line("user", [], toolUseResult={"id": "c1", "recurring": False, "nextFireAt": 1793902316000})
        info = ar.parse_transcript([made])
        self.assertEqual((info.wake_at, info.recurring_cron), (1793902316.0, False))


class Serving(Base):
    TABLE = "\n".join([
        "  100     1 claude", "  101   100 /bin/zsh", "  102   101 uv", "  103   102 python3",
        "  104   100 python3",
        "  200     1 claude", "  201   200 /bin/zsh", "  202   201 npm",
        "  300     1 claude", "  301   300 -zsh", "  302   300 /bin/zsh", "  303   302 node",
        "  400     1 claude", "  401   400 python3",
    ])
    LISTEN = "p103\nf9\np303\nf3\np401\n"

    def test_only_sessions_whose_every_shell_listens_are_serving(self):
        found = ar.serving_pids({100, 200, 300, 400}, self.TABLE, self.LISTEN)
        # 200: a build with no socket; 300: one shell serves, the other still runs; 400: no shell at all.
        self.assertEqual(found, {100})

    def test_build_passes_serving_through(self):
        sessions = {"t1": {"status": "shell", "sessionId": "s", "pid": 100}}
        quiet = lambda s: t(last_ts=NOW - 60, last_text="Server up.")
        hidden = ar.build(snapshot(term()), sessions, set(), {}, NOW, read=quiet, subagents=lambda s: None)
        shown = ar.build(snapshot(term()), sessions, set(), {}, NOW, read=quiet, subagents=lambda s: None,
                         serving={100})
        self.assertEqual((len(hidden.rows), len(shown.rows)), (0, 1))


class Snooze(Base):
    def test_until_time_and_new_activity_end_it(self):
        snooze = {"until": NOW + 100, "activity": "a"}
        self.assertTrue(ar.is_snoozed(snooze, "a", NOW))
        self.assertFalse(ar.is_snoozed(snooze, "b", NOW))
        self.assertFalse(ar.is_snoozed(snooze, "a", NOW + 101))
        self.assertFalse(ar.is_snoozed(None, "a", NOW))

    def test_next_morning_is_tomorrow_at_nine(self):
        import datetime as dt
        moment = dt.datetime(2026, 11, 5, 23, 30).timestamp()
        self.assertEqual(dt.datetime.fromtimestamp(ar.next_morning(moment)), dt.datetime(2026, 11, 6, 9))

    def test_choices_drop_times_already_past(self):
        import datetime as dt
        morning = dt.datetime(2026, 11, 5, 8, 0).timestamp()
        self.assertEqual([c[0] for c in ar.snooze_choices(morning)], ["1 h", "2 h", "Until 12:00", "Until 18:00", "Tomorrow 9:00"])
        evening = dt.datetime(2026, 11, 5, 17, 30).timestamp()
        self.assertEqual([c[0] for c in ar.snooze_choices(evening)], ["1 h", "2 h", "Tomorrow 9:00"])

    def test_save_uses_the_chosen_end(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "s.json")
            ar.save_snooze("t", "a", NOW, path, until=NOW + 3600)
            self.assertEqual(ar.load_snoozes(path)["t"]["until"], NOW + 3600)

    def test_focus_ends_on_its_own(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "sub", "f.json")
            ar.save_focus("side", NOW + 60, path)
            self.assertEqual(ar.load_focus(NOW, path), {"project": "side", "until": NOW + 60})
            self.assertIsNone(ar.load_focus(NOW + 61, path))
            ar.save_focus(None, 0, path)
            self.assertIsNone(ar.load_focus(NOW, path))

    def test_ignore_ends_on_its_own_and_can_be_stopped(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "sub", "i.json")
            ar.save_ignored("side", NOW + 60, NOW, path)
            ar.save_ignored("acme", NOW + 600, NOW, path)
            self.assertEqual(ar.load_ignored(NOW, path), {"side": NOW + 60, "acme": NOW + 600})
            self.assertEqual(ar.load_ignored(NOW + 61, path), {"acme": NOW + 600})
            ar.save_ignored("acme", None, NOW, path)
            self.assertEqual(ar.load_ignored(NOW, path), {"side": NOW + 60})

    def test_last_project_is_remembered(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "last.json")
            self.assertIsNone(ar.load_last_project(path))
            ar.save_last_project("acme", NOW, path)
            self.assertEqual(ar.load_last_project(path), "acme")

    def test_opened_is_remembered_for_a_day(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "sub", "o.json")
            ar.save_opened("old", "a", NOW - 200000, path)
            ar.save_opened("new", "b", NOW, path)
            self.assertEqual(ar.load_opened(path), {"new": {"activity": "b", "at": NOW}})

    def test_save_keeps_live_entries_and_drops_expired(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "sub", "s.json")
            ar.save_snooze("old", "a", NOW - 200000, path)
            ar.save_snooze("new", "b", NOW, path)
            self.assertEqual(set(ar.load_snoozes(path)), {"new"})


def snapshot(*terminals, name="feature-1", project="acme"):
    return {"workspaces": [{"id": "w1", "name": name, "project": project, "terminals": list(terminals)}]}


def term(id_="t1", state="idle", title="◑ Un titre"):
    return {"id": id_, "title": title, "agent": {"id": "claude", "state": state}}


class Build(Base):
    def build(self, snap, sessions, snoozes=None, transcript=None, opened=None):
        return ar.build(snap, sessions, set(), snoozes or {}, NOW,
                        read=lambda s: transcript or t(last_ts=NOW - 60, last_ts_raw="r", last_text="Ship it?"),
                        subagents=lambda s: None, opened=opened)

    def test_opened_until_the_session_writes_again(self):
        sessions = {"t1": {"status": "idle", "sessionId": "s"}}
        self.assertTrue(self.build(snapshot(term()), sessions, opened={"t1": {"activity": "r"}}).rows[0].opened)
        self.assertFalse(self.build(snapshot(term()), sessions, opened={"t1": {"activity": "old"}}).rows[0].opened)

    def test_row_fields(self):
        row = self.build(snapshot(term()), {"t1": {"status": "idle", "sessionId": "s"}}).rows[0]
        self.assertEqual((row.label, row.project, row.excerpt), ("feature-1", "acme", "Ship it?"))

    def test_placeholder_workspace_name_uses_terminal_title(self):
        radar = self.build(snapshot(term(), name="local"), {"t1": {"status": "idle", "sessionId": "s"}})
        self.assertEqual(radar.rows[0].label, "Un titre")

    def test_open_dialog_shows_its_question_not_the_last_text(self):
        transcript = t(last_ts=NOW, last_text="old", dialog_text="red or blue?", open_dialog=True)
        radar = self.build(snapshot(term()), {"t1": {"status": "waiting", "sessionId": "s"}}, transcript=transcript)
        self.assertEqual(radar.rows[0].excerpt, "red or blue?")

    def test_html_entities_in_names_are_decoded(self):
        radar = self.build(snapshot(term(), name="Review &amp; plan"), {"t1": {"status": "idle", "sessionId": "s"}})
        self.assertEqual(radar.rows[0].label, "Review & plan")

    def test_two_sessions_in_one_workspace_are_named_by_terminal_and_keep_their_pane(self):
        snap = snapshot(term("t1", title="◑ Grill"), term("t2", title="◐ Build"))
        snap["workspaces"][0]["layout"] = {"tabs": [{"panes": [{"id": "p1", "terminalId": "t1"}]}],
                                           "sidebar": {"tabs": [{"panes": [{"id": "p2", "terminalId": "t2"}]}]}}
        sessions = {"t1": {"status": "idle", "sessionId": "a"}, "t2": {"status": "idle", "sessionId": "b"}}
        rows = sorted(self.build(snap, sessions).rows, key=lambda r: r.terminal_id)
        self.assertEqual([(r.label, r.pane_id) for r in rows], [("feature-1 › Grill", "p1"), ("feature-1 › Build", "p2")])

    def test_two_sessions_in_a_placeholder_workspace_show_the_workspace(self):
        sessions = {"t1": {"status": "idle", "sessionId": "a"}, "t2": {"status": "idle", "sessionId": "b"}}
        snap = snapshot(term("t1", title="◑ Grill"), term("t2", title="◐ Build"), name="local")
        labels = sorted(r.label for r in self.build(snap, sessions).rows)
        self.assertEqual(labels, ["local › Build", "local › Grill"])

    def test_terminal_without_agent_is_ignored(self):
        radar = self.build(snapshot({"id": "t1", "title": "zsh"}), {})
        self.assertEqual((radar.rows, radar.warning), ([], ""))

    def test_same_session_in_two_terminals_counts_once(self):
        sessions = {"t1": {"status": "idle", "sessionId": "s"}, "t2": {"status": "idle", "sessionId": "s"}}
        self.assertEqual(len(self.build(snapshot(term(), term("t2")), sessions).rows), 1)

    def test_snoozed_session_leaves_the_count(self):
        snoozes = {"t1": {"until": NOW + 100, "activity": "r"}}
        radar = self.build(snapshot(term()), {"t1": {"status": "idle", "sessionId": "s"}}, snoozes)
        self.assertEqual((len(radar.rows), len(radar.snoozed)), (0, 1))

    def test_no_session_matched_warns_instead_of_guessing(self):
        self.assertTrue(self.build(snapshot(term()), {}).warning)

    def test_dialogs_first_then_oldest(self):
        def fake(session):
            return session["t"]
        sessions = {
            "new": {"status": "idle", "sessionId": "1", "t": t(last_ts=NOW - 10)},
            "old": {"status": "idle", "sessionId": "2", "t": t(last_ts=NOW - 100)},
            "dlg": {"status": "idle", "sessionId": "3", "t": t(last_ts=NOW - 1, open_dialog=True)},
        }
        radar = ar.build(snapshot(term("new"), term("old"), term("dlg")), sessions, set(), {}, NOW,
                         read=fake, subagents=lambda s: None)
        self.assertEqual([r.terminal_id for r in radar.rows], ["dlg", "old", "new"])


def binding(terminal="dead", workspace="w1", session="s1", at=5, ended=None, reason=None):
    return {"terminal_id": terminal, "workspace_id": workspace, "agent_id": "claude", "agent_session_id": session,
            "started_at": at, "last_event_at": at, "ended_at": ended, "end_reason": reason}


class Interrupted(Base):
    """A shutdown kills the terminals but leaves their bindings open in host.db, until Paradev sweeps them."""

    def rows(self, snap, bindings, dismissed=()):
        return ar.interrupted_rows(snap, bindings, dismissed)

    def test_open_binding_without_terminal_is_interrupted(self):
        rows = self.rows(snapshot(), [binding()])
        self.assertEqual([(r.workspace_id, r.session_id, r.agent, r.label) for r in rows],
                         [("w1", "s1", "claude", "feature-1")])

    def test_swept_as_terminal_exited_is_still_interrupted(self):
        self.assertEqual(len(self.rows(snapshot(), [binding(ended=9, reason="terminal-exited")])), 1)

    def test_closed_pane_or_resumed_binding_is_not(self):
        for reason in ("disposed", "resumed", "detached"):
            self.assertEqual(self.rows(snapshot(), [binding(ended=9, reason=reason)]), [], reason)

    def test_only_the_latest_binding_of_a_session_counts(self):
        older_killed_then_closed = [binding("a", at=1, ended=2, reason="terminal-exited"),
                                    binding("b", at=3, ended=4, reason="disposed")]
        self.assertEqual(self.rows(snapshot(), older_killed_then_closed), [])

    def test_live_terminal_is_not_interrupted(self):
        self.assertEqual(self.rows(snapshot(term("dead")), [binding()]), [])

    def test_session_resumed_in_a_new_terminal_is_back(self):
        resumed = [binding(at=1, ended=9, reason="terminal-exited"), binding("new", at=2)]
        self.assertEqual(self.rows(snapshot(term("new")), resumed), [])
        cli_resume_leaves_old_open = [binding(at=2), binding("new", at=1)]
        self.assertEqual(self.rows(snapshot(term("new")), cli_resume_leaves_old_open), [])

    def test_archived_ignored_or_dismissed_is_skipped(self):
        self.assertEqual(self.rows(snapshot(), [binding(workspace="gone")]), [])
        self.assertEqual(self.rows(snapshot(), [binding()], {"s1": NOW}), [])
        ar.configure(ar.Config(ignore_projects=["acme"]))
        self.assertEqual(self.rows(snapshot(), [binding()]), [])

    def test_one_row_per_session_newest_first(self):
        rows = self.rows(snapshot(), [binding("a", at=1), binding("b", at=5), binding("c", session="s2", at=9)])
        self.assertEqual([(r.session_id, r.terminal_id) for r in rows], [("s2", "c"), ("s1", "b")])

    def test_dismissed_is_remembered_a_bit_longer_than_it_can_show(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "sub", "d.json")
            ar.save_dismissed("old", NOW - 9 * 86400, path)
            ar.save_dismissed("new", NOW, path)
            self.assertEqual(ar.load_dismissed(path), {"new": NOW})

    def test_counted_and_offered_a_resume(self):
        radar = ar.Radar(interrupted=self.rows(snapshot(), [binding()]))
        out = ar.render(radar, "/p", NOW)
        self.assertEqual(out.splitlines()[0], "1 | sfimage=tray.full")
        self.assertIn('param1="resume" param2="w1" param3="dead" param4=""', out)
        self.assertIn('param1="resume-all"', out)
        self.assertIn('param1="dismiss" param2="s1"', out)
        self.assertNotIn("Inbox zero", out)

    def test_focus_folds_other_projects_interrupted_rows(self):
        radar = ar.Radar(interrupted=self.rows(snapshot(project="side"), [binding()]))
        out = ar.render(radar, "/p", NOW, {"project": "acme", "until": NOW + 60})
        self.assertEqual(out.splitlines()[0], "0 | sfimage=checkmark.circle")
        self.assertIn("Other projects  ·  1", out)
        self.assertIn('param1="resume-all" param2="side"', out)

    def test_knows_the_pane_the_session_died_in(self):
        snap = snapshot()
        snap["workspaces"][0]["layout"] = {"tabs": [{"panes": [{"id": "p9", "terminalId": "dead"}]}]}
        row = self.rows(snap, [binding()])[0]
        self.assertEqual(row.pane_id, "p9")
        out = ar.render(ar.Radar(interrupted=[row]), "/p", NOW)
        self.assertIn('param1="resume" param2="w1" param3="dead" param4="p9"', out)
        self.assertIn('param1="open" param2="w1" param3="p9"', out)

    def test_build_passes_bindings_through(self):
        radar = ar.build(snapshot(), {}, set(), {}, NOW, read=lambda s: t(), subagents=lambda s: None,
                         bindings=[binding()])
        self.assertEqual(len(radar.interrupted), 1)


class Resume(Base):
    """Paradev resumes a dead pane in place; the radar asks it to, instead of opening a second terminal."""

    def host(self, folder, terminal="dead"):
        os.makedirs(os.path.join(folder, "h"))
        path = os.path.join(folder, "h", "host.db")
        import sqlite3
        connection = sqlite3.connect(path)
        connection.execute("create table terminal_agent_bindings (terminal_id text)")
        connection.execute("insert into terminal_agent_bindings values (?)", (terminal,))
        connection.commit()
        connection.close()
        with open(os.path.join(folder, "h", "manifest.json"), "w") as handle:
            json.dump({"endpoint": "http://127.0.0.1:1", "authToken": "tok"}, handle)
        ar.configure(ar.Config(host_dbs=[os.path.join(folder, "*", "host.db")]))

    def test_calls_paradev_resume_on_the_host_that_knows_the_terminal(self):
        calls = []
        with tempfile.TemporaryDirectory() as folder:
            self.host(folder)
            old = ar.host_call
            ar.host_call = lambda host, proc, payload, timeout=60: calls.append((host, proc, payload)) or {"resumed": True}
            try:
                self.assertTrue(ar.resume("w1", "dead"))
                self.assertFalse(ar.resume("w1", "unknown"))
            finally:
                ar.host_call = old
        self.assertEqual(calls, [(("http://127.0.0.1:1", "tok"), "terminalAgents.resume",
                                  {"workspaceId": "w1", "terminalId": "dead"})])

    def test_unreachable_host_is_not_a_crash(self):
        with tempfile.TemporaryDirectory() as folder:
            self.host(folder)
            self.assertFalse(ar.resume("w1", "dead"))


PS = """1 0 launchd
10 1 Superset
20 10 -zsh
30 20 claude
31 30 node
32 31 zsh
40 10 zsh
41 40 claude
"""


class Pause(Base):
    """Pausing ends the terminal the way a crash does, so Paradev keeps the session resumable."""

    def test_kills_claude_first_its_children_then_the_terminal_shell(self):
        self.assertEqual(ar.terminal_processes(30, PS), [30, 31, 32, 20])

    def test_only_idle_sessions_are_paused(self):
        killed = []
        sessions = {"a": {"pid": 30, "status": "idle"}, "b": {"pid": 41, "status": "busy"}}
        count = ar.pause(["a", "b", "missing"], sessions, PS, kill=lambda pid, sig: killed.append(pid))
        self.assertEqual((count, killed), (["a"], [30, 31, 32, 20]))

    def test_binding_is_held_before_the_kill(self):
        order = []
        ar.pause(["a"], {"a": {"pid": 30, "status": "idle"}}, PS, kill=lambda pid, sig: order.append(pid),
                 hold=lambda terminal, now: order.append(terminal), now=NOW)
        self.assertEqual(order[0], "a")

    def test_held_binding_outlives_the_exit_and_comes_back_on_resume(self):
        """Paradev resumes an on-screen pane whose binding ended "terminal-exited": a pause must not end so."""
        import sqlite3
        with tempfile.TemporaryDirectory() as folder:
            os.makedirs(os.path.join(folder, "h"))
            path = os.path.join(folder, "h", "host.db")
            connection = sqlite3.connect(path)
            connection.execute("create table terminal_agent_bindings (terminal_id text, ended_at integer, end_reason text)")
            connection.execute("insert into terminal_agent_bindings values ('dead', null, null)")
            connection.commit()
            ar.configure(ar.Config(host_dbs=[os.path.join(folder, "*", "host.db")]))
            self.assertTrue(ar.hold_binding("dead", NOW))
            self.assertFalse(ar.hold_binding("dead", NOW + 1))  # Paradev's own end, after the kill, changes nothing
            self.assertEqual(connection.execute("select ended_at, end_reason from terminal_agent_bindings").fetchone(),
                             (int(NOW * 1000), "paused"))
            self.assertTrue(ar.release_binding("dead"))
            self.assertEqual(connection.execute("select end_reason from terminal_agent_bindings").fetchone(),
                             ("terminal-exited",))
            connection.close()

    def test_held_binding_still_shows_as_interrupted(self):
        rows = ar.interrupted_rows(snapshot(), [binding(ended=9, reason="paused")])
        self.assertEqual([r.terminal_id for r in rows], ["dead"])

    def test_paused_sessions_wait_apart_and_do_not_count(self):
        radar = ar.build(snapshot(), {}, set(), {}, NOW, read=lambda s: t(), subagents=lambda s: None,
                         bindings=[binding(), binding("other", session="s2", ended=9, reason="terminal-exited")],
                         paused={"dead": NOW})
        self.assertEqual(([r.terminal_id for r in radar.paused], [r.terminal_id for r in radar.interrupted]),
                         (["dead"], ["other"]))
        out = ar.render(ar.Radar(paused=radar.paused), "/p", NOW)
        self.assertEqual(out.splitlines()[0], "0 | sfimage=checkmark.circle")
        self.assertIn("Paused (1) | sfimage=pause.circle refresh=true", out)
        self.assertIn('param1="resume-paused"', out)
        self.assertIn('--feature-1 | bash="/p" param1="resume" param2="w1" param3="dead"', out)

    def test_paused_grouped_by_project_each_resumable_at_once(self):
        def row(terminal, project):
            return ar.Row("w1", terminal, terminal, project, ar.Verdict(ar.INTERRUPTED), NOW, "", session_id=terminal)
        out = ar.render(ar.Radar(paused=[row("a", "acme"), row("b", "side"), row("c", "acme")]), "/p", NOW)
        lines = out.splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith("Paused (3)")) + 1
        end = next(i for i in range(start, len(lines)) if lines[i] == "---" or not lines[i].startswith("--"))
        labels = [line.split(" | ")[0] for line in lines[start:end]
                  if line == "-----" or not line.startswith("----")]
        self.assertEqual(labels, ["--Resume all 3", "-----", "--ACME  ·  2", "--a", "--c", "-----", "--SIDE  ·  1", "--b"])
        self.assertIn('param1="resume-paused" param2="acme"', out)

    def test_pause_is_remembered_by_terminal(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "p.json")
            ar.save_paused(["old"], NOW - 9 * 86400, path)
            ar.save_paused(["new"], NOW, path)
            self.assertEqual(ar.load_paused(path), {"new": NOW})

    def row(self, status, project="acme", terminal="t1"):
        row = ar.Row("w1", terminal, "feature", project, ar.Verdict(ar.NEEDS), NOW - 60, "", "r")
        row.status = status
        return row

    def test_offered_on_a_finished_turn_and_for_the_whole_project(self):
        rows = [self.row("idle"), self.row("waiting", terminal="t2"), self.row("", terminal="t3")]
        out = ar.render(ar.Radar(rows=rows), "/p", NOW)
        self.assertEqual(out.count('param1="pause" param2="t'), 3)
        self.assertIn('param1="pause" param2="t1" param3="t2" terminal', out)
        self.assertIn("Pause all 2", out)

    def test_project_header_without_pausable_rows_stays_plain(self):
        out = ar.render(ar.Radar(rows=[self.row("")]), "/p", NOW)
        self.assertNotIn('param1="pause"', out)


class Orchestrator(Base):
    """An orchestrator whose turn is over waits on you, even while its workers run."""

    def snap(self, worker_state="working"):
        return {"workspaces": [
            {"id": "wo", "name": "billing", "project": "acme", "terminals": [term("orch")]},
            {"id": "ww", "name": "ticket-42", "project": "acme", "terminals": [term("work", worker_state)]},
        ]}

    def build(self, worker_status, inbox=0, transcript=None, worker_state="working"):
        sessions = {"orch": {"status": "idle", "sessionId": "o"}, "work": {"status": worker_status, "sessionId": "w"}}
        orchestrators = {"orch": {"workers": {"ww"}, "inbox": inbox}}
        radar = ar.build(self.snap(worker_state), sessions, set(), {}, NOW,
                         read=lambda s: (s["sessionId"] == "o" and transcript)
                         or t(last_ts=NOW - 60, last_text="The prod apply is yours."),
                         subagents=lambda s: None, orchestrators=orchestrators)
        return [r.terminal_id for r in radar.rows]

    def test_counted_while_a_worker_runs(self):
        self.assertEqual(self.build("busy"), ["orch"])
        self.assertEqual(self.build("shell"), ["orch"])

    def test_counted_once_workers_are_idle(self):
        self.assertEqual(sorted(self.build("idle", worker_state="idle")), ["orch", "work"])

    def test_unread_report_shows_on_its_row(self):
        self.assertEqual(self.build("busy", inbox=1), ["orch"])

    def test_open_dialog_still_shows(self):
        self.assertEqual(self.build("busy", transcript=t(last_ts=NOW - 60, open_dialog=True)), ["orch"])

    def test_load_reads_topic_pointer_workers_and_inbox(self):
        with tempfile.TemporaryDirectory() as root:
            topic = os.path.join(root, "billing")
            for sub in ("workers", "inbox"):
                os.makedirs(os.path.join(topic, sub))
            with open(os.path.join(topic, "orchestrator.json"), "w") as handle:
                json.dump({"workspace": "wo", "terminal": "orch"}, handle)
            with open(os.path.join(topic, "workers", "ww.json"), "w") as handle:
                json.dump({"workspace": "ww", "terminal": "work"}, handle)
            with open(os.path.join(topic, "inbox", "1-done.json"), "w") as handle:
                handle.write("{}")
            self.assertEqual(ar.load_orchestrators(root),
                             {"orch": {"topic": "billing", "workers": {"ww"}, "inbox": 1, "workspace": "wo"}})

    def test_load_a_root_that_is_itself_the_topic_finds_workers_from_reports(self):
        # The LPC layout: no workers/ folder, one topic per root.
        with tempfile.TemporaryDirectory() as parent:
            root = os.path.join(parent, "lpc-orchestrator")
            for sub in ("inbox", "handled"):
                os.makedirs(os.path.join(root, sub))
            with open(os.path.join(root, "orchestrator.json"), "w") as handle:
                json.dump({"workspace": "wo", "terminal": "orch"}, handle)
            for folder, workspace in (("handled", "w-old"), ("handled", "wo"), ("inbox", "w-new")):
                with open(os.path.join(root, folder, f"{workspace}.json"), "w") as handle:
                    json.dump({"kind": "done", "workspace": workspace}, handle)
            self.assertEqual(ar.load_orchestrators([root]),
                             {"orch": {"topic": "lpc-orchestrator", "workers": {"w-old", "w-new"}, "inbox": 1, "workspace": "wo"}})


def report_call(kind="done"):
    command = f"python3 /x/topic-orchestrator/scripts/orch.py report --topic billing --ticket VIA-1 --kind {kind} --text hi"
    return line("assistant", [{"type": "tool_use", "id": "r", "name": "Bash", "input": {"command": command}}])


def tool_result(id_="r"):
    return line("user", [{"type": "tool_result", "tool_use_id": id_, "content": "report saved"}])


class HandOff(Base):
    """A worker whose turn ended on a report waits on its orchestrator; anything after the report is for you."""

    def parse(self, *lines):
        return ar.parse_transcript(list(lines))

    def test_report_then_closing_text_is_handed_off(self):
        info = self.parse(report_call(), tool_result(), line("assistant", [{"type": "text", "text": "Reported."}]))
        self.assertTrue(info.handed_off)

    def test_work_after_the_report_is_not(self):
        # VIA-1552, 2026-11-10: reported done, then ran pulumi-deploy and stopped on "reply with one" for Jimmy.
        info = self.parse(
            report_call(), tool_result(),
            line("assistant", [{"type": "tool_use", "id": "s", "name": "Skill", "input": {"skill": "pulumi-deploy"}}]),
            tool_result("s"),
            line("assistant", [{"type": "text", "text": "Next steps (reply with one): 1. apply"}]),
        )
        self.assertFalse(info.handed_off)

    def test_a_waiting_report_points_back_at_you(self):
        self.assertFalse(self.parse(report_call("waiting"), tool_result()).handed_off)

    def test_you_typing_after_the_report_ends_it(self):
        self.assertFalse(self.parse(report_call(), tool_result(), line("user", "and the staging grants?")).handed_off)

    def test_lpc_report_command_counts_too(self):
        command = "python3 /r/.claude/skills/lpc-orchestrator/scripts/orch.py report --story LPC-15 --kind done --text x"
        call = line("assistant", [{"type": "tool_use", "id": "r", "name": "Bash", "input": {"command": command}}])
        self.assertTrue(self.parse(call, tool_result()).handed_off)

    def snap(self):
        return {"workspaces": [
            {"id": "wo", "name": "orchestrator-billing", "project": "acme", "terminals": [term("orch")]},
            {"id": "ww", "name": "ticket-42", "project": "acme", "terminals": [term("work")]},
        ]}

    def build(self, worker, orch_live=True, inbox=1):
        snap = self.snap()
        if not orch_live:
            snap["workspaces"][0]["terminals"] = []
        sessions = {"orch": {"status": "idle", "sessionId": "o"}, "work": {"status": "idle", "sessionId": "w"}}
        orchestrators = {"orch": {"topic": "billing", "workers": {"ww"}, "inbox": inbox}}
        return ar.build(snap, sessions, set(), {}, NOW, subagents=lambda s: None, orchestrators=orchestrators,
                        read=lambda s: worker if s["sessionId"] == "w" else t(last_ts=NOW - 60))

    def test_handed_off_worker_waits_on_the_orchestrator(self):
        radar = self.build(t(last_ts=NOW - 60, handed_off=True))
        verdicts = {r.terminal_id: r.verdict.bucket for r in radar.rows}
        self.assertEqual(verdicts, {"orch": ar.NEEDS, "work": ar.HANDED})
        self.assertEqual({r.terminal_id: (r.topic, r.role) for r in radar.rows},
                         {"orch": ("billing", "orchestrator"), "work": ("billing", "worker")})

    def test_worker_that_did_not_report_talks_to_you(self):
        radar = self.build(t(last_ts=NOW - 60))
        self.assertEqual({r.verdict.bucket for r in radar.rows}, {ar.NEEDS})

    def test_a_dialog_beats_the_report(self):
        radar = self.build(t(last_ts=NOW - 60, handed_off=True, open_dialog=True))
        self.assertEqual(next(r for r in radar.rows if r.terminal_id == "work").verdict.bucket, ar.NEEDS)

    def test_no_live_orchestrator_to_read_the_report_means_you(self):
        radar = self.build(t(last_ts=NOW - 60, handed_off=True), orch_live=False)
        self.assertEqual([r.verdict.bucket for r in radar.rows], [ar.NEEDS])

    def test_orchestrator_label_shows_unread_reports(self):
        radar = self.build(t(last_ts=NOW - 60), inbox=3)
        self.assertEqual(next(r for r in radar.rows if r.role == "orchestrator").label,
                         "orchestrator-billing  ·  3 report(s)")

    def test_worker_talking_to_you_names_its_topic(self):
        radar = self.build(t(last_ts=NOW - 60))
        self.assertEqual(next(r for r in radar.rows if r.role == "worker").label, "ticket-42  ·  ↳ billing")

    def test_menu_is_flat_oldest_first_and_handed_workers_stay_out(self):
        def row(label, terminal, since, topic="", role="", bucket=ar.NEEDS):
            return ar.Row("w", terminal, label, "acme", ar.Verdict(bucket), since, "", "a", topic=topic, role=role)
        rows = [row("orch", "o", NOW - 9000, "billing", "orchestrator"),
                row("solo", "s", NOW - 60),
                row("grill", "g", NOW - 600, "billing", "worker"),
                row("done", "d", NOW - 60, "billing", "worker", ar.HANDED)]
        out = ar.render(ar.Radar(rows=rows), "/p", NOW)
        top = [l.split(" | ")[0] for l in out.splitlines() if not l.startswith("--")]
        self.assertEqual(top[0], "3")
        self.assertEqual(top[2:6], ["ACME  ·  3", "🟡  orch", "🟠  grill", "🟠  solo"])
        self.assertNotIn("done", out)

    def test_handed_workers_of_a_topic_with_nothing_for_you_stay_out(self):
        rows = [ar.Row("w", "d", "done", "acme", ar.Verdict(ar.HANDED), NOW, "", "a", topic="billing", role="worker")]
        self.assertIn("Inbox zero", ar.render(ar.Radar(rows=rows), "/p", NOW))


class Reason(Base):
    def snap(self):
        return {"workspaces": [{"id": "w1", "name": "ticket", "project": "acme", "terminals": [term("t1")]}]}

    def build(self, transcript, status=None, session_status="idle"):
        statuses = {"t1": status} if status else {}
        return ar.build(self.snap(), {"t1": {"status": session_status, "sessionId": "s"}}, set(), {}, NOW,
                        read=lambda s: transcript, subagents=lambda s: None, statuses=statuses)

    def test_infer_from_the_last_message(self):
        self.assertEqual(ar.infer_reason("❓ **Q1** - **A**: x?\n\n❓ **Q2** - **B**: y?"), "decisions 2")
        self.assertEqual(ar.infer_reason("Questions :\n1. Lequel ?\n2. Quand ?\n3. Où ?"), "decisions 3")
        self.assertEqual(ar.infer_reason("Done.\n\n1. Merged.\n2. Deployed."), "question")
        self.assertEqual(ar.infer_reason("The migration is ready.\n\n**Go to run it on staging?**"), "approve")
        self.assertEqual(ar.infer_reason("Page: https://claude.ai/code/artifact/abc"), "review")
        self.assertEqual(ar.infer_reason("Which one do you prefer?"), "question")

    def test_status_written_this_turn_wins(self):
        status = {"label": "decisions", "count": 5, "next": "grill round 1", "at": NOW - 30}
        row = self.build(t(last_ts=NOW - 20, turn_start=NOW - 100, last_text="ok?"), status).rows[0]
        self.assertEqual((row.reason, row.hint, row.title), ("decisions 5", "grill round 1", "decisions 5  ·  ticket"))

    def test_status_from_an_earlier_turn_is_ignored(self):
        status = {"label": "quiet", "at": NOW - 500}
        row = self.build(t(last_ts=NOW - 20, turn_start=NOW - 100, last_text="Go to merge?"), status).rows[0]
        self.assertEqual(row.reason, "approve")

    def test_quiet_or_working_this_turn_is_not_waiting(self):
        for label in ("quiet", "working"):
            radar = self.build(t(last_ts=NOW - 20, turn_start=NOW - 100), {"label": label, "at": NOW - 30})
            self.assertEqual(radar.rows, [])

    def test_a_dialog_still_shows_despite_quiet(self):
        radar = self.build(t(last_ts=NOW - 20, turn_start=NOW - 100, open_dialog=True, dialog_count=3,
                             dialog_text="Which?"), {"label": "quiet", "at": NOW - 30})
        self.assertEqual((radar.rows[0].reason, radar.rows[0].hint), ("decisions 3", "Which?"))

    def test_turn_start_is_the_last_prompt_not_a_tool_result(self):
        info = ar.parse_transcript([
            line("user", "/topic-orchestrator tick", ts="2026-11-05T10:00:00.000Z"),
            line("assistant", [{"type": "tool_use", "id": "x", "name": "Bash", "input": {}}], ts="2026-11-05T10:00:05.000Z"),
            line("user", [{"type": "tool_result", "tool_use_id": "x"}], ts="2026-11-05T10:00:09.000Z"),
            line("assistant", [{"type": "text", "text": "tick: nothing for you"}], ts="2026-11-05T10:00:10.000Z"),
        ])
        self.assertEqual(info.turn_start, ar._parse_iso("2026-11-05T10:00:00.000Z"))

    def test_load_statuses_by_terminal_and_workspace(self):
        with tempfile.TemporaryDirectory() as root:
            folder = os.path.join(root, "billing", "status")
            os.makedirs(folder)
            with open(os.path.join(folder, "ws-1.json"), "w") as handle:
                json.dump({"label": "approve", "at": 5, "terminal": "term-1"}, handle)
            statuses = ar.load_statuses([os.path.join(root, "*", "status")])
            self.assertEqual(set(statuses), {"ws-1", "term-1"})
            self.assertEqual(ar.status_reason({"label": "decisions", "count": 0}), "decisions")


def iso(ts):
    return ar.dt.datetime.fromtimestamp(ts, ar.dt.timezone.utc).isoformat().replace("+00:00", "Z")


def loop_transcript(*turns, cron="13 */2 * * *", you_at=None):
    """A session that made a recurring cron, then got ticks. Each turn is (when the tick fired, closing text)."""
    lines = []
    if you_at:
        lines.append(line("user", "start the loop", ts=iso(you_at), origin={"kind": "human"}))
    lines += [
        line("assistant", [{"type": "tool_use", "id": "u1", "name": "CronCreate",
                            "input": {"cron": cron, "prompt": "/orch tick", "recurring": True}}], ts=iso(turns[0][0] - 600)),
        line("user", [{"type": "tool_result", "tool_use_id": "u1"}], ts=iso(turns[0][0] - 599),
             toolUseResult={"id": "c1", "recurring": True, "nextFireAt": 1}),
    ]
    for at, said in turns:
        lines.append(line("user", "/orch tick", ts=iso(at), isMeta=True))
        lines.append(line("assistant", [{"type": "text", "text": said}], ts=iso(at + 60)))
    return ar.parse_transcript(lines)


def local(hour, minute=0, days=0):
    day = ar.dt.datetime.fromtimestamp(NOW).date() + ar.dt.timedelta(days=days)
    return ar.dt.datetime.combine(day, ar.dt.time(hour, minute)).timestamp()


class Loop(Base):
    """A session on a recurring cron: hidden between ticks, queued when a tick asks, checked when a tick is late."""

    def verdict(self, transcript, now):
        return ar.classify("idle", "idle", transcript, False, now)

    def test_cron_next_reads_the_common_forms(self):
        self.assertEqual(ar.cron_next("13 */2 * * *", local(6, 13)), local(8, 13))
        self.assertEqual(ar.cron_next("13 */2 * * *", local(8, 20)), local(10, 13))
        self.assertEqual(ar.cron_next("*/15 * * * *", local(8, 1)), local(8, 15))
        self.assertEqual(ar.cron_next("0 9 * * *", local(9, 0)), local(9, days=1))
        self.assertIsNone(ar.cron_next("not a cron", local(9)))

    def test_cron_prev_includes_the_minute_itself(self):
        self.assertEqual(ar.cron_prev("13 */2 * * *", local(8, 13) + 30), local(8, 13))
        self.assertEqual(ar.cron_prev("13 */2 * * *", local(8, 12)), local(6, 13))

    def test_quiet_between_ticks_is_hidden_even_past_60_minutes(self):
        info = loop_transcript((local(8, 13), "Tick: no change."))
        self.assertEqual(info.cron_due, local(10, 13))
        self.assertEqual(self.verdict(info, local(9, 50)).bucket, ar.HIDDEN)

    def test_a_missed_tick_goes_to_check(self):
        info = loop_transcript((local(8, 13), "Tick: no change."))
        self.assertEqual(self.verdict(info, local(10, 25)).bucket, ar.HIDDEN)
        self.assertEqual(self.verdict(info, local(10, 30)).bucket, ar.CHECK)

    def test_a_cron_that_always_fires_late_is_expected_late(self):
        # Claude Code fired "17 */2" at :47: the next tick is expected at :47 too.
        info = loop_transcript((local(8, 47), "Tick: no change."), cron="17 */2 * * *")
        self.assertEqual(info.cron_due, local(10, 47))
        self.assertEqual(self.verdict(info, local(10, 55)).bucket, ar.HIDDEN)

    def test_a_question_from_a_tick_queues_it(self):
        info = loop_transcript((local(8, 13), "VIA-12 finished. Do I start VIA-13 now?"))
        self.assertEqual(self.verdict(info, local(8, 30)).bucket, ar.NEEDS)

    def test_the_question_outlives_later_quiet_ticks(self):
        info = loop_transcript((local(6, 13), "Do I start VIA-13 now?"), (local(8, 13), "Tick: no change."))
        self.assertEqual(self.verdict(info, local(8, 30)).bucket, ar.NEEDS)
        self.assertEqual((info.held_text, info.held_at), ("Do I start VIA-13 now?", local(6, 14)))

    def test_your_answer_clears_the_question(self):
        asked = [
            line("assistant", [{"type": "tool_use", "id": "u1", "name": "CronCreate",
                                "input": {"cron": "13 */2 * * *", "recurring": True}}], ts=iso(local(6))),
            line("user", [{"type": "tool_result", "tool_use_id": "u1"}], ts=iso(local(6)),
                 toolUseResult={"id": "c1", "recurring": True, "nextFireAt": 1}),
            line("user", "/orch tick", ts=iso(local(6, 13)), isMeta=True),
            line("assistant", [{"type": "text", "text": "Do I start VIA-13 now?"}], ts=iso(local(6, 14))),
        ]
        answer = [line("user", "yes", ts=iso(local(7)), origin={"kind": "human"}),
                  line("assistant", [{"type": "text", "text": "Started."}], ts=iso(local(7, 1)))]
        self.assertTrue(ar.parse_transcript(asked).held_ask)
        answered = ar.parse_transcript(asked + answer)
        self.assertFalse(answered.held_ask)
        self.assertEqual(answered.you_at, local(7))
        self.assertEqual(self.verdict(answered, local(7, 30)).bucket, ar.HIDDEN)

    def test_a_status_from_an_earlier_tick_still_wants_you(self):
        info = loop_transcript((local(6, 13), "Tick done."), (local(8, 13), "Tick: no change."), you_at=local(5))
        status = {"label": "question", "next": "VIA-13 or VIA-14 first", "at": local(6, 14)}
        radar = ar.build(Reason().snap(), {"t1": {"status": "idle", "sessionId": "s"}}, set(), {}, local(8, 30),
                         read=lambda s: info, subagents=lambda s: None, statuses={"t1": status})
        self.assertEqual((radar.rows[0].reason, radar.rows[0].hint), ("question", "VIA-13 or VIA-14 first"))

    def loop_build(self, info, status_at):
        return ar.build(Reason().snap(), {"t1": {"status": "idle", "sessionId": "s"}}, set(), {}, local(8, 30),
                        read=lambda s: info, subagents=lambda s: None,
                        statuses={"t1": {"label": "quiet", "at": status_at}})

    def test_a_held_question_keeps_its_date_over_an_older_quiet_status(self):
        info = loop_transcript((local(6, 13), "Do I start VIA-13 now?"), (local(8, 13), "Tick: no change."),
                               you_at=local(5))
        row = self.loop_build(info, local(5, 30)).rows[0]
        asked = local(6, 14)
        self.assertEqual((row.since, row.activity, row.reason), (asked, iso(asked), "question"))
        self.assertIn("VIA-13", row.excerpt)

    def test_a_quiet_status_after_the_question_drops_it(self):
        # The session withdrew it on its own, e.g. the workspace it asked about is gone.
        info = loop_transcript((local(6, 13), "Do I start VIA-13 now?"), (local(8, 13), "Dropped that question."))
        self.assertEqual(self.loop_build(info, local(8, 14)).rows, [])


class Render(Base):
    def row(self, bucket=ar.NEEDS, project="acme", since=NOW - 7200, kind="", rank=1, label="feature-1"):
        return ar.Row("w1", "t1", label, project, ar.Verdict(bucket, rank, kind), since, "end", "r", pane_id="p1")

    def top_level(self, out):
        return [line.split(" | ")[0] for line in out.splitlines() if not line.startswith("--")]

    def test_count_excludes_check_rows(self):
        out = ar.render(ar.Radar(rows=[self.row(), self.row(ar.CHECK)]), "/p", NOW)
        self.assertEqual(out.splitlines()[0], "1 | sfimage=tray.full")
        self.assertIn("TO CHECK", out)

    def test_zero_says_inbox_zero(self):
        self.assertIn("Inbox zero", ar.render(ar.Radar(), "/p", NOW))

    def test_warning_never_reads_as_zero(self):
        out = ar.render(ar.Radar(warning="broken"), "/p", NOW)
        self.assertTrue(out.startswith("!"))
        self.assertNotIn("Inbox zero", out)

    def test_grouped_by_project_most_urgent_project_first(self):
        rows = [self.row(project="side", since=NOW - 600, label="a"),
                self.row(project="acme", since=NOW - 7200, label="b"),
                self.row(project="acme", kind="dialog", rank=0, label="c")]
        top = self.top_level(ar.render(ar.Radar(rows=rows), "/p", NOW))
        self.assertEqual(top[1:7], ["→  Next: c  ·  acme", "ACME  ·  2", "🔴  c", "🟡  b", "SIDE  ·  1", "🟠  a"])

    def test_row_title_does_not_tick_with_time(self):
        first = ar.render(ar.Radar(rows=[self.row()]), "/p", NOW)
        later = ar.render(ar.Radar(rows=[self.row()]), "/p", NOW + 600)
        self.assertEqual(self.top_level(first), self.top_level(later))

    def test_row_with_submenu_carries_its_own_action(self):
        first = ar.render(ar.Radar(rows=[self.row()]), "/p", NOW).splitlines()
        row_line = next(line for line in first if line.startswith("🟡"))
        self.assertIn('param1="open"', row_line)

    def test_every_top_level_submenu_header_stays_enabled(self):
        # SwiftBar greys out a submenu parent that has no action of its own.
        rows = [self.row(project="acme", label="a"), self.row(project="side", label="b")]
        radar = ar.Radar(rows=rows, snoozed=[self.row(label="z")])
        for focus in (None, {"project": "side", "until": NOW + 3600}):
            out = ar.render(radar, "/p", NOW, focus, {"acme": NOW + 3600}).splitlines()
            for line, after in zip(out, out[1:]):
                if not line.startswith("-") and after.startswith("--") and not after.startswith("---"):
                    self.assertTrue("refresh=true" in line or "param1=" in line, line)

    def test_opened_row_goes_last_in_its_group(self):
        rows = [self.row(since=NOW - 600, label="a"), self.row(since=NOW - 7200, label="b")]
        rows[0].opened = True
        top = self.top_level(ar.render(ar.Radar(rows=rows), "/p", NOW))
        self.assertEqual(top[3:5], ["🟡  b", "🟠  a"])

    def test_next_is_the_oldest_with_dialogs_first(self):
        rows = [self.row(since=NOW - 600, label="a"), self.row(since=NOW - 7200, label="b"),
                self.row(since=NOW - 60, label="c", kind="dialog", rank=0)]
        self.assertEqual(ar.next_row(rows).label, "c")
        self.assertEqual(ar.next_row(rows[:2]).label, "b")

    def test_next_stays_in_the_last_project_while_it_has_something(self):
        rows = [self.row(project="acme", since=NOW - 7200, label="a"),
                self.row(project="side", since=NOW - 600, label="b")]
        self.assertEqual(ar.next_row(rows, "side").label, "b")
        rows[1].opened = True
        self.assertEqual(ar.next_row(rows, "side").label, "a")

    def test_next_skips_opened_check_and_handed_rows(self):
        rows = [self.row(ar.CHECK, label="a"), self.row(ar.HANDED, label="b"), self.row(label="c")]
        rows[2].opened = True
        self.assertIsNone(ar.next_row(rows))
        self.assertNotIn("Next:", ar.render(ar.Radar(rows=rows), "/p", NOW))

    def test_next_follows_focus(self):
        rows = [self.row(project="acme", since=NOW - 7200, label="a"), self.row(project="side", label="b")]
        out = ar.render(ar.Radar(rows=rows), "/p", NOW, {"project": "side", "until": NOW + 3600}, last_project="acme")
        self.assertIn("→  Next: b  ·  side", out)

    def test_next_opens_skips_and_remembers_the_project(self):
        out = ar.render(ar.Radar(rows=[self.row()]), "/p", NOW).splitlines()
        self.assertIn('param1="open" param2="w1" param3="p1" param4="t1" param5="r" param6="acme"', out[2])
        self.assertTrue(any('param1="skip" param2="t1" param3="r"' in l for l in out if l.startswith("--")))

    def test_urgency_by_age(self):
        marks = [ar.MARKS[ar.urgency(self.row(since=NOW - age), NOW)] for age in (60, 7200, 90000)]
        self.assertEqual(marks, ["🟠", "🟡", "⚪"])

    def test_hover_holds_open_first_and_every_snooze_choice(self):
        out = ar.render(ar.Radar(rows=[self.row()]), "/p", NOW).splitlines()
        submenu = [line for line in out if line.startswith("--") and not line.startswith("---")]
        self.assertIn('param1="open" param2="w1" param3="p1" param4="t1" param5="r"', submenu[0])
        snoozes = [line for line in out if 'param1="snooze"' in line]
        self.assertEqual(len(snoozes), len(ar.snooze_choices(NOW)))
        self.assertIn('param1="snooze" param2="t1" param3="r"', snoozes[0])

    def test_focus_keeps_one_project_and_folds_the_others(self):
        rows = [self.row(project="acme", label="a"), self.row(project="side", label="b"),
                self.row(project="side", label="c")]
        out = ar.render(ar.Radar(rows=rows), "/p", NOW, {"project": "side", "until": NOW + 3600})
        top = self.top_level(out)
        self.assertEqual(top[0], "2")
        self.assertIn("SIDE  ·  2", top)
        self.assertNotIn("🟡  a", top)
        self.assertIn("Other projects  ·  1", top)
        self.assertIn("--🟡  a", [line.split(" | ")[0] for line in out.splitlines()])
        self.assertIn('param1="unfocus"', out)
        self.assertNotIn('param1="focus"', out)

    def test_focus_menu_offers_each_project_and_duration(self):
        out = ar.render(ar.Radar(rows=[self.row(project="acme"), self.row(project="side")]), "/p", NOW)
        focus = [line for line in out.splitlines() if 'param1="focus"' in line]
        self.assertEqual(len(focus), 2 * len(ar.snooze_choices(NOW)))
        self.assertIn('param2="side"', focus[-1])

    def test_ignored_project_leaves_the_count_and_folds_away(self):
        rows = [self.row(project="acme", label="a"), self.row(project="side", label="b"),
                self.row(project="side", label="c")]
        out = ar.render(ar.Radar(rows=rows), "/p", NOW, ignored={"side": NOW + 3600})
        top = self.top_level(out)
        self.assertEqual(top[0], "1")
        self.assertNotIn("SIDE  ·  2", top)
        self.assertIn("Ignored projects  ·  2", top)
        self.assertIn("--SIDE  ·  2", [line.split(" | ")[0] for line in out.splitlines()])
        self.assertIn('param1="unignore" param2="side"', out)
        ignore = [line for line in out.splitlines() if 'param1="ignore"' in line]
        self.assertEqual(len(ignore), len(ar.snooze_choices(NOW)))
        self.assertIn('param2="acme"', ignore[0])

    def test_ignoring_everything_reads_as_inbox_zero(self):
        out = ar.render(ar.Radar(rows=[self.row()]), "/p", NOW, ignored={"acme": NOW + 3600})
        self.assertEqual(out.splitlines()[0], "0 | sfimage=checkmark.circle")
        self.assertIn("Inbox zero", out)

    def test_ignore_wins_over_focus(self):
        rows = [self.row(project="acme", label="a"), self.row(project="side", label="b")]
        out = ar.render(ar.Radar(rows=rows), "/p", NOW, {"project": "side", "until": NOW + 3600},
                        {"side": NOW + 3600})
        self.assertEqual(out.splitlines()[0], "0 | sfimage=checkmark.circle")
        self.assertIn("Other projects  ·  1", out)

    def test_text_cannot_break_the_swiftbar_line(self):
        for current in ar.render(ar.Radar(rows=[self.row()]), "/p", NOW).splitlines():
            self.assertLessEqual(current.count("|"), 1)


class Text(Base):
    def test_excerpt_keeps_end_of_last_paragraph(self):
        self.assertEqual(ar.excerpt("start\n\n**Ship it?**"), "Ship it?")
        self.assertTrue(ar.excerpt("x" * 200).startswith("…"))


class Settings(Base):
    def write(self, folder, data):
        path = os.path.join(folder, "config.json")
        with open(path, "w") as handle:
            json.dump(data, handle)
        return path

    def test_missing_file_gives_defaults(self):
        self.assertEqual(ar.load_config("/nonexistent/config.json"), ar.Config())

    def test_file_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            config = ar.load_config(self.write(folder, {"language": "fr", "stale_after_minutes": 30}))
        self.assertEqual((config.language, config.stale_after_minutes, config.morning_hour), ("fr", 30, 9))

    def test_old_superset_cli_key_still_works(self):
        with tempfile.TemporaryDirectory() as folder:
            config = ar.load_config(self.write(folder, {"superset_cli": "/bin/superset"}))
        self.assertEqual(config.cli, "/bin/superset")

    def test_unknown_key_is_an_error_not_silence(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                ar.load_config(self.write(folder, {"langauge": "fr"}))

    def test_bad_config_shows_in_the_menu(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as folder:
            path = self.write(folder, {"nope": 1})
            old, ar.CONFIG_FILE = ar.CONFIG_FILE, path
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out):
                    ar.main([], "/p")
            finally:
                ar.CONFIG_FILE = old
        self.assertTrue(out.getvalue().startswith("!"))

    def test_french(self):
        ar.configure(ar.Config(language="fr"))
        self.assertIn("Inbox zéro", ar.render(ar.Radar(), "/p", NOW))

    def test_stale_threshold_and_snooze_hours_are_configurable(self):
        import datetime as dt
        ar.configure(ar.Config(stale_after_minutes=10, snooze_hours=[14], morning_hour=8))
        old = t(last_ts=NOW - 601)
        self.assertEqual(ar.classify("idle", "busy", old, False, NOW).bucket, ar.CHECK)
        morning = dt.datetime(2026, 11, 5, 8, 0).timestamp()
        self.assertEqual([c[0] for c in ar.snooze_choices(morning)], ["1 h", "2 h", "Until 14:00", "Tomorrow 8:00"])

    def test_ignored_project_is_skipped(self):
        ar.configure(ar.Config(ignore_projects=["acme"]))
        radar = ar.build(snapshot(term()), {"t1": {"status": "idle", "sessionId": "s"}}, set(), {}, NOW,
                         read=lambda s: t(last_ts=NOW - 60), subagents=lambda s: None)
        self.assertEqual(radar.rows, [])

    def test_demo_menu_needs_no_paradev(self):
        out = ar.render(ar.demo_radar(NOW), "/p", NOW)
        self.assertTrue(out.splitlines()[0].endswith("sfimage=tray.full"))


if __name__ == "__main__":
    unittest.main()
