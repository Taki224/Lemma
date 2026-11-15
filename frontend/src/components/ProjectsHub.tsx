import React, { useEffect, useRef, useState } from "react";
import { type ProjectItem } from "./LeftSidebar";
import { ProjectCreateDialog } from "./ProjectCreateDialog";

export interface ProjectsHubProps {
  projects: ProjectItem[];
  /** The project that is open in the editor behind the hub, if any. */
  openProject: ProjectItem | null;
  csrfToken: string;
  onOpen: (project: ProjectItem) => void;
  onBackToProject: () => void;
  onRenamed: (project: ProjectItem) => void;
  onDuplicated: (project: ProjectItem) => void;
  onDeleted: (projectId: string) => void;
  onCreateProject: (name: string) => Promise<boolean>;
  createProjectError?: string | null;
  onCreateProjectDismiss?: () => void;
}

function formatEdited(iso?: string): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" });
}

async function readError(res: Response, fallback: string): Promise<string> {
  try {
    const data = await res.json();
    if (data && typeof data.detail === "string") return data.detail;
  } catch {
    // Body was not JSON.
  }
  return `${fallback} (HTTP ${res.status})`;
}

export function ProjectsHub({
  projects,
  openProject,
  csrfToken,
  onOpen,
  onBackToProject,
  onRenamed,
  onDuplicated,
  onDeleted,
  onCreateProject,
  createProjectError,
  onCreateProjectDismiss,
}: ProjectsHubProps): React.ReactElement {
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [duplicatingId, setDuplicatingId] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<ProjectItem | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [menuOpenId, setMenuOpenId] = useState<string | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menuOpenId) return;
    const close = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpenId(null);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuOpenId(null);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpenId]);

  const headers = { "Content-Type": "application/json", "X-CSRF-Token": csrfToken };

  const openCreate = () => {
    onCreateProjectDismiss?.();
    setCreateOpen(true);
  };

  const submitRename = async (project: ProjectItem) => {
    const name = renameDraft.trim();
    if (!name || name === project.name) {
      setRenamingId(null);
      return;
    }
    setBusyId(project.project_id);
    setErrorMessage(null);
    try {
      const res = await fetch(`/api/projects/${project.project_id}`, {
        method: "PATCH",
        headers,
        body: JSON.stringify({ name }),
      });
      if (!res.ok) {
        setErrorMessage(await readError(res, "Could not rename the project"));
        return;
      }
      onRenamed(await res.json());
      setRenamingId(null);
    } catch {
      setErrorMessage("Could not rename the project: network error.");
    } finally {
      setBusyId(null);
    }
  };

  const duplicate = async (project: ProjectItem) => {
    setBusyId(project.project_id);
    setDuplicatingId(project.project_id);
    setErrorMessage(null);
    try {
      const res = await fetch(`/api/projects/${project.project_id}/duplicate`, {
        method: "POST",
        headers,
        body: JSON.stringify({}),
      });
      if (!res.ok) {
        setErrorMessage(await readError(res, "Could not duplicate the project"));
        return;
      }
      onDuplicated(await res.json());
    } catch {
      setErrorMessage("Could not duplicate the project: network error.");
    } finally {
      setBusyId(null);
      setDuplicatingId(null);
    }
  };

  const confirmDelete = async () => {
    const project = pendingDelete;
    if (!project) return;
    setBusyId(project.project_id);
    setErrorMessage(null);
    try {
      const res = await fetch(`/api/projects/${project.project_id}`, { method: "DELETE", headers });
      if (!res.ok) {
        setErrorMessage(await readError(res, "Could not delete the project"));
        return;
      }
      onDeleted(project.project_id);
    } catch {
      setErrorMessage("Could not delete the project: network error.");
    } finally {
      setBusyId(null);
      setPendingDelete(null);
    }
  };

  return (
    <main className="lemma-projects-hub" data-testid="projects-hub">
      <div className="lemma-hub-panel">
        <header className="lemma-hub-topbar">
          <div className="lemma-sidebar-brand">
            <img className="lemma-sidebar-brand-icon" src="/favicon.svg" alt="" width={22} height={22} />
            <span className="lemma-sidebar-brand-name">Lemma</span>
          </div>
          {openProject && (
            <button type="button" className="lemma-btn lemma-btn-secondary lemma-btn-small" onClick={onBackToProject}>
              Back to {openProject.name}
            </button>
          )}
        </header>

        <div className="lemma-hub-content">
          <div className="lemma-hub-heading">
            <div>
              <h1>Projects</h1>
              <p>Pick a manuscript to work on, or start a new one.</p>
            </div>
            <button type="button" className="lemma-btn lemma-btn-primary" onClick={openCreate}>
              New project
            </button>
          </div>

          {errorMessage && (
            <div className="lemma-error-banner" role="alert">
              {errorMessage}
            </div>
          )}

          {projects.length === 0 ? (
            <div className="lemma-hub-empty">
              <p className="lemma-empty-state">No projects yet. Create one to start writing.</p>
            </div>
          ) : (
            <table className="lemma-hub-table" aria-label="Projects">
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col" className="lemma-hub-col-root">
                    Main document
                  </th>
                  <th scope="col">Last edited</th>
                  <th scope="col" className="lemma-hub-col-actions" aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {projects.map((project) => {
                  const isOpen = project.project_id === openProject?.project_id;
                  const isBusy = busyId === project.project_id;
                  const isRenaming = renamingId === project.project_id;
                  const menuOpen = menuOpenId === project.project_id;
                  return (
                    <tr
                      key={project.project_id}
                      className={isOpen ? "is-open" : undefined}
                      onClick={(e) => {
                        if (isRenaming || (e.target as HTMLElement).closest("button, input, .lemma-hub-row-menu")) return;
                        onOpen(project);
                      }}
                    >
                      <td>
                        {isRenaming ? (
                          <div className="lemma-hub-rename">
                            <input
                              className="lemma-settings-input"
                              aria-label={`New name for ${project.name}`}
                              value={renameDraft}
                              autoFocus
                              disabled={isBusy}
                              onChange={(e) => setRenameDraft(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === "Enter") {
                                  e.preventDefault();
                                  void submitRename(project);
                                } else if (e.key === "Escape") {
                                  e.preventDefault();
                                  setRenamingId(null);
                                }
                              }}
                            />
                            <button
                              type="button"
                              className="lemma-btn lemma-btn-primary lemma-btn-xs"
                              disabled={isBusy || !renameDraft.trim()}
                              onClick={() => void submitRename(project)}
                            >
                              Save
                            </button>
                            <button
                              type="button"
                              className="lemma-btn lemma-btn-secondary lemma-btn-xs"
                              disabled={isBusy}
                              onClick={() => setRenamingId(null)}
                            >
                              Cancel
                            </button>
                          </div>
                        ) : (
                          <div className="lemma-hub-name-cell">
                            <button
                              type="button"
                              className="lemma-hub-name"
                              aria-label={`Open ${project.name}`}
                              onClick={() => onOpen(project)}
                            >
                              {project.name}
                            </button>
                            {isOpen && <span className="lemma-badge-success lemma-model-badge">Open</span>}
                            {duplicatingId === project.project_id && (
                              <span className="lemma-hub-status">Duplicating…</span>
                            )}
                          </div>
                        )}
                      </td>
                      <td className="lemma-hub-col-root">
                        <code>{project.root_tex}</code>
                      </td>
                      <td className="lemma-hub-muted">{formatEdited(project.updated_at)}</td>
                      <td className="lemma-hub-col-actions">
                        <div className="lemma-hub-row-menu" ref={menuOpen ? menuRef : undefined}>
                          <button
                            type="button"
                            className="lemma-hub-more-btn"
                            aria-label={`Actions for ${project.name}`}
                            aria-haspopup="menu"
                            aria-expanded={menuOpen}
                            disabled={isBusy || isRenaming}
                            onClick={() => setMenuOpenId(menuOpen ? null : project.project_id)}
                          >
                            <svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor" aria-hidden="true">
                              <circle cx="3" cy="7" r="1.3" />
                              <circle cx="7" cy="7" r="1.3" />
                              <circle cx="11" cy="7" r="1.3" />
                            </svg>
                          </button>
                          {menuOpen && (
                            <div className="lemma-hub-menu" role="menu">
                              <button
                                type="button"
                                role="menuitem"
                                className="lemma-tools-item"
                                onClick={() => {
                                  setMenuOpenId(null);
                                  setErrorMessage(null);
                                  setRenamingId(project.project_id);
                                  setRenameDraft(project.name);
                                }}
                              >
                                Rename
                              </button>
                              <button
                                type="button"
                                role="menuitem"
                                className="lemma-tools-item"
                                onClick={() => {
                                  setMenuOpenId(null);
                                  void duplicate(project);
                                }}
                              >
                                Duplicate
                              </button>
                              <button
                                type="button"
                                role="menuitem"
                                className="lemma-tools-item lemma-hub-menu-danger"
                                onClick={() => {
                                  setMenuOpenId(null);
                                  setErrorMessage(null);
                                  setPendingDelete(project);
                                }}
                              >
                                Delete
                              </button>
                            </div>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {pendingDelete && (
        <div className="lemma-modal-backdrop">
          <div
            className="lemma-modal-dialog lemma-project-create-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="project-delete-title"
            onKeyDown={(e) => {
              if (e.key === "Escape") setPendingDelete(null);
            }}
          >
            <h2 id="project-delete-title">Delete “{pendingDelete.name}”?</h2>
            <p className="lemma-subtext">
              This permanently deletes every file, chat, and version-history snapshot in this project. It cannot be
              undone.
            </p>
            <div className="lemma-modal-actions">
              <button
                type="button"
                className="lemma-btn lemma-btn-secondary"
                autoFocus
                disabled={busyId === pendingDelete.project_id}
                onClick={() => setPendingDelete(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="lemma-btn lemma-btn-danger"
                disabled={busyId === pendingDelete.project_id}
                onClick={() => void confirmDelete()}
              >
                {busyId === pendingDelete.project_id ? "Deleting…" : "Delete project"}
              </button>
            </div>
          </div>
        </div>
      )}

      <ProjectCreateDialog
        isOpen={createOpen}
        error={createProjectError}
        onCancel={() => {
          onCreateProjectDismiss?.();
          setCreateOpen(false);
        }}
        onSubmit={async (name) => {
          if (await onCreateProject(name)) setCreateOpen(false);
        }}
      />
    </main>
  );
}

export default ProjectsHub;
