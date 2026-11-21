import React, { useCallback, useEffect, useRef, useState } from "react";

export interface AuditLogEntryItem {
  entry_id: string;
  timestamp: string;
  source_surface: string;
  provider: string;
  model_id: string;
  model_display_name: string;
  tier: number;
  category: string;
  purpose: string;
  affected_files: string[];
  section_hint: string;
  prompt_summary: string;
  action_summary: string;
  outcome: string;
  human_verified: boolean;
  verification_note: string;
  chat_id?: string | null;
  message_id?: string | null;
  hunk_id?: string | null;
  flags?: Record<string, boolean>;
}

export interface AiDisclosureLogDrawerProps {
  isOpen: boolean;
  projectId: string;
  csrfToken: string;
  onClose: () => void;
  onEntriesCountChange?: (count: number) => void;
  onDisclosureFileWritten?: (path: string) => void;
}

function formatTimestamp(iso: string): string {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

export function AiDisclosureLogDrawer({
  isOpen,
  projectId,
  csrfToken,
  onClose,
  onEntriesCountChange,
  onDisclosureFileWritten,
}: AiDisclosureLogDrawerProps): React.ReactElement | null {
  const [entries, setEntries] = useState<AuditLogEntryItem[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [writingFile, setWritingFile] = useState<boolean>(false);
  const [copyingLatex, setCopyingLatex] = useState<boolean>(false);
  const [submittingManual, setSubmittingManual] = useState<boolean>(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [showAddForm, setShowAddForm] = useState<boolean>(false);

  const onEntriesCountChangeRef = useRef(onEntriesCountChange);
  onEntriesCountChangeRef.current = onEntriesCountChange;
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  // Manual entry form state
  const [manualModel, setManualModel] = useState<string>("");
  const [manualPurpose, setManualPurpose] = useState<string>("");
  const [manualFiles, setManualFiles] = useState<string>("");
  const [manualSection, setManualSection] = useState<string>("");
  const [manualVerified, setManualVerified] = useState<boolean>(true);
  const [manualNote, setManualNote] = useState<string>("");

  const resolveCsrf = () =>
    csrfToken ||
    (typeof localStorage !== "undefined" ? localStorage.getItem("lemma_csrf_token") || "" : "");

  const loadLog = useCallback(async (signal?: AbortSignal) => {
    if (!projectId) return;
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/projects/${projectId}/integrity/log`, { signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as { entries?: AuditLogEntryItem[] };
      if (signal?.aborted) return;
      const list = Array.isArray(data.entries) ? data.entries : [];
      setEntries(list);
      onEntriesCountChangeRef.current?.(list.length);
    } catch (err) {
      if (signal?.aborted || (err instanceof DOMException && err.name === "AbortError")) return;
      setError("Could not load AI Disclosure Log.");
    } finally {
      if (!signal?.aborted) {
        setLoading(false);
      }
    }
  }, [projectId]);

  useEffect(() => {
    if (!isOpen) return;
    setStatusMessage(null);
    const controller = new AbortController();
    void loadLog(controller.signal);
    return () => {
      controller.abort();
    };
  }, [isOpen, loadLog]);

  useEffect(() => {
    if (!isOpen || typeof document === "undefined") return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCloseRef.current();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [isOpen]);

  if (!isOpen) return null;

  const verifiedCount = entries.filter((e) => e.human_verified).length;
  const pendingCount = entries.length - verifiedCount;

  const handlePatchEntry = async (
    entryId: string,
    patch: Partial<Pick<AuditLogEntryItem, "purpose" | "section_hint" | "human_verified" | "verification_note">>
  ): Promise<boolean> => {
    setError(null);
    try {
      const res = await fetch(`/api/projects/${projectId}/integrity/log/${entryId}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": resolveCsrf(),
        },
        body: JSON.stringify(patch),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const updated = (await res.json()) as AuditLogEntryItem;
      setEntries((prev) => prev.map((e) => (e.entry_id === entryId ? updated : e)));
      return true;
    } catch {
      setError("Could not save changes to log entry.");
      return false;
    }
  };

  const handleDeleteEntry = async (entryId: string) => {
    setError(null);
    setDeletingId(entryId);
    try {
      const res = await fetch(`/api/projects/${projectId}/integrity/log/${entryId}`, {
        method: "DELETE",
        headers: { "X-CSRF-Token": resolveCsrf() },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setEntries((prev) => {
        const next = prev.filter((e) => e.entry_id !== entryId);
        queueMicrotask(() => onEntriesCountChangeRef.current?.(next.length));
        return next;
      });
    } catch {
      setError("Could not delete log entry.");
    } finally {
      setDeletingId((curr) => (curr === entryId ? null : curr));
    }
  };

  const handleAddManual = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submittingManual || !manualModel.trim() || !manualPurpose.trim()) return;
    setError(null);
    setSubmittingManual(true);
    try {
      const res = await fetch(`/api/projects/${projectId}/integrity/log`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": resolveCsrf(),
        },
        body: JSON.stringify({
          source_surface: "manual",
          provider: "manual",
          model_id: manualModel.trim(),
          model_display_name: manualModel.trim(),
          tier: 2,
          category: "manual_disclosure",
          purpose: manualPurpose.trim(),
          affected_files: manualFiles
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean),
          section_hint: manualSection.trim(),
          outcome: "manual",
          human_verified: manualVerified,
          verification_note: manualNote.trim(),
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const created = (await res.json()) as AuditLogEntryItem;
      setEntries((prev) => {
        const next = [...prev, created];
        queueMicrotask(() => onEntriesCountChangeRef.current?.(next.length));
        return next;
      });
      setManualModel("");
      setManualPurpose("");
      setManualFiles("");
      setManualSection("");
      setManualVerified(true);
      setManualNote("");
      setShowAddForm(false);
    } catch {
      setError("Could not add manual entry.");
    } finally {
      setSubmittingManual(false);
    }
  };

  const handleCopyLatex = async () => {
    setCopyingLatex(true);
    setError(null);
    setStatusMessage(null);
    try {
      const res = await fetch(`/api/projects/${projectId}/integrity/appendix/preview`, {
        method: "POST",
        headers: { "X-CSRF-Token": resolveCsrf() },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as { latex?: string };
      if (!data.latex) {
        setError("No LaTeX disclosure content returned.");
        return;
      }
      if (typeof navigator === "undefined" || !navigator.clipboard) {
        setError("Clipboard API is unavailable in this browser context.");
        return;
      }
      await navigator.clipboard.writeText(data.latex);
      setStatusMessage("Copied LaTeX AI Disclosure snippet to clipboard.");
    } catch {
      setError("Could not copy LaTeX disclosure snippet.");
    } finally {
      setCopyingLatex(false);
    }
  };

  const handleWriteDisclosureFile = async () => {
    setWritingFile(true);
    setError(null);
    setStatusMessage(null);
    try {
      const res = await fetch(`/api/projects/${projectId}/integrity/appendix/write`, {
        method: "POST",
        headers: { "X-CSRF-Token": resolveCsrf() },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as { path?: string };
      const writtenPath = data.path || "ai_disclosure.tex";
      onDisclosureFileWritten?.(writtenPath);
      setStatusMessage(
        `Saved ${writtenPath} in the project root (not linked to your main .tex — add \\input{ai_disclosure} wherever you prefer).`
      );
    } catch {
      setError("Could not write ai_disclosure.tex.");
    } finally {
      setWritingFile(false);
    }
  };

  return (
    <div
      className="lemma-modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label="AI Disclosure Log"
      data-testid="ai-disclosure-log-drawer"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="lemma-modal-card lemma-disclosure-modal">
        <div className="lemma-disclosure-header">
          <div>
            <h2>AI Disclosure Log</h2>
            <p className="lemma-disclosure-subtitle">
              Reproducibility &amp; disclosure trail stored in <code>.ai-audit-log.json</code> ({entries.length}{" "}
              {entries.length === 1 ? "entry" : "entries"}, {pendingCount} pending human verification).
            </p>
          </div>
          <button type="button" aria-label="Close" className="lemma-modal-close" onClick={onClose}>
            ×
          </button>
        </div>

        <div className="lemma-disclosure-toolbar">
          <button
            type="button"
            className="lemma-btn lemma-btn-secondary lemma-btn-xs"
            onClick={() => setShowAddForm((v) => !v)}
          >
            {showAddForm ? "Cancel Manual Entry" : "+ Add Manual Entry"}
          </button>
          <div className="lemma-disclosure-toolbar-right">
            <button
              type="button"
              className="lemma-btn lemma-btn-secondary lemma-btn-xs"
              disabled={copyingLatex}
              onClick={handleCopyLatex}
            >
              {copyingLatex ? "Copying…" : "Copy LaTeX"}
            </button>
            <button
              type="button"
              className="lemma-btn lemma-btn-primary lemma-btn-xs"
              disabled={writingFile}
              data-testid="write-ai-disclosure-btn"
              onClick={handleWriteDisclosureFile}
            >
              {writingFile ? "Generating…" : "Generate / Update ai_disclosure.tex"}
            </button>
          </div>
        </div>

        {error && (
          <div className="lemma-integrity-alert lemma-integrity-alert-error" role="alert">
            {error}
          </div>
        )}
        {statusMessage && (
          <div className="lemma-integrity-alert lemma-integrity-alert-success" role="status">
            {statusMessage}
          </div>
        )}

        {showAddForm && (
          <form className="lemma-disclosure-add-form" onSubmit={handleAddManual}>
            <div className="lemma-disclosure-form-row">
              <label>
                Tool / Model
                <input
                  type="text"
                  required
                  placeholder="e.g. Claude 3.7 Sonnet or DeepL Write"
                  value={manualModel}
                  onChange={(e) => setManualModel(e.target.value)}
                />
              </label>
              <label>
                Purpose
                <input
                  type="text"
                  required
                  placeholder="e.g. Language polishing of Section 2"
                  value={manualPurpose}
                  onChange={(e) => setManualPurpose(e.target.value)}
                />
              </label>
            </div>
            <div className="lemma-disclosure-form-row">
              <label>
                Affected Files (comma-separated)
                <input
                  type="text"
                  placeholder="e.g. chapters/intro.tex"
                  value={manualFiles}
                  onChange={(e) => setManualFiles(e.target.value)}
                />
              </label>
              <label>
                Section Hint
                <input
                  type="text"
                  placeholder="e.g. Section 2.1"
                  value={manualSection}
                  onChange={(e) => setManualSection(e.target.value)}
                />
              </label>
            </div>
            <div className="lemma-disclosure-form-row">
              <label className="lemma-disclosure-checkbox-label">
                <input
                  type="checkbox"
                  checked={manualVerified}
                  onChange={(e) => setManualVerified(e.target.checked)}
                />
                Human verified
              </label>
              <input
                type="text"
                placeholder="Verification note (optional)"
                value={manualNote}
                onChange={(e) => setManualNote(e.target.value)}
              />
              <button type="submit" className="lemma-btn lemma-btn-primary lemma-btn-xs">
                Add Entry
              </button>
            </div>
          </form>
        )}

        <div className="lemma-disclosure-list">
          {loading ? (
            <p className="lemma-disclosure-empty">Loading disclosure log…</p>
          ) : entries.length === 0 ? (
            <p className="lemma-disclosure-empty">
              No substantive AI interactions logged yet. When Academic Integrity Mode is ON, accepted Tier 2+ edits
              are logged automatically, or you can add entries manually above.
            </p>
          ) : (
            entries
              .slice()
              .reverse()
              .map((entry) => (
                <AuditEntryCard
                  key={entry.entry_id}
                  entry={entry}
                  deleting={deletingId === entry.entry_id}
                  onPatch={handlePatchEntry}
                  onDelete={handleDeleteEntry}
                />
              ))
          )}
        </div>
      </div>
    </div>
  );
}

interface AuditEntryCardProps {
  entry: AuditLogEntryItem;
  deleting: boolean;
  onPatch: (
    entryId: string,
    patch: Partial<Pick<AuditLogEntryItem, "purpose" | "section_hint" | "human_verified" | "verification_note">>
  ) => Promise<boolean>;
  onDelete: (entryId: string) => void;
}

function AuditEntryCard({
  entry,
  deleting,
  onPatch,
  onDelete,
}: AuditEntryCardProps): React.ReactElement {
  const [purpose, setPurpose] = useState(entry.purpose);
  const [sectionHint, setSectionHint] = useState(entry.section_hint);
  const [verificationNote, setVerificationNote] = useState(entry.verification_note);

  useEffect(() => {
    setPurpose(entry.purpose);
    setSectionHint(entry.section_hint);
    setVerificationNote(entry.verification_note);
  }, [entry.purpose, entry.section_hint, entry.verification_note]);

  const commitField = async (
    field: "purpose" | "section_hint" | "verification_note",
    draft: string,
    serverVal: string,
    revert: (v: string) => void
  ) => {
    const trimmed = draft.trim();
    if (trimmed === serverVal) return;
    const ok = await onPatch(entry.entry_id, { [field]: trimmed });
    if (!ok) revert(serverVal);
  };

  return (
    <div className="lemma-disclosure-card">
      <div className="lemma-disclosure-card-top">
        <div className="lemma-disclosure-card-meta">
          <span className={`lemma-tier-pill tier-${entry.tier}`}>Tier {entry.tier}</span>
          <strong>{entry.model_display_name || entry.model_id || "Unspecified model"}</strong>
          <span className="lemma-provider-pill">{entry.provider}</span>
          <span className="lemma-disclosure-time">{formatTimestamp(entry.timestamp)}</span>
        </div>
        <button
          type="button"
          className="lemma-btn lemma-btn-secondary lemma-btn-xs"
          aria-label={`Delete log entry ${entry.entry_id}`}
          disabled={deleting}
          onClick={() => onDelete(entry.entry_id)}
        >
          {deleting ? "Deleting…" : "Delete"}
        </button>
      </div>

      <div className="lemma-disclosure-card-fields">
        <label>
          Purpose
          <input
            type="text"
            value={purpose}
            onChange={(e) => setPurpose(e.target.value)}
            onBlur={() => void commitField("purpose", purpose, entry.purpose, setPurpose)}
            onKeyDown={(e) => {
              if (e.key === "Enter") (e.target as HTMLInputElement).blur();
            }}
          />
        </label>
        <label>
          Section
          <input
            type="text"
            value={sectionHint}
            placeholder="Section hint"
            onChange={(e) => setSectionHint(e.target.value)}
            onBlur={() =>
              void commitField("section_hint", sectionHint, entry.section_hint, setSectionHint)
            }
            onKeyDown={(e) => {
              if (e.key === "Enter") (e.target as HTMLInputElement).blur();
            }}
          />
        </label>
      </div>

      {(entry.affected_files.length > 0 || entry.action_summary) && (
        <div className="lemma-disclosure-card-summary">
          {entry.affected_files.length > 0 && (
            <span>
              Files: <code>{entry.affected_files.join(", ")}</code>
            </span>
          )}
          {entry.action_summary && <span>{entry.action_summary}</span>}
        </div>
      )}

      <div className="lemma-disclosure-card-verify">
        <label className="lemma-disclosure-checkbox-label">
          <input
            type="checkbox"
            checked={entry.human_verified}
            onChange={(e) => void onPatch(entry.entry_id, { human_verified: e.target.checked })}
          />
          Human Verified
        </label>
        <input
          type="text"
          value={verificationNote}
          placeholder="Verification strategy / note (e.g. checked citations against Crossref)"
          onChange={(e) => setVerificationNote(e.target.value)}
          onBlur={() =>
            void commitField(
              "verification_note",
              verificationNote,
              entry.verification_note,
              setVerificationNote
            )
          }
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          }}
        />
      </div>
    </div>
  );
}
