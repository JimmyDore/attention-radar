import * as radar from "./radar";

/** Snooze the current session until its next message, then open the one after. */
export default function Command() {
  return radar.hud(async () => {
    const snoozed = await radar.snoozeCurrent();
    return `${snoozed}  ·  → ${await radar.next()}`;
  });
}
