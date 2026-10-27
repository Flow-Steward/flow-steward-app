import { useCallback, useState } from "react";

type SetKeyedStateAction<T> = T | ((current: T) => T);

export function useKeyedState<T>(stateKey: string, seed: T) {
  const [stateByKey, setStateByKey] = useState<Record<string, T>>({});

  const value = stateByKey[stateKey] ?? seed;

  const setValue = useCallback(
    (next: SetKeyedStateAction<T>) => {
      setStateByKey((currentState) => {
        const currentValue = currentState[stateKey] ?? seed;
        const resolvedValue =
          typeof next === "function"
            ? (next as (current: T) => T)(currentValue)
            : next;

        if (Object.is(currentValue, resolvedValue)) {
          return currentState;
        }

        return {
          ...currentState,
          [stateKey]: resolvedValue,
        };
      });
    },
    [seed, stateKey],
  );

  return [value, setValue] as const;
}
