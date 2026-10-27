import { useId, useState } from "react";

type RowKeyState = {
  expectedCount: number;
  keys: string[];
  nextSequence: number;
};

export type StableRowKeys = {
  append: () => void;
  keys: string[];
  move: (from: number, to: number) => void;
  remove: (index: number) => void;
};

function resizedState(
  state: RowKeyState,
  itemCount: number,
  prefix: string,
  instanceId: string,
): RowKeyState {
  if (state.expectedCount === itemCount) {
    return state;
  }
  const keys = state.keys.slice(0, itemCount);
  let nextSequence = state.nextSequence;
  while (keys.length < itemCount) {
    keys.push(`${prefix}-${instanceId}-${nextSequence}`);
    nextSequence += 1;
  }
  return { expectedCount: itemCount, keys, nextSequence };
}

function initialState(itemCount: number, prefix: string, instanceId: string): RowKeyState {
  return resizedState(
    { expectedCount: -1, keys: [], nextSequence: 0 },
    itemCount,
    prefix,
    instanceId,
  );
}

/**
 * Keeps React identities stable for editable rows whose persisted value does not
 * have an ID. The controller mirrors append/remove/reorder operations while also
 * reconciling values loaded asynchronously from an API.
 */
export function useStableRowKeys(itemCount: number, prefix: string): StableRowKeys {
  const instanceId = useId();
  const [state, setState] = useState(() => initialState(itemCount, prefix, instanceId));
  const renderState = resizedState(state, itemCount, prefix, instanceId);
  if (renderState !== state) {
    setState(renderState);
  }

  return {
    append: () => {
      setState((current) =>
        resizedState(current, itemCount + 1, prefix, instanceId),
      );
    },
    keys: renderState.keys,
    move: (from, to) => {
      setState((current) => {
        const next = resizedState(current, itemCount, prefix, instanceId);
        const keys = [...next.keys];
        const [moved] = keys.splice(from, 1);
        keys.splice(to, 0, moved);
        return { ...next, keys };
      });
    },
    remove: (index) => {
      setState((current) => {
        const next = resizedState(current, itemCount, prefix, instanceId);
        return {
          ...next,
          expectedCount: Math.max(0, itemCount - 1),
          keys: next.keys.filter((_, keyIndex) => keyIndex !== index),
        };
      });
    },
  };
}

type NestedRowKeyState = {
  expectedCounts: number[];
  groups: string[][];
  nextSequence: number;
};

export type StableNestedRowKeys = {
  append: (groupIndex: number) => void;
  appendGroup: () => void;
  groups: string[][];
  remove: (groupIndex: number, itemIndex: number) => void;
  removeGroup: (groupIndex: number) => void;
};

function resizedNestedState(
  state: NestedRowKeyState,
  itemCounts: readonly number[],
  prefix: string,
  instanceId: string,
): NestedRowKeyState {
  if (
    state.expectedCounts.length === itemCounts.length &&
    state.expectedCounts.every((count, index) => count === itemCounts[index])
  ) {
    return state;
  }
  const groups = state.groups.slice(0, itemCounts.length);
  let nextSequence = state.nextSequence;
  itemCounts.forEach((itemCount, groupIndex) => {
    const keys = (groups[groupIndex] ?? []).slice(0, itemCount);
    while (keys.length < itemCount) {
      keys.push(`${prefix}-${instanceId}-${nextSequence}`);
      nextSequence += 1;
    }
    groups[groupIndex] = keys;
  });
  return { expectedCounts: [...itemCounts], groups, nextSequence };
}

/** Stable identities for editable child rows grouped under editable parents. */
export function useStableNestedRowKeys(
  itemCounts: readonly number[],
  prefix: string,
): StableNestedRowKeys {
  const instanceId = useId();
  const [state, setState] = useState<NestedRowKeyState>(() =>
    resizedNestedState(
      { expectedCounts: [], groups: [], nextSequence: 0 },
      itemCounts,
      prefix,
      instanceId,
    ),
  );
  const renderState = resizedNestedState(state, itemCounts, prefix, instanceId);
  if (renderState !== state) {
    setState(renderState);
  }

  const update = (
    transform: (current: NestedRowKeyState) => NestedRowKeyState,
  ) => {
    setState((current) =>
      transform(resizedNestedState(current, itemCounts, prefix, instanceId)),
    );
  };

  return {
    append: (groupIndex) => {
      update((current) => {
        const counts = [...current.expectedCounts];
        counts[groupIndex] = (counts[groupIndex] ?? 0) + 1;
        return resizedNestedState(current, counts, prefix, instanceId);
      });
    },
    appendGroup: () => {
      update((current) =>
        resizedNestedState(
          current,
          [...current.expectedCounts, 0],
          prefix,
          instanceId,
        ),
      );
    },
    groups: renderState.groups,
    remove: (groupIndex, itemIndex) => {
      update((current) => {
        const groups = current.groups.map((group) => [...group]);
        groups[groupIndex]?.splice(itemIndex, 1);
        const expectedCounts = groups.map((group) => group.length);
        return { ...current, expectedCounts, groups };
      });
    },
    removeGroup: (groupIndex) => {
      update((current) => ({
        ...current,
        expectedCounts: current.expectedCounts.filter(
          (_, index) => index !== groupIndex,
        ),
        groups: current.groups.filter((_, index) => index !== groupIndex),
      }));
    },
  };
}
