import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { type IntegrityAssessmentItem, type ProposedHunkItem } from "./DiffExtension";
import { ModelSelect } from "./ModelSelect";
import { useEnabledModels } from "../context/EnabledModelsContext";
import { ChatBubble } from "./ChatBubble";
import { ResizeHandle } from "./ResizeHandle";
import { SanitizedMarkdown, type SanitizedMarkdownProps } from "./SanitizedMarkdown";
import { useChatBubble } from "../hooks/useChatBubble";
import {
  type ActiveAgentRun,
  type AgentRunController,
  chatScopeKey,
  useAgentRun,
} from "../hooks/useAgentRun";

export { SanitizedMarkdown, type SanitizedMarkdownProps };

export interface ChatContextEntry {
  path: string;
  detail: string;
  // "attachment" = attached to this chat only; it is not a project file.
  kind: "active" | "reference" | "attachment";
}

export interface ChatMessageItem {
  role: "user" | "assistant" | "status";
  content: string;
  pillFile?: string | null;
  messageId?: string;
  profileId?: string;
  error?: boolean;
  context?: ChatContextEntry[];
}

const MAX_REFERENCES = 8;
const PICKER_LIMIT = 8;
// "@" at the start or after whitespace, up to the caret.
const AT_QUERY_RE = /(^|\s)@([^\s@]*)$/;

type ReplyPart =
  | { type: "text"; text: string }
  | { type: "code"; text: string }
  | { type: "edit"; search: string; replace: string }
  | { type: "incomplete_edit" };

// Same block shape the backend parses into hunks (chat_service._SEARCH_REPLACE_RE).
const EDIT_BLOCK_SRC =
  "^[ \\t]*<{3,}[ \\t]*SEARCH\\b[^\\n]*\\r?\\n([\\s\\S]*?)\\r?\\n[ \\t]*={3,}[ \\t]*\\r?\\n(?:([\\s\\S]*?)\\r?\\n)?[ \\t]*>{3,}[ \\t]*REPLACE\\b[^\\n]*";
const FENCE_AROUND_EDITS_RE = new RegExp(
  "```[\\w+-]*[ \\t]*\\n((?:\\s*" + EDIT_BLOCK_SRC + "\\s*)+)```",
  "gim"
);
const REPLY_BLOCK_RE = new RegExp(EDIT_BLOCK_SRC + "|```[\\w+-]*[ \\t]*\\n([\\s\\S]*?)```", "gim");
const INCOMPLETE_EDIT_RE = /(?:^|\n)(?:```[\w+-]*[ \t]*\n)?[ \t]*<{3,}[ \t]*SEARCH\b[\s\S]*$/i;
const STRAY_REPLACE_RE = /^[ \t]*>{3,}[ \t]*REPLACE\b[^\n]*$/gim;

function splitAssistantReply(raw: string): ReplyPart[] {
  const content = raw.replace(FENCE_AROUND_EDITS_RE, "$1");
  const parts: ReplyPart[] = [];
  const pushText = (text: string) => {
    const incMatch = INCOMPLETE_EDIT_RE.exec(text);
    if (incMatch !== null) {
      const before = text.slice(0, incMatch.index).replace(STRAY_REPLACE_RE, "").trim();
      if (before) parts.push({ type: "text", text: before });
      parts.push({ type: "incomplete_edit" });
      return;
    }
    const trimmed = text.replace(STRAY_REPLACE_RE, "").trim();
    if (trimmed) parts.push({ type: "text", text: trimmed });
  };
  const re = new RegExp(REPLY_BLOCK_RE.source, "gim");
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(content)) !== null) {
    pushText(content.slice(last, m.index));
    if (m[1] !== undefined) {
      parts.push({ type: "edit", search: m[1], replace: m[2] ?? "" });
    } else {
      parts.push({ type: "code", text: m[3].replace(/\n$/, "") });
    }
    last = m.index + m[0].length;
  }
  pushText(content.slice(last));
  return parts;
}

function lineCount(text: string): number {
  return text ? text.split("\n").length : 0;
}

function AssistantReply({ content }: { content: string }): React.ReactElement {
  return (
    <div className="lemma-chat-reply">
      {splitAssistantReply(content).map((part, idx) => {
        if (part.type === "code") {
          return (
            <pre key={idx} className="lemma-chat-code">
              <code>{part.text}</code>
            </pre>
          );
        }
        if (part.type === "edit") {
          return (
            <details key={idx} className="lemma-chat-edit-card" data-testid="chat-edit-card">
              <summary className="lemma-chat-edit-head" aria-label="Proposed edit: show new text">
                <span className="lemma-chat-edit-chevron" aria-hidden="true" />
                <span className="lemma-chat-edit-title">Proposed edit</span>
                <span className="lemma-chat-edit-stats">
                  <span className="is-removed">−{lineCount(part.search)}</span>
                  <span className="is-added">+{lineCount(part.replace)}</span>
                </span>
                <span className="lemma-chat-edit-hint">Review in editor</span>
              </summary>
              <pre className="lemma-chat-code is-wrapped">
                <code>{part.replace}</code>
              </pre>
            </details>
          );
        }
        if (part.type === "incomplete_edit") {
          return (
            <p key={idx} className="lemma-chat-incomplete-edit" data-testid="chat-incomplete-edit">
              <em>Incomplete edit block was discarded and could not be applied.</em>
            </p>
          );
        }
        return <SanitizedMarkdown key={idx} content={part.text} />;
      })}
    </div>
  );
}

type TranscriptGroup =
  | { kind: "message"; msg: ChatMessageItem; idx: number }
  | { kind: "activity"; start: number; items: Array<{ msg: ChatMessageItem; idx: number }> };

// Consecutive status lines collapse into one activity block so tool progress does not read as separate messages.
function groupTranscript(messages: ChatMessageItem[]): TranscriptGroup[] {
  const groups: TranscriptGroup[] = [];
  messages.forEach((msg, idx) => {
    if (msg.role === "status") {
      const last = groups[groups.length - 1];
      if (last && last.kind === "activity") {
        last.items.push({ msg, idx });
      } else {
        groups.push({ kind: "activity", start: idx, items: [{ msg, idx }] });
      }
      return;
    }
    groups.push({ kind: "message", msg, idx });
  });
  return groups;
}

// Shows the file as an inline chip in place of its name, so "Proposed an edit to main.tex" does not repeat it.
function StatusText({ content, pillFile }: { content: string; pillFile?: string | null }): React.ReactElement {
  const pill = pillFile ? (
    <span className="lemma-chat-status-pill" data-testid="chat-status-pill">
      {pillFile}
    </span>
  ) : null;
  if (pillFile && content.endsWith(pillFile)) {
    return (
      <span className="lemma-chat-status-text">
        {content.slice(0, content.length - pillFile.length)}
        {pill}
      </span>
    );
  }
  return (
    <span className="lemma-chat-status-text">
      {content}
      {pill && <> {pill}</>}
    </span>
  );
}

function RunningIndicator(): React.ReactElement {
  return (
    <div className="lemma-chat-status-running" role="status" aria-label="Assistant is running">
      <span className="lemma-chat-status-running-dot" aria-hidden="true" />
      <span>Working…</span>
    </div>
  );
}

export interface FloatingChatBarProps {
  projectId: string;
  activeFile?: string;
  quarantinedChunksCount?: number;
  pendingCount?: number;
  initialMessages?: ChatMessageItem[];
  onProposeHunks?: (hunks: ProposedHunkItem[]) => void;
  cursorLine?: number;
  selection?: { text: string; startLine: number; endLine: number } | null;
  bufferText?: string;
  // Persisted chat to show; null starts a new chat on the next prompt. Omit to keep the transcript local.
  chatId?: string | null;
  // Bumped when the user picks a chat elsewhere (sidebar), so a minimised pill reopens.
  chatSelectNonce?: number;
  // Called once chatSelectNonce has been consumed so remounting does not reopen a minimised pill.
  onConsumeChatSelect?: () => void;
  // Draft prompt injected from outside (e.g. "Fix in chat" on a compile diagnostic), with a nonce so repeated clicks re-apply.
  draftPrompt?: { text: string; nonce: number; referenceFile?: string } | null;
  // Called once draftPrompt has been copied into the input so remounting does not re-inject stale prompts.
  onConsumeDraftPrompt?: () => void;
  // A reply was stored in this chat (new or existing); shown = the pill still displays it.
  onChatActivity?: (chatId: string, shown: boolean) => void;
  onNewChat?: () => void;
  // Project file paths the user can reference with @ or the 📎 menu.
  projectFiles?: string[];
  // Unsaved editor text by path; sent for referenced files so the AI sees what is on screen.
  dirtyBuffers?: Record<string, string>;
  // Upload a file from the computer into the project root; returns an error message or null.
  onUploadFile?: (file: File) => Promise<string | null>;
  agentRun?: AgentRunController;
  hasOpenRouterKey?: boolean;
  csrfToken?: string;
  academicIntegrityMode?: boolean;
  onIntegrityLogUpdated?: () => void;
  onSelectedModelChange?: (model: { id: string; name: string; provider: string } | null) => void;
}

const AUTO_PROFILE_ID = "auto";

function newChatId(): string {
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  return `chat_${Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")}`;
}

interface StoredChatMessage {
  role: string;
  content: string;
  pill_file?: string | null;
  message_id?: string;
  profile_id?: string | null;
  context?: ChatContextEntry[];
}

function mapStoredMessages(stored: StoredChatMessage[]): ChatMessageItem[] {
  return stored.map((m) => {
    if (m.role === "user") {
      return {
        role: "user",
        content: m.content,
        profileId: m.profile_id || undefined,
        context: m.context,
      };
    }
    if (m.role === "status") {
      return {
        role: "status",
        content: m.content,
        pillFile: m.pill_file ?? null,
        messageId: m.message_id,
      };
    }
    return { role: "assistant", content: m.content };
  });
}

function mergeRunIntoMessages(
  base: ChatMessageItem[],
  run: Pick<ActiveAgentRun, "userPrompt" | "profileId" | "context" | "lines">,
  extra?: { profileId?: string; context?: ChatContextEntry[] }
): ChatMessageItem[] {
  const next = [...base];
  const effectiveProfile = extra?.profileId || run.profileId || undefined;
  const effectiveContext = extra?.context || run.context;

  let lastUserIdx = -1;
  for (let i = next.length - 1; i >= 0; i--) {
    if (next[i].role === "user") {
      lastUserIdx = i;
      break;
    }
  }

  const hasTrailingUserTurn =
    lastUserIdx !== -1 &&
    !next.slice(lastUserIdx + 1).some((m) => m.role === "assistant") &&
    (!run.userPrompt || next[lastUserIdx].content === run.userPrompt);

  if (!hasTrailingUserTurn && run.userPrompt) {
    next.push({
      role: "user",
      content: run.userPrompt,
      profileId: effectiveProfile,
      context: effectiveContext,
    });
    lastUserIdx = next.length - 1;
  } else if (lastUserIdx !== -1 && (effectiveProfile || effectiveContext)) {
    const u = next[lastUserIdx];
    next[lastUserIdx] = {
      ...u,
      ...(effectiveProfile && !u.profileId ? { profileId: effectiveProfile } : {}),
      ...(effectiveContext && !u.context ? { context: effectiveContext } : {}),
    };
  }

  const turnStart = lastUserIdx !== -1 ? lastUserIdx + 1 : 0;
  const existingStatus = next.slice(turnStart).filter((m) => m.role === "status");
  const seenIds = new Set(existingStatus.map((m) => m.messageId).filter(Boolean) as string[]);
  const seenKeys = new Set(existingStatus.map((m) => `${m.content}\0${m.pillFile ?? ""}`));

  run.lines.forEach((line, idx) => {
    const idKey = line.messageId;
    const textKey = `${line.content}\0${line.pillFile ?? ""}`;
    if (idKey) {
      if (seenIds.has(idKey)) return;
      seenIds.add(idKey);
    } else {
      if (seenKeys.has(textKey)) return;
      seenKeys.add(textKey);
    }
    next.push({
      role: "status",
      content: line.content,
      pillFile: line.pillFile ?? null,
      messageId: line.messageId ?? `live-${idx}`,
    });
  });

  return next;
}

const ROUTED_PROFILE_LABELS: Record<string, { short: string; title: string }> = {
  writing: { short: "Drafting", title: "Academic Manuscript Drafting" },
  synthesis: { short: "Literature", title: "Literature Synthesis" },
  proofreading: { short: "Proofreading", title: "Academic Proofreading" },
  tighten: { short: "Tighten", title: "Tighten for Page Limits" },
  verify_claim: { short: "Verify Claim", title: "Claim Verifier" },
  formal_tone: { short: "Formal Tone", title: "Formal Tone Polish" },
  explain_math: { short: "Explain Math", title: "Explain Math Step-by-Step" },
  explain: { short: "Explain", title: "Research Q&A & Discussion" },
  fix_latex: { short: "Fix LaTeX", title: "LaTeX Error Fixer" },
};

const DEFAULT_PANEL_H = 260;
const MIN_PANEL_H = 60;
const MAX_PANEL_H = 1200;
// Space the header, input form and edge margin occupy around the message area.
const PANEL_CHROME_PX = 150;

export function FloatingChatBar({
  projectId,
  activeFile = "main.tex",
  quarantinedChunksCount = 0,
  pendingCount = 0,
  initialMessages = [],
  onProposeHunks,
  cursorLine,
  selection = null,
  bufferText,
  chatId,
  chatSelectNonce,
  onConsumeChatSelect,
  draftPrompt,
  onConsumeDraftPrompt,
  onChatActivity,
  onNewChat,
  projectFiles = [],
  dirtyBuffers = {},
  onUploadFile,
  agentRun: agentRunProp,
  hasOpenRouterKey = true,
  csrfToken,
  academicIntegrityMode = false,
  onIntegrityLogUpdated,
  onSelectedModelChange,
}: FloatingChatBarProps): React.ReactElement {
  const { models } = useEnabledModels();
  const { minimised, corner, setMinimised, setCorner } = useChatBubble();
  const [selectedModel, setSelectedModel] = useState<string>("");
  const [promptText, setPromptText] = useState<string>("");
  const [messages, setMessages] = useState<ChatMessageItem[]>(initialMessages);
  const [pendingDisclosure, setPendingDisclosure] = useState<{
    assessment: IntegrityAssessmentItem;
    modelId: string;
    promptSummary: string;
    chatId: string;
  } | null>(null);
  const [loggingDisclosure, setLoggingDisclosure] = useState<boolean>(false);
  const [disclosureLoggedNotice, setDisclosureLoggedNotice] = useState<boolean>(false);

  const selectedModelInfo = models.find((m) => m.id === selectedModel) ?? null;
  const isLocalModel = selectedModelInfo?.provider === "local";
  const academicIntegrityModeRef = useRef(academicIntegrityMode);
  academicIntegrityModeRef.current = academicIntegrityMode;
  const selectedModelRef = useRef(selectedModel);
  selectedModelRef.current = selectedModel;
  const onSelectedModelChangeRef = useRef(onSelectedModelChange);
  onSelectedModelChangeRef.current = onSelectedModelChange;

  useEffect(() => {
    onSelectedModelChangeRef.current?.(
      selectedModelInfo
        ? {
            id: selectedModelInfo.id,
            name: selectedModelInfo.name,
            provider: selectedModelInfo.provider || "openrouter",
          }
        : null
    );
  }, [selectedModelInfo]);
  // The chat the transcript shows. Set from chatId, or locally when the first prompt starts a chat.
  const [currentChatId, setCurrentChatId] = useState<string | null>(chatId ?? null);
  const currentChatRef = useRef<string | null>(chatId ?? null);
  currentChatRef.current = currentChatId;
  // A chat this pill started: its transcript is already on screen, so do not reload it when chatId catches up.
  const startedChatRef = useRef<string | null>(null);

  const projectIdRef = useRef(projectId);
  projectIdRef.current = projectId;
  const onProposeHunksRef = useRef(onProposeHunks);
  onProposeHunksRef.current = onProposeHunks;
  const onChatActivityRef = useRef(onChatActivity);
  onChatActivityRef.current = onChatActivity;
  const activeRunSnapshotRef = useRef<ActiveAgentRun | null>(null);
  const handledFinishedRunsRef = useRef<Set<string>>(new Set());
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const localAgentRun = useAgentRun({
    disabled: Boolean(agentRunProp),
    onRunFinished: (info) => {
      const snap = activeRunSnapshotRef.current;
      if (snap) {
        handledFinishedRunsRef.current.add(snap.runId);
      }
      if (info.hunks && onProposeHunksRef.current) {
        onProposeHunksRef.current(info.hunks);
      }
      const isShown =
        info.projectId === projectIdRef.current && info.chatId === currentChatRef.current;
      onChatActivityRef.current?.(info.chatId, isShown);
      if (!isShown) return;

      setMessages((prev) => {
        const merged = mergeRunIntoMessages(
          prev,
          snap ?? { lines: [], profileId: info.profileId, context: info.context },
          {
            profileId: info.profileId,
            context: info.context,
          }
        );
        if (info.status === "completed" && info.reply) {
          merged.push({ role: "assistant", content: info.reply });
        } else if (info.status === "failed") {
          const errMsg = info.error || "Agent run failed";
          merged.push({ role: "assistant", content: errMsg, error: true });
        }
        return merged;
      });
      if (info.status === "completed" && academicIntegrityMode && info.integrity && info.integrity.tier >= 2) {
        const hasHunks = Array.isArray(info.hunks) && info.hunks.length > 0;
        if (!hasHunks) {
          setDisclosureLoggedNotice(false);
          setPendingDisclosure({
            assessment: info.integrity,
            modelId: info.model || selectedModel,
            promptSummary: snap?.userPrompt || "",
            chatId: info.chatId,
          });
        }
      }
      if (info.status === "failed") {
        setErrorMessage(info.error || "Agent run failed");
      }
    },
  });
  const agentRun = agentRunProp ?? localAgentRun;
  if (agentRun.activeRun) {
    activeRunSnapshotRef.current = agentRun.activeRun;
  }

  const prevActiveRunRef = useRef<ActiveAgentRun | null>(null);
  useEffect(() => {
    const prev = prevActiveRunRef.current;
    prevActiveRunRef.current = agentRun.activeRun;
    if (!prev) return;
    if (agentRun.activeRun && agentRun.activeRun.runId === prev.runId) return;
    if (handledFinishedRunsRef.current.has(prev.runId)) return;
    if (prev.projectId !== projectId || prev.chatId !== currentChatRef.current) return;

    const key = chatScopeKey(prev.projectId, prev.chatId);
    const runErr = agentRun.errorByChat[key];
    const finished =
      agentRun.lastFinishedRun?.runId === prev.runId ? agentRun.lastFinishedRun : undefined;

    setMessages((curr) => {
      const merged = mergeRunIntoMessages(curr, prev, {
        profileId: finished?.profileId,
        context: finished?.context,
      });
      if (runErr) {
        merged.push({ role: "assistant", content: runErr, error: true });
      }
      return merged;
    });
    if (runErr) {
      setErrorMessage(runErr);
      return;
    }
    if (
      academicIntegrityModeRef.current &&
      finished?.status === "completed" &&
      finished.integrity &&
      finished.integrity.tier >= 2 &&
      (!finished.hunks || finished.hunks.length === 0)
    ) {
      setDisclosureLoggedNotice(false);
      setPendingDisclosure({
        assessment: finished.integrity,
        modelId: finished.model || selectedModelRef.current,
        promptSummary: prev.userPrompt || "",
        chatId: prev.chatId,
      });
    }

    const applyFinishedFallback = () => {
      if (currentChatRef.current !== prev.chatId) return;
      if (finished?.status === "completed" && finished.reply) {
        setMessages((curr) => [...curr, { role: "assistant", content: finished.reply! }]);
      } else if (finished?.status === "failed") {
        const errMsg = finished.error || "Agent run failed";
        setMessages((curr) => [...curr, { role: "assistant", content: errMsg, error: true }]);
        setErrorMessage(errMsg);
      }
    };

    if (typeof fetch !== "function") {
      applyFinishedFallback();
      return;
    }
    let cancelled = false;
    const finishedChatId = prev.chatId;
    fetch(`/api/projects/${prev.projectId}/chats/${finishedChatId}/messages`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data: { messages?: StoredChatMessage[] } | null) => {
        if (cancelled) return;
        if (!data || !Array.isArray(data.messages) || data.messages.length === 0) {
          applyFinishedFallback();
          return;
        }
        if (currentChatRef.current !== finishedChatId) return;
        setMessages(mapStoredMessages(data.messages));
      })
      .catch(() => {
        if (!cancelled) applyFinishedFallback();
      });
    return () => {
      cancelled = true;
    };
  }, [agentRun.activeRun, agentRun.errorByChat, agentRun.lastFinishedRun, projectId]);

  useEffect(() => {
    if (chatId === undefined) return;
    const alreadyShown = chatId !== null && chatId === startedChatRef.current && chatId === currentChatRef.current;
    startedChatRef.current = null;
    setCurrentChatId(chatId);
    currentChatRef.current = chatId;
    if (!alreadyShown) {
      setErrorMessage(null);
      setPendingDisclosure(null);
      setDisclosureLoggedNotice(false);
      setLoggingDisclosure(false);
    }
    if (chatId === null) {
      setMessages([]);
      setReferences([]);
      setAttachments([]);
      setPendingDisclosure(null);
      setDisclosureLoggedNotice(false);
      setLoggingDisclosure(false);
      return;
    }
    if (alreadyShown || typeof fetch !== "function") return;
    let cancelled = false;
    setMessages([]);
    setReferences([]);
    setAttachments([]);
    fetch(`/api/projects/${projectId}/chats/${chatId}/messages`)
      .then((res) => (res.ok ? res.json() : { messages: [] }))
      .then((data: { messages?: StoredChatMessage[] }) => {
        if (cancelled) return;
        const stored = data.messages || [];
        setMessages(mapStoredMessages(stored));
        // References stay on the chat: reopen it with the ones the last prompt used.
        const lastUser = [...stored].reverse().find((m) => m.role === "user");
        setReferences((lastUser?.context || []).filter((c) => c.kind === "reference").map((c) => c.path));
        setAttachments(
          (lastUser?.context || [])
            .filter((c) => c.kind === "attachment" && !c.detail.startsWith("not found"))
            .map((c) => c.path)
        );
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [chatId, projectId]);

  const onConsumeChatSelectRef = useRef(onConsumeChatSelect);
  onConsumeChatSelectRef.current = onConsumeChatSelect;
  const onConsumeDraftPromptRef = useRef(onConsumeDraftPrompt);
  onConsumeDraftPromptRef.current = onConsumeDraftPrompt;

  useEffect(() => {
    if (!chatSelectNonce) return;
    setMinimised(false);
    onConsumeChatSelectRef.current?.();
  }, [chatSelectNonce]);

  useEffect(() => {
    if (!draftPrompt?.nonce) return;
    setMinimised(false);
    setPromptText(draftPrompt.text);
    const refFile = draftPrompt.referenceFile;
    if (refFile && projectFiles.includes(refFile)) {
      setReferences((prev) => (prev.includes(refFile) ? prev : [...prev, refFile]));
    }
    onConsumeDraftPromptRef.current?.();
    requestAnimationFrame(() => {
      promptRef.current?.focus();
    });
  }, [draftPrompt?.nonce]);
  const [includeQuarantined, setIncludeQuarantined] = useState<boolean>(false);
  const [references, setReferences] = useState<string[]>([]);
  // Files attached to this chat only, by name; stored outside the project.
  const [attachments, setAttachments] = useState<string[]>([]);
  const selectionKey = selection ? `${activeFile}:${selection.startLine}-${selection.endLine}` : null;
  const [dismissedSelectionKey, setDismissedSelectionKey] = useState<string | null>(null);
  useEffect(() => {
    setDismissedSelectionKey(null);
  }, [selectionKey]);
  const activeSelection = selection && selectionKey !== dismissedSelectionKey ? selection : null;
  const selectionLineLabel = activeSelection
    ? activeSelection.startLine === activeSelection.endLine
      ? `[line ${activeSelection.startLine}]`
      : `[line ${activeSelection.startLine}-${activeSelection.endLine}]`
    : null;
  // File picker: opened by typing "@" (atQuery is the text after it) or from the 📎 menu (atQuery null).
  const [pickerOpen, setPickerOpen] = useState<boolean>(false);
  const [pickerFilter, setPickerFilter] = useState<string>("");
  const [atQuery, setAtQuery] = useState<string | null>(null);
  const [pickerIndex, setPickerIndex] = useState<number>(0);
  const [attachMenuOpen, setAttachMenuOpen] = useState<boolean>(false);
  const [refNotice, setRefNotice] = useState<{ text: string; error: boolean } | null>(null);
  const [dropActive, setDropActive] = useState<boolean>(false);
  const pickerFilterRef = useRef<HTMLInputElement>(null);
  const attachRef = useRef<HTMLDivElement>(null);

  const csrfHeader = () => ({
    "X-CSRF-Token": typeof localStorage !== "undefined" ? localStorage.getItem("lemma_csrf_token") || "" : "",
  });

  // Attachments are stored per chat, so a chat needs its id before the first prompt.
  const ensureChatId = (): string => {
    let id = currentChatRef.current;
    if (!id) {
      id = newChatId();
      startedChatRef.current = id;
      currentChatRef.current = id;
      setCurrentChatId(id);
    }
    return id;
  };

  const attachToChat = async (file: File) => {
    if (references.length + attachments.length >= MAX_REFERENCES && !attachments.includes(file.name)) {
      setRefNotice({ text: `Up to ${MAX_REFERENCES} files can be referenced.`, error: true });
      return;
    }
    const chat = ensureChatId();
    setRefNotice({ text: `Attaching ${file.name}…`, error: false });
    const form = new FormData();
    form.append("file", file);
    try {
      const res = await fetch(`/api/projects/${projectId}/chats/${chat}/attachments`, {
        method: "POST",
        headers: csrfHeader(),
        body: form,
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        setRefNotice({
          text: typeof data?.detail === "string" ? data.detail : `Could not attach ${file.name} (HTTP ${res.status}).`,
          error: true,
        });
        return;
      }
      const name = typeof data?.name === "string" ? data.name : file.name;
      if (currentChatRef.current !== chat) return;
      setRefNotice(null);
      setAttachments((prev) => (prev.includes(name) ? prev : [...prev, name]));
    } catch {
      setRefNotice({ text: `Could not attach ${file.name}: network error.`, error: true });
    }
  };

  const removeAttachment = (name: string) => {
    const chat = currentChatRef.current;
    setAttachments((prev) => prev.filter((n) => n !== name));
    if (!chat || typeof fetch !== "function") return;
    void fetch(`/api/projects/${projectId}/chats/${chat}/attachments/${encodeURIComponent(name)}`, {
      method: "DELETE",
      headers: csrfHeader(),
    }).catch(() => {});
  };

  const addReference = (path: string) => {
    setReferences((prev) => {
      if (prev.includes(path)) return prev;
      if (prev.length + attachments.length >= MAX_REFERENCES) {
        setRefNotice({ text: `Up to ${MAX_REFERENCES} files can be referenced.`, error: true });
        return prev;
      }
      return [...prev, path];
    });
  };

  const pickerQuery = (atQuery ?? pickerFilter).toLowerCase();
  const pickerMatches = projectFiles
    .filter((p) => !references.includes(p) && p.toLowerCase().includes(pickerQuery))
    .slice(0, PICKER_LIMIT);

  const closePicker = () => {
    setPickerOpen(false);
    setAtQuery(null);
    setPickerFilter("");
    setPickerIndex(0);
  };

  const choosePickerFile = (path: string) => {
    addReference(path);
    if (atQuery !== null) {
      setPromptText((prev) => prev.replace(AT_QUERY_RE, (_m, lead: string) => lead));
      promptRef.current?.focus();
    }
    closePicker();
  };

  const updateAtQuery = (value: string, caret: number) => {
    const match = AT_QUERY_RE.exec(value.slice(0, caret));
    if (match && projectFiles.length > 0) {
      setAtQuery(match[2]);
      setPickerOpen(true);
      setPickerIndex(0);
    } else if (atQuery !== null) {
      closePicker();
    }
  };

  const uploadAndReference = async (file: File) => {
    if (!onUploadFile) return;
    setRefNotice({ text: `Uploading ${file.name}…`, error: false });
    const error = await onUploadFile(file);
    if (error) {
      setRefNotice({ text: error, error: true });
      return;
    }
    setRefNotice(null);
    addReference(file.name);
  };

  useEffect(() => {
    if (pickerOpen && atQuery === null) pickerFilterRef.current?.focus();
  }, [pickerOpen, atQuery]);

  useEffect(() => {
    if (!attachMenuOpen && !pickerOpen) return;
    const close = (e: MouseEvent) => {
      if (attachRef.current && !attachRef.current.contains(e.target as Node)) {
        setAttachMenuOpen(false);
        if (atQuery === null) closePicker();
      }
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [attachMenuOpen, pickerOpen, atQuery]);

  const panelRef = useRef<HTMLDivElement>(null);
  const messagesRef = useRef<HTMLDivElement>(null);
  const promptRef = useRef<HTMLTextAreaElement>(null);
  // Once the prompt wraps, it spans the full bar width until it is cleared, so the layout cannot flip back and forth.
  const [promptMultiline, setPromptMultiline] = useState(false);

  // Grow the prompt with its content; the stylesheet's max-height caps it and scrolls beyond that.
  useLayoutEffect(() => {
    const el = promptRef.current;
    if (!el) return;
    el.style.height = "auto";
    if (!promptText) setPromptMultiline(false);
    else if (el.scrollHeight > el.clientHeight) setPromptMultiline(true);
    el.style.height = `${el.scrollHeight + el.offsetHeight - el.clientHeight}px`;
  }, [promptText, minimised, promptMultiline]);
  // The chat layer the panel and bubble live in. Read lazily: the parent only exists once mounted.
  const layerRef = useRef<{ readonly current: HTMLElement | null }>({
    get current() {
      return panelRef.current?.parentElement ?? null;
    },
  }).current;
  // Message-area height in px; null = stylesheet default. Session-only.
  const [panelHeight, setPanelHeight] = useState<number | null>(null);
  const panelHeightRef = useRef<number | null>(null);
  panelHeightRef.current = panelHeight;
  // The empty list is display:none. A shrink still stores a height, but the first
  // upward resize must grow from DEFAULT_PANEL_H — not from that hidden height.
  const [emptyPanelGrown, setEmptyPanelGrown] = useState(false);
  const emptyPanelGrownRef = useRef(false);
  emptyPanelGrownRef.current = emptyPanelGrown;

  // Upper bound for the panel height, derived from the layer's size (state, so aria-valuemax stays current).
  const [maxPanelH, setMaxPanelH] = useState<number>(MAX_PANEL_H);
  const maxPanelHRef = useRef<number>(MAX_PANEL_H);
  maxPanelHRef.current = maxPanelH;

  // Re-measure on expand and whenever the layer resizes. A zero-sized (hidden) layer keeps the last good bound.
  useEffect(() => {
    const measure = () => {
      const container = layerRef.current?.clientHeight;
      if (typeof container !== "number" || container <= 0) return;
      setMaxPanelH(Math.max(MIN_PANEL_H, Math.min(MAX_PANEL_H, container - PANEL_CHROME_PX)));
    };
    measure();
    const layer = layerRef.current;
    if (typeof ResizeObserver !== "undefined") {
      if (!layer) return;
      const observer = new ResizeObserver(measure);
      observer.observe(layer as Element);
      return () => observer.disconnect();
    }
    if (typeof window.addEventListener !== "function") return;
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [minimised]);

  const collapseEmptyToCompact = () => {
    emptyPanelGrownRef.current = false;
    setEmptyPanelGrown(false);
    panelHeightRef.current = null;
    setPanelHeight(null);
  };

  const isRunningCurrentChat = Boolean(
    agentRun.activeRun &&
      agentRun.activeRun.projectId === projectId &&
      agentRun.activeRun.chatId === currentChatId
  );
  const runInAnotherChat = Boolean(
    agentRun.activeRun &&
      (agentRun.activeRun.projectId !== projectId || agentRun.activeRun.chatId !== currentChatId)
  );
  const displayMessages =
    isRunningCurrentChat && agentRun.activeRun
      ? mergeRunIntoMessages(messages, agentRun.activeRun)
      : messages;

  // delta = pointer Y change: dragging up (negative) grows the panel. Returns the delta applied.
  const handleResizeDrag = (delta: number): number => {
    // Hidden/zero-sized layer: the bound is meaningless, so do not store a height.
    if (panelRef.current?.parentElement?.clientHeight === 0) return 0;
    const compactEmpty = displayMessages.length === 0 && !emptyPanelGrownRef.current;
    const current = panelHeightRef.current ?? (compactEmpty ? 0 : DEFAULT_PANEL_H);
    const floor =
      displayMessages.length === 0 ? 0 : Math.min(MIN_PANEL_H, current);
    const next = Math.max(floor, Math.min(maxPanelHRef.current, current - delta));
    if (displayMessages.length === 0) {
      if (next === 0) {
        collapseEmptyToCompact();
      } else if (
        delta > 0 &&
        emptyPanelGrownRef.current &&
        next <= MIN_PANEL_H
      ) {
        // Empty transcript: don't stop between compact and MIN_PANEL_H — snap back to the default pill.
        collapseEmptyToCompact();
      } else {
        panelHeightRef.current = next;
        setPanelHeight(next);
        if (compactEmpty && next > 0) {
          emptyPanelGrownRef.current = true;
          setEmptyPanelGrown(true);
        }
      }
    } else {
      panelHeightRef.current = next;
      setPanelHeight(next);
    }
    return current - next;
  };

  const compactEmpty = displayMessages.length === 0 && !emptyPanelGrown;

  const [copiedStatusKey, setCopiedStatusKey] = useState<string | null>(null);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    return () => {
      if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
    };
  }, []);

  const handleCopyStatus = (key: string, text: string) => {
    if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
      void navigator.clipboard.writeText(text).catch(() => {});
    }
    setCopiedStatusKey(key);
    if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
    copyTimerRef.current = setTimeout(() => {
      setCopiedStatusKey((curr) => (curr === key ? null : curr));
    }, 1500);
  };

  useEffect(() => {
    const el = messagesRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [displayMessages.length, isRunningCurrentChat, minimised]);

  const handleNewChat = () => {
    startedChatRef.current = null;
    currentChatRef.current = null;
    setCurrentChatId(null);
    setMessages([]);
    setReferences([]);
    setAttachments([]);
    setRefNotice(null);
    setErrorMessage(null);
    collapseEmptyToCompact();
    onNewChat?.();
  };

  const selectedProvider = models.find((m) => m.id === selectedModel)?.provider;
  const modelAllowed =
    Boolean(selectedModel) && (hasOpenRouterKey || selectedProvider === "local");

  const handleSend = async (e: React.FormEvent) => {
    e.preventDefault();
    setErrorMessage(null);
    const trimmed = promptText.trim();
    if (!trimmed || !modelAllowed || agentRun.activeRun !== null) return;

    let targetChat = currentChatRef.current;
    const isNewChat = !targetChat;
    if (!targetChat) {
      targetChat = newChatId();
      startedChatRef.current = targetChat;
      currentChatRef.current = targetChat;
      setCurrentChatId(targetChat);
    }
    const stillShown = () => currentChatRef.current === targetChat;

    setMessages((prev) => [...prev, { role: "user", content: trimmed }]);
    setPromptText("");

    const result = await agentRun.startRun({
      projectId,
      chatId: targetChat,
      prompt: trimmed,
      model: selectedModel,
      profileId: AUTO_PROFILE_ID,
      activeFile,
      includeQuarantined,
      selection: activeSelection
        ? {
            text: activeSelection.text,
            startLine: activeSelection.startLine,
            endLine: activeSelection.endLine,
          }
        : null,
      cursorLine: typeof cursorLine === "number" ? cursorLine : null,
      bufferText: typeof bufferText === "string" ? bufferText : null,
      references: references.length > 0 ? references : undefined,
      referenceBuffers:
        references.length > 0
          ? Object.fromEntries(
              references.filter((p) => p in dirtyBuffers).map((p) => [p, dirtyBuffers[p]])
            )
          : undefined,
      attachments: attachments.length > 0 ? attachments : undefined,
    });

    if (result.ok) {
      if (isNewChat) {
        onChatActivityRef.current?.(targetChat, stillShown());
      }
    } else if (stillShown()) {
      const errDetail = result.error || "Failed to start agent run";
      setErrorMessage(errDetail);
      setMessages((prev) => [...prev, { role: "assistant", content: errDetail, error: true }]);
    }
  };

  return (
    <>
      {minimised && (
        <ChatBubble
          containerRef={layerRef}
          corner={corner}
          pendingCount={pendingCount}
          onExpand={() => setMinimised(false)}
          onCornerChange={setCorner}
        />
      )}
      <div
        className={promptMultiline ? "lemma-floating-chat-bar expanded is-multiline" : "lemma-floating-chat-bar expanded"}
        data-testid="floating-chat-bar"
        hidden={minimised}
        ref={panelRef}
        style={
          (panelHeight !== null ? { "--chat-panel-h": `${panelHeight}px` } : {}) as React.CSSProperties
        }
      >
        {!minimised && (
          <ResizeHandle
            orientation="horizontal"
            ariaLabel="Resize chat panel"
            onDrag={handleResizeDrag}
            onToggleCollapse={collapseEmptyToCompact}
            height={{
              now: panelHeight ?? (compactEmpty ? 0 : DEFAULT_PANEL_H),
              min: displayMessages.length === 0 ? 0 : MIN_PANEL_H,
              max: maxPanelH,
            }}
          />
        )}
        <div className="lemma-chat-bar-header">
          <div className="lemma-chat-model">
            <ModelSelect
              id="openrouter-model-select"
              label="OpenRouter Model"
              value={selectedModel}
              onChange={setSelectedModel}
              openRouterEnabled={hasOpenRouterKey}
            />
          </div>
          <div className="lemma-chat-header-actions">
            {displayMessages.length > 0 && (
              <button
                type="button"
                className="lemma-chat-action-btn"
                aria-label="New chat"
                title="Start a new chat"
                onClick={handleNewChat}
              >
                <svg width="13" height="13" viewBox="0 0 16 16" aria-hidden="true">
                  <path
                    d="M7.5 2.75H4A1.75 1.75 0 0 0 2.25 4.5v7.5A1.75 1.75 0 0 0 4 13.75h7.5A1.75 1.75 0 0 0 13.25 12V8.5"
                    stroke="currentColor"
                    strokeWidth="1.4"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    fill="none"
                  />
                  <path
                    d="M11.6 2.4a1.25 1.25 0 0 1 1.77 1.77L8.25 9.3l-2.3.53.53-2.3 5.12-5.13Z"
                    stroke="currentColor"
                    strokeWidth="1.4"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    fill="none"
                  />
                </svg>
                <span className="lemma-chat-action-label">New</span>
              </button>
            )}
            <button
              type="button"
              className="lemma-chat-icon-btn"
              aria-label="Minimise to bubble"
              title="Minimise to bubble"
              onClick={() => setMinimised(true)}
            >
              <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">
                <path
                  d="M3.5 11h9"
                  stroke="currentColor"
                  strokeWidth="1.7"
                  strokeLinecap="round"
                  fill="none"
                />
              </svg>
            </button>
          </div>
        </div>

        {quarantinedChunksCount > 0 && (
          <div
            role="alert"
            className="lemma-quarantine-banner"
            data-testid="quarantine-warning-badge"
          >
            <span>
              Prompt-Injection Quarantine Active: {quarantinedChunksCount} PDF chunk(s)
              flagged and Quarantined by default.
            </span>
            <label htmlFor="override-quarantine-checkbox">
              <input
                id="override-quarantine-checkbox"
                type="checkbox"
                aria-label="Include quarantined chunks (override)"
                checked={includeQuarantined}
                onChange={(e) => setIncludeQuarantined(Boolean(e.target.checked))}
              />
              Include quarantined chunks (override)
            </label>
          </div>
        )}

        <div
          className={`lemma-chat-messages${displayMessages.length === 0 && !emptyPanelGrown ? " is-empty" : ""}`}
          data-testid="chat-messages-list"
          ref={messagesRef}
        >
          {groupTranscript(displayMessages).map((group, groupIdx, groups) => {
            if (group.kind === "activity") {
              // Live indicator joins the trailing activity group so progress reads as one block.
              const showRunning = isRunningCurrentChat && groupIdx === groups.length - 1;
              return (
                <div key={`activity-${group.start}`} className="lemma-chat-activity">
                  {group.items.map(({ msg, idx }) => {
                    const statusKey = msg.messageId ?? `status-${idx}`;
                    const isCopied = copiedStatusKey === statusKey;
                    return (
                      <div key={idx} className="lemma-chat-msg role-status" data-testid="chat-status-line">
                        <StatusText content={msg.content} pillFile={msg.pillFile} />
                        <button
                          type="button"
                          aria-label="Copy status line"
                          className="lemma-chat-status-copy"
                          onClick={() => handleCopyStatus(statusKey, msg.content)}
                        >
                          {isCopied ? "Copied" : "Copy"}
                        </button>
                      </div>
                    );
                  })}
                  {showRunning && <RunningIndicator />}
                </div>
              );
            }
            const { msg, idx } = group;
            const routed = msg.profileId ? ROUTED_PROFILE_LABELS[msg.profileId] : undefined;
            if (msg.role === "user") {
              const hasContext = !!msg.context && msg.context.length > 0;
              return (
                <div key={idx} className={`lemma-chat-msg role-user${idx === 0 ? " is-first" : ""}`}>
                  <div className="lemma-chat-user-bubble">{msg.content}</div>
                  {(routed || hasContext) && (
                    <div className="lemma-chat-msg-meta">
                      {routed && (
                        <span
                          className="lemma-chat-route"
                          data-testid="chat-routed-profile"
                          title={routed.title}
                          aria-label={routed.title}
                        >
                          {routed.short}
                        </span>
                      )}
                      {hasContext && (
                        <span
                          className="lemma-chat-msg-context"
                          data-testid="chat-msg-context"
                          aria-label="Context sent with this message"
                        >
                          {msg.context!.map((c) => (
                            <span
                              key={`${c.kind}:${c.path}`}
                              className="lemma-chat-msg-context-item"
                              title={c.kind === "attachment" ? `Attached to this chat · ${c.detail}` : c.detail}
                            >
                              {c.kind === "attachment" ? `📎 ${c.path}` : c.path}
                              <span className="lemma-chat-msg-context-detail"> · {c.detail}</span>
                            </span>
                          ))}
                        </span>
                      )}
                    </div>
                  )}
                </div>
              );
            }
            return (
              <div
                key={idx}
                className={msg.error ? "lemma-chat-msg role-assistant is-error" : "lemma-chat-msg role-assistant"}
              >
                <span className="lemma-sr-only">Assistant</span>
                {msg.error ? (
                  <p className="lemma-error-banner" role="alert">
                    {msg.content}
                  </p>
                ) : (
                  <AssistantReply content={msg.content} />
                )}
              </div>
            );
          })}
          {isRunningCurrentChat &&
            (displayMessages.length === 0 || displayMessages[displayMessages.length - 1].role !== "status") && (
              <div className="lemma-chat-activity">
                <RunningIndicator />
              </div>
            )}
        </div>

        {(activeSelection !== null || references.length > 0 || attachments.length > 0 || refNotice) && (
          <div className="lemma-chat-refs" aria-label="Referenced files">
            {activeSelection && selectionLineLabel && (
              <span
                className="lemma-chat-ref-chip is-selection"
                data-testid="chat-selection-chip"
                title={`${activeFile} ${selectionLineLabel}: selected passage sent as context`}
              >
                <span className="lemma-chat-ref-chip-name">{activeFile}</span>
                <span className="lemma-chat-ref-chip-range">{selectionLineLabel}</span>
                <button
                  type="button"
                  className="lemma-chat-ref-chip-remove"
                  aria-label="Remove selection context"
                  onClick={() => setDismissedSelectionKey(selectionKey)}
                >
                  ×
                </button>
              </span>
            )}
            {attachments.map((name) => (
              <span
                key={`att:${name}`}
                className="lemma-chat-ref-chip is-attachment"
                title={`${name}: attached to this chat only, not part of the project`}
              >
                <span aria-hidden="true">📎</span>
                <span className="lemma-chat-ref-chip-name">{name}</span>
                <button
                  type="button"
                  className="lemma-chat-ref-chip-remove"
                  aria-label={`Remove attachment ${name}`}
                  onClick={() => removeAttachment(name)}
                >
                  ×
                </button>
              </span>
            ))}
            {references.map((path) => (
              <span key={path} className="lemma-chat-ref-chip" title={`${path}: sent as read-only context`}>
                <span className="lemma-chat-ref-chip-name">{path}</span>
                <button
                  type="button"
                  className="lemma-chat-ref-chip-remove"
                  aria-label={`Remove reference ${path}`}
                  onClick={() => setReferences((prev) => prev.filter((p) => p !== path))}
                >
                  ×
                </button>
              </span>
            ))}
            {refNotice && (
              <span
                className={refNotice.error ? "lemma-chat-ref-notice is-error" : "lemma-chat-ref-notice"}
                role={refNotice.error ? "alert" : "status"}
              >
                {refNotice.text}
              </span>
            )}
          </div>
        )}

        {academicIntegrityMode && pendingDisclosure && (
          <div
            className={`lemma-integrity-banner tier-${pendingDisclosure.assessment.tier}`}
            role="status"
            data-testid="chat-disclosure-banner"
          >
            <div className="lemma-integrity-banner-body">
              <strong>
                {pendingDisclosure.assessment.tier === 3
                  ? "Compliance Check (Tier 3):"
                  : "Disclosure Notice (Tier 2):"}
              </strong>{" "}
              {pendingDisclosure.assessment.flags?.has_citations
                ? "Verify all citations and factual claims against primary sources before compiling. "
                : "This response affects the substance or phrasing of your work and should be declared in your AI Usage Appendix. "}
              {pendingDisclosure.assessment.flags?.proportionality_nudge && (
                <span>Consider reviewing directly to maintain human authorship.</span>
              )}
            </div>
            <div className="lemma-integrity-banner-actions">
              {disclosureLoggedNotice ? (
                <span className="lemma-integrity-logged-ok">Logged</span>
              ) : (
                <button
                  type="button"
                  className="lemma-btn lemma-btn-primary lemma-btn-xs"
                  disabled={loggingDisclosure}
                  onClick={async () => {
                    setLoggingDisclosure(true);
                    try {
                      const csrf =
                        csrfToken ||
                        (typeof localStorage !== "undefined"
                          ? localStorage.getItem("lemma_csrf_token") || ""
                          : "");
                      const mInfo = models.find((m) => m.id === pendingDisclosure.modelId);
                      const res = await fetch(`/api/projects/${projectId}/integrity/log`, {
                        method: "POST",
                        headers: {
                          "Content-Type": "application/json",
                          "X-CSRF-Token": csrf,
                        },
                        body: JSON.stringify({
                          source_surface: "chat",
                          provider: mInfo?.provider || "openrouter",
                          model_id: pendingDisclosure.modelId,
                          model_display_name: mInfo?.name || pendingDisclosure.modelId,
                          tier: pendingDisclosure.assessment.tier,
                          category: pendingDisclosure.assessment.category,
                          purpose: pendingDisclosure.assessment.suggested_purpose,
                          affected_files: activeFile ? [activeFile] : [],
                          prompt_summary: pendingDisclosure.promptSummary.slice(0, 300),
                          action_summary: (pendingDisclosure.assessment.reasons ?? []).join(" "),
                          outcome: "logged_from_chat",
                          chat_id: pendingDisclosure.chatId,
                          flags: pendingDisclosure.assessment.flags,
                        }),
                      });
                      if (!res.ok) {
                        setErrorMessage("Could not log AI usage to appendix.");
                        return;
                      }
                      setDisclosureLoggedNotice(true);
                      onIntegrityLogUpdated?.();
                    } catch {
                      setErrorMessage("Could not log AI usage to appendix.");
                    } finally {
                      setLoggingDisclosure(false);
                    }
                  }}
                >
                  {loggingDisclosure ? "Logging…" : "Log to AI Appendix"}
                </button>
              )}
              <button
                type="button"
                className="lemma-btn lemma-btn-secondary lemma-btn-xs"
                onClick={() => setPendingDisclosure(null)}
              >
                Dismiss
              </button>
            </div>
          </div>
        )}

        {academicIntegrityMode &&
          !isLocalModel &&
          (attachments.length > 0 || references.length > 0 || promptText.length >= 1500) && (
            <div
              className="lemma-integrity-banner tier-privacy"
              role="note"
              data-testid="chat-privacy-banner"
            >
              <div className="lemma-integrity-banner-body">
                <strong>Privacy Check:</strong> Using external cloud model (
                <code>{selectedModelInfo?.name || selectedModel || "OpenRouter"}</code>). Ensure no
                unpublished manuscripts under review, personal human-subject data, or confidential
                information are sent.
              </div>
            </div>
          )}

        <form
          id="chat-prompt-form"
          onSubmit={handleSend}
          className={dropActive ? "lemma-chat-input-form is-drop-target" : "lemma-chat-input-form"}
          onDragOver={(e) => {
            const types = Array.from(e.dataTransfer.types);
            if (!types.includes("text/plain") && !types.includes("Files")) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = types.includes("Files") ? "copy" : "link";
            if (!dropActive) setDropActive(true);
          }}
          onDragLeave={(e) => {
            if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDropActive(false);
          }}
          onDrop={(e) => {
            setDropActive(false);
            const dropped = e.dataTransfer.files?.[0];
            if (dropped) {
              e.preventDefault();
              void attachToChat(dropped);
              return;
            }
            const path = e.dataTransfer.getData("text/plain");
            if (path && projectFiles.includes(path)) {
              e.preventDefault();
              addReference(path);
            }
          }}
        >
          <div className="lemma-chat-attach-anchor" ref={attachRef}>
            <button
              type="button"
              className="lemma-chat-attach-btn"
              title="Reference a file"
              aria-label="Reference a file"
              aria-haspopup="menu"
              aria-expanded={attachMenuOpen}
              onClick={() => {
                closePicker();
                setAttachMenuOpen((open) => !open);
              }}
            >
              <span aria-hidden="true">📎</span>
            </button>
            {attachMenuOpen && (
              <div className="lemma-chat-attach-menu" role="menu">
                <button
                  type="button"
                  role="menuitem"
                  className="lemma-tools-item"
                  disabled={projectFiles.length === 0}
                  onClick={() => {
                    setAttachMenuOpen(false);
                    setAtQuery(null);
                    setPickerFilter("");
                    setPickerIndex(0);
                    setPickerOpen(true);
                  }}
                >
                  Project file…
                </button>
                <label
                  role="menuitem"
                  className="lemma-tools-item"
                  title="The AI can read it in this chat; it is not added to the project"
                >
                  Attach to this chat only…
                  <input
                    type="file"
                    className="lemma-file-input-hidden"
                    aria-label="Attach a file to this chat only"
                    onChange={(e) => {
                      const file = e.target.files?.[0];
                      e.target.value = "";
                      setAttachMenuOpen(false);
                      if (file) void attachToChat(file);
                    }}
                  />
                </label>
                {onUploadFile && (
                  <label role="menuitem" className="lemma-tools-item" title="Saved at the project root, then referenced">
                    Upload to project…
                    <input
                      type="file"
                      className="lemma-file-input-hidden"
                      aria-label="Upload a file to reference"
                      onChange={(e) => {
                        const file = e.target.files?.[0];
                        e.target.value = "";
                        setAttachMenuOpen(false);
                        if (file) void uploadAndReference(file);
                      }}
                    />
                  </label>
                )}
              </div>
            )}
            {pickerOpen && (
              <div className="lemma-chat-file-picker" role="dialog" aria-label="Reference a project file">
                {atQuery === null && (
                  <input
                    ref={pickerFilterRef}
                    className="lemma-sidebar-filter"
                    aria-label="Filter project files"
                    placeholder="Filter files"
                    value={pickerFilter}
                    onChange={(e) => {
                      setPickerFilter(e.target.value);
                      setPickerIndex(0);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                        e.preventDefault();
                        const step = e.key === "ArrowDown" ? 1 : -1;
                        setPickerIndex((i) => Math.max(0, Math.min(pickerMatches.length - 1, i + step)));
                      } else if (e.key === "Enter") {
                        e.preventDefault();
                        if (pickerMatches[pickerIndex]) choosePickerFile(pickerMatches[pickerIndex]);
                      } else if (e.key === "Escape") {
                        e.preventDefault();
                        closePicker();
                      }
                    }}
                  />
                )}
                <ul className="lemma-chat-file-picker-list" role="listbox" aria-label="Project files">
                  {pickerMatches.length === 0 ? (
                    <li className="lemma-chat-file-picker-empty">No matching files</li>
                  ) : (
                    pickerMatches.map((path, i) => (
                      <li key={path} role="option" aria-selected={i === pickerIndex}>
                        <button
                          type="button"
                          className={i === pickerIndex ? "lemma-tools-item is-active" : "lemma-tools-item"}
                          onMouseDown={(e) => e.preventDefault()}
                          onMouseEnter={() => setPickerIndex(i)}
                          onClick={() => choosePickerFile(path)}
                        >
                          {path}
                        </button>
                      </li>
                    ))
                  )}
                </ul>
              </div>
            )}
          </div>

          <textarea
            ref={promptRef}
            rows={1}
            className="lemma-chat-prompt-input"
            aria-label="AI Chat Prompt"
            placeholder={selection ? "Edit the selection" : "Ask anything · @ to reference a file"}
            value={promptText}
            onChange={(e) => {
              setPromptText(e.target.value);
              updateAtQuery(e.target.value, e.target.selectionStart ?? e.target.value.length);
            }}
            onPaste={(e) => {
              const items = Array.from(e.clipboardData?.items ?? []);
              const imageFiles: File[] = [];
              for (const item of items) {
                if (item.kind === "file" && item.type.startsWith("image/")) {
                  const file = item.getAsFile();
                  if (file) imageFiles.push(file);
                }
              }
              if (imageFiles.length === 0 && e.clipboardData?.files?.length) {
                for (const file of Array.from(e.clipboardData.files)) {
                  if (file.type.startsWith("image/")) imageFiles.push(file);
                }
              }
              if (imageFiles.length === 0) return;
              e.preventDefault();
              for (let i = 0; i < imageFiles.length; i += 1) {
                const raw = imageFiles[i];
                const ext = raw.type.split("/")[1]?.replace("jpeg", "jpg") || "png";
                const needsUniqueName = !raw.name || raw.name === "image.png" || attachments.includes(raw.name);
                const file = needsUniqueName
                  ? new File([raw], `pasted-image-${Date.now()}${i > 0 ? `-${i}` : ""}.${ext}`, { type: raw.type })
                  : raw;
                void attachToChat(file);
              }
            }}
            onBlur={() => {
              if (atQuery !== null) closePicker();
            }}
            onKeyDown={(e) => {
              if (pickerOpen && atQuery !== null) {
                if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                  e.preventDefault();
                  const step = e.key === "ArrowDown" ? 1 : -1;
                  setPickerIndex((i) => Math.max(0, Math.min(pickerMatches.length - 1, i + step)));
                  return;
                }
                if ((e.key === "Enter" || e.key === "Tab") && pickerMatches[pickerIndex]) {
                  e.preventDefault();
                  choosePickerFile(pickerMatches[pickerIndex]);
                  return;
                }
                if (e.key === "Escape") {
                  e.preventDefault();
                  closePicker();
                  return;
                }
              }
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                e.currentTarget.form?.requestSubmit();
              }
            }}
          />
        </form>
        {runInAnotherChat && (
          <div className="lemma-chat-run-notice" role="status">
            A run is already in progress
          </div>
        )}
        {isRunningCurrentChat && (
          <button
            type="button"
            aria-label="Stop run"
            title="Stop generating"
            className="lemma-btn lemma-btn-small lemma-chat-stop"
            onClick={() => void agentRun.stopRun()}
          >
            <svg width="8" height="8" viewBox="0 0 8 8" fill="currentColor" aria-hidden="true">
              <rect width="8" height="8" rx="1.5" />
            </svg>
            <span>Stop</span>
          </button>
        )}
        <button
          type="submit"
          form="chat-prompt-form"
          className="lemma-btn lemma-btn-primary lemma-btn-small lemma-chat-send"
          aria-label="Send Prompt"
          title={runInAnotherChat ? "A run is already in progress" : undefined}
          disabled={!modelAllowed || agentRun.activeRun !== null}
        >
          <span aria-hidden="true">↑</span>
        </button>
      </div>
    </>
  );
}

export default FloatingChatBar;
