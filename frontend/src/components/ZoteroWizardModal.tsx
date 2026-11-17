import React, { useEffect, useRef, useState } from "react";

export interface ZoteroCollectionOption {
  key: string;
  name: string;
  num_items: number;
  injection_warning?: boolean;
}

export interface ZoteroSyncSummary {
  collection_key: string;
  synced_bib_count: number;
  synced_pdf_count: number;
  key_remapping: Record<string, string>;
  injection_warnings: string[];
  etag: string;
}

export interface ZoteroProjectStatus {
  connected: boolean;
  collection_key: string | null;
  has_credentials: boolean;
  key_name?: string;
  library_id: string;
  library_type: "user" | "group";
  push_enabled: boolean;
}

interface NamedKeyOption {
  name: string;
}

export interface ZoteroWizardModalProps {
  isOpen: boolean;
  projectId: string;
  csrfToken?: string;
  onClose?: () => void;
  onSyncCompleted?: (summary: ZoteroSyncSummary) => void;
  onDisconnected?: () => void;
  fetchImpl?: typeof fetch;
}

type ZoteroView = "connected" | "credentials" | "collection";

export function ZoteroWizardModal({
  isOpen,
  projectId,
  csrfToken = "",
  onClose,
  onSyncCompleted,
  onDisconnected,
  fetchImpl = globalThis.fetch,
}: ZoteroWizardModalProps) {
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const [view, setView] = useState<ZoteroView>("credentials");
  const [status, setStatus] = useState<ZoteroProjectStatus | null>(null);

  const [selectedKeyName, setSelectedKeyName] = useState<string>("");
  const [zoteroKeys, setZoteroKeys] = useState<NamedKeyOption[]>([]);
  const [libraryId, setLibraryId] = useState<string>("");
  const [libraryType, setLibraryType] = useState<"user" | "group">("user");
  const [pushEnabled, setPushEnabled] = useState<boolean>(false);

  const [collections, setCollections] = useState<ZoteroCollectionOption[]>([]);
  const [selectedCollectionKey, setSelectedCollectionKey] = useState<string>("");
  const [hasWriteScope, setHasWriteScope] = useState<boolean>(false);
  const [warningMessage, setWarningMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [syncSummary, setSyncSummary] = useState<ZoteroSyncSummary | null>(null);

  useEffect(() => {
    const el = dialogRef.current;
    if (!el) return;
    if (isOpen) {
      if (typeof el.showModal === "function" && !el.open) {
        try {
          el.showModal();
        } catch {
          // Ignore in non-browser test environments
        }
      }
    } else if (typeof el.close === "function" && el.open) {
      el.close();
    }
  }, [isOpen]);

  const loadCollections = async (
    payload: {
      key_name?: string;
      project_id?: string;
      api_key?: string;
      library_id?: string;
      library_type?: "user" | "group";
      push_enabled?: boolean;
    },
    preferredKey?: string | null,
  ): Promise<boolean> => {
    setErrorMessage(null);
    setIsLoading(true);
    try {
      const resp = await fetchImpl("/api/zotero/verify-key", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify(payload),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || !data.valid) {
        setErrorMessage(
          data.warning_message || data.detail || "Failed to verify Zotero API key."
        );
        return false;
      }
      const colls: ZoteroCollectionOption[] = Array.isArray(data.collections)
        ? data.collections
        : [];
      setCollections(colls);
      if (preferredKey && colls.some((c) => c.key === preferredKey)) {
        setSelectedCollectionKey(preferredKey);
      } else if (colls.length > 0) {
        setSelectedCollectionKey((prev) => (prev && colls.some((c) => c.key === prev) ? prev : colls[0].key));
      } else {
        setSelectedCollectionKey("root");
      }
      setHasWriteScope(Boolean(data.has_write_scope));
      setWarningMessage(data.warning_message || null);
      return true;
    } catch (err) {
      setErrorMessage(
        err instanceof Error ? err.message : "Network error verifying Zotero key."
      );
      return false;
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    if (!isOpen) {
      setSelectedKeyName("");
      setErrorMessage(null);
      setSyncSummary(null);
      return;
    }
    let active = true;
    setErrorMessage(null);
    setSyncSummary(null);
    setWarningMessage(null);

    Promise.all([
      fetchImpl(`/api/projects/${projectId}/zotero`).then(async (res) => {
        if (!res.ok) return null;
        return (await res.json()) as ZoteroProjectStatus;
      }),
      fetchImpl("/api/settings/keys")
        .then(async (res) => (res.ok ? res.json() : null))
        .catch(() => null),
    ])
      .then(async ([data, keysData]) => {
        if (!active) return;
        const loadedKeys: NamedKeyOption[] = Array.isArray(keysData?.zotero)
          ? keysData.zotero
          : [];
        if (data?.key_name && !loadedKeys.some((k) => k.name === data.key_name)) {
          loadedKeys.push({ name: data.key_name });
        }
        setZoteroKeys(loadedKeys);
        if (!data) {
          setStatus(null);
          setSelectedKeyName(loadedKeys.length > 0 ? loadedKeys[0].name : "");
          setView("credentials");
          return;
        }
        setStatus(data);
        setSelectedKeyName(
          data.key_name || (loadedKeys.length > 0 ? loadedKeys[0].name : "")
        );
        setLibraryId(data.library_id || "");
        setLibraryType(data.library_type === "group" ? "group" : "user");
        setPushEnabled(Boolean(data.push_enabled));

        if (data.connected) {
          setSelectedCollectionKey(data.collection_key || "");
          setView("connected");
        } else {
          setView("credentials");
        }
      })
      .catch(() => {
        if (!active) return;
        setStatus(null);
        setView("credentials");
      });

    return () => {
      active = false;
    };
  }, [isOpen, projectId, fetchImpl]);

  if (!isOpen) {
    return null;
  }

  const handleVerifyKey = async () => {
    setErrorMessage(null);
    const hasSavedCreds = Boolean(status?.has_credentials);
    if (!hasSavedCreds && (!selectedKeyName.trim() || !libraryId.trim())) {
      setErrorMessage("Please select a Zotero key and enter your Library ID.");
      return;
    }
    if (hasSavedCreds && !libraryId.trim() && !selectedKeyName.trim()) {
      // Allow reusing saved credentials
    } else if (!libraryId.trim()) {
      setErrorMessage("Please enter your Zotero Library ID.");
      return;
    }

    const payload: {
      key_name?: string;
      project_id: string;
      library_id?: string;
      library_type: "user" | "group";
      push_enabled: boolean;
    } = {
      project_id: projectId,
      library_type: libraryType,
      push_enabled: pushEnabled,
    };
    if (selectedKeyName.trim()) payload.key_name = selectedKeyName.trim();
    if (libraryId.trim()) payload.library_id = libraryId.trim();

    const ok = await loadCollections(payload, status?.collection_key);
    if (ok) {
      setStatus((prev) =>
        prev
          ? {
              ...prev,
              has_credentials: true,
              key_name: selectedKeyName.trim() || prev.key_name,
              library_id: libraryId.trim() || prev.library_id,
              library_type: libraryType,
              push_enabled: pushEnabled,
            }
          : {
              connected: false,
              collection_key: null,
              has_credentials: true,
              key_name: selectedKeyName.trim() || undefined,
              library_id: libraryId.trim(),
              library_type: libraryType,
              push_enabled: pushEnabled,
            }
      );
      setView("collection");
    }
  };

  const handleSync = async (collectionKeyOverride?: string) => {
    setErrorMessage(null);
    setIsLoading(true);
    try {
      const targetCollection = collectionKeyOverride ?? selectedCollectionKey;
      const syncBody: Record<string, string> = {};
      if (targetCollection) syncBody.collection_key = targetCollection;
      if (selectedKeyName.trim()) syncBody.key_name = selectedKeyName.trim();
      const resp = await fetchImpl(`/api/projects/${projectId}/zotero/sync`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify(syncBody),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        setErrorMessage(data.detail || "Zotero sync failed.");
        return;
      }
      const summary: ZoteroSyncSummary = {
        collection_key: String(data.collection_key || targetCollection || ""),
        synced_bib_count: Number(data.synced_bib_count || 0),
        synced_pdf_count: Number(data.synced_pdf_count || 0),
        key_remapping: data.key_remapping || {},
        injection_warnings: Array.isArray(data.injection_warnings)
          ? data.injection_warnings
          : [],
        etag: String(data.etag || ""),
      };
      setSyncSummary(summary);
      setStatus((prev) =>
        prev
          ? { ...prev, connected: true, collection_key: summary.collection_key }
          : {
              connected: true,
              collection_key: summary.collection_key,
              has_credentials: true,
              library_id: libraryId.trim(),
              library_type: libraryType,
              push_enabled: pushEnabled,
            }
      );
      if (onSyncCompleted) {
        onSyncCompleted(summary);
      }
    } catch (err) {
      setErrorMessage(
        err instanceof Error ? err.message : "Network error running Zotero sync."
      );
    } finally {
      setIsLoading(false);
    }
  };

  const handleDisconnect = async () => {
    setErrorMessage(null);
    setSyncSummary(null);
    setIsLoading(true);
    try {
      const resp = await fetchImpl(`/api/projects/${projectId}/zotero/disconnect`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({}),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        setErrorMessage(data.detail || "Could not disconnect Zotero.");
        return;
      }
      setStatus((prev) => (prev ? { ...prev, connected: false, collection_key: null } : null));
      onDisconnected?.();
      setView("credentials");
    } catch (err) {
      setErrorMessage(
        err instanceof Error ? err.message : "Network error disconnecting Zotero."
      );
    } finally {
      setIsLoading(false);
    }
  };

  const isConnected = Boolean(status?.connected);
  const matchedCollection = collections.find((c) => c.key === (status?.collection_key || selectedCollectionKey));
  const collectionDisplay = matchedCollection
    ? `${matchedCollection.name} (${matchedCollection.key})`
    : status?.collection_key === "root"
      ? "Entire library (root)"
      : status?.collection_key || "Not set";

  const titleText =
    view === "connected"
      ? "Zotero"
      : view === "credentials"
        ? isConnected
          ? "Edit Zotero Credentials"
          : "Connect Zotero Library — Step 1 of 2"
        : isConnected
          ? "Change Zotero Collection"
          : "Connect Zotero Library — Step 2 of 2";

  return (
    <>
      <div className="lemma-modal-backdrop" />
      <dialog
        ref={dialogRef}
        open
        aria-labelledby="zotero-modal-title"
        data-testid="zotero-wizard-modal"
        className="lemma-zotero-dialog"
        onCancel={(event) => {
          event.preventDefault();
          onClose?.();
        }}
      >
        <div className="lemma-zotero-header">
          <h3 id="zotero-modal-title">{titleText}</h3>
          {onClose ? (
            <button type="button" className="lemma-modal-close" onClick={onClose} aria-label="Close">
              ×
            </button>
          ) : null}
        </div>

        {errorMessage ? (
          <div role="alert" aria-live="polite" className="lemma-error-banner">
            {errorMessage}
          </div>
        ) : null}

        {view === "connected" ? (
          <div className="lemma-zotero-body" data-testid="zotero-connected-view">
            <div className="lemma-connector-card">
              <div className="lemma-connector-card-header">
                <span className="lemma-status-pill is-connected">
                  <span className="lemma-status-dot" aria-hidden="true" />
                  Connected
                </span>
                <button
                  type="button"
                  className="lemma-btn lemma-btn-secondary lemma-btn-xs"
                  disabled={isLoading}
                  onClick={async () => {
                    setSyncSummary(null);
                    const ok = await loadCollections(
                      {
                        project_id: projectId,
                        ...(selectedKeyName ? { key_name: selectedKeyName } : {}),
                      },
                      status?.collection_key
                    );
                    if (ok) setView("collection");
                  }}
                >
                  Change collection
                </button>
              </div>
              <dl className="lemma-connector-meta">
                <div>
                  <dt>Collection</dt>
                  <dd><code>{collectionDisplay}</code></dd>
                </div>
                <div>
                  <dt>Library</dt>
                  <dd>
                    {status?.library_type === "group" ? "Group" : "Personal"} (<code>{status?.library_id || "—"}</code>)
                  </dd>
                </div>
                <div>
                  <dt>Push citations</dt>
                  <dd>{status?.push_enabled ? "Create-only enabled" : "Read-only (off)"}</dd>
                </div>
              </dl>
            </div>

            <p className="lemma-zotero-note">
              Your <code>references.bib</code> is locked to stay in sync with Zotero. Use the <strong>Sync</strong> button in the <code>references.bib</code> banner to pull updates anytime.
            </p>

            {syncSummary ? (
              <div className="lemma-zotero-summary" role="status" data-testid="zotero-sync-summary">
                <div><strong>Sync Complete!</strong></div>
                <div>
                  Imported {syncSummary.synced_bib_count} BibTeX references and{" "}
                  {syncSummary.synced_pdf_count} full-text PDFs.
                </div>
              </div>
            ) : null}

            <div className="lemma-modal-actions lemma-github-actions">
              <button
                type="button"
                className="lemma-btn lemma-btn-danger"
                disabled={isLoading}
                onClick={handleDisconnect}
              >
                Disconnect
              </button>
              <div className="lemma-github-action-gap">
                <button
                  type="button"
                  className="lemma-btn lemma-btn-secondary"
                  disabled={isLoading}
                  onClick={() => {
                    setSyncSummary(null);
                    setErrorMessage(null);
                    setView("credentials");
                  }}
                >
                  Edit credentials
                </button>
                <button
                  type="button"
                  className="lemma-btn lemma-btn-primary"
                  disabled={isLoading}
                  onClick={() => handleSync(status?.collection_key || undefined)}
                >
                  {isLoading ? "Syncing..." : "Sync now"}
                </button>
              </div>
            </div>
          </div>
        ) : null}

        {view === "credentials" ? (
          <form
            className="lemma-zotero-body"
            data-testid="zotero-wizard-step-1"
            onSubmit={(event) => {
              event.preventDefault();
              void handleVerifyKey();
            }}
          >
            <p className="lemma-zotero-help">
              <strong>Minimum Scope Guide:</strong> Open Zotero Settings → Feeds/API → Create
              new private key. Grant <strong>Allow library access (Read-Only recommended)</strong>.
              Existing Zotero items are never modified or deleted.
            </p>

            <div className="lemma-project-create-field">
              <div className="lemma-field-label-row">
                <label htmlFor="zotero-key-select">Zotero API Key</label>
              </div>
              <select
                id="zotero-key-select"
                className="lemma-project-create-input"
                aria-label="Zotero API Key"
                value={selectedKeyName}
                onChange={(e) => setSelectedKeyName(e.target.value)}
              >
                {zoteroKeys.length === 0 ? (
                  <option value="">
                    {status?.has_credentials
                      ? "Saved Zotero key"
                      : "No Zotero keys configured in Settings"}
                  </option>
                ) : (
                  <>
                    <option value="">Select a Zotero key</option>
                    {zoteroKeys.map((k) => (
                      <option key={k.name} value={k.name}>
                        {k.name}
                      </option>
                    ))}
                  </>
                )}
              </select>
            </div>

            <div className="lemma-zotero-split">
              <div className="lemma-project-create-field">
                <label htmlFor="zotero-library-id-input">Zotero Library ID</label>
                <input
                  id="zotero-library-id-input"
                  className="lemma-project-create-input"
                  aria-label="Zotero Library ID"
                  type="text"
                  value={libraryId}
                  onChange={(e) => setLibraryId(e.target.value)}
                  placeholder="e.g. 123456"
                />
              </div>

              <div className="lemma-project-create-field lemma-zotero-type">
                <label htmlFor="zotero-library-type-select">Library Type</label>
                <select
                  id="zotero-library-type-select"
                  className="lemma-project-create-input"
                  aria-label="Library Type"
                  value={libraryType}
                  onChange={(e) =>
                    setLibraryType(e.target.value === "group" ? "group" : "user")
                  }
                >
                  <option value="user">Personal Library (user)</option>
                  <option value="group">Group Library (group)</option>
                </select>
              </div>
            </div>

            <label htmlFor="zotero-push-toggle" className="lemma-checkbox-label">
              <input
                id="zotero-push-toggle"
                aria-label="Push newly accepted citations to Zotero"
                type="checkbox"
                checked={pushEnabled}
                onChange={(e) => setPushEnabled(Boolean(e.target.checked))}
              />
              <span>Push newly accepted citations to Zotero (create-only, off by default)</span>
            </label>

            <div className="lemma-modal-actions lemma-zotero-actions-split">
              {isConnected ? (
                <button
                  type="button"
                  className="lemma-btn lemma-btn-secondary"
                  disabled={isLoading}
                  onClick={() => {
                    setErrorMessage(null);
                    setView("connected");
                  }}
                >
                  Back
                </button>
              ) : onClose ? (
                <button
                  type="button"
                  className="lemma-btn lemma-btn-secondary"
                  disabled={isLoading}
                  onClick={onClose}
                >
                  Cancel
                </button>
              ) : (
                <span />
              )}
              <button
                type="submit"
                className="lemma-btn lemma-btn-primary"
                disabled={isLoading}
              >
                {isLoading ? "Verifying..." : "Verify Key & Continue"}
              </button>
            </div>
          </form>
        ) : null}

        {view === "collection" ? (
          <div className="lemma-zotero-body" data-testid="zotero-wizard-step-2">
            <p className="lemma-zotero-help">
              Select the Zotero collection to sync into your project&apos;s{" "}
              <code>references.bib</code> and cache attached PDFs for full-text citation grounding.
            </p>

            {warningMessage ? (
              <div className="lemma-status-banner">
                {warningMessage} (Write scope: {hasWriteScope ? "Yes" : "Read-Only"})
              </div>
            ) : null}

            {collections.length === 0 ? (
              <p className="lemma-zotero-note">
                No collections found in this library. The entire library (root) will be synced.
              </p>
            ) : (
              <div className="lemma-project-create-field">
                <label htmlFor="zotero-collection-select">Zotero Collection</label>
                <select
                  id="zotero-collection-select"
                  className="lemma-project-create-input"
                  aria-label="Zotero Collection"
                  value={selectedCollectionKey}
                  onChange={(e) => setSelectedCollectionKey(e.target.value)}
                >
                  {collections.map((c) => (
                    <option key={c.key} value={c.key}>
                      {c.name} ({c.num_items} items)
                    </option>
                  ))}
                </select>
              </div>
            )}

            {syncSummary ? (
              <div className="lemma-zotero-summary" role="status" data-testid="zotero-sync-summary">
                <div>
                  <strong>Sync Complete!</strong>
                </div>
                <div>
                  Imported {syncSummary.synced_bib_count} BibTeX references and{" "}
                  {syncSummary.synced_pdf_count} full-text PDFs.
                </div>
              </div>
            ) : null}

            <div className="lemma-modal-actions lemma-zotero-actions-split">
              <button
                type="button"
                className="lemma-btn lemma-btn-secondary"
                disabled={isLoading}
                onClick={() => {
                  setErrorMessage(null);
                  setSyncSummary(null);
                  setView(isConnected ? "connected" : "credentials");
                }}
              >
                Back
              </button>
              {syncSummary && onClose ? (
                <button
                  type="button"
                  className="lemma-btn lemma-btn-primary"
                  onClick={onClose}
                >
                  Done
                </button>
              ) : (
                <button
                  type="button"
                  className="lemma-btn lemma-btn-primary"
                  disabled={isLoading || !selectedCollectionKey}
                  onClick={() => handleSync()}
                >
                  {isLoading ? "Syncing..." : "Connect & Sync"}
                </button>
              )}
            </div>
          </div>
        ) : null}
      </dialog>
    </>
  );
}
