import { z } from "@/lib/validation/zod";

export const textFilterSchema = z.string();
export const booleanFilterSchema = z.boolean();
export const stringFiltersSchema = z.record(z.string(), z.string());
export const periodFilterSchema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("all") }),
  z.object({ kind: z.literal("preset"), preset: z.enum(["today", "last7", "last30"]) }),
  z.object({ kind: z.literal("custom"), from: z.iso.date(), to: z.iso.date() }).refine((value) => value.from <= value.to),
]);
