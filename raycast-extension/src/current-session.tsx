import { Action, ActionPanel, Keyboard, Detail, Icon } from "@raycast/api";
import { usePromise } from "@raycast/utils";
import { NextAction, RowActions, SnoozeThenNextAction } from "./actions";
import * as radar from "./radar";
import { markdown } from "./view";

const NONE = `### No current session

The current session is the last one you opened from Next, the menu or Raycast. It has none right now:
it is not waiting any more (it ran again since), or you have not opened one in the last day.`;

/** The session you are looking at, with "snooze until its next message" on ↵. */
export default function Command() {
  const { data, isLoading, error, revalidate } = usePromise(radar.loadRadar);
  const current = data?.current;
  const refresh = (
    <Action
      title="Refresh"
      icon={Icon.ArrowClockwise}
      shortcut={Keyboard.Shortcut.Common.Refresh}
      onAction={revalidate}
    />
  );
  let text = NONE;
  if (error) text = `### The radar is broken\n\n${error.message}`;
  else if (current?.snoozed) text = `${markdown(current)}\n\n---\n\n_Already snoozed._`;
  else if (current) text = markdown(current);
  return (
    <Detail
      isLoading={isLoading}
      navigationTitle={current ? current.label : "Current Session"}
      markdown={isLoading && !data ? "" : text}
      actions={
        current && !current.snoozed ? (
          <RowActions
            row={current}
            choices={data.snoozeChoices}
            primary="snooze"
            onDone={revalidate}
            extra={
              <>
                <SnoozeThenNextAction row={current} choices={data.snoozeChoices} onDone={revalidate} />
                <NextAction onDone={revalidate} />
                {refresh}
              </>
            }
          />
        ) : (
          <ActionPanel>
            <NextAction onDone={revalidate} />
            {refresh}
          </ActionPanel>
        )
      }
    />
  );
}
