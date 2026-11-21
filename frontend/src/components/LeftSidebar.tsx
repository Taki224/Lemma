import React, { useState } from "react";

export type TexEngine = "pdflatex" | "xelatex";

export interface ProjectItem {
  project_id: string;
  name: string;
  root_tex: string;
  tex_engine?: TexEngine;
  zotero_collection_key?: string | null;
  academic_integrity_mode?: boolean;
  updated_at?: string;
}

export interface FileItem {
  path: string;
  name: string;
  is_dir: boolean;
  size_bytes: number;
  etag?: string | null;
}

export interface OutlineNodeItem {
  title: string;
  level: "part" | "chapter" | "section" | "subsection" | "subsubsection";
  file_path: string;
  line_number: number;
  children: OutlineNodeItem[];
}

export interface ChatSummary {
  chat_id: string;
  title: string;
  updated_at: string;
  pinned?: boolean;
}

function formatChatTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  if (date.toDateString() === now.toDateString()) {
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

export interface LeftSidebarProps {
  activeProject: ProjectItem | null;
  onOpenProjects?: () => void;
  onUpdateRootTex?: (rootTex: string) => Promise<string | null>;
  activeFile: string | null;
  files?: FileItem[];
  onSelectFile: (filePath: string) => void;
  onUploadFile?: (file: File, folder?: string) => Promise<string | null>;
  onCreateFile?: (path: string) => Promise<string | null>;
  folders?: string[];
  onCreateFolder?: (path: string) => Promise<string | null>;
  onDeletePath?: (path: string, isDir: boolean) => Promise<string | null>;
  onMovePath?: (fromPath: string, toPath: string) => Promise<string | null>;
  outline?: OutlineNodeItem[];
  onJumpToLine: (filePath: string, lineNumber: number) => void;
  chats?: ChatSummary[];
  activeChatId?: string | null;
  onSelectChat?: (chatId: string) => void;
  onPinChat?: (chatId: string, pinned: boolean) => Promise<string | null>;
  onRenameChat?: (chatId: string, title: string) => Promise<string | null>;
  onDeleteChat?: (chatId: string) => Promise<string | null>;
  githubLinked?: boolean;
  onSyncRepo?: () => Promise<string | null> | void;
  syncingRepo?: boolean;
}

function renderOutlineNodes(
  nodes: OutlineNodeItem[],
  onSelectFile: (filePath: string) => void,
  onJumpToLine: (filePath: string, lineNumber: number) => void
): React.ReactElement[] {
  return nodes.map((node, idx) => (
    <li key={`${node.file_path}:${node.line_number}:${idx}`} className={`lemma-outline-item level-${node.level}`}>
      <button
        type="button"
        className="lemma-outline-btn"
        aria-label={`${node.title} (${node.file_path}:${node.line_number})`}
        onClick={() => {
          onSelectFile(node.file_path);
          onJumpToLine(node.file_path, node.line_number);
        }}
      >
        {node.title}
      </button>
      {node.children && node.children.length > 0 && (
        <ul className="lemma-outline-children">
          {renderOutlineNodes(node.children, onSelectFile, onJumpToLine)}
        </ul>
      )}
    </li>
  ));
}

interface FileTreeNode {
  name: string;
  path: string;
  is_dir: boolean;
  children: FileTreeNode[];
}

function glyphKind(name: string, isDir: boolean): string {
  if (isDir) return "dir";
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  if (ext === "tex" || ext === "cls" || ext === "sty") return "tex";
  if (ext === "bib") return "bib";
  if (ext === "pdf") return "pdf";
  if (ext === "png" || ext === "jpg" || ext === "jpeg" || ext === "svg" || ext === "webp") return "img";
  if (ext === "md") return "md";
  return "file";
}

function FileGlyph({ kind }: { kind: string }): React.ReactElement {
  return (
    <svg className={`lemma-file-glyph kind-${kind}`} width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">
      {kind === "dir" ? (
        <path d="M1.5 3.8h4.2l1.3 1.4H14.5V13H1.5V3.8z" fill="currentColor" />
      ) : (
        <path d="M4 1.6h5.1L13 5.4V14.4H4V1.6z" fill="none" stroke="currentColor" strokeWidth="1.2" />
      )}
    </svg>
  );
}

function buildFileTree(files: FileItem[], folders: string[]): FileTreeNode[] {
  const root: FileTreeNode[] = [];
  const dirs = new Map<string, FileTreeNode>();

  const ensureDir = (path: string, name: string, parent: FileTreeNode[]): FileTreeNode => {
    const existing = dirs.get(path);
    if (existing) return existing;
    const node: FileTreeNode = { name, path, is_dir: true, children: [] };
    dirs.set(path, node);
    parent.push(node);
    return node;
  };

  for (const folder of folders) {
    let parent = root;
    let acc = "";
    for (const part of folder.split("/").filter(Boolean)) {
      acc = acc ? `${acc}/${part}` : part;
      parent = ensureDir(acc, part, parent).children;
    }
  }

  for (const file of files) {
    const parts = file.path.split("/").filter(Boolean);
    if (parts.length === 0) continue;
    let parent = root;
    let acc = "";
    parts.forEach((part, index) => {
      acc = acc ? `${acc}/${part}` : part;
      const last = index === parts.length - 1;
      if (!last || file.is_dir) {
        const dir = ensureDir(acc, part, parent);
        parent = dir.children;
      } else {
        parent.push({
          name: file.name || part,
          path: file.path,
          is_dir: false,
          children: [],
        });
      }
    });
  }

  const sortNodes = (nodes: FileTreeNode[]) => {
    nodes.sort((a, b) => Number(b.is_dir) - Number(a.is_dir) || a.name.localeCompare(b.name));
    nodes.forEach((node) => sortNodes(node.children));
  };
  sortNodes(root);
  return root;
}

export function LeftSidebar({
  activeProject,
  onOpenProjects,
  onUpdateRootTex,
  activeFile,
  files = [],
  onSelectFile,
  onUploadFile,
  onCreateFile,
  folders = [],
  onCreateFolder,
  onDeletePath,
  onMovePath,
  outline = [],
  onJumpToLine,
  chats = [],
  activeChatId = null,
  onSelectChat,
  onPinChat,
  onRenameChat,
  onDeleteChat,
  githubLinked,
  onSyncRepo,
  syncingRepo = false,
}: LeftSidebarProps): React.ReactElement {
  const [activeTab, setActiveTab] = useState<"files" | "chats">("files");
  const [outlineExpanded, setOutlineExpanded] = useState<boolean>(true);
  const [rootTexSaving, setRootTexSaving] = useState<boolean>(false);
  const [rootTexError, setRootTexError] = useState<string | null>(null);
  const [filterOpen, setFilterOpen] = useState<boolean>(false);
  const [filterText, setFilterText] = useState<string>("");
  const [collapsedDirs, setCollapsedDirs] = useState<Record<string, boolean>>({});

  const [newMenuOpen, setNewMenuOpen] = useState<boolean>(false);
  const [newFileKind, setNewFileKind] = useState<"tex" | "bib" | "folder" | null>(null);
  const [newFileName, setNewFileName] = useState<string>("");
  const [newFileError, setNewFileError] = useState<string | null>(null);
  const [newFileSaving, setNewFileSaving] = useState<boolean>(false);
  const newMenuRef = React.useRef<HTMLDivElement>(null);
  const newFileInputRef = React.useRef<HTMLInputElement>(null);
  const [uploadStatus, setUploadStatus] = useState<{ text: string; error: boolean } | null>(null);

  const [rowMenu, setRowMenu] = useState<{ path: string; isDir: boolean; top: number; right: number } | null>(null);
  const rowMenuRef = React.useRef<HTMLDivElement>(null);
  const [renamingPath, setRenamingPath] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState<string>("");
  const [renameError, setRenameError] = useState<string | null>(null);
  const [renameSaving, setRenameSaving] = useState<boolean>(false);
  const renameInputRef = React.useRef<HTMLInputElement>(null);
  const [pendingDelete, setPendingDelete] = useState<{ path: string; isDir: boolean } | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<boolean>(false);
  const [dragPath, setDragPath] = useState<string | null>(null);
  const [dropTarget, setDropTarget] = useState<string | null>(null);

  const [chatMenu, setChatMenu] = useState<{ chat: ChatSummary; top: number; right: number } | null>(null);
  const chatMenuRef = React.useRef<HTMLDivElement>(null);
  const [renamingChatId, setRenamingChatId] = useState<string | null>(null);
  const [chatRenameDraft, setChatRenameDraft] = useState<string>("");
  const [chatRenameError, setChatRenameError] = useState<string | null>(null);
  const [chatRenameSaving, setChatRenameSaving] = useState<boolean>(false);
  const chatRenameInputRef = React.useRef<HTMLInputElement>(null);
  const [pendingDeleteChat, setPendingDeleteChat] = useState<ChatSummary | null>(null);
  const [chatDeleteError, setChatDeleteError] = useState<string | null>(null);
  const [deletingChat, setDeletingChat] = useState<boolean>(false);

  const uploadFile = async (file: File, folder = "") => {
    if (!onUploadFile) return;
    setUploadStatus({ text: `Uploading ${file.name}…`, error: false });
    const error = await onUploadFile(file, folder);
    setUploadStatus(error ? { text: error, error: true } : null);
  };

  React.useEffect(() => {
    const input = newFileInputRef.current;
    if (!newFileKind || !input) return;
    input.focus();
    const start = input.value.lastIndexOf("/") + 1;
    const dot = input.value.lastIndexOf(".");
    input.setSelectionRange(start, dot > start ? dot : input.value.length);
  }, [newFileKind]);

  React.useEffect(() => {
    const input = renameInputRef.current;
    if (!renamingPath || !input) return;
    input.focus();
    const start = input.value.lastIndexOf("/") + 1;
    const dot = input.value.lastIndexOf(".");
    input.setSelectionRange(start, dot > start ? dot : input.value.length);
  }, [renamingPath]);

  React.useEffect(() => {
    const input = chatRenameInputRef.current;
    if (!renamingChatId || !input) return;
    input.focus();
    input.select();
  }, [renamingChatId]);

  React.useEffect(() => {
    setRootTexError(null);
    setNewFileKind(null);
    setUploadStatus(null);
    setRowMenu(null);
    setRenamingPath(null);
    setPendingDelete(null);
    setChatMenu(null);
    setRenamingChatId(null);
    setPendingDeleteChat(null);
  }, [activeProject?.project_id]);

  React.useEffect(() => {
    if (!rowMenu && !chatMenu) return;
    const close = (e: MouseEvent) => {
      if (rowMenu && rowMenuRef.current && !rowMenuRef.current.contains(e.target as Node)) setRowMenu(null);
      if (chatMenu && chatMenuRef.current && !chatMenuRef.current.contains(e.target as Node)) setChatMenu(null);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setRowMenu(null);
        setChatMenu(null);
      }
    };
    const onScroll = () => {
      setRowMenu(null);
      setChatMenu(null);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", onScroll);
    document.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", onScroll);
      document.removeEventListener("scroll", onScroll, true);
    };
  }, [rowMenu, chatMenu]);

  React.useEffect(() => {
    if (!newMenuOpen) return;
    const close = (e: MouseEvent) => {
      if (newMenuRef.current && !newMenuRef.current.contains(e.target as Node)) setNewMenuOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setNewMenuOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [newMenuOpen]);

  const startNewFile = (kind: "tex" | "bib" | "folder", dir = "") => {
    const taken = new Set([...files.map((f) => f.path), ...folders]);
    const prefix = dir ? `${dir}/` : "";
    const base = kind === "bib" ? "references" : kind === "folder" ? "new-folder" : "untitled";
    const ext = kind === "folder" ? "" : `.${kind}`;
    let name = `${prefix}${base}${ext}`;
    for (let n = 2; taken.has(name); n += 1) name = `${prefix}${base}-${n}${ext}`;
    setNewMenuOpen(false);
    setRowMenu(null);
    setActiveTab("files");
    if (dir) setCollapsedDirs((prev) => ({ ...prev, [dir]: false }));
    setNewFileKind(kind);
    setNewFileName(name);
    setNewFileError(null);
  };

  const submitNewFile = async () => {
    if (!newFileKind) return;
    let path = newFileName.trim().replace(/^\/+/, "").replace(/\/+$/, "");
    if (!path) {
      setNewFileError(newFileKind === "folder" ? "Enter a folder name." : "Enter a file name.");
      return;
    }
    const create = newFileKind === "folder" ? onCreateFolder : onCreateFile;
    if (!create) return;
    if (newFileKind !== "folder" && !path.toLowerCase().endsWith(`.${newFileKind}`)) path = `${path}.${newFileKind}`;
    setNewFileSaving(true);
    setNewFileError(null);
    const error = await create(path);
    setNewFileSaving(false);
    if (error) {
      setNewFileError(error);
      return;
    }
    setNewFileKind(null);
  };

  const rootTex = activeProject?.root_tex || "main.tex";
  const texFiles = files.filter((f) => !f.is_dir && f.path.toLowerCase().endsWith(".tex")).map((f) => f.path);
  if (activeProject && !texFiles.includes(rootTex)) texFiles.unshift(rootTex);

  const changeRootTex = async (next: string) => {
    if (!onUpdateRootTex || next === rootTex) return;
    setRootTexSaving(true);
    setRootTexError(null);
    const error = await onUpdateRootTex(next);
    setRootTexSaving(false);
    setRootTexError(error);
  };

  const handleTabKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      setActiveTab((prev) => (prev === "files" ? "chats" : "files"));
    }
  };

  const startRename = (path: string) => {
    setRowMenu(null);
    setRenamingPath(path);
    setRenameDraft(path);
    setRenameError(null);
  };

  const submitRename = async () => {
    if (!renamingPath || !onMovePath) return;
    const target = renameDraft.trim().replace(/^\/+/, "").replace(/\/+$/, "");
    if (!target) {
      setRenameError("Enter a name.");
      return;
    }
    if (target === renamingPath) {
      setRenamingPath(null);
      return;
    }
    setRenameSaving(true);
    setRenameError(null);
    const error = await onMovePath(renamingPath, target);
    setRenameSaving(false);
    if (error) {
      setRenameError(error);
      return;
    }
    setRenamingPath(null);
  };

  const confirmDelete = async () => {
    if (!pendingDelete || !onDeletePath) return;
    setDeleting(true);
    setDeleteError(null);
    const error = await onDeletePath(pendingDelete.path, pendingDelete.isDir);
    setDeleting(false);
    if (error) {
      setDeleteError(error);
      return;
    }
    setPendingDelete(null);
  };

  const baseName = (path: string) => path.split("/").pop() ?? path;
  const parentDir = (path: string) => (path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "");
  const canDropInto = (source: string | null, dir: string) =>
    !!source && source !== dir && !dir.startsWith(`${source}/`) && parentDir(source) !== dir;

  const dropOn = (dir: string) => {
    const source = dragPath;
    setDragPath(null);
    setDropTarget(null);
    if (!source || !onMovePath || !canDropInto(source, dir)) return;
    void onMovePath(source, dir ? `${dir}/${baseName(source)}` : baseName(source)).then((error) => {
      if (error) setUploadStatus({ text: error, error: true });
    });
  };

  const dropHandlers = (dir: string) => ({
    onDragOver: (e: React.DragEvent) => {
      if (!canDropInto(dragPath, dir)) return;
      e.preventDefault();
      e.stopPropagation();
      e.dataTransfer.dropEffect = "move";
      if (dropTarget !== dir) setDropTarget(dir);
    },
    onDrop: (e: React.DragEvent) => {
      if (!canDropInto(dragPath, dir)) return;
      e.preventDefault();
      e.stopPropagation();
      dropOn(dir);
    },
  });

  const dragHandlers = (path: string) =>
    onMovePath
      ? {
          draggable: true,
          onDragStart: (e: React.DragEvent) => {
            e.dataTransfer.effectAllowed = "move";
            e.dataTransfer.setData("text/plain", path);
            setDragPath(path);
            setRowMenu(null);
          },
          onDragEnd: () => {
            setDragPath(null);
            setDropTarget(null);
          },
        }
      : {};

  const openRowMenu = (e: React.MouseEvent<HTMLButtonElement>, path: string, isDir: boolean) => {
    if (rowMenu?.path === path) {
      setRowMenu(null);
      return;
    }
    const rect = e.currentTarget.getBoundingClientRect();
    setRowMenu({ path, isDir, top: rect.bottom + 4, right: window.innerWidth - rect.right });
  };

  const hasChatActions = Boolean(onPinChat || onRenameChat || onDeleteChat);

  const openChatMenu = (e: React.MouseEvent<HTMLButtonElement>, chat: ChatSummary) => {
    setRowMenu(null);
    if (chatMenu?.chat.chat_id === chat.chat_id) {
      setChatMenu(null);
      return;
    }
    const rect = e.currentTarget.getBoundingClientRect();
    setChatMenu({ chat, top: rect.bottom + 4, right: window.innerWidth - rect.right });
  };

  const startChatRename = (chat: ChatSummary) => {
    setChatMenu(null);
    setRenamingChatId(chat.chat_id);
    setChatRenameDraft(chat.title);
    setChatRenameError(null);
  };

  const submitChatRename = async () => {
    if (!renamingChatId || !onRenameChat) return;
    const target = chatRenameDraft.trim();
    if (!target) {
      setChatRenameError("Enter a title.");
      return;
    }
    const current = chats.find((c) => c.chat_id === renamingChatId);
    if (current && target === current.title) {
      setRenamingChatId(null);
      return;
    }
    setChatRenameSaving(true);
    setChatRenameError(null);
    const error = await onRenameChat(renamingChatId, target);
    setChatRenameSaving(false);
    if (error) {
      setChatRenameError(error);
      return;
    }
    setRenamingChatId(null);
  };

  const confirmChatDelete = async () => {
    if (!pendingDeleteChat || !onDeleteChat) return;
    setDeletingChat(true);
    setChatDeleteError(null);
    const error = await onDeleteChat(pendingDeleteChat.chat_id);
    setDeletingChat(false);
    if (error) {
      setChatDeleteError(error);
      return;
    }
    setPendingDeleteChat(null);
  };

  const hasRowActions = !!(onMovePath || onDeletePath);

  const query = filterText.trim().toLowerCase();
  const visibleFiles = query ? files.filter((file) => file.path.toLowerCase().includes(query)) : files;
  const visibleFolders = query ? folders.filter((folder) => folder.toLowerCase().includes(query)) : folders;
  const fileTree = buildFileTree(visibleFiles, visibleFolders);
  const pendingDeleteCount = pendingDelete?.isDir
    ? files.filter((f) => f.path.startsWith(`${pendingDelete.path}/`)).length
    : 0;

  const renderFileNodes = (nodes: FileTreeNode[], depth: number): React.ReactElement => (
    <ul className="lemma-file-list" aria-label={depth === 0 ? "Project Files" : undefined}>
      {nodes.map((node) => {
        const classes = [
          !node.is_dir && node.path === activeFile ? "active" : "",
          node.is_dir && dropTarget === node.path ? "is-drop-target" : "",
          rowMenu?.path === node.path ? "menu-open" : "",
        ]
          .filter(Boolean)
          .join(" ");
        return (
          <li key={node.path} className={classes} {...(node.is_dir ? dropHandlers(node.path) : {})}>
            {renamingPath === node.path ? (
              <form
                className="lemma-file-rename-row"
                onSubmit={(e) => {
                  e.preventDefault();
                  void submitRename();
                }}
              >
                <input
                  className="lemma-sidebar-filter"
                  aria-label={`Rename or move ${node.path}`}
                  value={renameDraft}
                  ref={renameInputRef}
                  readOnly={renameSaving}
                  onChange={(e) => setRenameDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      void submitRename();
                    } else if (e.key === "Escape") {
                      e.preventDefault();
                      setRenamingPath(null);
                    }
                  }}
                  onBlur={() => {
                    if (!renameSaving && !renameError) setRenamingPath(null);
                  }}
                />
                <span className="lemma-new-file-hint">Edit the path to move · Enter to save</span>
                {renameError && (
                  <p className="lemma-root-tex-error" role="alert">
                    {renameError}
                  </p>
                )}
              </form>
            ) : node.is_dir ? (
              <button
                type="button"
                className="lemma-file-row is-dir"
                aria-expanded={!collapsedDirs[node.path]}
                aria-label={`Folder ${node.path}`}
                onClick={() => setCollapsedDirs((prev) => ({ ...prev, [node.path]: !prev[node.path] }))}
                {...dragHandlers(node.path)}
              >
                <FileGlyph kind="dir" />
                <span className="lemma-file-name">{node.name}</span>
              </button>
            ) : (
              <button
                type="button"
                className="lemma-file-row"
                aria-label={node.path}
                onClick={() => onSelectFile(node.path)}
                {...dragHandlers(node.path)}
              >
                <FileGlyph kind={glyphKind(node.name, false)} />
                <span className="lemma-file-name">{node.name}</span>
                {activeProject && node.path === rootTex && (
                  <span className="lemma-file-root-badge" title="Main document: compiled into the PDF">
                    main
                  </span>
                )}
              </button>
            )}
            {hasRowActions && renamingPath !== node.path && (
              <button
                type="button"
                className="lemma-file-more-btn"
                aria-label={`Actions for ${node.path}`}
                aria-haspopup="menu"
                aria-expanded={rowMenu?.path === node.path}
                onClick={(e) => openRowMenu(e, node.path, node.is_dir)}
              >
                ⋯
              </button>
            )}
            {node.is_dir &&
              !collapsedDirs[node.path] &&
              node.children.length > 0 &&
              renamingPath !== node.path &&
              renderFileNodes(node.children, depth + 1)}
          </li>
        );
      })}
    </ul>
  );

  return (
    <nav className="lemma-left-sidebar" aria-label="Project Navigation">
      <div className="lemma-sidebar-brand">
        <img className="lemma-sidebar-brand-icon" src="/favicon.svg" alt="" width={22} height={22} />
        <span className="lemma-sidebar-brand-name">Lemma</span>
      </div>
      <div className="lemma-project-switcher">
        {onOpenProjects && (
          <button type="button" className="lemma-sidebar-back" onClick={onOpenProjects}>
            <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
              <path d="M7.5 2.5 4 6l3.5 3.5" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            All projects
          </button>
        )}
        <h2 className="lemma-sidebar-project-name" title={activeProject?.name}>
          {activeProject ? activeProject.name : "No project open"}
        </h2>

        {activeProject && (
          <div className="lemma-root-tex-setting" title="The file LaTeX compiles into the PDF">
            <label htmlFor="root-tex-select">Main document</label>
            <select
              id="root-tex-select"
              value={rootTex}
              disabled={!onUpdateRootTex || rootTexSaving || texFiles.length < 2}
              onChange={(e) => void changeRootTex(e.target.value)}
            >
              {texFiles.map((path) => (
                <option key={path} value={path}>
                  {path}
                </option>
              ))}
            </select>
          </div>
        )}
        {rootTexError && (
          <p className="lemma-root-tex-error" role="alert">
            {rootTexError}
          </p>
        )}
      </div>

      <div className="lemma-sidebar-tabs-row">
        <div
          role="tablist"
          aria-label="Sidebar View"
          className="lemma-sidebar-tablist"
          onKeyDown={handleTabKeyDown}
        >
          <button
            type="button"
            role="tab"
            className={activeTab === "files" ? "active" : ""}
            aria-selected={activeTab === "files"}
            aria-label="Files"
            onClick={() => setActiveTab("files")}
          >
            Files
          </button>
          <button
            type="button"
            role="tab"
            className={activeTab === "chats" ? "active" : ""}
            aria-selected={activeTab === "chats"}
            aria-label="Chats"
            onClick={() => setActiveTab("chats")}
          >
            Chats
          </button>
        </div>
        {activeTab === "files" && (
          <div className="lemma-sidebar-tab-actions">
            <button
              type="button"
              className="lemma-sidebar-icon-btn"
              aria-label="Search files"
              aria-pressed={filterOpen}
              title="Search files"
              onClick={() => {
                setFilterOpen((open) => {
                  if (open) setFilterText("");
                  return !open;
                });
              }}
            >
              <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">
                <circle cx="7" cy="7" r="4.25" fill="none" stroke="currentColor" strokeWidth="1.4" />
                <path d="M10.2 10.2 13.5 13.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
              </svg>
            </button>
            <div className="lemma-new-file-menu-anchor" ref={newMenuRef}>
              <button
                type="button"
                className="lemma-sidebar-icon-btn"
                aria-label="New file"
                title="New file"
                aria-haspopup="menu"
                aria-expanded={newMenuOpen}
                aria-pressed={newMenuOpen}
                onClick={() => setNewMenuOpen((open) => !open)}
              >
                <span aria-hidden="true">+</span>
              </button>
              {newMenuOpen && (
                <div className="lemma-new-file-menu" role="menu">
                  {onCreateFile && (
                    <>
                      <button type="button" role="menuitem" className="lemma-tools-item" onClick={() => startNewFile("tex")}>
                        New .tex file
                      </button>
                      <button type="button" role="menuitem" className="lemma-tools-item" onClick={() => startNewFile("bib")}>
                        New .bib file
                      </button>
                    </>
                  )}
                  {onCreateFolder && (
                    <button type="button" role="menuitem" className="lemma-tools-item" onClick={() => startNewFile("folder")}>
                      New folder
                    </button>
                  )}
                  <label role="menuitem" className="lemma-tools-item">
                    Upload file…
                    <input
                      id="sidebar-file-upload"
                      type="file"
                      className="lemma-file-input-hidden"
                      aria-label="Upload PDF / Asset"
                      onChange={(e) => {
                        const f = e.target.files?.[0];
                        e.target.value = "";
                        setNewMenuOpen(false);
                        if (f) void uploadFile(f);
                      }}
                    />
                  </label>
                </div>
              )}
            </div>
            {(githubLinked ?? Boolean(onSyncRepo)) && (
              <button
                type="button"
                className="lemma-sidebar-icon-btn"
                aria-label="Sync repo"
                title="Sync repo"
                disabled={syncingRepo}
                onClick={() => {
                  if (onSyncRepo) void onSyncRepo();
                }}
              >
                <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">
                  <path
                    d="M13.5 8a5.5 5.5 0 0 1-9.39 3.89M2.5 8a5.5 5.5 0 0 1 9.39-3.89"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.4"
                    strokeLinecap="round"
                  />
                  <path
                    d="M10.2 1.8h3v3M5.8 14.2h-3v-3"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.4"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </button>
            )}
          </div>
        )}
      </div>

      {activeTab === "files" && filterOpen && (
        <input
          className="lemma-sidebar-filter"
          aria-label="Filter project files"
          placeholder="Filter files"
          value={filterText}
          onChange={(e) => setFilterText(e.target.value)}
        />
      )}

      {activeTab === "files" && uploadStatus && (
        <p
          className={uploadStatus.error ? "lemma-root-tex-error" : "lemma-new-file-hint"}
          role={uploadStatus.error ? "alert" : "status"}
        >
          {uploadStatus.text}
        </p>
      )}

      {activeTab === "files" && newFileKind && (
        <form
          className="lemma-new-file-row"
          onSubmit={(e) => {
            e.preventDefault();
            void submitNewFile();
          }}
        >
          <input
            className="lemma-sidebar-filter"
            aria-label={newFileKind === "folder" ? "New folder name" : `New .${newFileKind} file name`}
            value={newFileName}
            ref={newFileInputRef}
            readOnly={newFileSaving}
            onChange={(e) => setNewFileName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void submitNewFile();
              } else if (e.key === "Escape") {
                e.preventDefault();
                setNewFileKind(null);
              }
            }}
            onBlur={() => {
              if (!newFileSaving && !newFileError) setNewFileKind(null);
            }}
          />
          <span className="lemma-new-file-hint">Use / for folders · Enter to create · Esc to cancel</span>
          {newFileError && (
            <p className="lemma-root-tex-error" role="alert">
              {newFileError}
            </p>
          )}
        </form>
      )}

      {activeTab === "files" ? (
        <div
          className={`lemma-file-tree-section${dropTarget === "" ? " is-drop-target" : ""}`}
          {...dropHandlers("")}
          onDragLeave={(e) => {
            if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDropTarget(null);
          }}
        >
          {renderFileNodes(fileTree, 0)}
          {dragPath && parentDir(dragPath) !== "" && (
            <p className="lemma-file-drop-hint">Drop here to move to the project root</p>
          )}
        </div>
      ) : chats.length === 0 ? (
        <div className="lemma-chats-list-placeholder">No chats yet. Ask something in the chat bar to start one.</div>
      ) : (
        <div className="lemma-file-tree-section">
          <ul className="lemma-file-list lemma-chat-list" aria-label="Project Chats">
            {chats.map((chat) => {
              const isRenaming = renamingChatId === chat.chat_id;
              const isMenuOpen = chatMenu?.chat.chat_id === chat.chat_id;
              const rowClasses = [
                chat.chat_id === activeChatId ? "active" : "",
                chat.pinned ? "is-pinned" : "",
                isMenuOpen ? "menu-open" : "",
                hasChatActions ? "has-actions" : "",
              ]
                .filter(Boolean)
                .join(" ");
              return (
                <li key={chat.chat_id} className={rowClasses}>
                  {isRenaming ? (
                    <div className="lemma-file-rename-row">
                      <input
                        ref={chatRenameInputRef}
                        type="text"
                        className="lemma-sidebar-filter"
                        aria-label={`Rename chat ${chat.title}`}
                        value={chatRenameDraft}
                        disabled={chatRenameSaving}
                        onChange={(e) => setChatRenameDraft(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            e.preventDefault();
                            void submitChatRename();
                          } else if (e.key === "Escape") {
                            setRenamingChatId(null);
                          }
                        }}
                        onBlur={() => {
                          if (!chatRenameSaving) setRenamingChatId(null);
                        }}
                      />
                      {chatRenameError && (
                        <p className="lemma-root-tex-error" role="alert">
                          {chatRenameError}
                        </p>
                      )}
                    </div>
                  ) : (
                    <>
                      <button
                        type="button"
                        className="lemma-chat-row"
                        aria-current={chat.chat_id === activeChatId ? "true" : undefined}
                        title={chat.title}
                        onClick={() => onSelectChat?.(chat.chat_id)}
                      >
                        {chat.pinned && (
                          <span className="lemma-chat-pin-icon" aria-label="Pinned" title="Pinned">
                            <svg width="11" height="11" viewBox="0 0 16 16" aria-hidden="true">
                              <path
                                d="M9.8 2.2 13.8 6.2 12.6 7.4 11.8 6.8 8.9 9.7 9.4 11.8 8.2 13 5.7 10.5 2.5 13.7 2.3 13.5 5.5 10.3 3 7.8 4.2 6.6 6.3 7.1 9.2 4.2 8.6 3.4Z"
                                fill="currentColor"
                              />
                            </svg>
                          </span>
                        )}
                        <span className="lemma-chat-row-title">{chat.title}</span>
                        <span className="lemma-chat-row-time">{formatChatTime(chat.updated_at)}</span>
                      </button>
                      {hasChatActions && (
                        <button
                          type="button"
                          className="lemma-file-more-btn"
                          aria-label={`More actions for ${chat.title}`}
                          aria-haspopup="menu"
                          aria-expanded={isMenuOpen}
                          onClick={(e) => {
                            e.stopPropagation();
                            openChatMenu(e, chat);
                          }}
                        >
                          ⋯
                        </button>
                      )}
                    </>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}

      <div className="lemma-outline-section">
        <button
          type="button"
          className="lemma-outline-toggle"
          aria-expanded={outlineExpanded}
          aria-label="Toggle Document Outline"
          onClick={() => setOutlineExpanded((prev) => !prev)}
        >
          <span>Outline</span>
          <span aria-hidden="true">{outlineExpanded ? "▾" : "▸"}</span>
        </button>
        {outlineExpanded && (
          <ul className="lemma-outline-tree" aria-label="Document Outline">
            {renderOutlineNodes(outline, onSelectFile, onJumpToLine)}
          </ul>
        )}
      </div>

      {rowMenu && (
        <div
          className="lemma-file-row-menu"
          role="menu"
          ref={rowMenuRef}
          style={{ top: rowMenu.top, right: rowMenu.right }}
        >
          {rowMenu.isDir && onCreateFile && (
            <>
              <button type="button" role="menuitem" className="lemma-tools-item" onClick={() => startNewFile("tex", rowMenu.path)}>
                New .tex file here
              </button>
              <button type="button" role="menuitem" className="lemma-tools-item" onClick={() => startNewFile("bib", rowMenu.path)}>
                New .bib file here
              </button>
            </>
          )}
          {rowMenu.isDir && onCreateFolder && (
            <button type="button" role="menuitem" className="lemma-tools-item" onClick={() => startNewFile("folder", rowMenu.path)}>
              New folder here
            </button>
          )}
          {rowMenu.isDir && onUploadFile && (
            <label role="menuitem" className="lemma-tools-item">
              Upload file here…
              <input
                type="file"
                className="lemma-file-input-hidden"
                aria-label={`Upload file to ${rowMenu.path}`}
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  const dir = rowMenu.path;
                  e.target.value = "";
                  setRowMenu(null);
                  if (f) {
                    setCollapsedDirs((prev) => ({ ...prev, [dir]: false }));
                    void uploadFile(f, dir);
                  }
                }}
              />
            </label>
          )}
          {onMovePath && (
            <button type="button" role="menuitem" className="lemma-tools-item" onClick={() => startRename(rowMenu.path)}>
              Rename / move
            </button>
          )}
          {onDeletePath && (
            <button
              type="button"
              role="menuitem"
              className="lemma-tools-item lemma-hub-menu-danger"
              disabled={!rowMenu.isDir && rowMenu.path === rootTex}
              title={!rowMenu.isDir && rowMenu.path === rootTex ? "Choose another main document first" : undefined}
              onClick={() => {
                setPendingDelete({ path: rowMenu.path, isDir: rowMenu.isDir });
                setDeleteError(null);
                setRowMenu(null);
              }}
            >
              Delete
            </button>
          )}
        </div>
      )}

      {chatMenu && (
        <div
          className="lemma-file-row-menu"
          role="menu"
          ref={chatMenuRef}
          style={{ top: chatMenu.top, right: chatMenu.right }}
        >
          {onPinChat && (
            <button
              type="button"
              role="menuitem"
              className="lemma-tools-item"
              onClick={() => {
                const target = chatMenu.chat;
                setChatMenu(null);
                void onPinChat(target.chat_id, !target.pinned);
              }}
            >
              {chatMenu.chat.pinned ? "Unpin" : "Pin"}
            </button>
          )}
          {onRenameChat && (
            <button
              type="button"
              role="menuitem"
              className="lemma-tools-item"
              onClick={() => startChatRename(chatMenu.chat)}
            >
              Rename
            </button>
          )}
          {onDeleteChat && (
            <button
              type="button"
              role="menuitem"
              className="lemma-tools-item lemma-hub-menu-danger"
              onClick={() => {
                setPendingDeleteChat(chatMenu.chat);
                setChatDeleteError(null);
                setChatMenu(null);
              }}
            >
              Delete
            </button>
          )}
        </div>
      )}

      {pendingDeleteChat && (
        <div className="lemma-modal-backdrop">
          <div
            className="lemma-modal-dialog lemma-project-create-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="delete-chat-title"
            onKeyDown={(e) => {
              if (e.key === "Escape" && !deletingChat) setPendingDeleteChat(null);
            }}
          >
            <h2 id="delete-chat-title">Delete chat?</h2>
            <p className="lemma-subtext">
              <strong>{pendingDeleteChat.title}</strong> will be permanently deleted.
            </p>
            {chatDeleteError && (
              <p className="lemma-root-tex-error" role="alert">
                {chatDeleteError}
              </p>
            )}
            <div className="lemma-modal-actions">
              <button
                type="button"
                className="lemma-btn lemma-btn-secondary"
                autoFocus
                disabled={deletingChat}
                onClick={() => setPendingDeleteChat(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="lemma-btn lemma-btn-danger"
                disabled={deletingChat}
                onClick={() => void confirmChatDelete()}
              >
                {deletingChat ? "Deleting…" : "Delete"}
              </button>
            </div>
          </div>
        </div>
      )}

      {pendingDelete && (
        <div className="lemma-modal-backdrop">
          <div
            className="lemma-modal-dialog lemma-project-create-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="delete-path-title"
            onKeyDown={(e) => {
              if (e.key === "Escape" && !deleting) setPendingDelete(null);
            }}
          >
            <h2 id="delete-path-title">Delete {pendingDelete.isDir ? "folder" : "file"}?</h2>
            <p className="lemma-subtext">
              <strong>{pendingDelete.path}</strong>
              {pendingDelete.isDir
                ? pendingDeleteCount > 0
                  ? ` and the ${pendingDeleteCount} file${pendingDeleteCount === 1 ? "" : "s"} inside it will be permanently deleted.`
                  : " will be permanently deleted."
                : " will be permanently deleted."}
            </p>
            {pendingDelete.isDir && rootTex.startsWith(`${pendingDelete.path}/`) && (
              <p className="lemma-root-tex-error" role="alert">
                This folder contains the main document. Choose another main document first.
              </p>
            )}
            {deleteError && (
              <p className="lemma-root-tex-error" role="alert">
                {deleteError}
              </p>
            )}
            <div className="lemma-modal-actions">
              <button
                type="button"
                className="lemma-btn lemma-btn-secondary"
                autoFocus
                disabled={deleting}
                onClick={() => setPendingDelete(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="lemma-btn lemma-btn-danger"
                disabled={deleting || (pendingDelete.isDir && rootTex.startsWith(`${pendingDelete.path}/`))}
                onClick={() => void confirmDelete()}
              >
                {deleting ? "Deleting…" : "Delete"}
              </button>
            </div>
          </div>
        </div>
      )}
    </nav>
  );
}

export default LeftSidebar;
