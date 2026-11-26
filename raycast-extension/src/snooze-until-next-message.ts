import * as radar from "./radar";

/** Next sent you to a session that did not need you: set it aside until it writes again. */
export default function Command() {
  return radar.hud(radar.snoozeCurrent);
}
