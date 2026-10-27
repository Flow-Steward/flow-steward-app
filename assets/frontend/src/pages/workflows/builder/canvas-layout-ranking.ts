/**
 * Scope-local edges for column ranking only. Reachability and drawn edges must
 * continue to use the original adjacency, including the omitted back edges.
 */
export function acyclicAdjacency(
  entryId: string,
  nodeIds: readonly string[],
  adjacency: ReadonlyMap<string, readonly string[]>,
): Map<string, string[]> {
  const inside = new Set(nodeIds);
  const kept = new Map<string, string[]>(nodeIds.map((id) => [id, []]));
  const hasIncoming = new Set<string>();
  const visited = new Set<string>();
  const active = new Set<string>();
  for (const id of nodeIds) {
    for (const target of adjacency.get(id) ?? []) {
      if (inside.has(target)) hasIncoming.add(target);
    }
  }

  const visit = (start: string) => {
    if (visited.has(start)) return;
    const path = [{ id: start, next: 0 }];
    visited.add(start);
    active.add(start);
    while (path.length) {
      const frame = path.at(-1)!;
      const targets = adjacency.get(frame.id) ?? [];
      if (frame.next === targets.length) {
        active.delete(frame.id);
        path.pop();
        continue;
      }
      const target = targets[frame.next++];
      if (!inside.has(target) || active.has(target)) continue;
      kept.get(frame.id)?.push(target);
      if (!visited.has(target)) {
        visited.add(target);
        active.add(target);
        path.push({ id: target, next: 0 });
      }
    }
  };

  if (entryId && inside.has(entryId)) visit(entryId);
  for (const id of nodeIds) {
    if (!hasIncoming.has(id)) visit(id);
  }
  for (const id of nodeIds) visit(id);
  return kept;
}

/** Longest paths from the supplied starts, restricted to an acyclic scope. */
function rankFrom(
  starts: ReadonlyMap<string, number>,
  nodeIds: readonly string[],
  adjacency: ReadonlyMap<string, readonly string[]>,
): Map<string, number> {
  const incoming = new Map(nodeIds.map((id) => [id, 0]));
  for (const id of nodeIds) {
    for (const target of adjacency.get(id) ?? []) {
      if (incoming.has(target)) incoming.set(target, (incoming.get(target) ?? 0) + 1);
    }
  }
  const queue = nodeIds.filter((id) => incoming.get(id) === 0);
  const rank = new Map(starts);
  for (const id of queue) {
    const column = rank.get(id);
    for (const target of adjacency.get(id) ?? []) {
      const count = incoming.get(target);
      if (count === undefined) continue;
      if (column !== undefined && (rank.get(target) ?? -1) < column + 1) {
        rank.set(target, column + 1);
      }
      incoming.set(target, count - 1);
      if (count === 1) queue.push(target);
    }
  }
  return rank;
}

/** Rank every scope node without changing execution reachability or geometry. */
export function columnsFor(
  entryId: string,
  nodeIds: readonly string[],
  adjacency: ReadonlyMap<string, readonly string[]>,
): Map<string, number> {
  const rankAdjacency = acyclicAdjacency(entryId, nodeIds, adjacency);
  const starts = new Map<string, number>();
  if (entryId && rankAdjacency.has(entryId)) starts.set(entryId, 0);
  const main = rankFrom(starts, nodeIds, rankAdjacency);
  let base = 0;
  for (const column of main.values()) base = Math.max(base, column + 1);

  const remaining = nodeIds.filter((id) => !main.has(id));
  const remainingIds = new Set(remaining);
  const hasIncoming = new Set<string>();
  for (const id of remaining) {
    for (const target of rankAdjacency.get(id) ?? []) {
      if (remainingIds.has(target)) hasIncoming.add(target);
    }
  }
  const roots = new Map(remaining.filter((id) => !hasIncoming.has(id)).map((id) => [id, base]));
  const disconnected = rankFrom(roots, remaining, rankAdjacency);
  return new Map([...main, ...disconnected]);
}
