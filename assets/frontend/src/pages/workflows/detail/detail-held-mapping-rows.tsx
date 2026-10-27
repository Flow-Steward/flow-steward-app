import { useState, type ReactNode } from "react";

import {
  configEntries,
  newDraftMapRow,
  objectFromEntries,
  objectOrEmpty,
  storableDraftMapRows,
  type DraftMapRow,
} from "@/pages/workflows/detail/detail-helpers";

type MappingRow = { key: string; value: string };

type HeldRow<Row> = { index: number; row: Row };

/** The stored rows, with the held ones back in the places they were edited at. */
function rowsWithHeld<Row extends MappingRow>(stored: Row[], held: HeldRow<Row>[]): Row[] {
  if (!held.length) return stored;
  // The one empty row a mapping with no rows shows stands for what is held.
  const rows = stored.filter((row) => row.key || row.value);
  held.forEach(({ index, row }) => {
    const same = row.key ? rows.findIndex((candidate) => candidate.key === row.key) : -1;
    if (same >= 0) rows[same] = row;
    else rows.splice(Math.min(index, rows.length), 0, row);
  });
  return rows.length ? rows : stored;
}

/**
 * A step's input or output mapping is an object keyed by the parameter (or the
 * returned field) each row maps, and it keeps only rows with something to map.
 * A row with no parameter chosen yet, or an optional parameter whose value was
 * just cleared, therefore cannot be stored: what was typed into it vanished on
 * every keystroke (or the row itself did, with the focus), so the Value field
 * never showed the text and its variable list never filtered (B24).
 *
 * Such rows are held here, in the places they were edited at, and shown with the
 * stored ones; every edit still stores what the mapping can keep. A held row is
 * stored as soon as it has a parameter and a value. Give each step editor its
 * own `key` so nothing held carries over to another step.
 */
export function HeldMappingRows<Row extends MappingRow>({
  children,
  rows,
  storable,
  store,
}: Readonly<{
  children: (rows: Row[], updateRows: (next: Row[]) => void) => ReactNode;
  rows: Row[];
  storable: (row: Row) => boolean;
  store: (next: Row[]) => void;
}>) {
  const [held, setHeld] = useState<HeldRow<Row>[]>([]);
  const shown = rowsWithHeld(rows, held);
  const updateRows = (next: Row[]) => {
    setHeld(next.flatMap((row, index) => (storable(row) ? [] : [{ index, row }])));
    store(next);
  };
  return <>{children(shown, updateRows)}</>;
}

function draftRowsFromStored(stored: unknown): DraftMapRow[] {
  const rows = configEntries(stored).map((row) => newDraftMapRow(row.key, row.value));
  return rows.length ? rows : [newDraftMapRow()];
}

/**
 * A config map (Set variable's assignments, an Invoke step's mappings) is saved as an
 * object keyed by the row's key, and an object cannot hold an empty or a repeated key.
 * Rows saved straight into it therefore lost a cleared key's whole row, and "+ Add"
 * had to invent a key for the new row (item 11).
 *
 * The rows are drafts here, each with a stable id; every edit saves what the map can
 * keep (storableDraftMapRows) and the drafts stay as typed. A change of the saved map
 * from elsewhere (undo, restore, another step) replaces them.
 */
export function DraftMapRows({
  children,
  store,
  stored,
}: Readonly<{
  children: (rows: DraftMapRow[], updateRows: (next: DraftMapRow[]) => void) => ReactNode;
  store: (next: MappingRow[]) => void;
  stored: unknown;
}>) {
  const storedSignature = JSON.stringify(objectOrEmpty(stored));
  const [draft, setDraft] = useState(() => ({ basis: storedSignature, rows: draftRowsFromStored(stored) }));
  let current = draft;
  if (draft.basis !== storedSignature) {
    const ownEdit = JSON.stringify(objectFromEntries(storableDraftMapRows(draft.rows))) === storedSignature;
    current = { basis: storedSignature, rows: ownEdit ? draft.rows : draftRowsFromStored(stored) };
    setDraft(current);
  }
  const updateRows = (next: DraftMapRow[]) => {
    const rows = next.length ? next : [newDraftMapRow()];
    const saved = storableDraftMapRows(rows);
    setDraft({ basis: JSON.stringify(objectFromEntries(saved)), rows });
    store(saved);
  };
  return <>{children(current.rows, updateRows)}</>;
}
