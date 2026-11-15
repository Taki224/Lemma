import React, { useEffect, useRef, useState } from "react";

export interface GithubLinkStatus {
  linked: boolean;
  owner?: string;
  repo?: string;
  branch?: string;
  subpath?: string;
  last_synced_sha?: string | null;
  has_token?: boolean;
  token_source?: "environment" | "saved" | null;
}

export interface GithubSyncResult {
  commit_sha: string | null;
  added: string[];
  updated: string[];
  deleted: string[];
  unchanged: boolean;
  snapshot_warning?: string | null;
}

interface GithubBlock {
  code?: string;
  message: string;
  paths: string[];
}

export interface GitHubConnectorModalProps {
  isOpen: boolean;
  projectId: string;
  csrfToken?: string;
  onClose?: () => void;
  onPullCompleted?: (projectId: string) => void | Promise<void>;
  beforeSync?: () => Promise<void>;
  onBusyChange?: (busy: boolean) => void;
  onLinkChange?: (linked: boolean) => void;
  fetchImpl?: typeof fetch;
}

function readBlock(data: unknown): GithubBlock {
  if (!data || typeof data !== "object") {
    return { message: "GitHub request failed.", paths: [] };
  }
  const detail = (data as { detail?: unknown }).detail;
  if (typeof detail === "string") return { message: detail, paths: [] };
  if (detail && typeof detail === "object") {
    const record = detail as { message?: string; code?: string; paths?: unknown };
    return {
      message: record.message || "GitHub request failed.",
      code: record.code,
      paths: Array.isArray(record.paths) ? record.paths.filter((item) => typeof item === "string") : [],
    };
  }
  return { message: "GitHub request failed.", paths: [] };
}

export function GitHubConnectorModal({
  isOpen,
  projectId,
  csrfToken = "",
  onClose,
  onPullCompleted,
  beforeSync,
  onBusyChange,
  onLinkChange,
  fetchImpl = globalThis.fetch,
}: GitHubConnectorModalProps): React.ReactElement | null {
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const [owner, setOwner] = useState<string>("");
  const [repo, setRepo] = useState<string>("");
  const [branch, setBranch] = useState<string>("main");
  const [subpath, setSubpath] = useState<string>("");
  const [token, setToken] = useState<string>("");
  const [message, setMessage] = useState<string>("Update project files");
  const [link, setLink] = useState<GithubLinkStatus | null>(null);
  const [editingSettings, setEditingSettings] = useState<boolean>(false);
  const [summary, setSummary] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [confirmPaths, setConfirmPaths] = useState<string[]>([]);
  const [confirmAction, setConfirmAction] = useState<"pull" | "push" | null>(null);
  const [busy, setBusy] = useState<boolean>(false);

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

  useEffect(() => {
    if (!isOpen) {
      setToken("");
      setConfirmAction(null);
      setConfirmPaths([]);
      setEditingSettings(false);
      return;
    }
    let active = true;
    setErrorMessage(null);
    setSummary(null);
    fetchImpl(`/api/projects/${projectId}/github`)
      .then(async (res) => {
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(readBlock(data).message);
        return data as GithubLinkStatus;
      })
      .then((data) => {
        if (!active) return;
        setLink(data);
        onLinkChange?.(Boolean(data.linked));
        if (data.linked) {
          setOwner(data.owner || "");
          setRepo(data.repo || "");
          setBranch(data.branch || "main");
          setSubpath(data.subpath || "");
          setEditingSettings(false);
        } else {
          setEditingSettings(false);
        }
      })
      .catch((err: unknown) => {
        if (!active) return;
        setErrorMessage(err instanceof Error ? err.message : "Could not load the GitHub link.");
      });
    return () => {
      active = false;
    };
  }, [isOpen, projectId, fetchImpl]);

  if (!isOpen) return null;

  const send = async (url: string, method: "PUT" | "POST" | "DELETE", body?: unknown) => {
    const timeout = typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function"
      ? AbortSignal.timeout(120000)
      : undefined;
    const res = await fetchImpl(url, {
      method,
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: timeout,
    });
    const data = await res.json().catch(() => ({}));
    return { res, data };
  };

  const saveLink = async () => {
    setBusy(true);
    setErrorMessage(null);
    setSummary(null);
    setConfirmAction(null);
    try {
      const payload: Record<string, string> = {
        owner: owner.trim(),
        repo: repo.trim(),
        branch: branch.trim() || "main",
        subpath: subpath.trim(),
      };
      if (token.trim()) payload.token = token.trim();
      const { res, data } = await send(`/api/projects/${projectId}/github`, "PUT", payload);
      if (!res.ok) {
        setErrorMessage(readBlock(data).message);
        return;
      }
      const nextLink = data as GithubLinkStatus;
      setLink(nextLink);
      onLinkChange?.(Boolean(nextLink.linked));
      setToken("");
      setEditingSettings(false);
      setSummary("GitHub repository linked.");
    } catch (err) {
      setErrorMessage(err instanceof Error ? err.message : "Could not save the GitHub link.");
    } finally {
      setBusy(false);
    }
  };

  const unlink = async () => {
    setBusy(true);
    setErrorMessage(null);
    setSummary(null);
    setConfirmAction(null);
    try {
      const { res, data } = await send(`/api/projects/${projectId}/github`, "DELETE");
      if (!res.ok) {
        setErrorMessage(readBlock(data).message);
        return;
      }
      const nextLink = data as GithubLinkStatus;
      setLink(nextLink);
      onLinkChange?.(Boolean(nextLink.linked));
      setEditingSettings(false);
      setSummary("GitHub folder unlinked. Local version history is unchanged.");
    } catch (err) {
      setErrorMessage(err instanceof Error ? err.message : "Could not unlink GitHub.");
    } finally {
      setBusy(false);
    }
  };

  const sync = async (action: "pull" | "push", confirmOverwrite: boolean) => {
    setBusy(true);
    onBusyChange?.(true);
    setErrorMessage(null);
    setSummary(null);
    try {
      if (beforeSync) await beforeSync();
      const body = action === "pull"
        ? { confirm_overwrite: confirmOverwrite }
        : { message, confirm_overwrite: confirmOverwrite };
      const { res, data } = await send(`/api/projects/${projectId}/github/${action}`, "POST", body);
      if (res.status === 409) {
        const block = readBlock(data);
        if (block.code === "confirm_required") {
          setConfirmAction(action);
          setConfirmPaths(block.paths);
          setErrorMessage(block.message);
          return;
        }
        setConfirmAction(null);
        setConfirmPaths([]);
        setErrorMessage(block.message);
        return;
      }
      if (!res.ok) {
        setErrorMessage(readBlock(data).message);
        return;
      }
      const result = data as GithubSyncResult;
      setConfirmAction(null);
      setConfirmPaths([]);
      const verb = action === "pull" ? "Pulled" : "Pushed";
      const detail = result.unchanged
        ? "GitHub already matches this project."
        : `${verb} ${result.added.length} added, ${result.updated.length} updated, ${result.deleted.length} removed.`;
      setSummary(result.snapshot_warning ? `${detail} ${result.snapshot_warning}` : detail);
      setLink((current) => (current ? { ...current, last_synced_sha: result.commit_sha } : current));
      if (action === "pull") await onPullCompleted?.(projectId);
    } catch (err) {
      setErrorMessage(err instanceof Error ? err.message : "GitHub sync failed.");
    } finally {
      setBusy(false);
      onBusyChange?.(false);
    }
  };

  const clearToken = async () => {
    setBusy(true);
    setErrorMessage(null);
    setSummary(null);
    try {
      const payload = link?.linked
        ? {
            owner: link.owner || "",
            repo: link.repo || "",
            branch: link.branch || "main",
            subpath: link.subpath || "",
            clear_token: true,
          }
        : { owner: "", repo: "", clear_token: true };
      const { res, data } = await send(`/api/projects/${projectId}/github`, "PUT", payload);
      if (!res.ok) {
        setErrorMessage(readBlock(data).message);
        return;
      }
      setLink(data as GithubLinkStatus);
      setToken("");
      setSummary("Saved GitHub token removed. Local version history is unchanged.");
    } catch (err) {
      setErrorMessage(err instanceof Error ? err.message : "Could not remove the GitHub token.");
    } finally {
      setBusy(false);
    }
  };

  const linked = Boolean(link?.linked);
  const showForm = !linked || editingSettings;
  const savedFolder = link?.subpath ? link.subpath : "Whole repository";
  const tokenLabel = link?.token_source === "environment"
    ? "GITHUB_TOKEN (env)"
    : link?.has_token
      ? "Saved token"
      : "Not set";

  return (
    <>
      <div className="lemma-modal-backdrop" />
      <dialog
        ref={dialogRef}
        open
        aria-labelledby="github-modal-title"
        className="lemma-zotero-dialog lemma-github-dialog"
        onCancel={(event) => {
          event.preventDefault();
          onClose?.();
        }}
      >
        <div className="lemma-zotero-header">
          <h3 id="github-modal-title">GitHub</h3>
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
        {summary ? (
          <div role="status" className="lemma-zotero-summary">
            {summary}
          </div>
        ) : null}
        {confirmPaths.length > 0 ? (
          <ul className="lemma-github-file-list">
            {confirmPaths.map((path) => (
              <li key={path}>{path}</li>
            ))}
          </ul>
        ) : null}

        {!showForm ? (
          <div className="lemma-zotero-body">
            <div className="lemma-connector-card" data-testid="github-connected-card">
              <div className="lemma-connector-card-header">
                <span className="lemma-status-pill is-connected">
                  <span className="lemma-status-dot" aria-hidden="true" />
                  Connected
                </span>
                <button
                  type="button"
                  className="lemma-btn lemma-btn-secondary lemma-btn-xs"
                  disabled={busy}
                  onClick={() => {
                    setErrorMessage(null);
                    setSummary(null);
                    setEditingSettings(true);
                  }}
                >
                  Edit settings
                </button>
              </div>
              <dl className="lemma-connector-meta">
                <div>
                  <dt>Repository</dt>
                  <dd><code>{link?.owner}/{link?.repo}</code></dd>
                </div>
                <div>
                  <dt>Branch &amp; folder</dt>
                  <dd>
                    <code>{link?.branch || "main"}</code> · <code>{savedFolder}</code>
                  </dd>
                </div>
                <div>
                  <dt>Last commit</dt>
                  <dd>
                    {link?.last_synced_sha ? (
                      <code>{link.last_synced_sha.slice(0, 7)}</code>
                    ) : (
                      "Not synced yet"
                    )}
                  </dd>
                </div>
                <div>
                  <dt>Token</dt>
                  <dd>{tokenLabel}</dd>
                </div>
              </dl>
            </div>

            <p className="lemma-zotero-note">
              Use the <strong>Sync</strong> button in the Files sidebar to pull and push changes, or run a single action below.
            </p>

            <div className="lemma-project-create-field">
              <label htmlFor="github-message-input">Push message</label>
              <input
                id="github-message-input"
                className="lemma-project-create-input"
                aria-label="Push message"
                type="text"
                value={message}
                onChange={(event) => setMessage(event.target.value)}
              />
            </div>

            <div className="lemma-modal-actions lemma-github-actions">
              <button type="button" className="lemma-btn lemma-btn-danger" disabled={busy} onClick={unlink}>
                Unlink
              </button>
              <div className="lemma-github-action-gap">
                <button
                  type="button"
                  className="lemma-btn lemma-btn-secondary"
                  disabled={busy || !link?.has_token}
                  onClick={() => sync("pull", confirmAction === "pull")}
                >
                  {confirmAction === "pull" ? "Replace local files and pull" : "Pull from GitHub"}
                </button>
                <button
                  type="button"
                  className="lemma-btn lemma-btn-primary"
                  disabled={busy || !link?.has_token}
                  onClick={() => sync("push", confirmAction === "push")}
                >
                  {confirmAction === "push" ? "Overwrite GitHub folder and push" : "Push to GitHub"}
                </button>
              </div>
            </div>
          </div>
        ) : (
          <form
            className="lemma-zotero-body"
            onSubmit={(event) => {
              event.preventDefault();
              void saveLink();
            }}
          >
            <p className="lemma-zotero-help">
              {editingSettings
                ? "Update the repository, branch, folder, or token for this project."
                : "Link a GitHub repository or folder to enable 1-click sync from the sidebar. Local version history stays in this workspace."}
            </p>

            <div className="lemma-project-create-field">
              <div className="lemma-field-label-row">
                <label htmlFor="github-token-input">GitHub token</label>
                {link?.token_source === "saved" ? (
                  <button
                    type="button"
                    className="lemma-inline-link-btn"
                    disabled={busy}
                    onClick={clearToken}
                  >
                    Remove token
                  </button>
                ) : null}
              </div>
              <input
                id="github-token-input"
                className="lemma-project-create-input"
                aria-label="GitHub token"
                type="password"
                value={token}
                onChange={(event) => setToken(event.target.value)}
                placeholder={
                  link?.token_source === "environment"
                    ? "GITHUB_TOKEN from the environment is used"
                    : link?.has_token
                      ? "Saved token stays if this is left blank"
                      : "Paste a personal access token"
                }
                autoComplete="off"
                autoFocus={!linked}
              />
            </div>

            <div className="lemma-zotero-split">
              <div className="lemma-project-create-field">
                <label htmlFor="github-owner-input">Owner</label>
                <input
                  id="github-owner-input"
                  className="lemma-project-create-input"
                  aria-label="GitHub owner"
                  type="text"
                  value={owner}
                  onChange={(event) => setOwner(event.target.value)}
                  placeholder="octocat"
                  required
                />
              </div>
              <div className="lemma-project-create-field">
                <label htmlFor="github-repo-input">Repository</label>
                <input
                  id="github-repo-input"
                  className="lemma-project-create-input"
                  aria-label="GitHub repository"
                  type="text"
                  value={repo}
                  onChange={(event) => setRepo(event.target.value)}
                  placeholder="manuscript"
                  required
                />
              </div>
            </div>

            <div className="lemma-zotero-split">
              <div className="lemma-project-create-field">
                <label htmlFor="github-branch-input">Branch</label>
                <input
                  id="github-branch-input"
                  className="lemma-project-create-input"
                  aria-label="GitHub branch"
                  type="text"
                  value={branch}
                  onChange={(event) => setBranch(event.target.value)}
                />
              </div>
              <div className="lemma-project-create-field">
                <label htmlFor="github-folder-input">Folder (optional)</label>
                <input
                  id="github-folder-input"
                  className="lemma-project-create-input"
                  aria-label="Folder in the repository"
                  type="text"
                  value={subpath}
                  onChange={(event) => setSubpath(event.target.value)}
                  placeholder="Whole repository"
                />
              </div>
            </div>

            <div className="lemma-modal-actions lemma-zotero-actions-split">
              {editingSettings ? (
                <button
                  type="button"
                  className="lemma-btn lemma-btn-secondary"
                  disabled={busy}
                  onClick={() => {
                    setOwner(link?.owner || "");
                    setRepo(link?.repo || "");
                    setBranch(link?.branch || "main");
                    setSubpath(link?.subpath || "");
                    setToken("");
                    setErrorMessage(null);
                    setEditingSettings(false);
                  }}
                >
                  Cancel
                </button>
              ) : onClose ? (
                <button type="button" className="lemma-btn lemma-btn-secondary" disabled={busy} onClick={onClose}>
                  Cancel
                </button>
              ) : (
                <span />
              )}
              <button type="submit" className="lemma-btn lemma-btn-primary" disabled={busy}>
                {editingSettings ? "Save changes" : "Save link"}
              </button>
            </div>
          </form>
        )}
      </dialog>
    </>
  );
}
