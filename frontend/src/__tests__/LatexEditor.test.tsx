import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { test, expect, vi } from "vitest";
import { LatexEditor } from "../components/LatexEditor";

test("test_show_in_pdf_invokes_forward_synctex_with_cursor_line", () => {
  const onForwardSync = vi.fn();

  render(
    <LatexEditor
      projectId="manuscript-2026"
      filePath="chapters/introduction.tex"
      initialContent={"\\chapter{Intro}\nLine 2\nLine 3\n"}
      initialEtag="etag-v1"
      cursorLine={3}
      diagnostics={[]}
      onForwardSync={onForwardSync}
    />
  );

  const goToPdfBtn = screen.getByRole("button", { name: /Show in PDF/i });
  fireEvent.click(goToPdfBtn);

  expect(onForwardSync).toHaveBeenCalledWith("chapters/introduction.tex", 3);
});

test("test_etag_409_conflict_shows_merge_reload_modal", async () => {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.includes("/files/main.tex") && init?.method === "PUT") {
      return {
        ok: false,
        status: 409,
        json: async () => ({
          detail: "ETag conflict: file modified on disk",
          current_etag: "etag-server-v2",
          server_content: "\\documentclass{article}\n% Updated on disk\n",
        }),
      };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  });

  vi.stubGlobal("fetch", fetchMock);
  localStorage.setItem("lemma_csrf_token", "csrf-save-token");

  render(
    <LatexEditor
      projectId="manuscript-2026"
      filePath="main.tex"
      initialContent={"\\documentclass{article}\n% Local edit\n"}
      initialEtag="etag-stale-v1"
      diagnostics={[]}
    />
  );

  const saveBtn = screen.getByRole("button", { name: /Save/i });
  fireEvent.click(saveBtn);

  const conflictModal = await screen.findByRole("dialog", { name: /ETag Conflict/i });
  expect(conflictModal).toBeInTheDocument();
  expect(screen.getByText(/modified on disk/i)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Reload from Disk/i })).toBeInTheDocument();
});

const baseProps = {
  projectId: "manuscript-2026",
  filePath: "chapters/introduction.tex",
  initialContent: "\\chapter{Intro}\nLine 2\nLine 3\n",
  initialEtag: "etag-v1",
};

test("test_button_label_is_show_in_pdf", () => {
  render(<LatexEditor {...baseProps} />);

  const btn = screen.getByRole("button", { name: "Show in PDF" });
  expect(btn.textContent).toBe("Show in PDF");
  expect(btn.getAttribute("aria-label")).toBe("Show in PDF");
  expect(btn.getAttribute("title")).toBe("Show in PDF (Cmd/Ctrl+Click)");
  expect(btn.getAttribute("class")).toBe("lemma-btn lemma-btn-secondary lemma-btn-xs");
  expect(btn.getAttribute("disabled")).not.toBe("");
  expect(btn.getAttribute("disabled")).not.toBe("true");
});

test("test_button_disabled_without_pdf", () => {
  render(<LatexEditor {...baseProps} pdfAvailable={false} />);

  const btn = screen.getByRole("button", { name: "Show in PDF" });
  expect(btn.hasAttribute("disabled")).toBe(true);
  expect(btn.getAttribute("title")).toBe("Compile first");
});

// The fake CodeMirror never dispatches DOM events: capture the handlers the editor registers.
async function captureMousedown(platform: string) {
  const originalNavigator = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  Object.defineProperty(globalThis, "navigator", { value: { platform }, configurable: true, writable: true });
  const { EditorView } = await import("@codemirror/view");
  const capture = { handlers: [] as Array<Record<string, (e: any, v: any) => boolean | void>> };
  const originalFn = (EditorView as any).domEventHandlers;
  (EditorView as any).domEventHandlers = (h: any) => {
    capture.handlers.push(h);
    return originalFn(h);
  };
  return {
    capture,
    restore: () => {
      (EditorView as any).domEventHandlers = originalFn;
      if (originalNavigator) Object.defineProperty(globalThis, "navigator", originalNavigator);
      else delete (globalThis as any).navigator;
    },
  };
}

function fakeView(lineNumber: number | null) {
  return {
    posAtCoords: vi.fn(() => (lineNumber === null ? null : 42)),
    state: { doc: { lineAt: vi.fn(() => ({ number: lineNumber ?? 0 })) } },
  };
}

test("test_cmd_click_calls_forward_sync_with_clicked_line", async () => {
  const { capture, restore } = await captureMousedown("MacIntel");
  try {
    const onForwardSync = vi.fn();
    render(<LatexEditor {...baseProps} cursorLine={1} onForwardSync={onForwardSync} />);
    const mousedown = capture.handlers[capture.handlers.length - 1].mousedown;

    const view = fakeView(3);
    const event = { metaKey: true, ctrlKey: false, button: 0, clientX: 10, clientY: 20, preventDefault: vi.fn() };
    const handled = mousedown(event, view);

    expect(view.posAtCoords).toHaveBeenCalledWith({ x: 10, y: 20 });
    expect(onForwardSync).toHaveBeenCalledWith("chapters/introduction.tex", 3);
    expect(event.preventDefault).toHaveBeenCalled();
    expect(handled).toBe(true);
  } finally {
    restore();
  }
});

test("test_ctrl_click_on_non_mac_calls_forward_sync_and_meta_click_does_not", async () => {
  const { capture, restore } = await captureMousedown("Win32");
  try {
    const onForwardSync = vi.fn();
    render(<LatexEditor {...baseProps} onForwardSync={onForwardSync} />);
    const mousedown = capture.handlers[capture.handlers.length - 1].mousedown;

    mousedown({ metaKey: true, ctrlKey: false, button: 0, clientX: 1, clientY: 1, preventDefault: vi.fn() }, fakeView(2));
    expect(onForwardSync).not.toHaveBeenCalled();

    mousedown({ metaKey: false, ctrlKey: true, button: 0, clientX: 1, clientY: 1, preventDefault: vi.fn() }, fakeView(2));
    expect(onForwardSync).toHaveBeenCalledWith("chapters/introduction.tex", 2);
  } finally {
    restore();
  }
});

test("test_plain_click_does_not_call_forward_sync", async () => {
  const { capture, restore } = await captureMousedown("MacIntel");
  try {
    const onForwardSync = vi.fn();
    render(<LatexEditor {...baseProps} onForwardSync={onForwardSync} />);
    const mousedown = capture.handlers[capture.handlers.length - 1].mousedown;

    const event = { metaKey: false, ctrlKey: false, button: 0, clientX: 10, clientY: 20, preventDefault: vi.fn() };
    const handled = mousedown(event, fakeView(3));
    const secondary = { metaKey: true, ctrlKey: false, button: 2, clientX: 10, clientY: 20, preventDefault: vi.fn() };
    const handledSecondary = mousedown(secondary, fakeView(3));

    expect(onForwardSync).not.toHaveBeenCalled();
    expect(event.preventDefault).not.toHaveBeenCalled();
    expect(secondary.preventDefault).not.toHaveBeenCalled();
    expect(handled).toBeFalsy();
    expect(handledSecondary).toBeFalsy();
  } finally {
    restore();
  }
});

test("test_cmd_click_ignored_when_pdf_unavailable", async () => {
  const { capture, restore } = await captureMousedown("MacIntel");
  try {
    const onForwardSync = vi.fn();
    render(<LatexEditor {...baseProps} pdfAvailable={false} onForwardSync={onForwardSync} />);
    const mousedown = capture.handlers[capture.handlers.length - 1].mousedown;

    const event = { metaKey: true, ctrlKey: false, button: 0, clientX: 10, clientY: 20, preventDefault: vi.fn() };
    const handled = mousedown(event, fakeView(3));

    expect(onForwardSync).not.toHaveBeenCalled();
    expect(event.preventDefault).not.toHaveBeenCalled();
    expect(handled).toBeFalsy();
  } finally {
    restore();
  }
});

test("test_programmatic_content_load_does_not_report_dirty", async () => {
  const { EditorView } = await import("@codemirror/view");
  const listeners: Array<(u: any) => void> = [];
  const originalOf = (EditorView as any).updateListener.of;
  const originalDispatch = (EditorView as any).prototype.dispatch;
  (EditorView as any).updateListener.of = (fn: any) => {
    listeners.push(fn);
    return originalOf(fn);
  };
  // The fake view never notifies listeners: mimic real CodeMirror, which calls them synchronously on dispatch.
  (EditorView as any).prototype.dispatch = function (tr: any) {
    originalDispatch.call(this, tr);
    if (tr?.changes) {
      const text = this.state.doc.toString();
      listeners[listeners.length - 1]?.({
        docChanged: true,
        selectionSet: false,
        state: {
          doc: { toString: () => text, lineAt: () => ({ number: 1 }) },
          selection: { main: { head: 0, from: 0, to: 0 } },
          sliceDoc: () => "",
        },
      });
    }
  };
  try {
    const onDirtyChange = vi.fn();
    const view = render(<LatexEditor {...baseProps} initialContent="old" onDirtyChange={onDirtyChange} />);
    view.rerender(<LatexEditor {...baseProps} initialContent="reloaded from disk" onDirtyChange={onDirtyChange} />);
    expect(onDirtyChange).not.toHaveBeenCalled();

    // A genuine user edit still reports.
    listeners[listeners.length - 1]({
      docChanged: true,
      selectionSet: false,
      state: {
        doc: { toString: () => "typed", lineAt: () => ({ number: 1 }) },
        selection: { main: { head: 0, from: 0, to: 0 } },
        sliceDoc: () => "",
      },
    });
    expect(onDirtyChange).toHaveBeenCalledWith("chapters/introduction.tex", "typed");
  } finally {
    (EditorView as any).updateListener.of = originalOf;
    (EditorView as any).prototype.dispatch = originalDispatch;
  }
});

// Capture the (fake) view the editor creates by observing dispatch, then read the flash decorations off its state.
async function captureFlashView() {
  const { EditorView } = await import("@codemirror/view");
  const { flashLineField } = await import("../components/flashLine");
  const originalDispatch = (EditorView as any).prototype.dispatch;
  const state: { view: any } = { view: null };
  (EditorView as any).prototype.dispatch = function (tr: any) {
    state.view = this;
    return originalDispatch.call(this, tr);
  };
  const realSet = globalThis.setTimeout;
  globalThis.setTimeout = ((() => 0) as unknown) as typeof setTimeout;
  return {
    view: (): any => state.view,
    doc: (): string | undefined => state.view?.state.doc.toString(),
    flashed: (): number[] => {
      const set: any = state.view?.state.field(flashLineField, false)?.decorations;
      return (set?.items ?? []).map((r: any) => state.view.state.doc.lineAt(r.from).number);
    },
    restore: () => {
      (EditorView as any).prototype.dispatch = originalDispatch;
      globalThis.setTimeout = realSet;
    },
  };
}

const flashContent = "one\ntwo\nthree\nfour\n";

test("test_no_flash_on_mount", async () => {
  const h = await captureFlashView();
  try {
    render(<LatexEditor {...baseProps} initialContent={flashContent} cursorLine={3} jumpNonce={1} />);
    expect(h.flashed()).toEqual([]);
  } finally {
    h.restore();
  }
});

test("test_flashes_when_cursorLine_changes", async () => {
  const h = await captureFlashView();
  try {
    const view = render(<LatexEditor {...baseProps} initialContent={flashContent} cursorLine={1} />);
    view.rerender(<LatexEditor {...baseProps} initialContent={flashContent} cursorLine={3} />);
    expect(h.flashed()).toEqual([3]);
  } finally {
    h.restore();
  }
});

test("test_flashes_again_when_nonce_changes_same_line", async () => {
  const h = await captureFlashView();
  try {
    const view = render(<LatexEditor {...baseProps} initialContent={flashContent} cursorLine={2} jumpNonce={1} />);
    expect(h.flashed()).toEqual([]);
    view.rerender(<LatexEditor {...baseProps} initialContent={flashContent} cursorLine={2} jumpNonce={2} />);
    expect(h.flashed()).toEqual([2]);
  } finally {
    h.restore();
  }
});

test("test_out_of_range_line_clamped_no_throw", async () => {
  const h = await captureFlashView();
  try {
    const view = render(<LatexEditor {...baseProps} initialContent={"a\nb\nc"} cursorLine={1} />);
    view.rerender(<LatexEditor {...baseProps} initialContent={"a\nb\nc"} cursorLine={999} />);
    expect(h.flashed()).toEqual([3]);
  } finally {
    h.restore();
  }
});

// ---- Task 3.8: dirty buffer reporting ----

// The fake CodeMirror never invokes update listeners: capture the editor's listener to simulate an edit.
async function withListenerCapture(run: (edit: (text: string) => void) => Promise<void>) {
  const listeners: Array<(u: any) => void> = [];
  const { EditorView } = await import("@codemirror/view");
  const originalOf = (EditorView as any).updateListener.of;
  (EditorView as any).updateListener.of = (fn: any) => {
    listeners.push(fn);
    return originalOf(fn);
  };
  const edit = (text: string) =>
    listeners[listeners.length - 1]({
      docChanged: true,
      selectionSet: false,
      state: {
        doc: { toString: () => text, lineAt: () => ({ number: 1 }) },
        selection: { main: { head: 0, from: 0, to: 0 } },
        sliceDoc: () => "",
      },
    });
  try {
    await run(edit);
  } finally {
    (EditorView as any).updateListener.of = originalOf;
  }
}

test("test_dirty_change_reported_on_edit", async () => {
  await withListenerCapture(async (edit) => {
    const onDirtyChange = vi.fn();
    render(<LatexEditor {...baseProps} onDirtyChange={onDirtyChange} />);
    edit("edited text");
    expect(onDirtyChange.mock.calls.at(-1)).toEqual(["chapters/introduction.tex", "edited text"]);
  });
});

test("test_dirty_cleared_after_save", async () => {
  await withListenerCapture(async (edit) => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ etag: "etag-v2" }) }))
    );
    const onDirtyChange = vi.fn();
    render(<LatexEditor {...baseProps} onDirtyChange={onDirtyChange} />);
    edit("edited text");
    expect(onDirtyChange.mock.calls.at(-1)).toEqual(["chapters/introduction.tex", "edited text"]);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(onDirtyChange.mock.calls.at(-1)).toEqual(["chapters/introduction.tex", null]);
    });
  });
});

// ---- Deferred jump, draft restore, forced reload ----

test("test_jump_deferred_until_content_for_target_file_loaded", async () => {
  const h = await captureFlashView();
  try {
    const props = { ...baseProps, filePath: "a.tex", contentFilePath: "a.tex", initialContent: flashContent };
    const view = render(<LatexEditor {...props} cursorLine={1} jumpNonce={0} />);
    // Jump to another file: its content has not arrived yet (props still hold a.tex content).
    view.rerender(<LatexEditor {...props} filePath="b.tex" cursorLine={3} jumpNonce={1} />);
    expect(h.flashed()).toEqual([]);
    expect(h.doc()).toBe(flashContent);

    view.rerender(
      <LatexEditor {...props} filePath="b.tex" contentFilePath="b.tex" initialContent={"b1\nb2\nb3\nb4\n"} cursorLine={3} jumpNonce={1} />
    );
    expect(h.doc()).toBe("b1\nb2\nb3\nb4\n");
    expect(h.flashed()).toEqual([3]);
  } finally {
    h.restore();
  }
});

test("test_plain_file_switch_does_not_flash_after_load", async () => {
  const h = await captureFlashView();
  try {
    const props = { ...baseProps, filePath: "a.tex", contentFilePath: "a.tex", initialContent: flashContent };
    const view = render(<LatexEditor {...props} cursorLine={2} jumpNonce={1} />);
    view.rerender(<LatexEditor {...props} filePath="b.tex" cursorLine={2} jumpNonce={1} />);
    view.rerender(<LatexEditor {...props} filePath="b.tex" contentFilePath="b.tex" initialContent={"b1\nb2\nb3\n"} cursorLine={2} jumpNonce={1} />);
    expect(h.doc()).toBe("b1\nb2\nb3\n");
    expect(h.flashed()).toEqual([]);
  } finally {
    h.restore();
  }
});

test("test_flash_on_mount_when_mounted_by_a_jump_request", async () => {
  const h = await captureFlashView();
  try {
    render(<LatexEditor {...baseProps} initialContent={flashContent} cursorLine={3} jumpNonce={1} flashOnMount />);
    expect(h.flashed()).toEqual([3]);
  } finally {
    h.restore();
  }
});

test("test_draft_content_shown_and_still_dirty_on_mount", async () => {
  const h = await captureFlashView();
  try {
    await withListenerCapture(async (edit) => {
      const onDirtyChange = vi.fn();
      render(
        <LatexEditor {...baseProps} initialContent="saved text" draftContent="unsaved draft" onDirtyChange={onDirtyChange} />
      );
      expect(h.doc()).toBe("unsaved draft");
      // Restoring the draft is not a user edit and must not re-report it.
      expect(onDirtyChange).not.toHaveBeenCalled();
      // Typing back to the saved text makes the buffer clean again (still tracked as dirty before that).
      edit("saved text");
      expect(onDirtyChange.mock.calls.at(-1)).toEqual(["chapters/introduction.tex", null]);
    });
  } finally {
    h.restore();
  }
});

test("test_returning_to_a_dirty_file_restores_its_draft", async () => {
  const h = await captureFlashView();
  try {
    const a = { ...baseProps, filePath: "a.tex", contentFilePath: "a.tex", initialContent: "a saved" };
    const view = render(<LatexEditor {...a} />);
    view.rerender(<LatexEditor {...a} filePath="b.tex" contentFilePath="b.tex" initialContent="b saved" />);
    expect(h.doc()).toBe("b saved");
    // Back to a.tex: parent still holds its unsaved buffer and passes it as the draft.
    view.rerender(<LatexEditor {...a} filePath="a.tex" contentFilePath="b.tex" initialContent="b saved" draftContent="a unsaved" />);
    view.rerender(<LatexEditor {...a} filePath="a.tex" contentFilePath="a.tex" initialContent="a saved" draftContent="a unsaved" />);
    expect(h.doc()).toBe("a unsaved");
  } finally {
    h.restore();
  }
});

test("test_reload_key_discards_unsaved_text", async () => {
  const h = await captureFlashView();
  try {
    const view = render(<LatexEditor {...baseProps} initialContent="saved text" reloadKey={0} />);
    h.view().dispatch({ changes: { from: 0, to: 10, insert: "typed over" } });
    expect(h.doc()).toBe("typed over");
    view.rerender(<LatexEditor {...baseProps} initialContent="saved text" reloadKey={1} />);
    expect(h.doc()).toBe("saved text");
  } finally {
    h.restore();
  }
});

// ---- snapToHeading (PDF double-click jumps) ----

const headingDoc = "\\section{A}\ntext\ntext\n\n\n";

test("test_snapToHeading_flashes_heading_above_target", async () => {
  const h = await captureFlashView();
  try {
    const view = render(<LatexEditor {...baseProps} initialContent={headingDoc} cursorLine={1} />);
    view.rerender(<LatexEditor {...baseProps} initialContent={headingDoc} cursorLine={5} jumpNonce={1} snapToHeading />);
    expect(h.flashed()).toEqual([1]);
  } finally {
    h.restore();
  }
});

test("test_without_snapToHeading_flashes_exact_line", async () => {
  const h = await captureFlashView();
  try {
    const view = render(<LatexEditor {...baseProps} initialContent={headingDoc} cursorLine={1} />);
    view.rerender(<LatexEditor {...baseProps} initialContent={headingDoc} cursorLine={5} jumpNonce={1} />);
    expect(h.flashed()).toEqual([5]);
  } finally {
    h.restore();
  }
});

test("test_snapToHeading_waits_for_target_file_content", async () => {
  const h = await captureFlashView();
  try {
    const props = { ...baseProps, filePath: "a.tex", contentFilePath: "a.tex", initialContent: flashContent };
    const view = render(<LatexEditor {...props} cursorLine={1} jumpNonce={0} />);
    view.rerender(<LatexEditor {...props} filePath="b.tex" cursorLine={4} jumpNonce={1} snapToHeading />);
    expect(h.flashed()).toEqual([]);
    view.rerender(
      <LatexEditor {...props} filePath="b.tex" contentFilePath="b.tex" initialContent={"\\chapter{B}\nb2\nb3\nb4\n"} cursorLine={4} jumpNonce={1} snapToHeading />
    );
    expect(h.flashed()).toEqual([1]);
  } finally {
    h.restore();
  }
});

test("does not report editor context before that file's content is ready", () => {
  const onEditorContext = vi.fn();
  render(
    <LatexEditor
      {...baseProps}
      contentFilePath="other.tex"
      onEditorContext={onEditorContext}
    />
  );
  expect(onEditorContext).not.toHaveBeenCalled();
});

test("reports the editor buffer for chat on mount", async () => {
  const onEditorContext = vi.fn();
  render(<LatexEditor {...baseProps} onEditorContext={onEditorContext} />);
  await waitFor(() => {
    expect(onEditorContext).toHaveBeenCalled();
  });
  expect(onEditorContext.mock.calls.at(-1)[0]).toMatchObject({
    filePath: "chapters/introduction.tex",
    content: baseProps.initialContent,
    cursorLine: 1,
    selection: null,
  });
});

test("test_zotero_connected_bib_shows_banner_with_disconnect_and_sync_and_locks_manual_editing", async () => {
  const h = await captureFlashView();
  try {
    await withListenerCapture(async (edit) => {
      const fetchMock = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ etag: "etag-v2" }) }));
      vi.stubGlobal("fetch", fetchMock);
      const onDirtyChange = vi.fn();
      const onZoteroSync = vi.fn();
      const onZoteroDisconnect = vi.fn();
      const bibContent = "@article{vaswani2017,\n  title = {Attention Is All You Need}\n}\n";

      const view = render(
        <LatexEditor
          projectId="manuscript-2026"
          filePath="references.bib"
          initialContent={bibContent}
          initialEtag="etag-bib-1"
          zoteroSynced={true}
          onZoteroSync={onZoteroSync}
          onZoteroDisconnect={onZoteroDisconnect}
          onDirtyChange={onDirtyChange}
          zoteroSyncStatus="Zotero: 1 added, 0 updated"
        />
      );

      // Banner is shown at the top stating the file is Zotero synced
      const banner = screen.getByTestId("zotero-sync-banner");
      expect(banner).toBeInTheDocument();
      expect(banner.textContent).toMatch(/this file is zotero synced/i);
      expect(banner.textContent).toMatch(/Zotero: 1 added, 0 updated/);

      // Two buttons under the banner: Disconnect Zotero and Sync
      const disconnectBtn = screen.getByRole("button", { name: "Disconnect Zotero" });
      const syncBtn = screen.getByRole("button", { name: "Sync" });
      expect(disconnectBtn).toBeInTheDocument();
      expect(syncBtn).toBeInTheDocument();

      fireEvent.click(syncBtn);
      expect(onZoteroSync).toHaveBeenCalledTimes(1);

      // Manual editing and saving are locked while Zotero is connected
      const saveBtn = screen.getByRole("button", { name: "Save" });
      expect(saveBtn.hasAttribute("disabled")).toBe(true);

      h.view().dispatch({ changes: { from: 0, to: bibContent.length, insert: "manual overwrite" } });
      expect(h.doc()).toBe(bibContent);

      edit("manual overwrite");
      expect(onDirtyChange).not.toHaveBeenCalled();

      fireEvent.click(saveBtn);
      expect(fetchMock).not.toHaveBeenCalled();

      // Clicking Disconnect Zotero triggers callback; once disconnected, content is preserved and editing unlocks
      fireEvent.click(disconnectBtn);
      expect(onZoteroDisconnect).toHaveBeenCalledTimes(1);

      view.rerender(
        <LatexEditor
          projectId="manuscript-2026"
          filePath="references.bib"
          initialContent={bibContent}
          initialEtag="etag-bib-1"
          zoteroSynced={false}
          onZoteroSync={undefined}
          onZoteroDisconnect={undefined}
          onDirtyChange={onDirtyChange}
        />
      );

      expect(screen.queryByTestId("zotero-sync-banner")).toBeNull();
      expect(h.doc()).toBe(bibContent);
      expect(screen.getByRole("button", { name: "Save" }).hasAttribute("disabled")).toBe(false);

      h.view().dispatch({ changes: { from: 0, to: bibContent.length, insert: `${bibContent}\n% manual edit\n` } });
      expect(h.doc()).toBe(`${bibContent}\n% manual edit\n`);
      edit(`${bibContent}\n% manual edit\n`);
      expect(onDirtyChange).toHaveBeenCalledWith("references.bib", `${bibContent}\n% manual edit\n`);
    });
  } finally {
    h.restore();
  }
});
