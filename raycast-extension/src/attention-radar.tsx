import { Action, ActionPanel, Keyboard, Icon, List } from "@raycast/api";
import { usePromise } from "@raycast/utils";
import { useState } from "react";
import { NextAction, RowActions, SnoozeThenNextAction } from "./actions";
import * as radar from "./radar";
import { accessories, icon, markdown } from "./view";

export default function Command() {
  const { data, isLoading, error, revalidate } = usePromise(radar.loadRadar);
  const [showDetail, setShowDetail] = useState(true);
  const toggleDetail = (
    <Action
      title={showDetail ? "Hide Message" : "Show Message"}
      icon={Icon.Sidebar}
      shortcut={{ modifiers: ["cmd"], key: "d" }}
      onAction={() => setShowDetail(!showDetail)}
    />
  );
  const refresh = (
    <Action
      title="Refresh"
      icon={Icon.ArrowClockwise}
      shortcut={Keyboard.Shortcut.Common.Refresh}
      onAction={revalidate}
    />
  );

  if (error || data?.warning) {
    return (
      <List>
        <List.EmptyView
          icon={Icon.ExclamationMark}
          title="The radar is broken"
          description={error?.message ?? data?.warning}
          actions={<ActionPanel>{refresh}</ActionPanel>}
        />
      </List>
    );
  }

  // Grouped by project, in the menu's order: the radar already sorted the rows.
  const groups = new Map<string, radar.Row[]>();
  for (const row of data?.rows ?? []) {
    const key = row.interrupted ? "Interrupted" : row.project || "No project";
    groups.set(key, [...(groups.get(key) ?? []), row]);
  }
  const choices = data?.snoozeChoices ?? [];
  const item = (row: radar.Row, snoozed = false) => (
    <List.Item
      key={`${snoozed ? "z" : ""}${row.terminalId}`}
      icon={snoozed ? { source: Icon.Moon } : icon(row)}
      title={{ value: row.label, tooltip: radar.title(row) }}
      subtitle={showDetail ? undefined : row.reason}
      keywords={[row.project, row.reason, row.hint].filter(Boolean)}
      accessories={data ? accessories(row, data, showDetail) : []}
      detail={<List.Item.Detail markdown={markdown(row)} />}
      actions={
        <RowActions
          row={snoozed ? { ...row, waiting: false } : row}
          choices={choices}
          primary="open"
          onDone={revalidate}
          extra={
            <>
              {!snoozed && <SnoozeThenNextAction row={row} choices={choices} onDone={revalidate} />}
              <NextAction onDone={revalidate} />
              {toggleDetail}
              {refresh}
            </>
          }
        />
      }
    />
  );

  const focus = data?.focus ? `  ·  focus: ${data.focus.project}` : "";
  return (
    <List
      isLoading={isLoading}
      isShowingDetail={showDetail && (data?.rows.length ?? 0) + (data?.snoozed.length ?? 0) > 0}
      navigationTitle={data ? `${data.count} waiting${focus}` : "Attention Radar"}
      searchBarPlaceholder="Filter sessions by name, project or ask"
    >
      <List.EmptyView icon={Icon.CheckCircle} title="Inbox zero" actions={<ActionPanel>{refresh}</ActionPanel>} />
      {[...groups].map(([project, rows]) => (
        <List.Section key={project} title={project.toUpperCase()} subtitle={`${rows.filter((r) => r.waiting).length}`}>
          {rows.map((row) => item(row))}
        </List.Section>
      ))}
      {data?.snoozed.length ? (
        <List.Section title="Snoozed" subtitle={`${data.snoozed.length}`}>
          {data.snoozed.map((row) => item(row, true))}
        </List.Section>
      ) : null}
    </List>
  );
}
