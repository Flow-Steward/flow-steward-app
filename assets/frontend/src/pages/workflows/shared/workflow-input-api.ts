/**
 * A workflow input's configured value: a secret's value or a file input's test
 * file. Kept apart from the general workflow API, which had grown past what one
 * file should hold.
 */
import { fetchJson } from "@/lib/http/client";

/**
 * Set or replace a secret workflow input's configured value.
 *
 * The only path that carries a secret value, and it carries it one way. There
 * is deliberately no counterpart that reads one back: the contract reports
 * `default_configured`, and that is all a browser is given.
 */
export async function setWorkflowInputSecret({
  workflowId,
  name,
  value,
  expectedRevision,
}: {
  workflowId: string;
  name: string;
  value: string;
  expectedRevision?: number | null;
}) {
  return fetchJson<{ default_configured: boolean; revision: number }>(
    `/api/workflows/${encodeURIComponent(workflowId)}/inputs/${encodeURIComponent(name)}/secret`,
    {
      method: "PUT",
      body: JSON.stringify({
        value,
        expected_revision: expectedRevision ?? null,
      }),
    },
  );
}

/** Clear a secret workflow input's configured value. */
export async function clearWorkflowInputSecret({
  workflowId,
  name,
  expectedRevision,
}: {
  workflowId: string;
  name: string;
  expectedRevision?: number | null;
}) {
  const query =
    expectedRevision === undefined || expectedRevision === null
      ? ""
      : `?expected_revision=${encodeURIComponent(String(expectedRevision))}`;
  return fetchJson<{ default_configured: boolean; revision: number }>(
    `/api/workflows/${encodeURIComponent(workflowId)}/inputs/${encodeURIComponent(name)}/secret${query}`,
    { method: "DELETE" },
  );
}

/**
 * Attach or replace a file input's test file: the file a test run uses when it
 * passes none for that input.
 */
export async function setWorkflowInputSampleFile({
  workflowId,
  name,
  file,
  expectedRevision,
}: {
  workflowId: string;
  name: string;
  file: File;
  expectedRevision?: number | null;
}) {
  const body = new FormData();
  body.append("file", file, file.name);
  if (expectedRevision !== undefined && expectedRevision !== null) {
    body.append("expected_revision", String(expectedRevision));
  }
  return fetchJson<{ revision: number }>(
    `/api/workflows/${encodeURIComponent(workflowId)}/inputs/${encodeURIComponent(name)}/sample-file`,
    { method: "PUT", body },
  );
}

/** Remove a file input's test file. */
export async function clearWorkflowInputSampleFile({
  workflowId,
  name,
  expectedRevision,
}: {
  workflowId: string;
  name: string;
  expectedRevision?: number | null;
}) {
  const query =
    expectedRevision === undefined || expectedRevision === null
      ? ""
      : `?expected_revision=${encodeURIComponent(String(expectedRevision))}`;
  return fetchJson<{ revision: number }>(
    `/api/workflows/${encodeURIComponent(workflowId)}/inputs/${encodeURIComponent(name)}/sample-file${query}`,
    { method: "DELETE" },
  );
}
