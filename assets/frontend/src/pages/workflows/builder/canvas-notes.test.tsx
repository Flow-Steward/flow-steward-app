import { act, renderHook, waitFor } from "@testing-library/react";

import { canvasNoteNodes, handleCanvasNoteShortcut, NOTE_UNDO_WINDOW_MS, useCanvasNotes } from "@/pages/workflows/builder/canvas-notes";

const note = { id: "note-1", text: "Read this", x: 120, y: 40, width: 280, height: 200 };

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

describe("canvas note editing", () => {
  it("sends both revisions when deleting a note image and then the note", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response({ revision: 7, annotations_revision: 4, annotations: [note] }))
      .mockResolvedValueOnce(response({ revision: 7, annotations_revision: 5, annotations: [] }));
    const { result } = renderHook(() => useCanvasNotes({ workflowId: "wf", revision: 7, settings: { canvas_annotations: [note], canvas_annotations_revision: 3 }, onSaved: async () => undefined }));
    await act(async () => { await result.current.clearImage(note.id); });
    await act(async () => { await result.current.remove(note.id); });
    expect(fetchMock.mock.calls.map(([url, init]) => ({ url, method: init?.method }))).toEqual([
      { url: "/api/workflows/wf/annotations/note-1/image?expected_revision=7&expected_annotations_revision=3", method: "DELETE" },
      { url: "/api/workflows/wf/annotations/note-1?expected_revision=7&expected_annotations_revision=4", method: "DELETE" },
    ]);
  });

  it("advances only the annotation CAS across note edits", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response({ revision: 7, annotations_revision: 4, annotations: [note] }))
      .mockResolvedValueOnce(response({ revision: 7, annotations_revision: 5, annotations: [{ ...note, x: 400 }] }));
    const onSaved = vi.fn(async () => undefined);
    const { result } = renderHook(() => useCanvasNotes({ workflowId: "wf", revision: 7, settings: { canvas_annotations: [note], canvas_annotations_revision: 3 }, onSaved }));
    await act(async () => { await result.current.save(note); });
    await act(async () => { await result.current.save({ ...note, x: 400 }); });
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({ expected_revision: 7, expected_annotations_revision: 3 });
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toMatchObject({ expected_revision: 7, expected_annotations_revision: 4 });
    expect(onSaved.mock.calls).toEqual([[7], [7]]);
  });

  it("preserves an open draft's annotation CAS until explicit reload after a conflict", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response({ detail: { code: "workflow_annotations_revision_conflict" } }, 409))
      .mockResolvedValueOnce(response({ revision: 7, annotations_revision: 4, annotations: [{ ...note, text: "Other author" }] }))
      .mockResolvedValueOnce(response({ revision: 7, annotations_revision: 5, annotations: [{ ...note, text: "My draft" }] }));
    const onSaved = vi.fn(async () => undefined);
    const { result, rerender } = renderHook(({ settings }) => useCanvasNotes({ workflowId: "wf", revision: 7, settings, onSaved }), {
      initialProps: { settings: { canvas_annotations: [note], canvas_annotations_revision: 3 } },
    });
    act(() => result.current.edit({ ...note, text: "My draft" }));
    rerender({ settings: { canvas_annotations: [{ ...note, text: "Other author" }], canvas_annotations_revision: 4 } });
    await act(async () => { expect(await result.current.save(result.current.draft!)).toBe(false); });
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({ expected_revision: 7, expected_annotations_revision: 3 });
    expect(result.current.draft?.text).toBe("My draft");
    expect(onSaved).not.toHaveBeenCalled();
    await act(async () => { await result.current.reload(); });
    await act(async () => { await result.current.save(result.current.draft!); });
    expect(JSON.parse(String(fetchMock.mock.calls[2][1]?.body))).toMatchObject({ expected_revision: 7, expected_annotations_revision: 4 });
  });

  it("persists the final keyboard position without a pointer drag-stop event", async () => {
    let persisted = { ...note };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) => {
      persisted = JSON.parse(String(init?.body)).annotation;
      return response({ revision: 8, annotations_revision: 1, annotations: [persisted] });
    });
    const props = { workflowId: "wf", revision: 7, settings: { canvas_annotations: [note] }, onSaved: async () => undefined };
    const view = renderHook(() => useCanvasNotes(props));
    act(() => {
      for (let y = 60; y <= 540; y += 20) {
        view.result.current.onNodesChange([{ id: "annotation:note-1", type: "position", position: { x: 430, y }, dragging: false }]);
      }
    });
    await waitFor(() => expect(persisted).toMatchObject({ x: 430, y: 540 }));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body)).expected_revision).toBe(7);
    view.unmount();
    const { result: reopened } = renderHook(() => useCanvasNotes({ ...props, revision: 8, settings: { canvas_annotations: [persisted] } }));
    expect(reopened.current.notes[0]).toMatchObject({ x: 430, y: 540 });
  });

  it("does not save disabled keyboard moves or a move queued before unmount", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const props = { workflowId: "wf", revision: 7, settings: { canvas_annotations: [note] }, onSaved: async () => undefined };
    try {
      const { result: readonly, unmount: unmountReadonly } = renderHook(() => useCanvasNotes({ ...props, disabled: true }));
      act(() => readonly.current.onNodesChange([{ id: "annotation:note-1", type: "position", position: { x: 430, y: 540 }, dragging: false }]));
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
      expect(readonly.current.notes[0]).toMatchObject({ x: 120, y: 40 });
      unmountReadonly();
      const view = renderHook(() => useCanvasNotes(props));
      act(() => view.result.current.onNodesChange([{ id: "annotation:note-1", type: "position", position: { x: 430, y: 540 }, dragging: false }]));
      view.unmount();
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
      expect(fetchMock).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("cancels queued movement on reload and leaves pointer drag saving to drag-stop", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(response({ revision: 8, annotations_revision: 1, annotations: [note] }));
    const props = { workflowId: "wf", revision: 7, settings: { canvas_annotations: [note] }, onSaved: async () => undefined };
    try {
      const { result } = renderHook(() => useCanvasNotes(props));
      act(() => result.current.onNodesChange([{ id: "annotation:note-1", type: "position", position: { x: 430, y: 540 }, dragging: false }]));
      await act(async () => { await result.current.reload(); });
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(fetchMock.mock.calls[0][1]?.method || "GET").toBe("GET");
      act(() => result.current.onNodesChange([{ id: "annotation:note-1", type: "position", position: { x: 430, y: 540 }, dragging: true }]));
      act(() => result.current.onNodesChange([{ id: "annotation:note-1", type: "position", position: { x: 430, y: 560 }, dragging: false }]));
      await act(async () => { await result.current.save(result.current.notes[0]); });
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toMatchObject({ expected_revision: 8, annotation: { x: 430, y: 560 } });
    } finally {
      vi.useRealTimers();
    }
  });

  it("serializes multi-note keyboard moves with the latest revision", async () => {
    let persisted = [note, { ...note, id: "note-2", x: 220 }];
    let revision = 7;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) => {
      const body = JSON.parse(String(init?.body));
      expect(body.expected_revision).toBe(revision);
      persisted = persisted.map((saved) => saved.id === body.annotation.id ? body.annotation : saved);
      revision += 1;
      return response({ revision, annotations_revision: revision - 7, annotations: persisted });
    });
    const { result } = renderHook(() => useCanvasNotes({ workflowId: "wf", revision: 7, settings: { canvas_annotations: [note, { ...note, id: "note-2", x: 220 }] }, onSaved: async () => undefined }));
    act(() => result.current.onNodesChange([
      { id: "annotation:note-1", type: "position", position: { x: 430, y: 540 }, dragging: false },
      { id: "annotation:note-2", type: "position", position: { x: 530, y: 540 }, dragging: false },
    ]));
    await waitFor(() => expect(persisted.map(({ x, y }) => ({ x, y }))).toEqual([{ x: 430, y: 540 }, { x: 530, y: 540 }]));
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("discards every queued keyboard move after a revision conflict", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(response({ detail: { code: "workflow_revision_conflict" } }, 409));
    const onSaved = vi.fn(async () => undefined);
    const notes = [note, { ...note, id: "note-2", x: 220 }];
    try {
      const { result } = renderHook(() => useCanvasNotes({ workflowId: "wf", revision: 7, settings: { canvas_annotations: notes }, onSaved }));
      act(() => result.current.onNodesChange(notes.map((item) => ({ id: `annotation:${item.id}`, type: "position", position: { x: 430, y: 540 }, dragging: false }))));
      await act(async () => { await vi.advanceTimersByTimeAsync(250); });
      expect(result.current.error).toBeTruthy();
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(result.current.notes).toEqual(notes);
      expect(onSaved).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("discards queued coordinates when switching workflows", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const onSaved = vi.fn(async () => undefined);
    try {
      const { result, rerender } = renderHook(({ workflowId }) => useCanvasNotes({ workflowId, revision: 7, settings: { canvas_annotations: [note] }, onSaved }), { initialProps: { workflowId: "wf" } });
      act(() => result.current.onNodesChange([{ id: "annotation:note-1", type: "position", position: { x: 430, y: 540 }, dragging: false }]));
      rerender({ workflowId: "other" });
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
      expect(fetchMock).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  afterEach(() => vi.restoreAllMocks());

  it("saves an edited note with the current revision, retaining its image", async () => {
    const image = { artifact_id: "image-1", filename: "diagram.png" };
    const changed = { ...note, text: "Updated", image };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(response({ revision: 8, annotations_revision: 1, annotations: [changed] }));
    const onSaved = vi.fn(async () => undefined);
    const { result } = renderHook(() => useCanvasNotes({ workflowId: "wf", revision: 7, settings: { canvas_annotations: [{ ...note, image }] }, onSaved }));
    await act(async () => { await result.current.save({ ...note, text: "Updated", image }); });
    expect(result.current.notes).toEqual([changed]);
    expect(onSaved).toHaveBeenCalledWith(8);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/workflows/wf/annotations/note-1");
    expect(JSON.parse(String(init?.body))).toEqual({ annotation: { ...note, text: "Updated" }, expected_revision: 7, expected_annotations_revision: 0 });
  });

  it("keeps the draft and exposes a revision conflict instead of claiming it saved", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response({ detail: { code: "workflow_revision_conflict", message: "Reload before saving." } }, 409));
    const onSaved = vi.fn(async () => undefined);
    const { result } = renderHook(() => useCanvasNotes({ workflowId: "wf", revision: 7, settings: { canvas_annotations: [note] }, onSaved }));
    act(() => result.current.edit(note));
    act(() => result.current.setDraft({ ...note, text: "Unsaved text" }));
    await act(async () => { expect(await result.current.save(result.current.draft!)).toBe(false); });
    expect(result.current.draft?.text).toBe("Unsaved text");
    expect(result.current.error).toBeTruthy();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("uses the revision returned by the previous save for the next edit", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response({ revision: 8, annotations_revision: 1, annotations: [note] }))
      .mockResolvedValueOnce(response({ revision: 9, annotations_revision: 2, annotations: [{ ...note, x: 400 }] }));
    const { result } = renderHook(() => useCanvasNotes({ workflowId: "wf", revision: 7, settings: {}, onSaved: async () => undefined }));
    await act(async () => { await result.current.save(note); });
    await act(async () => { await result.current.save({ ...note, x: 400 }); });
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body)).expected_revision).toBe(8);
    expect(result.current.notes[0].x).toBe(400);
  });

  it("uploads an image using the existing multipart transport without creating a job", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(response({ revision: 8, annotations_revision: 1, annotations: [note] }));
    const { result } = renderHook(() => useCanvasNotes({ workflowId: "wf", revision: 7, settings: { canvas_annotations: [note] }, onSaved: async () => undefined }));
    const file = new File(["png"], "diagram.png", { type: "image/png" });
    await act(async () => { await result.current.uploadImage(note.id, file); });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/workflows/wf/annotations/note-1/image");
    expect((init?.body as FormData).get("expected_revision")).toBe("7");
    expect((init?.body as FormData).get("expected_annotations_revision")).toBe("0");
    expect((init?.body as FormData).get("file")).toMatchObject({ name: "diagram.png", type: "image/png", size: 3 });
  });

  it("reloads after a conflict without losing the draft or rebasing structural edits as its own write", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response({ revision: 9, annotations_revision: 2, annotations: [{ ...note, text: "Other author's text", x: 600 }] }));
    const onSaved = vi.fn(async () => undefined);
    const onReload = vi.fn(async () => undefined);
    const { result } = renderHook(() => useCanvasNotes({ workflowId: "wf", revision: 7, settings: { canvas_annotations: [note] }, onSaved, onReload }));
    act(() => result.current.edit({ ...note, text: "My unsaved draft" }));
    await act(async () => { await result.current.reload(); });
    expect(result.current.notes[0].text).toBe("Other author's text");
    expect(result.current.draft?.text).toBe("My unsaved draft");
    expect(onReload).toHaveBeenCalledWith(9);
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("blocks writes during structural apply without locking the note dialog closed", () => {
    const { result } = renderHook(() => useCanvasNotes({ workflowId: "wf", revision: 7, settings: {}, onSaved: async () => undefined, disabled: true }));
    expect(result.current.disabled).toBe(true);
    expect(result.current.busy).toBe(false);
  });

  it("does not authorize a stale open draft with a revision received in a background refresh", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(response({ detail: { code: "workflow_revision_conflict" } }, 409));
    const onSaved = vi.fn(async () => undefined);
    const { result, rerender } = renderHook(({ revision, saved }) => useCanvasNotes({ workflowId: "wf", revision, settings: { canvas_annotations: [saved] }, onSaved }), { initialProps: { revision: 7, saved: note } });
    act(() => result.current.edit({ ...note, text: "My draft" }));
    rerender({ revision: 8, saved: { ...note, text: "Other author", x: 900 } });
    await act(async () => { await result.current.save(result.current.draft!); });
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body)).expected_revision).toBe(7);
    expect(result.current.draft?.text).toBe("My draft");
    expect(onSaved).not.toHaveBeenCalled();
  });
  it("opens a new note as a 256 x 160 sheet on the grid and selects it for writing", () => {
    const { result } = renderHook(() => useCanvasNotes({ workflowId: "wf", revision: 7, settings: {}, onSaved: async () => undefined }));
    act(() => result.current.add({ x: 101, y: 57 }));
    expect(result.current.draft).toMatchObject({ text: "", x: 96, y: 64, width: 256, height: 160 });
    const [node] = canvasNoteNodes(result.current, "wf");
    expect(node).toMatchObject({ id: `annotation:${result.current.draft?.id}`, selected: true, draggable: false, data: { editing: true } });
  });

  it("drops a new note left empty quietly, without a write or a notice", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const notify = vi.fn();
    const { result } = renderHook(() => useCanvasNotes({ workflowId: "wf", revision: 7, settings: {}, onSaved: async () => undefined, notify }));
    act(() => result.current.add({ x: 0, y: 0 }));
    await act(async () => { expect(await result.current.finish("   ")).toBe(true); });
    expect(result.current.draft).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(notify).not.toHaveBeenCalled();
  });

  it("saves the written text when writing ends and keeps an unchanged note without a write", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(response({ revision: 7, annotations_revision: 4, annotations: [{ ...note, text: "Changed" }] }));
    const { result } = renderHook(() => useCanvasNotes({ workflowId: "wf", revision: 7, settings: { canvas_annotations: [note], canvas_annotations_revision: 3 }, onSaved: async () => undefined }));
    act(() => result.current.edit(note));
    await act(async () => { await result.current.finish(note.text); });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.draft).toBeNull();
    act(() => result.current.edit(note));
    await act(async () => { await result.current.finish("Changed"); });
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({ annotation: { id: note.id, text: "Changed" }, expected_annotations_revision: 3 });
    expect(result.current.draft).toBeNull();
  });

  it("keeps a new sheet open while another note's save completes", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response({ revision: 7, annotations_revision: 4, annotations: [{ ...note, x: 400 }] }));
    const { result } = renderHook(() => useCanvasNotes({ workflowId: "wf", revision: 7, settings: { canvas_annotations: [note] }, onSaved: async () => undefined }));
    act(() => result.current.add({ x: 0, y: 0 }));
    const draftId = result.current.draft?.id;
    await act(async () => { await result.current.save({ ...note, x: 400 }); });
    expect(result.current.draft?.id).toBe(draftId);
  });

  it("deletes at once with Undo and only forgets the note on the server when Undo was not used", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(response({ revision: 7, annotations_revision: 4, annotations: [] }));
    const notify = vi.fn();
    try {
      const { result } = renderHook(() => useCanvasNotes({ workflowId: "wf", revision: 7, settings: { canvas_annotations: [note], canvas_annotations_revision: 3 }, onSaved: async () => undefined, notify }));
      act(() => result.current.removeWithUndo(note.id));
      expect(result.current.notes).toEqual([]);
      expect(notify).toHaveBeenLastCalledWith({ label: "Note deleted.", tone: "success", action: { label: "Undo", onAction: expect.any(Function) } });
      act(() => { notify.mock.lastCall?.[0].action.onAction(); });
      expect(result.current.notes).toEqual([note]);
      await act(async () => { await vi.advanceTimersByTimeAsync(NOTE_UNDO_WINDOW_MS * 2); });
      expect(fetchMock).not.toHaveBeenCalled();

      act(() => result.current.removeWithUndo(note.id));
      await act(async () => { await vi.advanceTimersByTimeAsync(NOTE_UNDO_WINDOW_MS); });
      expect(fetchMock.mock.calls.map(([url, init]) => ({ url, method: init?.method }))).toEqual([
        { url: "/api/workflows/wf/annotations/note-1?expected_revision=7&expected_annotations_revision=3", method: "DELETE" },
      ]);
      expect(result.current.notes).toEqual([]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("removes a note whose text was erased with Undo, naming it as empty", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(response({ revision: 7, annotations_revision: 1, annotations: [] }));
    const notify = vi.fn();
    const { result, unmount } = renderHook(() => useCanvasNotes({ workflowId: "wf", revision: 7, settings: { canvas_annotations: [note] }, onSaved: async () => undefined, notify }));
    act(() => result.current.edit(note));
    act(() => { void result.current.finish(""); });
    expect(result.current.draft).toBeNull();
    expect(result.current.notes).toEqual([]);
    expect(notify).toHaveBeenLastCalledWith(expect.objectContaining({ label: "Empty note removed.", action: expect.objectContaining({ label: "Undo" }) }));
    expect(fetchMock).not.toHaveBeenCalled();
    unmount();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  });

  it("sends a removal still waiting for Undo when the canvas closes", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(response({ revision: 7, annotations_revision: 4, annotations: [] }));
    const { result, unmount } = renderHook(() => useCanvasNotes({ workflowId: "wf", revision: 7, settings: { canvas_annotations: [note], canvas_annotations_revision: 3 }, onSaved: async () => undefined, notify: vi.fn() }));
    act(() => result.current.removeWithUndo(note.id));
    unmount();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][0]).toBe("/api/workflows/wf/annotations/note-1?expected_revision=7&expected_annotations_revision=3");
  });

  it("removes notes left empty by an earlier session when the canvas opens", async () => {
    const blank = { ...note, id: "note-blank", text: "  " };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(response({ revision: 7, annotations_revision: 4, annotations: [note] }));
    const { result } = renderHook(() => useCanvasNotes({ workflowId: "wf", revision: 7, settings: { canvas_annotations: [note, blank], canvas_annotations_revision: 3 }, onSaved: async () => undefined }));
    await waitFor(() => expect(result.current.notes).toEqual([note]));
    expect(fetchMock.mock.calls.map(([url, init]) => ({ url, method: init?.method }))).toEqual([
      { url: "/api/workflows/wf/annotations/note-blank?expected_revision=7&expected_annotations_revision=3", method: "DELETE" },
    ]);
  });

  it("takes notes out of a canvas delete and leaves the steps to the graph delete", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response({ revision: 7, annotations_revision: 1, annotations: [{ ...note, id: "note-2" }] }))
      .mockResolvedValueOnce(response({ revision: 7, annotations_revision: 2, annotations: [] }));
    const notify = vi.fn();
    const { result, unmount } = renderHook(() => useCanvasNotes({ workflowId: "wf", revision: 7, settings: { canvas_annotations: [note, { ...note, id: "note-2" }] }, onSaved: async () => undefined, notify }));
    let remaining: string[] = [];
    act(() => { remaining = result.current.removeFromSelection(["annotation:note-1", "step_a", "annotation:note-2"]); });
    expect(remaining).toEqual(["step_a"]);
    expect(result.current.notes).toEqual([]);
    expect(notify).toHaveBeenLastCalledWith(expect.objectContaining({ label: "2 notes deleted." }));
    unmount();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/workflows/wf/annotations/note-1?expected_revision=7&expected_annotations_revision=0",
      "/api/workflows/wf/annotations/note-2?expected_revision=7&expected_annotations_revision=1",
    ]);
  });

  it("duplicates a note beside the original as a new saved note", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) => response({ revision: 7, annotations_revision: 4, annotations: [note, JSON.parse(String(init?.body)).annotation] }));
    const { result } = renderHook(() => useCanvasNotes({ workflowId: "wf", revision: 7, settings: { canvas_annotations: [note] }, onSaved: async () => undefined }));
    await act(async () => { await result.current.duplicate(note); });
    const written = JSON.parse(String(fetchMock.mock.calls[0][1]?.body)).annotation;
    expect(written).toMatchObject({ text: note.text, x: note.x + 32, y: note.y + 32, width: note.width, height: note.height });
    expect(written.id).not.toBe(note.id);
  });

  it("attaches an image to a new sheet by saving its text first", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) => {
      if (init?.body instanceof FormData) return response({ revision: 7, annotations_revision: 5, annotations: [] });
      return response({ revision: 7, annotations_revision: 4, annotations: [JSON.parse(String(init?.body)).annotation] });
    });
    const { result } = renderHook(() => useCanvasNotes({ workflowId: "wf", revision: 7, settings: {}, onSaved: async () => undefined }));
    act(() => result.current.add({ x: 0, y: 0 }));
    const file = new File(["png"], "diagram.png", { type: "image/png" });
    await act(async () => { await result.current.attachImage(file, "With a picture"); });
    expect(fetchMock.mock.calls.map(([, init]) => init?.method)).toEqual(["PUT", "PUT"]);
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body)).annotation.text).toBe("With a picture");
    expect((fetchMock.mock.calls[1][1]?.body as FormData).get("expected_annotations_revision")).toBe("4");
  });

  it("answers N with a new note and leaves modified keys alone", () => {
    const addNote = vi.fn();
    const plain = new KeyboardEvent("keydown", { code: "KeyN", key: "n", cancelable: true });
    expect(handleCanvasNoteShortcut(plain, addNote)).toBe(true);
    expect(plain.defaultPrevented).toBe(true);
    expect(handleCanvasNoteShortcut(new KeyboardEvent("keydown", { code: "KeyN", key: "n", metaKey: true }), addNote)).toBe(false);
    expect(handleCanvasNoteShortcut(new KeyboardEvent("keydown", { code: "KeyN", key: "n" }), null)).toBe(false);
    expect(addNote).toHaveBeenCalledTimes(1);
  });
});
