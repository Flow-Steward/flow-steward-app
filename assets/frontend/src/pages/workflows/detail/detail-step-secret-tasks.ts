/**
 * Work a step's secret fields do when the step is saved: a value typed into one is
 * kept as a stored secret first, the way a connection keeps it on save (ADR 0057).
 * One store lives as long as the step editor; fields register and clear their task.
 */
export type StepSecretTasks = {
  register: (key: string, task: (() => Promise<void>) | null) => void;
  runAll: () => Promise<void>;
};

export function createStepSecretTasks(): StepSecretTasks {
  const tasks = new Map<string, () => Promise<void>>();
  return {
    register: (key, task) => {
      if (task) tasks.set(key, task);
      else tasks.delete(key);
    },
    runAll: async () => {
      // Saving rerenders fields, whose effect cleanups change registrations.
      // Keep every task present at the start of this save in the same batch.
      const pendingTasks = [...tasks.values()];
      for (const task of pendingTasks) await task();
      tasks.clear();
    },
  };
}
