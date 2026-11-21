import React, { useState, useEffect, useRef, useMemo } from "react";
import { EditorState } from "@codemirror/state";
import { EditorView, lineNumbers } from "@codemirror/view";
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { tags } from "@lezer/highlight";
import { latex } from "codemirror-lang-latex";
import { findHeadingLineAtOrAboveBy } from "../utils/headingLine";
import type { BibEntry } from "../utils/bibParser";
import { citationAutocompleteExtension } from "./citationAutocomplete";
import { cancelFlash, flashLine, flashLineExtension } from "./flashLine";
import { applyEditorSearch, editorSearchExtension, findTextMatches } from "./editorSearch";
import { PaneFindBar } from "./PaneFindBar";
import {
  type DiffHunkActions,
  type ProposedHunkItem,
  acceptProposedHunk,
  inlineDiffExtension,
  rejectProposedHunk,
  setDiffHunksEffect,
} from "./DiffExtension";

const latexHighlightStyle = HighlightStyle.define([
  { tag: tags.keyword, color: "var(--syntax-command)" },
  { tag: tags.heading, color: "var(--syntax-command)" },
  { tag: tags.operator, color: "var(--text-secondary)" },
  { tag: tags.comment, color: "var(--text-dim)", fontStyle: "italic" },
  { tag: tags.string, color: "var(--syntax-string)" },
  { tag: tags.className, color: "var(--accent-cyan)" },
  { tag: tags.bracket, color: "var(--text-secondary)" },
  { tag: tags.processingInstruction, color: "var(--syntax-math)" },
  { tag: tags.variableName, color: "var(--syntax-command)" },
]);

export interface LatexDiagnosticItem {
  severity: "error" | "warning" | "typesetting";
  file_path: string;
  line: number | null;
  message: string;
  context_lines: string;
  missing_package?: string | null;
}

export interface EditorSelection {
  text: string;
  startLine: number;
  endLine: number;
}

export interface EditorContextSnapshot {
  filePath: string;
  content: string;
  cursorLine: number;
  selection: EditorSelection | null;
}

export interface LatexEditorProps {
  projectId: string;
  filePath: string;
  initialContent: string;
  initialEtag: string | null;
  // Path that initialContent/initialEtag belong to. While it differs from filePath the new file is still loading,
  // so its content is not applied and pending jumps wait. Omit when content always matches filePath.
  contentFilePath?: string | null;
  // Unsaved text the parent kept for this file (shown instead of initialContent and still counted as dirty).
  draftContent?: string | null;
  // Bump to force-reload initialContent into the editor, discarding unsaved text.
  reloadKey?: number;
  cursorLine?: number;
  jumpNonce?: number;
  // The editor was mounted by a jump request (e.g. from Review Studio): flash on that first jump too.
  flashOnMount?: boolean;
  // Snap this jump to the closest sectioning heading at or above cursorLine (PDF double-click jumps).
  snapToHeading?: boolean;
  diagnostics?: LatexDiagnosticItem[];
  proposedHunks?: ProposedHunkItem[];
  csrfToken?: string;
  onForwardSync?: (filePath: string, line: number) => void;
  onSaveSuccess?: (newEtag: string, content: string) => void;
  onTriggerCompile?: () => void;
  onProposeHunk?: (hunk: ProposedHunkItem) => void;
  // A proposed hunk was accepted or rejected in the editor.
  onHunkResolved?: (hunkId: string) => void;
  onHunkAccepted?: (hunk: ProposedHunkItem) => void;
  onDirtyChange?: (filePath: string, content: string | null) => void;
  onEditorContext?: (snapshot: EditorContextSnapshot) => void;
  pdfAvailable?: boolean;
  readOnly?: boolean;
  // Shown for a Zotero-linked bibliography: locks manual editing and shows the Zotero sync banner.
  zoteroSynced?: boolean;
  onZoteroSync?: () => void;
  onZoteroDisconnect?: () => void;
  zoteroSyncBusy?: boolean;
  zoteroDisconnectBusy?: boolean;
  zoteroSyncDisabledReason?: string | null;
  zoteroSyncStatus?: string | null;
  bibEntries?: BibEntry[];
}

export function LatexEditor({
  projectId,
  filePath,
  initialContent,
  initialEtag,
  contentFilePath,
  draftContent,
  reloadKey,
  cursorLine = 1,
  jumpNonce,
  flashOnMount = false,
  snapToHeading = false,
  diagnostics = [],
  proposedHunks = [],
  csrfToken = "",
  onForwardSync,
  onSaveSuccess,
  onTriggerCompile,
  onProposeHunk,
  onHunkResolved,
  onHunkAccepted,
  onDirtyChange,
  onEditorContext,
  pdfAvailable = true,
  readOnly = false,
  zoteroSynced = false,
  onZoteroSync,
  onZoteroDisconnect,
  zoteroSyncBusy = false,
  zoteroDisconnectBusy = false,
  zoteroSyncDisabledReason = null,
  zoteroSyncStatus = null,
  bibEntries = [],
}: LatexEditorProps): React.ReactElement {
  const [content, setContent] = useState<string>(draftContent ?? initialContent);
  const [etag, setEtag] = useState<string | null>(initialEtag);
  const [activeLine, setActiveLine] = useState<number>(cursorLine);
  const [selectedRange, setSelectedRange] = useState<{
    text: string;
    startLine: number;
    endLine: number;
  } | null>(null);
  const [conflictState, setConflictState] = useState<{
    currentEtag: string;
    serverContent: string;
  } | null>(null);

  const isZoteroSynced = Boolean(zoteroSynced || onZoteroSync || onZoteroDisconnect);
  const effectiveReadOnly = readOnly || isZoteroSynced;
  const editorContainerRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  const readOnlyRef = useRef(effectiveReadOnly);
  readOnlyRef.current = effectiveReadOnly;
  const bibEntriesRef = useRef<BibEntry[]>(bibEntries);
  bibEntriesRef.current = bibEntries;
  const onDirtyChangeRef = useRef(onDirtyChange);
  onDirtyChangeRef.current = onDirtyChange;
  const onEditorContextRef = useRef(onEditorContext);
  onEditorContextRef.current = onEditorContext;
  const onForwardSyncRef = useRef(onForwardSync);
  onForwardSyncRef.current = onForwardSync;
  const pdfAvailableRef = useRef(pdfAvailable);
  pdfAvailableRef.current = pdfAvailable;
  const etagRef = useRef(etag);
  etagRef.current = etag;
  const onHunkResolvedRef = useRef(onHunkResolved);
  onHunkResolvedRef.current = onHunkResolved;
  const onSaveSuccessRef = useRef(onSaveSuccess);
  onSaveSuccessRef.current = onSaveSuccess;
  const onHunkAcceptedRef = useRef(onHunkAccepted);
  onHunkAcceptedRef.current = onHunkAccepted;
  // A hunk for another file would be anchored and saved against this file's buffer.
  const fileHunks = proposedHunks.filter((h) => h.file_path === filePath);
  // True while the editor applies content loaded from props, so those updates are not reported as user edits.
  // The first applied jump is the mount: scroll but do not flash (unless a jump request mounted the editor).
  const isInitialJumpRef = useRef(!flashOnMount);
  const appliedJumpRef = useRef<string | null>(null);
  const programmaticLoadRef = useRef(false);
  const savedContentRef = useRef(initialContent);
  const isDirtyRef = useRef(draftContent != null && draftContent !== initialContent);
  const draftContentRef = useRef(draftContent);
  draftContentRef.current = draftContent;
  const contentReady = contentFilePath === undefined || contentFilePath === filePath;
  const loadedFilePathRef = useRef(filePath);

  useEffect(() => {
    if (!contentReady) return;
    onEditorContextRef.current?.({
      filePath,
      content,
      cursorLine: activeLine,
      selection: selectedRange,
    });
  }, [contentReady, filePath, content, activeLine, selectedRange]);

  const [isSaving, setIsSaving] = useState<boolean>(false);
  const [searchOpen, setSearchOpen] = useState<boolean>(false);
  const [searchQuery, setSearchQuery] = useState<string>("");
  const [searchCaseSensitive, setSearchCaseSensitive] = useState<boolean>(false);
  const [searchMatchIndex, setSearchMatchIndex] = useState<number>(0);
  const [searchFocusNonce, setSearchFocusNonce] = useState<number>(0);
  const shouldScrollSearchRef = useRef<boolean>(false);

  const searchMatches = useMemo(
    () => (searchOpen ? findTextMatches(content, searchQuery, searchCaseSensitive) : []),
    [searchOpen, content, searchQuery, searchCaseSensitive]
  );
  const activeSearchIndex =
    searchMatches.length === 0 ? 0 : Math.min(searchMatchIndex, searchMatches.length - 1);

  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    applyEditorSearch(
      view,
      searchOpen ? searchMatches : [],
      activeSearchIndex,
      searchOpen && shouldScrollSearchRef.current
    );
    shouldScrollSearchRef.current = false;
  }, [searchOpen, searchMatches, activeSearchIndex, filePath]);

  useEffect(() => {
    const switchedFile = loadedFilePathRef.current !== filePath;
    loadedFilePathRef.current = filePath;
    // A file switch leaves the previous file's buffer dirty in the parent; this editor now shows a different file.
    if (switchedFile) isDirtyRef.current = false;
    // The new file's content is still loading: keep what is shown rather than flashing the wrong file's text.
    if (!contentReady) return;
    const draft = draftContentRef.current;
    const nextText = draft ?? initialContent;
    const nextDirty = draft != null && draft !== initialContent;
    setContent(nextText);
    setEtag(initialEtag);
    savedContentRef.current = initialContent;
    // Reloading the same file from disk clears its dirty entry in the parent.
    if (isDirtyRef.current && !nextDirty) onDirtyChangeRef.current?.(filePath, null);
    isDirtyRef.current = nextDirty;
    if (viewRef.current && viewRef.current.state.doc.toString() !== nextText) {
      programmaticLoadRef.current = true;
      try {
        viewRef.current.dispatch({
          changes: { from: 0, to: viewRef.current.state.doc.length, insert: nextText },
        });
      } finally {
        programmaticLoadRef.current = false;
      }
    }
  }, [filePath, initialContent, initialEtag, contentReady, reloadKey]);

  useEffect(() => {
    if (viewRef.current) {
      viewRef.current.dispatch({
        effects: setDiffHunksEffect.of(fileHunks),
      });
    }
  }, [proposedHunks, filePath]);

  useEffect(() => {
    if (!editorContainerRef.current) return;
    const isMac = typeof navigator !== "undefined" && /mac/i.test(navigator.platform);
    const hunkActions: DiffHunkActions = {
      onAccept: async (view, hunk, choice) => {
        if (readOnlyRef.current) return "The editor is read-only right now.";
        const result = await acceptProposedHunk({
          view,
          currentEtag: etagRef.current,
          hunk,
          projectId,
          allowUnknownCitations: choice.allowUnknownCitations,
          rejectedCiteKeys: choice.rejectedCiteKeys,
        });
        if (!result.accepted) return result.blockedReason || "Could not apply this edit.";
        // The accepted buffer was saved to disk, so it is the new clean baseline.
        const saved = view.state.doc.toString();
        savedContentRef.current = saved;
        if (result.newEtag) setEtag(result.newEtag);
        if (isDirtyRef.current) {
          isDirtyRef.current = false;
          onDirtyChangeRef.current?.(filePath, null);
        }
        if (result.newEtag) onSaveSuccessRef.current?.(result.newEtag, saved);
        onHunkAcceptedRef.current?.(hunk);
        onHunkResolvedRef.current?.(hunk.hunk_id);
        return null;
      },
      onReject: (view, hunk) => {
        rejectProposedHunk(view, hunk.hunk_id);
        onHunkResolvedRef.current?.(hunk.hunk_id);
      },
    };
    const state = EditorState.create({
      doc: content,
      extensions: [
        lineNumbers(),
        EditorView.lineWrapping,
        EditorState.transactionFilter.of((tr) =>
          readOnlyRef.current && tr.docChanged && !programmaticLoadRef.current ? [] : tr
        ),
        latex({ enableAutocomplete: false, enableLinting: false }),
        syntaxHighlighting(latexHighlightStyle),
        citationAutocompleteExtension(
          () => bibEntriesRef.current,
          () => readOnlyRef.current || filePath.toLowerCase().endsWith(".bib")
        ),
        inlineDiffExtension(fileHunks, hunkActions),
        flashLineExtension(),
        editorSearchExtension(),
        EditorView.theme({
          "&": { backgroundColor: "transparent", color: "var(--text-primary)" },
          ".cm-content": { caretColor: "var(--text-primary)" },
          ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--text-primary)" },
          ".cm-gutters": {
            backgroundColor: "transparent",
            color: "var(--text-dim)",
            border: "none",
          },
          ".cm-activeLineGutter": { backgroundColor: "transparent", color: "var(--text-secondary)" },
          ".cm-activeLine": { backgroundColor: "rgba(255, 255, 255, 0.035)" },
          "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection": {
            backgroundColor: "rgba(255, 255, 255, 0.14)",
          },
          ".cm-lineNumbers .cm-gutterElement": { padding: "0 12px 0 16px" },
          ".cm-tooltip.cm-tooltip-autocomplete": {
            backgroundColor: "var(--bg-elevated)",
            border: "1px solid var(--border-color)",
            borderRadius: "8px",
            boxShadow: "0 10px 28px rgba(0, 0, 0, 0.45)",
            overflow: "hidden",
            fontFamily: "var(--font-sans, system-ui, sans-serif)",
          },
          ".cm-tooltip-autocomplete > ul": {
            maxHeight: "240px",
            minWidth: "280px",
            maxWidth: "520px",
          },
          ".cm-tooltip-autocomplete > ul > li": {
            padding: "6px 10px",
            display: "flex",
            alignItems: "baseline",
            gap: "8px",
            color: "var(--text-primary)",
          },
          ".cm-tooltip-autocomplete > ul > li[aria-selected]": {
            backgroundColor: "var(--bg-hover)",
            color: "var(--text-primary)",
          },
          ".cm-tooltip-autocomplete .cm-completionLabel": {
            fontFamily: "var(--font-mono, monospace)",
            fontSize: "12.5px",
            color: "var(--syntax-command)",
            flexShrink: 0,
          },
          ".cm-tooltip-autocomplete .cm-completionDetail": {
            fontSize: "12px",
            color: "var(--text-muted)",
            fontStyle: "normal",
            marginLeft: "4px",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          },
        }, { dark: true }),
        EditorView.domEventHandlers({
          mousedown(event: MouseEvent, view: EditorView) {
            const modifier = isMac ? event.metaKey : event.ctrlKey;
            if (!modifier || event.button !== 0 || !pdfAvailableRef.current) return false;
            const pos = view.posAtCoords({ x: event.clientX, y: event.clientY });
            if (pos === null) return false;
            event.preventDefault();
            onForwardSyncRef.current?.(filePath, view.state.doc.lineAt(pos).number);
            return true;
          },
        }),
        EditorView.updateListener.of((update: any) => {
          if (update.docChanged) {
            if (readOnlyRef.current && !programmaticLoadRef.current) return;
            const nextContent = update.state.doc.toString();
            setContent(nextContent);
            if (!programmaticLoadRef.current) {
              if (nextContent !== savedContentRef.current) {
                isDirtyRef.current = true;
                onDirtyChangeRef.current?.(filePath, nextContent);
              } else if (isDirtyRef.current) {
                isDirtyRef.current = false;
                onDirtyChangeRef.current?.(filePath, null);
              }
            }
          }
          if (update.selectionSet || update.docChanged) {
            const head = update.state.selection.main.head;
            const line = update.state.doc.lineAt(head).number;
            setActiveLine(line);

            const { from, to } = update.state.selection.main;
            if (to > from) {
              const text = update.state.sliceDoc(from, to);
              if (text.trim().length > 0) {
                const startLine = update.state.doc.lineAt(from).number;
                const endLine = update.state.doc.lineAt(to).number;
                setSelectedRange({ text, startLine, endLine });
              } else {
                setSelectedRange(null);
              }
            } else {
              setSelectedRange(null);
            }
          }
        }),
      ],
    });
    const view = new EditorView({
      state,
      parent: editorContainerRef.current,
    });
    viewRef.current = view;
    return () => {
      cancelFlash(view);
      view.destroy();
    };
  }, [filePath]);

  // Apply a jump (scroll + select + flash) once, and only after the target file's content is loaded.
  useEffect(() => {
    if (!contentReady || !viewRef.current || cursorLine <= 0) return;
    const jumpKey = `${cursorLine}:${jumpNonce ?? 0}:${snapToHeading ? 1 : 0}`;
    if (appliedJumpRef.current === jumpKey) return;
    appliedJumpRef.current = jumpKey;
    const doc = viewRef.current.state.doc;
    const clampedLine = Math.min(Math.max(1, cursorLine), doc.lines);
    const targetLine = snapToHeading ? findHeadingLineAtOrAboveBy((n) => doc.line(n).text, doc.lines, clampedLine) : clampedLine;
    const lineObj = doc.line(targetLine);
    viewRef.current.dispatch({
      selection: { anchor: lineObj.from, head: lineObj.from },
      effects: EditorView.scrollIntoView(lineObj.from, { y: "start" }),
    });
    if (!isInitialJumpRef.current) flashLine(viewRef.current, targetLine);
    isInitialJumpRef.current = false;
  }, [cursorLine, jumpNonce, snapToHeading, contentReady]);

  const handleSave = async (compileAfter = false) => {
    if (isSaving || !contentReady || effectiveReadOnly) return;
    setIsSaving(true);
    try {
      const activeCsrf = csrfToken || (typeof localStorage !== "undefined" ? localStorage.getItem("lemma_csrf_token") || "" : "");
      const res = await fetch(`/api/projects/${projectId}/files/${filePath}`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": activeCsrf,
        },
        body: JSON.stringify({
          content,
          expected_etag: etag,
        }),
      });

      if (res.status === 409) {
        const conflictData = await res.json();
        setConflictState({
          currentEtag: conflictData.current_etag,
          serverContent: conflictData.server_content,
        });
        return;
      }

      if (res.ok) {
        const data = await res.json();
        setEtag(data.etag);
        savedContentRef.current = content;
        if (isDirtyRef.current) {
          isDirtyRef.current = false;
          onDirtyChangeRef.current?.(filePath, null);
        }
        if (onSaveSuccess) onSaveSuccess(data.etag, content);
        if (compileAfter && onTriggerCompile) onTriggerCompile();
      }
    } finally {
      setIsSaving(false);
    }
  };

  const openSearch = () => {
    if (
      !searchOpen &&
      selectedRange &&
      !selectedRange.text.includes("\n") &&
      selectedRange.text.length <= 120
    ) {
      setSearchQuery(selectedRange.text);
      setSearchMatchIndex(0);
      shouldScrollSearchRef.current = true;
    }
    setSearchOpen(true);
    setSearchFocusNonce((n) => n + 1);
  };

  const closeSearch = () => {
    setSearchOpen(false);
    viewRef.current?.focus();
  };

  const stepSearchMatch = (delta: number) => {
    if (searchMatches.length === 0) return;
    shouldScrollSearchRef.current = true;
    setSearchMatchIndex((idx) => {
      const len = searchMatches.length;
      return (((idx + delta) % len) + len) % len;
    });
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "f") {
      e.preventDefault();
      e.stopPropagation();
      openSearch();
      return;
    }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      handleSave(true);
    }
  };

  const zoteroBannerDescId = "lemma-zotero-sync-banner-desc";

  return (
    <div className="lemma-latex-editor" onKeyDown={handleKeyDown}>
      <div className="lemma-editor-toolbar">
        <span className="lemma-active-filename">{filePath}</span>
        <div className="lemma-editor-actions">
          <button
            type="button"
            className="lemma-btn lemma-btn-secondary lemma-btn-small"
            aria-label="Save"
            disabled={effectiveReadOnly || undefined}
            title={isZoteroSynced ? "Manual editing is locked while this file is Zotero synced" : undefined}
            onClick={() => handleSave(false)}
          >
            Save
          </button>
          <button
            type="button"
            className="lemma-btn lemma-btn-secondary lemma-btn-xs"
            aria-label="Find in editor"
            title="Find in editor (Cmd/Ctrl+F)"
            onClick={openSearch}
          >
            Find
          </button>
          <button
            type="button"
            className="lemma-btn lemma-btn-secondary lemma-btn-xs"
            aria-label="Show in PDF"
            disabled={!pdfAvailable}
            title={pdfAvailable ? "Show in PDF (Cmd/Ctrl+Click)" : "Compile first"}
            onClick={() => {
              if (onForwardSync) {
                onForwardSync(filePath, activeLine);
              }
            }}
          >
            Show in PDF
          </button>
        </div>
      </div>

      {searchOpen && (
        <PaneFindBar
          paneLabel="Editor"
          query={searchQuery}
          onQueryChange={(q) => {
            shouldScrollSearchRef.current = true;
            setSearchQuery(q);
            setSearchMatchIndex(0);
          }}
          caseSensitive={searchCaseSensitive}
          onToggleCaseSensitive={() => {
            shouldScrollSearchRef.current = true;
            setSearchCaseSensitive((c) => !c);
            setSearchMatchIndex(0);
          }}
          matchCount={searchMatches.length}
          activeIndex={activeSearchIndex}
          onNext={() => stepSearchMatch(1)}
          onPrev={() => stepSearchMatch(-1)}
          onClose={closeSearch}
          focusNonce={searchFocusNonce}
          placeholder="Find in file..."
        />
      )}

      {isZoteroSynced && (
        <div
          className="lemma-zotero-sync-banner"
          data-testid="zotero-sync-banner"
        >
          <div className="lemma-zotero-sync-banner-header">
            <span id={zoteroBannerDescId} className="lemma-zotero-sync-banner-text">
              This file is Zotero synced — manual editing is locked.
            </span>
            {zoteroSyncStatus && (
              <span className="lemma-editor-sync-status" role="status" title={zoteroSyncStatus}>
                {zoteroSyncStatus}
              </span>
            )}
          </div>
          <div className="lemma-zotero-sync-banner-actions">
            <button
              type="button"
              className="lemma-btn lemma-btn-secondary lemma-btn-xs"
              disabled={(zoteroSyncBusy || zoteroDisconnectBusy || !onZoteroDisconnect) || undefined}
              onClick={onZoteroDisconnect}
            >
              {zoteroDisconnectBusy ? "Disconnecting…" : "Disconnect Zotero"}
            </button>
            <button
              type="button"
              className="lemma-btn lemma-btn-primary lemma-btn-xs"
              disabled={(zoteroSyncBusy || zoteroDisconnectBusy || Boolean(zoteroSyncDisabledReason) || !onZoteroSync) || undefined}
              title={zoteroSyncDisabledReason || "Pull new and changed entries from the linked Zotero collection"}
              onClick={onZoteroSync}
            >
              {zoteroSyncBusy ? "Syncing…" : "Sync"}
            </button>
          </div>
        </div>
      )}

      <div
        ref={editorContainerRef}
        className="lemma-cm6-host"
        data-testid="codemirror-container"
        data-readonly={effectiveReadOnly ? "true" : undefined}
        role="region"
        aria-label={`LaTeX Source Editor (${filePath})`}
        aria-readonly={effectiveReadOnly ? "true" : undefined}
        aria-describedby={isZoteroSynced ? zoteroBannerDescId : undefined}
      />

      {conflictState && (
        <div
          role="dialog"
          aria-label="ETag Conflict"
          className="lemma-etag-conflict-modal"
        >
          <h3>File Modified on Disk</h3>
          <p>
            This file was modified on disk outside the editor. Reload from disk or review changes before overwriting.
          </p>
          <div className="lemma-conflict-actions">
            <button
              type="button"
              className="lemma-btn lemma-btn-primary"
              aria-label="Reload from Disk"
              onClick={() => {
                const srv = conflictState.serverContent;
                setContent(srv);
                setEtag(conflictState.currentEtag);
                savedContentRef.current = srv;
                if (viewRef.current) {
                  viewRef.current.dispatch({
                    changes: { from: 0, to: viewRef.current.state.doc.length, insert: srv },
                  });
                }
                setConflictState(null);
              }}
            >
              Reload from Disk
            </button>
            <button
              type="button"
              className="lemma-btn lemma-btn-secondary"
              aria-label="Keep Local Draft"
              onClick={() => {
                // Adopt the latest server etag so the user can force save their draft
                setEtag(conflictState.currentEtag);
                setConflictState(null);
              }}
            >
              Keep Local Draft
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default LatexEditor;
