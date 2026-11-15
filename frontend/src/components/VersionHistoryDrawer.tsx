import React, { useCallback, useEffect, useRef, useState } from "react";
import { describeSnapshot, groupByDay } from "../utils/snapshotLabels";
import type { RestorePreviewData, SnapshotItem } from "../utils/snapshotLabels";
import { UnifiedDiffViewer } from "./UnifiedDiffViewer";

export interface VersionHistoryDrawerProps {
  isOpen: boolean;
  projectId: string;
  csrfToken?: string;
  dirtyBuffers: Record<string, string>;
  onClose?: () => void;
  onRestoreCompleted?: (sha: string) => void;
  fetchImpl?: typeof fetch;
  layout?: "drawer" | "page";
}

function formatTime(timestamp: string): string {
  const d = new Date(timestamp);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function VersionHistoryDrawer({
  isOpen,
  projectId,
  csrfToken = "",
  dirtyBuffers,
  onClose,
  onRestoreCompleted,
  fetchImpl = globalThis.fetch,
  layout = "drawer",
}: VersionHistoryDrawerProps) {
  const [snapshots, setSnapshots] = useState<SnapshotItem[]>([]);
  const [selectedSha, setSelectedSha] = useState<string>("");
  const [preview, setPreview] = useState<RestorePreviewData | null>(null);
  const [loadingSnapshots, setLoadingSnapshots] = useState<boolean>(isOpen && Boolean(projectId));
  const [loadingPreview, setLoadingPreview] = useState<boolean>(false);
  const [restoring, setRestoring] = useState<boolean>(false);
  const [confirming, setConfirming] = useState<boolean>(false);
  const [confirmBibLoss, setConfirmBibLoss] = useState<boolean>(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const activeShaRef = useRef<string>("");
  const rootRef = useRef<HTMLDivElement>(null);

  const loadPreview = useCallback(async (sha: string) => {
    activeShaRef.current = sha;
    setLoadingPreview(true);
    setConfirmBibLoss(false);
    setConfirming(false);
    setErrorMessage(null);
    try {
      const res = await fetchImpl(`/api/projects/${projectId}/history/${encodeURIComponent(sha)}/preview`);
      if (activeShaRef.current !== sha) return;
      if (!res.ok) throw new Error(`Failed to load restore preview (${res.status})`);
      const data: RestorePreviewData = await res.json();
      if (activeShaRef.current !== sha) return;
      setPreview(data);
    } catch (err: any) {
      if (activeShaRef.current === sha) {
        setErrorMessage(String(err.message || err));
        setPreview(null);
      }
    } finally {
      if (activeShaRef.current === sha) {
        setLoadingPreview(false);
      }
    }
  }, [fetchImpl, projectId]);

  // Closing discards the session (selection, preview, confirm step, stale list) so reopening starts fresh.
  useEffect(() => {
    if (isOpen) return;
    activeShaRef.current = "";
    setSelectedSha("");
    setPreview(null);
    setLoadingPreview(false);
    setConfirming(false);
    setConfirmBibLoss(false);
    setErrorMessage(null);
    setSnapshots((prev) => (prev.length === 0 ? prev : []));
    setLoadingSnapshots(true);
  }, [isOpen]);

  // Load snapshots when the drawer opens; newest entry is selected by default.
  useEffect(() => {
    if (!isOpen || !projectId) return;

    let active = true;
    setLoadingSnapshots(true);
    setErrorMessage(null);

    fetchImpl(`/api/projects/${projectId}/history`)
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load history (${res.status})`);
        return res.json();
      })
      .then((data) => {
        if (!active) return;
        const list: SnapshotItem[] = data.snapshots || [];
        setSnapshots(list);
        const newest = groupByDay(list)[0]?.items[0];
        if (newest) {
          setSelectedSha(newest.commit_sha);
          loadPreview(newest.commit_sha);
        } else {
          setSelectedSha("");
          setPreview(null);
        }
      })
      .catch((err) => {
        if (active) setErrorMessage(String(err.message || err));
      })
      .finally(() => {
        if (active) setLoadingSnapshots(false);
      });

    return () => {
      active = false;
    };
  }, [isOpen, projectId, loadPreview]);

  // Move focus into the drawer so Escape works without a backdrop.
  useEffect(() => {
    if (isOpen && rootRef.current && typeof rootRef.current.focus === "function") {
      rootRef.current.focus();
    }
  }, [isOpen]);

  const handleSelect = (sha: string) => {
    // Re-clicking the selected row retries a preview that failed to load.
    const retrying = sha === selectedSha && preview === null && !loadingPreview;
    if (restoring || (sha === selectedSha && !retrying)) return;
    setSelectedSha(sha);
    loadPreview(sha);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape" && !restoring && onClose) {
      onClose();
    }
  };

  const handleRestore = async () => {
    if (!selectedSha || restoring) return;
    if (preview?.has_bib_loss_warning && !confirmBibLoss) return;
    setRestoring(true);
    setErrorMessage(null);
    try {
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (csrfToken) {
        headers["X-CSRF-Token"] = csrfToken;
      }
      const res = await fetchImpl(`/api/projects/${projectId}/history/${encodeURIComponent(selectedSha)}/restore`, {
        method: "POST",
        headers,
        body: JSON.stringify({ unsaved_buffers: dirtyBuffers }),
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || `Restore failed (${res.status})`);
      }
      setConfirming(false);
      onRestoreCompleted?.(selectedSha);
      onClose?.();
    } catch (err: any) {
      setErrorMessage(String(err.message || err));
      setConfirming(false);
    } finally {
      setRestoring(false);
    }
  };

  if (!isOpen) return null;

  const groups = groupByDay(snapshots);
  const bibGated = Boolean(preview?.has_bib_loss_warning) && !confirmBibLoss;
  const canRestore = Boolean(selectedSha) && preview !== null && !loadingPreview && !restoring;
  const dirtyCount = Object.keys(dirtyBuffers).length;
  const dirtyLabel = `${dirtyCount} ${dirtyCount === 1 ? "file" : "files"}`;
  const backupNotice =
    dirtyCount > 0
      ? `Your unsaved edits (${dirtyLabel}) are saved in a backup first.`
      : "Your current work is saved in a backup first.";

  return (
    <div
      ref={rootRef}
      role="dialog"
      aria-labelledby="history-drawer-title"
      tabIndex={-1}
      className={layout === "page" ? "lemma-history-drawer lemma-history-page" : "lemma-history-drawer"}
      onKeyDown={handleKeyDown}
    >
      <div className="lemma-history-header">
        <div className="lemma-history-heading">
          <h2 id="history-drawer-title" className="lemma-history-title">
            Version history
          </h2>
          {layout === "page" ? (
            <p className="lemma-history-subtitle">Snapshots of this project. Restoring brings you back to the editor.</p>
          ) : null}
        </div>
        {layout === "page" ? (
          <button
            type="button"
            onClick={onClose}
            disabled={restoring}
            className="lemma-btn lemma-btn-secondary lemma-history-back"
          >
            Back to Editor
          </button>
        ) : (
          <button
            type="button"
            onClick={onClose}
            disabled={restoring}
            className="lemma-history-close"
            aria-label="Close"
          >
            ✕
          </button>
        )}
      </div>

      <div className="lemma-history-body">
        {errorMessage ? (
          <div className="lemma-error-banner" role="alert">
            {errorMessage}
          </div>
        ) : null}

        {loadingSnapshots ? (
          <div className="lemma-empty-state">Loading version history...</div>
        ) : snapshots.length === 0 ? (
          <div className="lemma-empty-state">No version snapshots found for this project yet.</div>
        ) : (
          <>
            <div className="lemma-history-list">
              {groups.map((group) => (
                <div key={group.dayLabel} className="lemma-history-group">
                  <h3 className="lemma-history-day">{group.dayLabel}</h3>
                  {group.items.map((item) => {
                    const { label, detail } = describeSnapshot(item);
                    const selected = item.commit_sha === selectedSha;
                    return (
                      <button
                        key={item.commit_sha}
                        type="button"
                        className="lemma-history-row"
                        aria-current={selected ? "true" : undefined}
                        disabled={restoring}
                        onClick={() => handleSelect(item.commit_sha)}
                      >
                        <span className="lemma-history-row-label">{label}</span>
                        <span className="lemma-history-row-time">{formatTime(item.timestamp)}</span>
                        {detail ? (
                          <span className="lemma-history-row-detail" title={detail}>
                            {detail}
                          </span>
                        ) : null}
                      </button>
                    );
                  })}
                </div>
              ))}
            </div>

            {preview?.has_bib_loss_warning ? (
              <div className="lemma-bib-loss-warning">
                <div className="lemma-bib-loss-header">
                  <span className="lemma-bib-loss-icon" aria-hidden="true">⚠️</span>
                  <div>
                    <h4 className="lemma-bib-loss-title">
                      Warning: Restoring this version will remove bibliography entries
                    </h4>
                    <p className="lemma-bib-loss-desc">
                      The target version does not contain the following citation keys present in your
                      current references.bib:
                    </p>
                  </div>
                </div>
                <div className="lemma-bib-keys-list">
                  {preview.lost_bib_keys.map((k) => (
                    <span key={k} className="lemma-bib-key-tag">
                      {k}
                    </span>
                  ))}
                </div>
                <div className="lemma-bib-confirm-row">
                  <label htmlFor="history-confirm-bib-loss" className="lemma-checkbox-label">
                    <input
                      id="history-confirm-bib-loss"
                      type="checkbox"
                      checked={confirmBibLoss}
                      onChange={(e) => setConfirmBibLoss(e.target.checked)}
                    />
                    <span>I confirm that references.bib entries will be removed</span>
                  </label>
                </div>
              </div>
            ) : null}

            <div className="lemma-timeline-diff-section">
              {preview ? (
                <div className="lemma-timeline-files-changed">
                  Changed files ({preview.changed_files.length}):{" "}
                  <span className="lemma-timeline-files-list">
                    {preview.changed_files.join(", ") || "None"}
                  </span>
                </div>
              ) : null}
              <div className="lemma-timeline-diff-box">
                {loadingPreview ? (
                  <div className="lemma-timeline-diff-placeholder">Loading diff preview...</div>
                ) : preview?.unified_diff ? (
                  <UnifiedDiffViewer unifiedDiff={preview.unified_diff} />
                ) : (
                  <div className="lemma-timeline-diff-placeholder">No differences from current workspace.</div>
                )}
              </div>
            </div>
          </>
        )}
      </div>

      {snapshots.length > 0 ? (
        <div className="lemma-history-footer">
          {confirming ? (
            <div className="lemma-history-confirm">
              <p className="lemma-history-confirm-text">
                Restore this version? {backupNotice}
              </p>
              <div className="lemma-history-confirm-actions">
                <button
                  type="button"
                  className="lemma-btn lemma-btn-secondary"
                  disabled={restoring}
                  onClick={() => setConfirming(false)}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="lemma-btn lemma-btn-primary"
                  disabled={restoring || bibGated}
                  onClick={handleRestore}
                >
                  {restoring ? "Restoring..." : "Confirm"}
                </button>
              </div>
            </div>
          ) : (
            <button
              type="button"
              className="lemma-btn lemma-btn-primary"
              disabled={!canRestore}
              onClick={() => setConfirming(true)}
            >
              Restore this version
            </button>
          )}
        </div>
      ) : null}
    </div>
  );
}
