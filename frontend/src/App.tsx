import React, { useState, useEffect, useRef, useMemo } from "react";
import {
  LeftSidebar,
  type ChatSummary,
  type ProjectItem,
  type FileItem,
  type OutlineNodeItem,
} from "./components/LeftSidebar";
import { LatexEditor, type EditorContextSnapshot, type LatexDiagnosticItem } from "./components/LatexEditor";
import { PdfPreview, type ForwardHighlightBox } from "./components/PdfPreview";
import { FloatingChatBar } from "./components/FloatingChatBar";
import { type ProposedHunkItem } from "./components/DiffExtension";
import { ZoteroWizardModal } from "./components/ZoteroWizardModal";
import { GitHubConnectorModal, type GithubSyncResult } from "./components/GitHubConnectorModal";
import { CandidateSourcesTray } from "./components/CandidateSourcesTray";
import { VersionHistoryDrawer } from "./components/VersionHistoryDrawer";
import { ReviewStudioView } from "./components/ReviewStudioView";
import { ModelsSettingsView } from "./components/ModelsSettingsView";
import { ProjectsHub } from "./components/ProjectsHub";
import { EnabledModelsProvider } from "./context/EnabledModelsContext";
import { ToolsMenuModal } from "./components/ToolsMenuAndToolbar";
import { ResizeHandle } from "./components/ResizeHandle";
import { usePaneLayout, maxWidthFor, MIN_WIDTHS } from "./hooks/usePaneLayout";
import { useAgentRun, chatScopeKey } from "./hooks/useAgentRun";
import { useBibEntries } from "./hooks/useBibEntries";

export interface AuthStatusResponse {
  authenticated: boolean;
  csrf_token: string | null;
  insecure_http_warning: boolean;
  privacy_consent_accepted: boolean;
}

export function App(): React.ReactElement {
  const [loading, setLoading] = useState<boolean>(true);
  const [authenticated, setAuthenticated] = useState<boolean>(false);
  const [csrfToken, setCsrfToken] = useState<string | null>(null);
  const [insecureHttpWarning, setInsecureHttpWarning] = useState<boolean>(false);
  const [privacyConsentAccepted, setPrivacyConsentAccepted] = useState<boolean>(false);
  const [password, setPassword] = useState<string>("");
  const [loginError, setLoginError] = useState<string | null>(null);
  const [workspaceMode, setWorkspaceMode] = useState<"editor" | "review_studio" | "history" | "settings">("editor");
  const [hubOpen, setHubOpen] = useState<boolean>(true);

  const [projects, setProjects] = useState<ProjectItem[]>([]);
  const [activeProject, setActiveProject] = useState<ProjectItem | null>(null);
  const [files, setFiles] = useState<FileItem[]>([]);
  const [folders, setFolders] = useState<string[]>([]);
  const [activeFile, setActiveFile] = useState<string>("main.tex");
  const [fileContent, setFileContent] = useState<string>("");
  const [fileEtag, setFileEtag] = useState<string | null>(null);
  // Which file fileContent/fileEtag belong to (null until the first load), so the editor ignores stale content.
  const [fileContentPath, setFileContentPath] = useState<string | null>(null);
  // Bumped after a restore reload so the editor drops any unsaved text even when the content is unchanged.
  const [editorReloadKey, setEditorReloadKey] = useState<number>(0);
  const [cursorLine, setCursorLine] = useState<number>(1);
  const [jumpNonce, setJumpNonce] = useState<number>(0);
  const [jumpSnapToHeading, setJumpSnapToHeading] = useState<boolean>(false);
  const [outline, setOutline] = useState<OutlineNodeItem[]>([]);
  const [compileStatus, setCompileStatus] = useState<"idle" | "compiling" | "success" | "failed" | "timeout">("idle");
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [diagnostics, setDiagnostics] = useState<LatexDiagnosticItem[]>([]);
  const [forwardHighlight, setForwardHighlight] = useState<ForwardHighlightBox | null>(null);
  const [editedSinceCompile, setEditedSinceCompile] = useState<boolean>(false);
  const [dirtyBuffers, setDirtyBuffers] = useState<Record<string, string>>({});
  const [editorContext, setEditorContext] = useState<EditorContextSnapshot | null>(null);
  const [syncNotice, setSyncNotice] = useState<string | null>(null);
  const forwardNonceRef = useRef<number>(0);
  // True only for the render that mounts the editor on behalf of a Review Studio jump (so that jump flashes).
  const flashOnMountRef = useRef<boolean>(false);
  const activeProjectIdRef = useRef<string | null>(null);
  const activeFileRef = useRef<string>("main.tex");
  const dirtyBuffersRef = useRef<Record<string, string>>({});
  const [githubBusy, setGithubBusy] = useState<boolean>(false);
  const [githubReloadBlocked, setGithubReloadBlocked] = useState<boolean>(false);
  const [fileLoadNonce, setFileLoadNonce] = useState<number>(0);
  const githubReloadBlockedRef = useRef<boolean>(false);
  githubReloadBlockedRef.current = githubReloadBlocked;
  activeProjectIdRef.current = activeProject?.project_id ?? null;
  activeFileRef.current = activeFile;
  dirtyBuffersRef.current = dirtyBuffers;
  useEffect(() => {
    flashOnMountRef.current = false;
  });
  const [manualHunks, setManualHunks] = useState<ProposedHunkItem[]>([]);
  const [chats, setChats] = useState<ChatSummary[]>([]);
  // null = the chat pill starts a new chat on its next prompt.
  const [activeChatId, setActiveChatId] = useState<string | null>(null);
  const activeChatIdRef = useRef<string | null>(null);
  activeChatIdRef.current = activeChatId;
  const [chatSelectNonce, setChatSelectNonce] = useState<number>(0);

  const bibEntries = useBibEntries({
    projectId: activeProject?.project_id,
    files,
    activeFile,
    fileContent,
    fileContentPath,
    dirtyBuffers,
    refreshKey: editorReloadKey + fileLoadNonce,
  });

  const agentRun = useAgentRun({
    disabled: !authenticated,
    csrfToken: csrfToken || undefined,
    onRunFinished: ({ projectId, chatId }) => {
      if (projectId === activeProjectIdRef.current) {
        handleChatActivity(chatId, chatId === activeChatIdRef.current);
      }
    },
  });

  const activeChatHunks =
    activeProject && activeChatId
      ? agentRun.pendingHunksByChat[chatScopeKey(activeProject.project_id, activeChatId)] ?? []
      : [];
  const proposedHunks = useMemo(
    () => [...activeChatHunks, ...manualHunks],
    [activeChatHunks, manualHunks],
  );
  const [zoteroWizardOpen, setZoteroWizardOpen] = useState<boolean>(false);
  const [zoteroPullBusy, setZoteroPullBusy] = useState<boolean>(false);
  const [zoteroDisconnectBusy, setZoteroDisconnectBusy] = useState<boolean>(false);
  const [zoteroPullStatus, setZoteroPullStatus] = useState<string | null>(null);
  const [githubOpen, setGithubOpen] = useState<boolean>(false);
  const [githubLinked, setGithubLinked] = useState<boolean>(false);
  const [toolsModalTab, setToolsModalTab] = useState<"health_check" | "visual_to_latex" | "export" | null>(null);
  const [toolsOpen, setToolsOpen] = useState<boolean>(false);
  const toolsRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!toolsOpen || typeof document === "undefined") return;
    const onPointerDown = (event: Event) => {
      if (toolsRef.current && !toolsRef.current.contains(event.target as Node)) setToolsOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setToolsOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [toolsOpen]);

  const gridRef = useRef<HTMLDivElement | null>(null);
  const [gridWidth, setGridWidth] = useState<number>(() =>
    typeof window !== "undefined" && typeof window.innerWidth === "number" ? window.innerWidth : Number.POSITIVE_INFINITY,
  );
  const { layout: paneLayout, effective: effectivePanes, resizeBy: resizePane, togglePane, canHide: canHidePane } = usePaneLayout(gridWidth);
  const maxPaneWidth = (pane: "left" | "right"): number | undefined =>
    Number.isFinite(gridWidth) ? maxWidthFor(paneLayout, pane, gridWidth) : undefined;
  const gridMounted = !loading && authenticated;
  useEffect(() => {
    const el = gridRef.current;
    if (!gridMounted || !el) return;
    const measure = () => {
      const width = el.getBoundingClientRect().width;
      // A hidden or not-yet-laid-out grid reports 0. Keep the last real width
      // so the panes do not collapse before the first layout.
      if (width > 0) setGridWidth(width);
    };
    if (typeof ResizeObserver === "undefined") {
      // Fallback: track window resizes (absent in non-browser harnesses).
      if (typeof window === "undefined" || typeof window.addEventListener !== "function") return;
      measure();
      window.addEventListener("resize", measure);
      return () => window.removeEventListener("resize", measure);
    }
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [gridMounted]);

  useEffect(() => {
    let active = true;
    fetch("/api/auth/status")
      .then((res) => res.json())
      .then((data: AuthStatusResponse) => {
        if (!active) return;
        setAuthenticated(Boolean(data.authenticated));
        if (data.csrf_token) {
          setCsrfToken(data.csrf_token);
          localStorage.setItem("lemma_csrf_token", data.csrf_token);
        }
        setInsecureHttpWarning(Boolean(data.insecure_http_warning));
        setPrivacyConsentAccepted(Boolean(data.privacy_consent_accepted));
        setLoading(false);
      })
      .catch(() => {
        if (!active) return;
        setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  // Fetch projects list once authenticated
  useEffect(() => {
    if (!authenticated) return;
    let active = true;
    fetch("/api/projects")
      .then((res) => res.json())
      .then((data: { projects?: ProjectItem[] }) => {
        if (!active) return;
        const projectList = data.projects || [];
        setProjects(projectList);
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [authenticated]);

  // Fetch file list and outline when active project changes
  useEffect(() => {
    if (!activeProject) return;
    let active = true;
    const projId = activeProject.project_id;
    fetch(`/api/projects/${projId}/files`)
      .then((res) => res.json())
      .then((data: { files?: FileItem[]; folders?: string[] }) => {
        if (!active) return;
        setFiles(data.files || []);
        setFolders(data.folders || []);
      })
      .catch(() => {});

    fetch(`/api/projects/${projId}/outline`)
      .then((res) => res.json())
      .then((data: { outline?: OutlineNodeItem[] }) => {
        if (!active) return;
        setOutline(data.outline || []);
      })
      .catch(() => {});

    return () => {
      active = false;
    };
  }, [activeProject]);

  // Probe for an existing compiled PDF when the active project changes
  const activeProjectId = activeProject?.project_id;
  useEffect(() => {
    setPdfUrl(null);
    if (!activeProjectId) return;
    let cancelled = false;
    const url = `/api/projects/${activeProjectId}/pdf`;
    fetch(url)
      .then((res) => {
        // Only status matters here; drop the PDF body instead of downloading it.
        res.body?.cancel().catch(() => {});
        if (cancelled || !res.ok) return;
        setPdfUrl(url);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [activeProjectId]);

  // Fetch active file content when activeFile or activeProject changes
  useEffect(() => {
    if (!activeProject || !activeFile) return;
    let active = true;
    fetch(`/api/projects/${activeProject.project_id}/files/${activeFile}`)
      .then((res) => {
        if (!res.ok) throw new Error("File fetch failed");
        return res.json();
      })
      .then((data: { content: string; etag: string }) => {
        if (!active) return;
        setFileContent(data.content);
        setFileEtag(data.etag);
        setFileContentPath(activeFile);
        setGithubReloadBlocked(false);
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [activeProject, activeFile, fileLoadNonce]);

  useEffect(() => {
    setZoteroPullStatus(null);
  }, [activeProjectId, activeFile]);

  useEffect(() => {
    setGithubLinked(false);
    if (!activeProjectId) return;
    let cancelled = false;
    fetch(`/api/projects/${activeProjectId}/github`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data: { linked?: boolean } | null) => {
        if (cancelled || !data) return;
        setGithubLinked(Boolean(data.linked));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [activeProjectId]);

  const handleZoteroPull = async () => {
    if (!activeProject) return;
    const projectId = activeProject.project_id;
    const pulledFile = activeFile;
    setZoteroPullBusy(true);
    setZoteroPullStatus(null);
    try {
      const res = await fetch(`/api/projects/${projectId}/zotero/sync`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken || (typeof localStorage !== "undefined" ? localStorage.getItem("lemma_csrf_token") || "" : ""),
        },
        body: JSON.stringify({}),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setZoteroPullStatus(typeof data.detail === "string" ? data.detail : "Zotero sync failed.");
        return;
      }
      setZoteroPullStatus(`Zotero: ${Number(data.added_bib_count || 0)} added, ${Number(data.updated_bib_count || 0)} updated`);
      const fileRes = await fetch(`/api/projects/${projectId}/files/${pulledFile}`);
      if (!fileRes.ok || activeProjectIdRef.current !== projectId || activeFileRef.current !== pulledFile) return;
      const fileData: { content: string; etag: string } = await fileRes.json();
      setFileContent(fileData.content);
      setFileEtag(fileData.etag);
      setFileContentPath(pulledFile);
      setEditorReloadKey((k) => k + 1);
    } catch {
      setZoteroPullStatus("Network error during Zotero sync.");
    } finally {
      setZoteroPullBusy(false);
    }
  };

  const handleZoteroDisconnect = async () => {
    if (!activeProject) return;
    const projectId = activeProject.project_id;
    setZoteroDisconnectBusy(true);
    setZoteroPullStatus(null);
    try {
      const res = await fetch(`/api/projects/${projectId}/zotero/disconnect`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken || (typeof localStorage !== "undefined" ? localStorage.getItem("lemma_csrf_token") || "" : ""),
        },
        body: JSON.stringify({}),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setZoteroPullStatus(typeof data.detail === "string" ? data.detail : "Could not disconnect Zotero.");
        return;
      }
      setProjects((prev) =>
        prev.map((p) => (p.project_id === projectId ? { ...p, zotero_collection_key: null } : p))
      );
      setActiveProject((prev) =>
        prev && prev.project_id === projectId ? { ...prev, zotero_collection_key: null } : prev
      );
    } catch {
      setZoteroPullStatus("Network error while disconnecting Zotero.");
    } finally {
      setZoteroDisconnectBusy(false);
    }
  };

  const loadChats = (projectId: string): Promise<ChatSummary[]> =>
    fetch(`/api/projects/${projectId}/chats`)
      .then((res) => (res.ok ? res.json() : { chats: [] }))
      .then((data: { chats?: ChatSummary[] }) => (Array.isArray(data?.chats) ? data.chats : []))
      .catch(() => []);

  // Reopen the project's most recent chat.
  useEffect(() => {
    setChats((prev) => (prev.length === 0 ? prev : []));
    setActiveChatId(null);
    if (!activeProjectId) return;
    let cancelled = false;
    loadChats(activeProjectId).then((list) => {
      if (cancelled) return;
      setChats(list);
      setActiveChatId((current) => current ?? list[0]?.chat_id ?? null);
    });
    return () => {
      cancelled = true;
    };
  }, [activeProjectId]);

  const handleChatActivity = (chatId: string, shown: boolean) => {
    const projectId = activeProjectIdRef.current;
    if (!projectId) return;
    if (shown) setActiveChatId(chatId);
    loadChats(projectId).then((list) => {
      if (activeProjectIdRef.current === projectId) setChats(list);
    });
  };

  // Unsaved buffers and the stale-PDF flag belong to one project: never carry them over to another.
  useEffect(() => {
    setDirtyBuffers((prev) => (Object.keys(prev).length === 0 ? prev : {}));
    setEditedSinceCompile(false);
    setManualHunks([]);
  }, [activeProjectId]);

  const [newProjectName, setNewProjectName] = useState<string>("");
  const [createProjectError, setCreateProjectError] = useState<string | null>(null);

  const handleCreateProject = async (name: string): Promise<boolean> => {
    setCreateProjectError(null);
    const token = csrfToken || (typeof localStorage !== "undefined" ? localStorage.getItem("lemma_csrf_token") || "" : "");
    try {
      const res = await fetch("/api/projects", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": token,
        },
        body: JSON.stringify({ name }),
      });
      if (res.ok) {
        const newProj = await res.json();
        setProjects((prev) => [...prev, newProj]);
        setActiveProject(newProj);
        setActiveFile(newProj.root_tex || "main.tex");
        setHubOpen(false);
        return true;
      } else {
        const errData = await res.json().catch(() => ({}));
        setCreateProjectError(errData.detail || `Failed to create project (HTTP ${res.status})`);
      }
    } catch (err: any) {
      setCreateProjectError(err.message || "Failed to create project");
    }
    return false;
  };

  const handleLoginSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoginError(null);
    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    if (!res.ok) {
      setLoginError("Invalid workspace password or rate limit exceeded.");
      return;
    }
    const data = await res.json();
    if (data.csrf_token) {
      setCsrfToken(data.csrf_token);
      localStorage.setItem("lemma_csrf_token", data.csrf_token);
    }
    setAuthenticated(Boolean(data.authenticated));
    setInsecureHttpWarning(Boolean(data.insecure_http_warning));
    setPrivacyConsentAccepted(Boolean(data.privacy_consent_accepted));
  };

  const handleAcceptConsent = async () => {
    const token = csrfToken || localStorage.getItem("lemma_csrf_token") || "";
    const res = await fetch("/api/settings/consent", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": token,
      },
      body: JSON.stringify({ accepted: true }),
    });
    if (res.ok) {
      setPrivacyConsentAccepted(true);
    }
  };

  const saveDirtyBuffers = async (actionLabel: string) => {
    if (githubReloadBlockedRef.current) {
      throw new Error("Reload this project before syncing. The last pull did not refresh the editor.");
    }
    const projectId = activeProjectIdRef.current;
    if (!projectId) return;
    const pending = { ...dirtyBuffersRef.current };
    const entries = Object.entries(pending);
    if (entries.length === 0) return;
    const token = csrfToken || (typeof localStorage !== "undefined" ? localStorage.getItem("lemma_csrf_token") || "" : "");
    const timeout = typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function"
      ? AbortSignal.timeout(30000)
      : undefined;
    for (const [path, content] of entries) {
      const res = await fetch(`/api/projects/${projectId}/files/${path}`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": token,
        },
        body: JSON.stringify({ content }),
        signal: timeout,
      });
      if (!res.ok) throw new Error(`Could not save ${path} before ${actionLabel}.`);
      const saved = (await res.json().catch(() => ({}))) as { etag?: string };
      if (path === activeFileRef.current) {
        if (typeof saved.etag === "string") setFileEtag(saved.etag);
        const latest = dirtyBuffersRef.current[path];
        if (latest === undefined || latest === content) setFileContent(content);
      }
    }
    setDirtyBuffers((prev) => {
      const next = { ...prev };
      for (const [path, content] of entries) {
        if (next[path] === content) delete next[path];
      }
      return next;
    });
  };

  const saveDirtyBuffersBeforeSync = () => saveDirtyBuffers("syncing");

  const handleTriggerCompile = async () => {
    if (!activeProject) return;
    const token = csrfToken || localStorage.getItem("lemma_csrf_token") || "";
    setCompileStatus("compiling");
    try {
      await saveDirtyBuffers("compiling");
      const res = await fetch(`/api/projects/${activeProject.project_id}/compile`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": token,
        },
      });
      if (!res.ok) {
        setCompileStatus("failed");
        return;
      }
      const data = await res.json();
      setCompileStatus(data.status);
      if (data.status === "success") setEditedSinceCompile(false);
      // Every build is served at the same path; a fresh query makes PdfPreview reload it.
      setPdfUrl(data.pdf_url ? `${data.pdf_url}?v=${Date.now()}` : null);
      setDiagnostics(data.diagnostics || []);
    } catch {
      setCompileStatus("failed");
    }
  };

  const reloadAfterGithubPull = async (pulledProjectId: string) => {
    if (activeProjectIdRef.current !== pulledProjectId) return;
    setManualHunks([]);
    setEditedSinceCompile(true);
    const timeout = typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function"
      ? AbortSignal.timeout(30000)
      : undefined;
    const stillHere = () => activeProjectIdRef.current === pulledProjectId;
    try {
      const listRes = await fetch(`/api/projects/${pulledProjectId}/files`, { signal: timeout });
      if (!listRes.ok) throw new Error("File list failed");
      const data = (await listRes.json()) as { files?: FileItem[] };
      if (!stillHere()) return;
      const newFiles = data.files || [];
      setFiles(newFiles);
      // The first pull can switch the main document to the repository's own root file.
      let pulledRoot: string | null = null;
      const projectsRes = await fetch("/api/projects", { signal: timeout }).catch(() => null);
      if (projectsRes?.ok) {
        const listed = ((await projectsRes.json()) as { projects?: ProjectItem[] }).projects || [];
        const updated = listed.find((p) => p.project_id === pulledProjectId);
        if (updated && stillHere()) {
          pulledRoot = updated.root_tex;
          setProjects((prev) => prev.map((p) => (p.project_id === pulledProjectId ? updated : p)));
          setActiveProject(updated);
        }
      }
      const openFile = activeFileRef.current;
      const fileStillExists = newFiles.some((file) => file.path === openFile);
      const rootExists = !!pulledRoot && newFiles.some((file) => file.path === pulledRoot);
      const targetFile = fileStillExists
        ? openFile
        : (rootExists && pulledRoot) || newFiles[0]?.path || "main.tex";
      if (!fileStillExists) setActiveFile(targetFile);
      const fileRes = await fetch(`/api/projects/${pulledProjectId}/files/${targetFile}`, { signal: timeout });
      if (!fileRes.ok) throw new Error("File not found");
      const fileData = (await fileRes.json()) as { content?: string; etag?: string };
      if (!stillHere()) return;
      setDirtyBuffers({});
      setFileContent(fileData.content !== undefined ? fileData.content : "");
      setFileEtag(fileData.etag !== undefined ? fileData.etag : null);
      setFileContentPath(targetFile);
      setEditorReloadKey((key) => key + 1);
      setGithubReloadBlocked(false);
      void handleTriggerCompile();
    } catch {
      if (!stillHere()) return;
      setDirtyBuffers({});
      setFileContentPath(null);
      setGithubReloadBlocked(true);
      setFileLoadNonce((nonce) => nonce + 1);
      setSyncNotice("Pulled files could not be reloaded. Reload this project before editing.");
    }
  };

  const handleGithubSync = async () => {
    const projectId = activeProjectIdRef.current;
    if (!projectId || githubBusy) return;
    const token = csrfToken || (typeof localStorage !== "undefined" ? localStorage.getItem("lemma_csrf_token") || "" : "");
    const timeout = typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function"
      ? AbortSignal.timeout(120000)
      : undefined;
    const headers = {
      "Content-Type": "application/json",
      "X-CSRF-Token": token,
    };
    setGithubBusy(true);
    try {
      await saveDirtyBuffersBeforeSync();

      const pullRes = await fetch(`/api/projects/${projectId}/github/pull`, {
        method: "POST",
        headers,
        body: JSON.stringify({ confirm_overwrite: false }),
        signal: timeout,
      });
      const pullData = await pullRes.json().catch(() => ({}));
      if (!pullRes.ok) {
        const detail = (pullData as { detail?: unknown })?.detail;
        const msg = typeof detail === "string"
          ? detail
          : (detail as { message?: string })?.message || "GitHub pull failed.";
        setSyncNotice(msg);
        if (pullRes.status === 409 || pullRes.status === 400) {
          setGithubOpen(true);
        }
        return;
      }
      const pullResult = pullData as GithubSyncResult;
      if (!pullResult.unchanged) {
        await reloadAfterGithubPull(projectId);
      }

      const pushRes = await fetch(`/api/projects/${projectId}/github/push`, {
        method: "POST",
        headers,
        body: JSON.stringify({ message: "Update project files", confirm_overwrite: false }),
        signal: timeout,
      });
      const pushData = await pushRes.json().catch(() => ({}));
      if (!pushRes.ok) {
        const detail = (pushData as { detail?: unknown })?.detail;
        const msg = typeof detail === "string"
          ? detail
          : (detail as { message?: string })?.message || "GitHub push failed.";
        setSyncNotice(msg);
        if (pushRes.status === 409 || pushRes.status === 400) {
          setGithubOpen(true);
        }
        return;
      }
    } catch (err) {
      setSyncNotice(err instanceof Error ? err.message : "GitHub sync failed.");
    } finally {
      setGithubBusy(false);
    }
  };

  const requestJump = (targetFile: string, line: number, options?: { snapToHeading?: boolean }) => {
    // Re-show a hidden editor first (effective state: togglePane flips the stored preference).
    if (!effectivePanes.visible.center) togglePane("center");
    setActiveFile(targetFile);
    setCursorLine(line);
    setJumpSnapToHeading(!!options?.snapToHeading);
    setJumpNonce((n) => n + 1);
  };

  const handleForwardSync = async (texFile: string, line: number) => {
    if (!activeProject) return;
    const params = new URLSearchParams({ file: texFile, line: String(line) });
    setSyncNotice(null);
    try {
      const res = await fetch(`/api/projects/${activeProject.project_id}/synctex/forward?${params.toString()}`);
      if (res.status === 404) {
        setSyncNotice("Compile first");
        return;
      }
      if (!res.ok) {
        setSyncNotice("Could not locate this line in the PDF");
        return;
      }
      const box = (await res.json()) as Omit<ForwardHighlightBox, "nonce">;
      forwardNonceRef.current += 1;
      setForwardHighlight({ ...box, nonce: forwardNonceRef.current });
      // Show in PDF also flashes the source line: point the editor at the synced line, then re-trigger the jump.
      setCursorLine(line);
      setJumpNonce((n) => n + 1);
      if (!box.exact || editedSinceCompile) {
        // Keep the backend hint alongside the staleness warning instead of replacing it.
        setSyncNotice(box.hint ? `${box.hint} — PDF may be out of date` : "PDF may be out of date");
      }
    } catch {
      setSyncNotice("Could not locate this line in the PDF");
    }
  };

  const handleDirtyChange = (path: string, content: string | null) => {
    if (content === null) {
      if (path in dirtyBuffersRef.current) {
        const next = { ...dirtyBuffersRef.current };
        delete next[path];
        dirtyBuffersRef.current = next;
      }
    } else {
      dirtyBuffersRef.current = { ...dirtyBuffersRef.current, [path]: content };
    }
    setDirtyBuffers((prev) => {
      if (content === null) {
        if (!(path in prev)) return prev;
        const { [path]: _removed, ...rest } = prev;
        return rest;
      }
      return { ...prev, [path]: content };
    });
    if (content !== null && content !== fileContent) setEditedSinceCompile(true);
  };

  const openPlace = (place: "editor" | "review_studio" | "history" | "settings") => {
    setZoteroWizardOpen(false);
    setGithubOpen(false);
    setToolsModalTab(null);
    setToolsOpen(false);
    setWorkspaceMode(place);
  };

  const openUtility = (utility: "zotero" | "github" | "health_check" | "visual_to_latex" | "export") => {
    setToolsOpen(false);
    if (utility === "zotero") {
      setGithubOpen(false);
      setToolsModalTab(null);
      setZoteroWizardOpen((open) => !open);
      return;
    }
    if (utility === "github") {
      setZoteroWizardOpen(false);
      setToolsModalTab(null);
      setGithubOpen((open) => !open);
      return;
    }
    setZoteroWizardOpen(false);
    setGithubOpen(false);
    setToolsModalTab((tab) => (tab === utility ? null : utility));
  };

  const renderWorkspaceTools = () => (
    <div className="lemma-workspace-tools" ref={toolsRef}>
      <button
        type="button"
        className="lemma-tools-btn"
        aria-label="Tools"
        aria-expanded={toolsOpen}
        aria-haspopup="true"
        onClick={() => setToolsOpen((open) => !open)}
      >
        Tools
      </button>
      <div className="lemma-tools-menu" hidden={!toolsOpen}>
        <div className="lemma-tools-section">
          <div className="lemma-tools-menu-label">Panes</div>
          {workspaceMode === "editor" ? (
            <div className="lemma-pane-toggle" role="group" aria-label="Pane Visibility">
              {(
                [
                  { pane: "left", label: "Files", aria: "Toggle sidebar" },
                  { pane: "center", label: "Code", aria: "Toggle editor" },
                  { pane: "right", label: "PDF", aria: "Toggle PDF preview" },
                ] as const
              ).map(({ pane, label, aria }) => {
                const visible = effectivePanes.visible[pane];
                const locked = visible && !canHidePane(pane);
                return (
                  <button
                    key={pane}
                    type="button"
                    className={visible ? "active" : ""}
                    aria-label={aria}
                    aria-pressed={visible}
                    disabled={locked}
                    title={
                      locked
                        ? "At least one pane must stay visible"
                        : visible
                          ? `Hide ${label}`
                          : `Show ${label}`
                    }
                    onClick={() => togglePane(pane)}
                  >
                    {label}
                  </button>
                );
              })}
            </div>
          ) : (
            <p className="lemma-tools-note">Shown in the editor.</p>
          )}
        </div>
        <div className="lemma-tools-section">
          <div className="lemma-tools-menu-label">Go to</div>
          <div className="lemma-tools-list" role="group" aria-label="Workspace place">
            {(
              [
                { place: "editor", label: "Editor", hint: "Manuscript, files, and PDF" },
                { place: "review_studio", label: "Review Studio", hint: "Peer-review findings" },
                { place: "history", label: "Version history", aria: "Version History", hint: "Snapshots and restore" },
                { place: "settings", label: "Settings", hint: "Models and keys" },
              ] as const
            ).map(({ place, label, hint, ...rest }) => {
              const current = workspaceMode === place;
              return (
                <button
                  key={place}
                  type="button"
                  className={current ? "lemma-tools-item active" : "lemma-tools-item"}
                  aria-label={"aria" in rest ? rest.aria : label}
                  aria-pressed={current}
                  onClick={() => openPlace(place)}
                >
                  <span className="lemma-tools-item-copy">
                    <span className="lemma-tools-item-label">{label}</span>
                    <span className="lemma-tools-item-hint">{hint}</span>
                  </span>
                  {current ? (
                    <span className="lemma-tools-check" aria-hidden="true">
                      ✓
                    </span>
                  ) : null}
                </button>
              );
            })}
          </div>
        </div>
        <div className="lemma-tools-section">
          <div className="lemma-tools-menu-label">Actions</div>
          <div className="lemma-tools-list" role="group" aria-label="Workspace actions">
            {(
              [
                {
                  id: "zotero",
                  label: activeProject?.zotero_collection_key ? "Zotero" : "Connect Zotero",
                  hint: activeProject?.zotero_collection_key ? "Manage Zotero sync" : "Sync a Zotero library",
                },
                {
                  id: "github",
                  label: "GitHub",
                  hint: githubLinked ? "Manage GitHub link" : "Push or pull a repository folder",
                },
                { id: "health_check", label: "Citation health", aria: "Citation Health Check", hint: "Find citation problems" },
                { id: "visual_to_latex", label: "Visual to LaTeX", hint: "Turn a figure into code" },
                { id: "export", label: "Export camera-ready", aria: "Export Camera-Ready", hint: "Download a submission archive" },
              ] as const
            ).map(({ id, label, hint, ...rest }) => {
              const open = id === "zotero" ? zoteroWizardOpen : id === "github" ? githubOpen : toolsModalTab === id;
              return (
                <button
                  key={id}
                  type="button"
                  className={open ? "lemma-tools-item active" : "lemma-tools-item"}
                  aria-label={"aria" in rest ? rest.aria : label}
                  aria-pressed={open}
                  onClick={() => openUtility(id)}
                >
                  <span className="lemma-tools-item-copy">
                    <span className="lemma-tools-item-label">{label}</span>
                    <span className="lemma-tools-item-hint">{hint}</span>
                  </span>
                  {open ? (
                    <span className="lemma-tools-check" aria-hidden="true">
                      ✓
                    </span>
                  ) : null}
                </button>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );

  const openProject = (project: ProjectItem) => {
    setActiveProject(project);
    setActiveFile(project.root_tex || "main.tex");
  };

  const uploadProjectFile = async (file: File, folder?: string): Promise<string | null> => {
    if (!activeProject) return "Open a project first.";
    const projectId = activeProject.project_id;
    const form = new FormData();
    form.append("file", file);
    if (folder) form.append("folder", folder);
    try {
      const res = await fetch(`/api/projects/${projectId}/uploads`, {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken || localStorage.getItem("lemma_csrf_token") || "" },
        body: form,
      });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        return typeof data?.detail === "string" ? data.detail : `Could not upload ${file.name} (HTTP ${res.status}).`;
      }
      const listRes = await fetch(`/api/projects/${projectId}/files`);
      const data = (await listRes.json()) as { files?: FileItem[] };
      if (activeProjectIdRef.current === projectId) setFiles(data.files || []);
      return null;
    } catch {
      return `Could not upload ${file.name}: network error.`;
    }
  };

  const projectsHub = (
    <ProjectsHub
      projects={projects}
      openProject={activeProject}
      csrfToken={csrfToken || (typeof localStorage !== "undefined" ? localStorage.getItem("lemma_csrf_token") || "" : "")}
      onOpen={(project) => {
        if (project.project_id !== activeProject?.project_id) {
          openProject(project);
          setWorkspaceMode("editor");
        }
        setHubOpen(false);
      }}
      onBackToProject={() => setHubOpen(false)}
      onRenamed={(project) => {
        setProjects((prev) => prev.map((p) => (p.project_id === project.project_id ? project : p)));
        if (activeProject?.project_id === project.project_id) setActiveProject(project);
      }}
      onDuplicated={(project) => setProjects((prev) => [project, ...prev])}
      onDeleted={(projectId) => {
        setProjects((prev) => prev.filter((p) => p.project_id !== projectId));
        if (activeProject?.project_id === projectId) {
          setActiveProject(null);
          setFiles([]);
          setFolders([]);
          setOutline([]);
          setManualHunks([]);
          setWorkspaceMode("editor");
        }
      }}
      onCreateProject={async (name) => {
        const ok = await handleCreateProject(name);
        if (ok) setWorkspaceMode("editor");
        return ok;
      }}
      createProjectError={createProjectError}
      onCreateProjectDismiss={() => setCreateProjectError(null)}
    />
  );

  const placeView =
    workspaceMode === "settings" ? (
      <ModelsSettingsView
        csrfToken={csrfToken || (typeof localStorage !== "undefined" ? localStorage.getItem("lemma_csrf_token") || "" : "")}
        onClose={() => setWorkspaceMode("editor")}
      />
    ) : workspaceMode === "review_studio" ? (
      <ReviewStudioView
        projectId={activeProject?.project_id || "default"}
        csrfToken={csrfToken || (typeof localStorage !== "undefined" ? localStorage.getItem("lemma_csrf_token") || "" : "")}
        onJumpToLine={(filePath, line) => {
          flashOnMountRef.current = true;
          setWorkspaceMode("editor");
          requestJump(filePath, line);
        }}
        onJumpToPage={(_page) => {
          setWorkspaceMode("editor");
        }}
        onClose={() => setWorkspaceMode("editor")}
      />
    ) : workspaceMode === "history" ? (
      <VersionHistoryDrawer
        isOpen
        layout="page"
        projectId={activeProject?.project_id || "default"}
        csrfToken={csrfToken || (typeof localStorage !== "undefined" ? localStorage.getItem("lemma_csrf_token") || "" : "")}
        dirtyBuffers={dirtyBuffers}
        onClose={() => setWorkspaceMode("editor")}
        onRestoreCompleted={() => {
          setDirtyBuffers({});
          if (activeProject) {
            setManualHunks([]);
            fetch(`/api/projects/${activeProject.project_id}/files`)
              .then((res) => res.json())
              .then((data: { files?: FileItem[] }) => {
                const newFiles = data.files || [];
                setFiles(newFiles);
                const fileStillExists = newFiles.some((f) => f.path === activeFile);
                const targetFile = fileStillExists ? activeFile : newFiles[0]?.path || "main.tex";
                if (!fileStillExists) {
                  setActiveFile(targetFile);
                }
                fetch(`/api/projects/${activeProject.project_id}/files/${targetFile}`)
                  .then((res) => {
                    if (!res.ok) throw new Error("File not found");
                    return res.json();
                  })
                  .then((fData: { content?: string; etag?: string }) => {
                    setFileContent(fData.content !== undefined ? fData.content : "");
                    setFileEtag(fData.etag !== undefined ? fData.etag : null);
                    setFileContentPath(targetFile);
                    setEditorReloadKey((k) => k + 1);
                  })
                  .catch(() => {
                    setFileContent("");
                    setFileEtag(null);
                    setFileContentPath(targetFile);
                    setEditorReloadKey((k) => k + 1);
                  });
              })
              .catch(() => {});
            handleTriggerCompile();
          }
        }}
      />
    ) : null;

  if (loading) {
    return (
      <div className="lemma-loading" data-testid="app-loading">
        Loading workspace...
      </div>
    );
  }

  return (
    <div className="lemma-app-shell dark">
      {insecureHttpWarning && (
        <div role="alert" className="lemma-http-warning-banner" data-testid="http-warning-banner">
          Warning: Workspace is served over unencrypted HTTP on a non-localhost address. Configure an HTTPS reverse proxy.
        </div>
      )}

      {!authenticated ? (
        <main className="lemma-login-container">
          <form onSubmit={handleLoginSubmit} className="lemma-login-card">
            <h1>Lemma</h1>
            <label htmlFor="workspace-password-input">Workspace Password</label>
            <input
              id="workspace-password-input"
              type="password"
              aria-label="Workspace Password"
              placeholder="Enter workspace password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            {loginError && <p role="alert">{loginError}</p>}
            <button type="submit" aria-label="Sign In">
              Sign In
            </button>
          </form>
        </main>
      ) : (
        <EnabledModelsProvider>
        <div className="lemma-authenticated-workspace">
          {!privacyConsentAccepted && (
            <div className="lemma-modal-backdrop">
              <div role="dialog" aria-modal="true" aria-label="Manuscript Privacy Consent" className="lemma-consent-modal">
                <div className="lemma-consent-badge">Privacy &amp; Data Protection</div>
                <h2>Manuscript Privacy &amp; OpenRouter Notice</h2>
                <p>
                  Prompts, manuscript excerpts, and rendered PDF page images are transmitted to your selected OpenRouter models with Zero Data Retention (ZDR) enabled by default.
                </p>
                <div className="lemma-modal-actions">
                  <button
                    type="button"
                    className="lemma-btn lemma-btn-primary"
                    aria-label="Accept Privacy Notice"
                    onClick={handleAcceptConsent}
                  >
                    Accept Privacy Notice
                  </button>
                </div>
              </div>
            </div>
          )}

          <ToolsMenuModal
            projectId={activeProject?.project_id || "default"}
            csrfToken={csrfToken || (typeof localStorage !== "undefined" ? localStorage.getItem("lemma_csrf_token") || "" : "")}
            isOpen={toolsModalTab !== null}
            initialTab={toolsModalTab || "health_check"}
            onClose={() => setToolsModalTab(null)}
            onHunkProposed={(hunk) => setManualHunks((prev) => [...prev, hunk])}
            onJumpToLine={requestJump}
            rootTex={activeProject?.root_tex || "main.tex"}
            filePath={activeFile}
          />

          <ZoteroWizardModal
            isOpen={zoteroWizardOpen}
            projectId={activeProject?.project_id || "default"}
            csrfToken={csrfToken || (typeof localStorage !== "undefined" ? localStorage.getItem("lemma_csrf_token") || "" : "")}
            onClose={() => setZoteroWizardOpen(false)}
            onDisconnected={() => {
              const projectId = activeProject?.project_id;
              if (projectId) {
                setProjects((prev) =>
                  prev.map((p) => (p.project_id === projectId ? { ...p, zotero_collection_key: null } : p))
                );
                setActiveProject((prev) =>
                  prev && prev.project_id === projectId ? { ...prev, zotero_collection_key: null } : prev
                );
              }
              setZoteroPullStatus(null);
            }}
            onSyncCompleted={(summary) => {
              const projectId = activeProject?.project_id;
              if (projectId) {
                setProjects((prev) =>
                  prev.map((p) => (p.project_id === projectId ? { ...p, zotero_collection_key: summary.collection_key } : p))
                );
                handleDirtyChange("references.bib", null);
              }
              setActiveProject((prev) =>
                prev ? { ...prev, zotero_collection_key: summary.collection_key } : prev
              );
              setFileLoadNonce((n) => n + 1);
              setEditorReloadKey((k) => k + 1);
              if (activeProject) {
                fetch(`/api/projects/${activeProject.project_id}/files`)
                  .then((res) => res.json())
                  .then((data: { files?: FileItem[] }) => setFiles(data.files || []))
                  .catch(() => {});
              }
            }}
          />

          <GitHubConnectorModal
            isOpen={githubOpen}
            projectId={activeProject?.project_id || "default"}
            csrfToken={csrfToken || (typeof localStorage !== "undefined" ? localStorage.getItem("lemma_csrf_token") || "" : "")}
            onClose={() => setGithubOpen(false)}
            onBusyChange={setGithubBusy}
            onLinkChange={setGithubLinked}
            beforeSync={saveDirtyBuffersBeforeSync}
            onPullCompleted={reloadAfterGithubPull}
          />

            {hubOpen && projectsHub}
            <div
              className="lemma-three-pane-grid"
              data-testid="pane-grid"
              hidden={hubOpen}
              ref={gridRef}
              style={{
                "--pane-left-w": `${effectivePanes.widths.left}px`,
                "--pane-right-w": `${effectivePanes.widths.right}px`,
              } as React.CSSProperties}
            >
              <aside className="lemma-left-pane" data-testid="left-sidebar-pane" hidden={!effectivePanes.visible.left}>
                <LeftSidebar
                  activeProject={activeProject}
                  onOpenProjects={() => setHubOpen(true)}
                  onUpdateRootTex={async (rootTex) => {
                    if (!activeProject) return null;
                    try {
                      const res = await fetch(`/api/projects/${activeProject.project_id}/settings`, {
                        method: "PATCH",
                        headers: {
                          "Content-Type": "application/json",
                          "X-CSRF-Token": csrfToken || localStorage.getItem("lemma_csrf_token") || "",
                        },
                        body: JSON.stringify({ root_tex: rootTex }),
                      });
                      if (!res.ok) {
                        const data = await res.json().catch(() => null);
                        return typeof data?.detail === "string" ? data.detail : `Could not change the main document (HTTP ${res.status}).`;
                      }
                      const updated: ProjectItem = await res.json();
                      setProjects((prev) => prev.map((p) => (p.project_id === updated.project_id ? updated : p)));
                      setActiveProject(updated);
                      return null;
                    } catch {
                      return "Could not change the main document: network error.";
                    }
                  }}
                  activeFile={activeFile}
                  files={files}
                  onSelectFile={setActiveFile}
                  onUploadFile={uploadProjectFile}
                  onCreateFile={async (path) => {
                    if (!activeProject) return "Open a project first.";
                    if (files.some((f) => f.path === path)) return `${path} already exists.`;
                    const projectId = activeProject.project_id;
                    try {
                      const res = await fetch(`/api/projects/${projectId}/files/${path}`, {
                        method: "PUT",
                        headers: {
                          "Content-Type": "application/json",
                          "X-CSRF-Token": csrfToken || localStorage.getItem("lemma_csrf_token") || "",
                        },
                        body: JSON.stringify({ content: "" }),
                      });
                      if (!res.ok) {
                        const data = await res.json().catch(() => null);
                        return typeof data?.detail === "string" ? data.detail : `Could not create ${path} (HTTP ${res.status}).`;
                      }
                      const listRes = await fetch(`/api/projects/${projectId}/files`);
                      const data = (await listRes.json()) as { files?: FileItem[]; folders?: string[] };
                      if (activeProjectIdRef.current === projectId) {
                        setFiles(data.files || []);
                        setFolders(data.folders || []);
                        setActiveFile(path);
                      }
                      return null;
                    } catch {
                      return `Could not create ${path}: network error.`;
                    }
                  }}
                  folders={folders}
                  onCreateFolder={async (path) => {
                    if (!activeProject) return "Open a project first.";
                    const projectId = activeProject.project_id;
                    try {
                      const res = await fetch(`/api/projects/${projectId}/folders`, {
                        method: "POST",
                        headers: {
                          "Content-Type": "application/json",
                          "X-CSRF-Token": csrfToken || localStorage.getItem("lemma_csrf_token") || "",
                        },
                        body: JSON.stringify({ path }),
                      });
                      if (!res.ok) {
                        const data = await res.json().catch(() => null);
                        return typeof data?.detail === "string" ? data.detail : `Could not create ${path} (HTTP ${res.status}).`;
                      }
                      if (activeProjectIdRef.current === projectId) setFolders((prev) => [...prev, path]);
                      return null;
                    } catch {
                      return `Could not create ${path}: network error.`;
                    }
                  }}
                  onDeletePath={async (path, isDir) => {
                    if (!activeProject) return "Open a project first.";
                    const projectId = activeProject.project_id;
                    try {
                      const res = await fetch(`/api/projects/${projectId}/${isDir ? "folders" : "files"}/${path}`, {
                        method: "DELETE",
                        headers: { "X-CSRF-Token": csrfToken || localStorage.getItem("lemma_csrf_token") || "" },
                      });
                      if (!res.ok) {
                        const data = await res.json().catch(() => null);
                        return typeof data?.detail === "string" ? data.detail : `Could not delete ${path} (HTTP ${res.status}).`;
                      }
                      if (activeProjectIdRef.current !== projectId) return null;
                      const removed = (p: string) => p === path || p.startsWith(`${path}/`);
                      setFiles((prev) => prev.filter((f) => !removed(f.path)));
                      setFolders((prev) => prev.filter((f) => !removed(f)));
                      setDirtyBuffers((prev) => {
                        const next = Object.fromEntries(Object.entries(prev).filter(([p]) => !removed(p)));
                        return Object.keys(next).length === Object.keys(prev).length ? prev : next;
                      });
                      if (activeFile && removed(activeFile)) setActiveFile(activeProject.root_tex);
                      return null;
                    } catch {
                      return `Could not delete ${path}: network error.`;
                    }
                  }}
                  onMovePath={async (fromPath, toPath) => {
                    if (!activeProject) return "Open a project first.";
                    const projectId = activeProject.project_id;
                    try {
                      const res = await fetch(`/api/projects/${projectId}/move`, {
                        method: "POST",
                        headers: {
                          "Content-Type": "application/json",
                          "X-CSRF-Token": csrfToken || localStorage.getItem("lemma_csrf_token") || "",
                        },
                        body: JSON.stringify({ from_path: fromPath, to_path: toPath }),
                      });
                      if (!res.ok) {
                        const data = await res.json().catch(() => null);
                        return typeof data?.detail === "string" ? data.detail : `Could not move ${fromPath} (HTTP ${res.status}).`;
                      }
                      const { path: newPath, project: updated } = (await res.json()) as { path: string; project: ProjectItem };
                      if (activeProjectIdRef.current !== projectId) return null;
                      const remap = (p: string) =>
                        p === fromPath ? newPath : p.startsWith(`${fromPath}/`) ? newPath + p.slice(fromPath.length) : p;
                      setDirtyBuffers((prev) => {
                        const keys = Object.keys(prev);
                        if (!keys.some((p) => remap(p) !== p)) return prev;
                        return Object.fromEntries(keys.map((p) => [remap(p), prev[p]]));
                      });
                      if (activeFile) setActiveFile(remap(activeFile));
                      setProjects((prev) => prev.map((p) => (p.project_id === updated.project_id ? updated : p)));
                      setActiveProject(updated);
                      const listRes = await fetch(`/api/projects/${projectId}/files`);
                      const data = (await listRes.json()) as { files?: FileItem[]; folders?: string[] };
                      if (activeProjectIdRef.current === projectId) {
                        setFiles(data.files || []);
                        setFolders(data.folders || []);
                      }
                      return null;
                    } catch {
                      return `Could not move ${fromPath}: network error.`;
                    }
                  }}
                  outline={outline}
                  onJumpToLine={requestJump}
                  chats={chats}
                  activeChatId={activeChatId}
                  onSelectChat={(chatId) => {
                    setActiveChatId(chatId);
                    setChatSelectNonce((n) => n + 1);
                  }}
                  githubLinked={githubLinked}
                  onSyncRepo={githubLinked ? handleGithubSync : undefined}
                  syncingRepo={githubBusy}
                />
                <CandidateSourcesTray
                  projectId={activeProject ? activeProject.project_id : ""}
                  csrfToken={csrfToken || (typeof localStorage !== "undefined" ? localStorage.getItem("lemma_csrf_token") || "" : "")}
                  onDecisionApplied={() => {
                    if (activeProject) {
                      fetch(`/api/projects/${activeProject.project_id}/files`)
                        .then((res) => res.json())
                        .then((data: { files?: FileItem[] }) => setFiles(data.files || []))
                        .catch(() => {});
                    }
                  }}
                />
              </aside>
              <ResizeHandle
                ariaLabel="Resize sidebar"
                width={{ now: effectivePanes.widths.left, min: MIN_WIDTHS.left, max: maxPaneWidth("left") }}
                onDrag={(delta) => (effectivePanes.visible.left ? resizePane("left", delta) : 0)}
                onToggleCollapse={() => togglePane("left")}
              />
              <main
                className="lemma-center-pane"
                data-testid="center-editor-pane"
                hidden={workspaceMode === "editor" && !effectivePanes.visible.center}
              >
                {placeView ? (
                  placeView
                ) : activeProject ? (
                  <>
                    <LatexEditor
                      projectId={activeProject.project_id}
                      filePath={activeFile}
                      initialContent={fileContent}
                      initialEtag={fileEtag}
                      contentFilePath={fileContentPath}
                      draftContent={dirtyBuffers[activeFile]}
                      reloadKey={editorReloadKey}
                      cursorLine={cursorLine}
                      jumpNonce={jumpNonce}
                      flashOnMount={flashOnMountRef.current}
                      snapToHeading={jumpSnapToHeading}
                      diagnostics={diagnostics}
                      proposedHunks={proposedHunks}
                      csrfToken={csrfToken || (typeof localStorage !== "undefined" ? localStorage.getItem("lemma_csrf_token") || "" : "")}
                      onForwardSync={handleForwardSync}
                      pdfAvailable={Boolean(pdfUrl)}
                      onDirtyChange={handleDirtyChange}
                      onEditorContext={setEditorContext}
                      onSaveSuccess={(newEtag, newContent) => {
                        setFileEtag(newEtag);
                        setFileContent(newContent);
                      }}
                      readOnly={githubBusy || githubReloadBlocked}
                      onTriggerCompile={handleTriggerCompile}
                      onProposeHunk={(hunk) => setManualHunks((prev) => [...prev, hunk])}
                      onHunkResolved={(hunkId) => {
                        setManualHunks((prev) => prev.filter((h) => h.hunk_id !== hunkId));
                        if (activeProject && activeChatId) {
                          agentRun.resolveChatHunk(activeProject.project_id, activeChatId, hunkId);
                        }
                      }}
                      zoteroSynced={Boolean(activeFile.endsWith(".bib") && activeProject.zotero_collection_key)}
                      onZoteroSync={
                        activeFile.endsWith(".bib") && activeProject.zotero_collection_key
                          ? handleZoteroPull
                          : undefined
                      }
                      onZoteroDisconnect={
                        activeFile.endsWith(".bib") && activeProject.zotero_collection_key
                          ? handleZoteroDisconnect
                          : undefined
                      }
                      zoteroSyncBusy={zoteroPullBusy}
                      bibEntries={bibEntries}
                      zoteroDisconnectBusy={zoteroDisconnectBusy}
                      zoteroSyncDisabledReason={
                        dirtyBuffers[activeFile] !== undefined ? "Save your changes before syncing from Zotero" : null
                      }
                      zoteroSyncStatus={zoteroPullStatus}
                    />
                  </>
                ) : (
                  <div className="lemma-empty-workspace-prompt">
                    <div className="lemma-empty-workspace-card">
                      <img className="lemma-empty-icon" src="/favicon.svg" alt="" width={40} height={40} />
                      <h2>Lemma</h2>
                      <p>
                        No project is currently open. Create your first LaTeX manuscript to start writing with AI drafting, citation grounding, and PDF preview.
                      </p>
                      <div className="lemma-empty-create-row">
                        <input
                          type="text"
                          aria-label="New Project Name"
                          placeholder="Project name (e.g. Research Manuscript)"
                          value={newProjectName}
                          onChange={(e) => setNewProjectName(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" && newProjectName.trim()) {
                              handleCreateProject(newProjectName.trim());
                              setNewProjectName("");
                            }
                          }}
                        />
                        <button
                          type="button"
                          className="lemma-btn lemma-btn-primary"
                          onClick={() => {
                            if (newProjectName.trim()) {
                              handleCreateProject(newProjectName.trim());
                              setNewProjectName("");
                            }
                          }}
                        >
                          Create Project
                        </button>
                      </div>
                      {createProjectError && (
                        <p className="lemma-error" role="alert">
                          {createProjectError}
                        </p>
                      )}
                      <div className="lemma-empty-feature-pills">
                        <span>Grounded Citations</span>
                        <span>Inline Diff Review</span>
                        <span>SyncTeX PDF Preview</span>
                      </div>
                    </div>
                  </div>
                )}
              </main>
              {/* Outside the center pane so the assistant stays reachable while the editor is hidden.
                  Inset to the center column while it is visible so the panel keeps its usual footprint; spans the grid otherwise. */}
              {activeProject && workspaceMode === "editor" && (
                <div
                  className="lemma-chat-layer"
                  style={{
                    "--chat-layer-left": effectivePanes.visible.center && effectivePanes.visible.left ? `${effectivePanes.widths.left + 4}px` : "0px",
                    "--chat-layer-right": effectivePanes.visible.center && effectivePanes.visible.right ? `${effectivePanes.widths.right + 4}px` : "0px",
                  } as React.CSSProperties}
                >
                  <FloatingChatBar
                    projectId={activeProject.project_id}
                    activeFile={activeFile}
                    pendingCount={proposedHunks.length}
                    cursorLine={editorContext?.filePath === activeFile ? editorContext.cursorLine : undefined}
                    selection={editorContext?.filePath === activeFile ? editorContext.selection : null}
                    bufferText={
                      editorContext?.filePath === activeFile
                        ? editorContext.content
                        : dirtyBuffers[activeFile]
                    }
                    onProposeHunks={(hunks) => setManualHunks(hunks)}
                    chatId={activeChatId}
                    chatSelectNonce={chatSelectNonce}
                    onChatActivity={handleChatActivity}
                    onNewChat={() => setActiveChatId(null)}
                    projectFiles={files.filter((f) => !f.is_dir).map((f) => f.path)}
                    dirtyBuffers={dirtyBuffers}
                    onUploadFile={uploadProjectFile}
                    agentRun={agentRun}
                  />
                </div>
              )}
              <ResizeHandle
                ariaLabel="Resize PDF preview"
                width={{ now: effectivePanes.widths.right, min: MIN_WIDTHS.right, max: maxPaneWidth("right") }}
                onDrag={(delta) => (effectivePanes.visible.right ? -resizePane("right", -delta) : 0)}
                onToggleCollapse={() => togglePane("right")}
              />
              <section className="lemma-right-pane" data-testid="right-pdf-pane" hidden={!effectivePanes.visible.right}>
                <PdfPreview
                  projectId={activeProject ? activeProject.project_id : ""}
                  status={compileStatus}
                  pdfUrl={pdfUrl}
                  diagnostics={diagnostics}
                  forwardHighlight={forwardHighlight}
                  isStale={editedSinceCompile}
                  syncNotice={syncNotice}
                  onJumpToLine={requestJump}
                  onSyncJump={(file, line) => requestJump(file, line, { snapToHeading: true })}
                  onRecompile={handleTriggerCompile}
                />
              </section>
              {renderWorkspaceTools()}
            </div>
        </div>
        </EnabledModelsProvider>
      )}
    </div>
  );
}

export default App;
