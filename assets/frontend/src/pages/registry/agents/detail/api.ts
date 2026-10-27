import { fetchJson } from "@/lib/http/client";

type AgentRelationshipMutationResult = {
  status: "no_change" | "updated";
};

function uniqueIds(values: string[]): string[] {
  return Array.from(
    new Set(values.map((value) => value.trim()).filter(Boolean)),
  );
}

function sameIds(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

export async function replaceAgentRelationships(
  agentId: string,
  expected: { skills: string[]; tools: string[] },
  desired: { skills: string[]; tools: string[] },
): Promise<AgentRelationshipMutationResult> {
  const expectedSkills = uniqueIds(expected.skills);
  const expectedTools = uniqueIds(expected.tools);
  const desiredSkills = uniqueIds(desired.skills);
  const desiredTools = uniqueIds(desired.tools);
  if (sameIds(expectedSkills, desiredSkills) && sameIds(expectedTools, desiredTools)) {
    return { status: "no_change" };
  }
  return fetchJson<AgentRelationshipMutationResult>(
    `/api/agents/${encodeURIComponent(agentId)}/relationships`,
    {
      method: "PUT",
      body: JSON.stringify({
        expected_skills: expectedSkills,
        expected_tools: expectedTools,
        skills: desiredSkills,
        tools: desiredTools,
      }),
    },
  );
}
