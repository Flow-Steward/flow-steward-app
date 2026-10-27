import { fetchJson } from "@/lib/http/client";
import { HttpError } from "@/lib/http/errors";

export type CanvasNote = {
  id: string;
  text: string;
  x: number;
  y: number;
  width: number;
  height: number;
  image?: { artifact_id: string; filename: string; content_type?: string; size_bytes?: number };
};

export type CanvasNotesResult = { revision: number; annotations_revision: number; annotations: CanvasNote[] };

export function fetchCanvasNotes(workflowId: string) {
  return fetchJson<CanvasNotesResult>(`/api/workflows/${encodeURIComponent(workflowId)}/annotations`);
}

function notePath(workflowId: string, noteId: string) {
  return `/api/workflows/${encodeURIComponent(workflowId)}/annotations/${encodeURIComponent(noteId)}`;
}

export function noteImageUrl(workflowId: string, note: CanvasNote) {
  return `${notePath(workflowId, note.id)}/image?artifact=${encodeURIComponent(note.image?.artifact_id ?? "")}`;
}

export function saveCanvasNote(workflowId: string, note: CanvasNote, revision: number, annotationsRevision: number) {
  const { id, text, x, y, width, height } = note;
  return fetchJson<CanvasNotesResult>(notePath(workflowId, id), {
    method: "PUT", body: JSON.stringify({ annotation: { id, text, x, y, width, height }, expected_revision: revision, expected_annotations_revision: annotationsRevision }),
  });
}

export function deleteCanvasNote(workflowId: string, noteId: string, revision: number, annotationsRevision: number) {
  return fetchJson<CanvasNotesResult>(`${notePath(workflowId, noteId)}?expected_revision=${revision}&expected_annotations_revision=${annotationsRevision}`, { method: "DELETE" });
}

export function uploadCanvasNoteImage(workflowId: string, noteId: string, file: File, revision: number, annotationsRevision: number) {
  const body = new FormData();
  body.append("file", file, file.name);
  body.append("expected_revision", String(revision));
  body.append("expected_annotations_revision", String(annotationsRevision));
  return fetchJson<CanvasNotesResult>(`${notePath(workflowId, noteId)}/image`, { method: "PUT", body });
}

export function clearCanvasNoteImage(workflowId: string, noteId: string, revision: number, annotationsRevision: number) {
  return fetchJson<CanvasNotesResult>(`${notePath(workflowId, noteId)}/image?expected_revision=${revision}&expected_annotations_revision=${annotationsRevision}`, { method: "DELETE" });
}

export function isCanvasNotesRevisionConflict(error: unknown): boolean {
  if (!(error instanceof HttpError) || error.status !== 409 || !error.payload || typeof error.payload !== "object") return false;
  const detail = (error.payload as { detail?: unknown }).detail;
  return Boolean(detail && typeof detail === "object" && "code" in detail && detail.code === "workflow_annotations_revision_conflict");
}
