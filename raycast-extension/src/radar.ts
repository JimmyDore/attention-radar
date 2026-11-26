import { getPreferenceValues, showHUD } from "@raycast/api";
import { execFile } from "node:child_process";
import { homedir } from "node:os";

// Everything here goes through attention_radar.py: the radar stays the one source of truth,
// and the SwiftBar menu and this extension agree on what waits for you.

export type Row = {
  workspaceId: string;
  terminalId: string;
  paneId: string;
  activity: string;
  sessionId: string;
  project: string;
  label: string;
  reason: string;
  hint: string;
  message: string;
  since: number | null;
  waiting: boolean;
  urgency: number;
  opened: boolean;
  interrupted: boolean;
  pausable: boolean;
};

export type SnoozeChoice = { label: string; until: number };

export type Radar = {
  count: number;
  warning: string;
  focus: { project: string; until: number } | null;
  next: string | null;
  current: (Row & { snoozed: boolean }) | null;
  rows: Row[];
  snoozed: Row[];
  snoozeChoices: SnoozeChoice[];
};

function script(): string {
  const { radarScript } = getPreferenceValues<{ radarScript: string }>();
  return radarScript.replace(/^~(?=\/)/, homedir());
}

function run(args: string[], timeout = 30_000): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile("/usr/bin/python3", ["-B", script(), ...args], { timeout }, (error, stdout, stderr) => {
      // `json` exits 1 with {"error": …} on stdout: let the caller read it.
      if (error && !stdout.trim()) reject(new Error(stderr.trim() || error.message));
      else resolve(stdout);
    });
  });
}

/** The menu bar plugin redraws on its own every 10 s; this makes it follow an action right away. */
function refreshMenuBar() {
  execFile("open", ["-g", "swiftbar://refreshallplugins"], () => undefined);
}

export async function loadRadar(): Promise<Radar> {
  const data = JSON.parse(await run(["json"]));
  if (data.error) throw new Error(data.error);
  return data as Radar;
}

async function act(args: string[]) {
  await run(args, 60_000);
  refreshMenuBar();
}

/** Open the row's pane, mark its message opened, and remember its project for Next, like a click in the menu. */
export function open(row: Row) {
  if (row.interrupted) return act(["open", row.workspaceId, row.paneId]); // showing the dead pane resumes it
  return act(["open", row.workspaceId, row.paneId, row.terminalId, row.activity, row.project]);
}

export function skip(row: Row) {
  return act(["skip", row.terminalId, row.activity]);
}

export function snooze(row: Row, until: number) {
  return act(["snooze", row.terminalId, row.activity, String(until)]);
}

export function pause(row: Row) {
  return act(["pause", row.terminalId]);
}

/** Open what Next points at; prints where it went. */
export async function next(): Promise<string> {
  return (await run(["next"], 60_000)).trim();
}

export function title(row: Row): string {
  return row.reason ? `${row.reason}  ·  ${row.label}` : row.label;
}

export function age(since: number | null, now = Date.now() / 1000): string {
  if (!since) return "";
  const minutes = Math.max(0, Math.round((now - since) / 60));
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.round(minutes / 60);
  return hours < 24 ? `${hours} h` : `${Math.round(hours / 24)} d`;
}

/** Snooze the current session until its next message. Returns the HUD text. */
export async function snoozeCurrent(): Promise<string> {
  const data = await loadRadar();
  const current = data.current;
  if (!current || !current.waiting) return "No current session to snooze";
  if (current.snoozed) return `${current.label} is already snoozed`;
  await snooze(current, data.snoozeChoices[0].until);
  return `Snoozed ${current.label} until its next message`;
}

/** For a no-view command: run it, say what happened in a HUD, never fail silently. */
export async function hud(work: () => Promise<string>) {
  try {
    await showHUD(await work());
  } catch (error) {
    await showHUD(`Radar broken: ${error instanceof Error ? error.message : String(error)}`);
  }
}
