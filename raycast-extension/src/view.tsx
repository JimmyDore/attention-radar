import { Color, Icon, Image, List } from "@raycast/api";
import * as radar from "./radar";

// The menu's dots: 🔴 dialog or failure, 🟠 under 1 h, 🟡 1 to 24 h, ⚪ over 24 h (finished?).
const DOTS = [Color.Red, Color.Orange, Color.Yellow, Color.SecondaryText];

export function icon(row: radar.Row): Image.ImageLike {
  if (row.interrupted) return { source: Icon.RotateClockwise, tintColor: Color.Blue };
  if (!row.waiting) return { source: Icon.Hourglass, tintColor: Color.SecondaryText };
  return { source: Icon.CircleFilled, tintColor: DOTS[row.urgency] ?? Color.SecondaryText };
}

/** The message the session ended on, as Claude wrote it: markdown. */
export function markdown(row: radar.Row): string {
  const head = `### ${radar.title(row)}\n\n${row.project}${row.since ? `  ·  waiting ${radar.age(row.since)}` : ""}`;
  const body = row.interrupted
    ? "_Terminal gone, conversation kept. Open it to resume it in its pane._"
    : row.message || row.hint || "_No message._";
  return `${head}\n\n---\n\n${body}`;
}

/** compact: the detail pane is open and repeats the ask and the age, so the name gets the room. */
export function accessories(row: radar.Row, data: radar.Radar, compact: boolean): List.Item.Accessory[] {
  const isCurrent = data.current?.terminalId === row.terminalId;
  const isNext = data.next === row.terminalId;
  const marks: List.Item.Accessory[] = [];
  if (compact) {
    if (isCurrent) marks.push({ icon: { source: Icon.Person, tintColor: Color.Blue }, tooltip: "Current" });
    if (isNext) marks.push({ icon: { source: Icon.ArrowRight, tintColor: Color.Green }, tooltip: "Next" });
  } else {
    if (isCurrent) marks.push({ tag: { value: "current", color: Color.Blue } });
    if (isNext) marks.push({ tag: { value: "next", color: Color.Green } });
  }
  if (row.opened) marks.push({ icon: Icon.Eye, tooltip: "Opened since its last message" });
  if (!compact && row.since) marks.push({ text: radar.age(row.since) });
  return marks;
}
