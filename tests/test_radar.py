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
        self.assertIn('--feature-1  ·  acme | bash="/p" param1="resume" param2="w1" param3="dead"', out)

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
    """An orchestrator idle while its worker runs is not waiting on you."""

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
                         or t(last_ts=NOW - 60, last_text="Nothing is needed until then."),
                         subagents=lambda s: None, orchestrators=orchestrators)
        return [r.terminal_id for r in radar.rows]

    def test_hidden_while_a_worker_runs(self):
        self.assertEqual(self.build("busy"), [])
        self.assertEqual(self.build("shell"), [])

    def test_counted_once_workers_are_idle(self):
        self.assertEqual(sorted(self.build("idle", worker_state="idle")), ["orch", "work"])

    def test_unread_report_brings_it_back(self):
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
            self.assertEqual(ar.load_orchestrators(root), {"orch": {"workers": {"ww"}, "inbox": 1}})


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
        self.assertEqual(top[1:6], ["ACME  ·  2", "🔴  c", "🟡  b", "SIDE  ·  1", "🟠  a"])

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
        self.assertEqual(top[2:4], ["🟡  b", "🟠  a"])

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
