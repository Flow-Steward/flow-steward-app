import { createStepSecretTasks } from "./detail-step-secret-tasks";

it("saves the initial tasks even when a completed task changes field registrations", async () => {
  const tasks = createStepSecretTasks();
  const saved: string[] = [];
  tasks.register("first", async () => {
    saved.push("first");
    await Promise.resolve();
    tasks.register("second", null);
    tasks.register("late", async () => { saved.push("late"); });
  });
  tasks.register("second", async () => { saved.push("second"); });

  await tasks.runAll();

  expect(saved).toEqual(["first", "second"]);
});
