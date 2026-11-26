import * as radar from "./radar";

/** Open the session Next points at, worked out now: pressing twice goes to two different sessions. */
export default function Command() {
  return radar.hud(async () => `→ ${await radar.next()}`);
}
