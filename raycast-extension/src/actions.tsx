import { Action, ActionPanel, Color, Icon, Keyboard, PopToRootType, showHUD, showToast, Toast } from "@raycast/api";
import * as radar from "./radar";

type Props = {
  row: radar.Row;
  choices: radar.SnoozeChoice[];
  /** "snooze" puts "until its next message" on ↵: for the session you are already looking at. */
  primary: "open" | "snooze";
  onDone: () => void;
  extra?: React.ReactNode;
};

/** Run an action, then close Raycast with a HUD, the way a menu click closes the menu.
 * work may return the HUD text, e.g. where Next went. */
async function perform(label: string, work: () => Promise<unknown>, onDone: () => void) {
  const toast = await showToast({ style: Toast.Style.Animated, title: label });
  try {
    const said = await work();
    onDone();
    await showHUD(typeof said === "string" && said ? said : label, { popToRootType: PopToRootType.Immediate });
  } catch (error) {
    toast.style = Toast.Style.Failure;
    toast.title = `${label} failed`;
    toast.message = error instanceof Error ? error.message : String(error);
  }
}

export function RowActions({ row, choices, primary, onDone, extra }: Props) {
  const [untilNext, ...later] = choices;
  const openAction = (
    <Action
      key="open"
      title={row.interrupted ? "Open and Resume" : "Open Workspace"}
      icon={Icon.ArrowNe}
      onAction={() => perform(`Opened ${row.label}`, () => radar.open(row), onDone)}
    />
  );
  const snoozeNext = untilNext && row.waiting && !row.interrupted && (
    <Action
      key="snooze-next"
      title={untilNext.label}
      icon={Icon.Moon}
      shortcut={primary === "open" ? { modifiers: ["cmd"], key: "s" } : undefined}
      onAction={() =>
        perform(`Snoozed ${row.label} until its next message`, () => radar.snooze(row, untilNext.until), onDone)
      }
    />
  );
  const first = primary === "snooze" && snoozeNext ? [snoozeNext, openAction] : [openAction, snoozeNext];
  return (
    <ActionPanel title={radar.title(row)}>
      <ActionPanel.Section>{first}</ActionPanel.Section>
      {row.waiting && !row.interrupted && (
        <ActionPanel.Section>
          <ActionPanel.Submenu title="Snooze…" icon={Icon.Clock} shortcut={{ modifiers: ["cmd", "shift"], key: "s" }}>
            {later.map((choice) => (
              <Action
                key={choice.label}
                title={choice.label}
                onAction={() =>
                  perform(`Snoozed ${row.label}: ${choice.label}`, () => radar.snooze(row, choice.until), onDone)
                }
              />
            ))}
          </ActionPanel.Submenu>
          <Action
            title="Skip, Come Back Later"
            icon={Icon.Forward}
            shortcut={{ modifiers: ["cmd"], key: "arrowRight" }}
            onAction={() => perform(`Skipped ${row.label}`, () => radar.skip(row), onDone)}
          />
          {row.pausable && (
            <Action
              title="Pause and Free Memory"
              icon={{ source: Icon.Pause, tintColor: Color.SecondaryText }}
              shortcut={{ modifiers: ["cmd", "shift"], key: "p" }}
              onAction={() => perform(`Paused ${row.label}`, () => radar.pause(row), onDone)}
            />
          )}
        </ActionPanel.Section>
      )}
      <ActionPanel.Section>
        {extra}
        {row.message && (
          <Action.CopyToClipboard title="Copy Message" content={row.message} shortcut={Keyboard.Shortcut.Common.Copy} />
        )}
      </ActionPanel.Section>
    </ActionPanel>
  );
}

export function NextAction({ onDone }: { onDone: () => void }) {
  return (
    <Action
      title="Go to Next"
      icon={Icon.ArrowRight}
      shortcut={{ modifiers: ["cmd"], key: "n" }}
      onAction={() => perform("Next", async () => `→ ${await radar.next()}`, onDone)}
    />
  );
}

/** The flow after Next brought you to a session that did not need you: set it aside, go to the one after. */
export function SnoozeThenNextAction({
  row,
  choices,
  onDone,
}: {
  row: radar.Row;
  choices: radar.SnoozeChoice[];
  onDone: () => void;
}) {
  const untilNext = choices[0];
  if (!untilNext || !row.waiting || row.interrupted) return null;
  return (
    <Action
      title="Snooze Until Its Next Message, Then Next"
      icon={Icon.ArrowRightCircle}
      shortcut={{ modifiers: ["cmd"], key: "return" }}
      onAction={() =>
        perform(
          "Snooze, then Next",
          async () => {
            await radar.snooze(row, untilNext.until);
            return `→ ${await radar.next()}`;
          },
          onDone,
        )
      }
    />
  );
}
