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
    def verdict(self, state="idle", status="idle", transcript=None, failed=False, activity=None):
        return ar.classify(state, status, transcript or t(last_ts=NOW - 10), failed, NOW, activity)

    def test_idle_with_nothing_running_needs_you(self):
        self.assertEqual(self.verdict().bucket, ar.NEEDS)

    def test_background_shell_or_subagent_is_hidden_even_if_paradev_says_idle(self):
        self.assertEqual(self.verdict(status="shell").bucket, ar.HIDDEN)
        self.assertEqual(self.verdict(status="busy").bucket, ar.HIDDEN)

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
    def build(self, snap, sessions, snoozes=None, transcript=None):
        return ar.build(snap, sessions, set(), snoozes or {}, NOW,
                        read=lambda s: transcript or t(last_ts=NOW - 60, last_ts_raw="r", last_text="Ship it?"),
                        subagents=lambda s: None)

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
        return ar.Row("w1", "t1", label, project, ar.Verdict(bucket, rank, kind), since, "end", "r",
                      detail="end | of the text", pane_id="p1")

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

    def test_urgency_by_age(self):
        marks = [ar.MARKS[ar.urgency(self.row(since=NOW - age), NOW)] for age in (60, 7200, 90000)]
        self.assertEqual(marks, ["🟠", "🟡", "⚪"])

    def test_hover_holds_text_open_and_every_snooze_choice(self):
        out = ar.render(ar.Radar(rows=[self.row()]), "/p", NOW).splitlines()
        self.assertIn("--end   of the text | size=13 emojize=false symbolize=false trim=true", out)
        self.assertTrue(any('param1="open" param2="w1" param3="p1"' in line for line in out))
        snoozes = [line for line in out if line.startswith("----") and not line.startswith("-----")]
        self.assertEqual(len(snoozes), len(ar.snooze_choices(NOW)))
        self.assertIn('param1="snooze" param2="t1" param3="r"', snoozes[0])

    def test_text_cannot_break_the_swiftbar_line(self):
        for current in ar.render(ar.Radar(rows=[self.row()]), "/p", NOW).splitlines():
            self.assertLessEqual(current.count("|"), 1)


class Text(Base):
    def test_excerpt_keeps_end_of_last_paragraph(self):
        self.assertEqual(ar.excerpt("start\n\n**Ship it?**"), "Ship it?")
        self.assertTrue(ar.excerpt("x" * 200).startswith("…"))

    def test_duration(self):
        self.assertEqual([ar.duration(s) for s in (59, 3600, 7200, 200000)], ["0 min", "1 h", "2 h", "2 d"])


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
        self.assertEqual(ar.duration(200000), "2 j")

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
