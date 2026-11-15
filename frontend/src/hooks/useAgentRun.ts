import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatContextEntry } from "../components/FloatingChatBar";
import type { ProposedHunkItem } from "../components/DiffExtension";

export interface AgentStatusLine {
  messageId?: string;
  content: string;
  pillFile?: string | null;
}

export interface ActiveAgentRun {
  runId: string;
  projectId: string;
  chatId: string;
  profileId: string;
  status: "running";
  userPrompt?: string;
  context?: ChatContextEntry[];
  lines: AgentStatusLine[];
  error?: string | null;
}

export interface StartAgentRunInput {
  projectId: string;
  chatId: string;
  prompt: string;
  model: string;
  profileId?: string;
  activeFile?: string | null;
  includeQuarantined?: boolean;
  selection?: {
    text: string;
    startLine: number;
    endLine: number;
  } | null;
  cursorLine?: number | null;
  bufferText?: string | null;
  references?: string[];
  referenceBuffers?: Record<string, string>;
  attachments?: string[];
  csrfToken?: string;
}

export interface StartAgentRunResult {
  ok: boolean;
  runId?: string;
  status?: number;
  error?: string;
}

export interface AgentRunFinishedInfo {
  projectId: string;
  chatId: string;
  status: "completed" | "stopped" | "failed";
  reply?: string;
  profileId?: string;
  context?: ChatContextEntry[];
  hunks?: ProposedHunkItem[];
  error?: string;
}

export interface UseAgentRunOptions {
  disabled?: boolean;
  csrfToken?: string;
  onRunFinished?: (info: AgentRunFinishedInfo) => void;
}

export interface AgentRunController {
  activeRun: ActiveAgentRun | null;
  pendingHunksByChat: Record<string, ProposedHunkItem[]>;
  errorByChat: Record<string, string | null>;
  lastFinishedRun?: (AgentRunFinishedInfo & { runId: string }) | null;
  startRun: (input: StartAgentRunInput) => Promise<StartAgentRunResult>;
  stopRun: () => Promise<void>;
  clearChatHunks: (projectId: string, chatId: string) => void;
  resolveChatHunk: (projectId: string, chatId: string, hunkId: string) => void;
}

/** Build the composite storage key for chat-scoped hunks and errors. */
export function chatScopeKey(projectId: string, chatId: string): string {
  return `${projectId}:${chatId}`;
}

function getCsrfToken(explicit?: string): string {
  if (explicit) return explicit;
  if (typeof localStorage !== "undefined") {
    return localStorage.getItem("lemma_csrf_token") || "";
  }
  return "";
}

async function readErrorDetail(res: Response, fallback: string): Promise<string> {
  try {
    const data = await res.json();
    if (data && typeof data.detail === "string" && data.detail) {
      return data.detail;
    }
  } catch {
    // Non-JSON error body.
  }
  return `${fallback} (HTTP ${res.status})`;
}

interface ParsedSseEvent {
  event?: string;
  data: Record<string, unknown>;
}

function parseSseBlock(block: string): ParsedSseEvent | null {
  const lines = block.split(/\r?\n/);
  let eventType: string | undefined;
  const dataLines: string[] = [];

  for (const line of lines) {
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) {
      eventType = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trimStart());
    }
  }

  if (dataLines.length === 0) return null;
  try {
    const parsed = JSON.parse(dataLines.join("\n"));
    if (parsed && typeof parsed === "object") {
      return { event: eventType, data: parsed as Record<string, unknown> };
    }
  } catch {
    // Ignore malformed JSON event blocks.
  }
  return null;
}

function removeKey<T>(record: Record<string, T>, key: string): Record<string, T> {
  if (!(key in record)) return record;
  const next = { ...record };
  delete next[key];
  return next;
}

/**
 * Manage the global active AI agent run, SSE event streaming, and chat-scoped proposed hunks.
 */
export function useAgentRun(options: UseAgentRunOptions = {}): AgentRunController {
  const { disabled = false, csrfToken: optionCsrfToken, onRunFinished } = options;

  const [activeRun, setActiveRun] = useState<ActiveAgentRun | null>(null);
  const [pendingHunksByChat, setPendingHunksByChat] = useState<Record<string, ProposedHunkItem[]>>(
    {}
  );
  const [errorByChat, setErrorByChat] = useState<Record<string, string | null>>({});
  const [lastFinishedRun, setLastFinishedRun] = useState<
    (AgentRunFinishedInfo & { runId: string }) | null
  >(null);

  const activeRunRef = useRef<ActiveAgentRun | null>(null);
  activeRunRef.current = activeRun;

  const onRunFinishedRef = useRef(onRunFinished);
  onRunFinishedRef.current = onRunFinished;

  const mountedRef = useRef(true);
  const streamAbortRef = useRef<AbortController | null>(null);
  const terminalEmittedRunIdsRef = useRef<Set<string>>(new Set());

  const handleSseEvent = useCallback(
    (runId: string, fallbackProjectId: string, fallbackChatId: string, evt: ParsedSseEvent) => {
      if (!mountedRef.current) return;
      const payload = evt.data;
      const type = (typeof payload.type === "string" ? payload.type : evt.event) || "";
      const projectId =
        typeof payload.project_id === "string" ? payload.project_id : fallbackProjectId;
      const chatId = typeof payload.chat_id === "string" ? payload.chat_id : fallbackChatId;
      const key = chatScopeKey(projectId, chatId);

      if (type === "started") {
        setActiveRun((prev) => {
          if (!prev || prev.runId !== runId) return prev;
          return {
            ...prev,
            profileId:
              typeof payload.profile_id === "string" ? payload.profile_id : prev.profileId,
            context: Array.isArray(payload.context)
              ? (payload.context as ChatContextEntry[])
              : prev.context,
          };
        });
        return;
      }

      if (type === "status") {
        const messageId =
          typeof payload.message_id === "string" ? payload.message_id : undefined;
        const content = typeof payload.content === "string" ? payload.content : "";
        const pillFile =
          typeof payload.pill_file === "string" ? payload.pill_file : null;
        if (!content) return;

        setActiveRun((prev) => {
          if (!prev || prev.runId !== runId) return prev;
          if (messageId && prev.lines.some((line) => line.messageId === messageId)) {
            return prev;
          }
          return {
            ...prev,
            lines: [...prev.lines, { messageId, content, pillFile }],
          };
        });
        return;
      }

      if (type === "done") {
        terminalEmittedRunIdsRef.current.add(runId);
        const hunks = Array.isArray(payload.hunks)
          ? (payload.hunks as ProposedHunkItem[])
          : undefined;
        if (Array.isArray(hunks) && hunks.length > 0) {
          setPendingHunksByChat((prev) => ({ ...prev, [key]: hunks }));
        } else {
          setPendingHunksByChat((prev) => removeKey(prev, key));
        }
        setActiveRun((prev) => (prev && prev.runId === runId ? null : prev));
        const finishedInfo: AgentRunFinishedInfo = {
          projectId,
          chatId,
          status: "completed",
          reply: typeof payload.reply === "string" ? payload.reply : undefined,
          profileId: typeof payload.profile_id === "string" ? payload.profile_id : undefined,
          context: Array.isArray(payload.context)
            ? (payload.context as ChatContextEntry[])
            : undefined,
          hunks,
        };
        setLastFinishedRun({ ...finishedInfo, runId });
        onRunFinishedRef.current?.(finishedInfo);
        return;
      }

      if (type === "stopped") {
        terminalEmittedRunIdsRef.current.add(runId);
        setPendingHunksByChat((prev) => removeKey(prev, key));
        setActiveRun((prev) => (prev && prev.runId === runId ? null : prev));
        const finishedInfo: AgentRunFinishedInfo = {
          projectId,
          chatId,
          status: "stopped",
        };
        setLastFinishedRun({ ...finishedInfo, runId });
        onRunFinishedRef.current?.(finishedInfo);
        return;
      }

      if (type === "error") {
        terminalEmittedRunIdsRef.current.add(runId);
        const errorMsg =
          (typeof payload.detail === "string" && payload.detail) ||
          (typeof payload.error === "string" && payload.error) ||
          "Agent run failed";
        setPendingHunksByChat((prev) => removeKey(prev, key));
        setErrorByChat((prev) => ({ ...prev, [key]: errorMsg }));
        setActiveRun((prev) => (prev && prev.runId === runId ? null : prev));
        const finishedInfo: AgentRunFinishedInfo = {
          projectId,
          chatId,
          status: "failed",
          error: errorMsg,
        };
        setLastFinishedRun({ ...finishedInfo, runId });
        onRunFinishedRef.current?.(finishedInfo);
      }
    },
    []
  );

  const attachToStream = useCallback(
    async (runId: string, projectId: string, chatId: string) => {
      if (typeof fetch !== "function") return;
      streamAbortRef.current?.abort();
      const controller = new AbortController();
      streamAbortRef.current = controller;

      const failStream = (errMsg: string) => {
        if (!mountedRef.current || controller.signal.aborted) return;
        if (terminalEmittedRunIdsRef.current.has(runId)) return;
        terminalEmittedRunIdsRef.current.add(runId);
        const key = chatScopeKey(projectId, chatId);
        setErrorByChat((prev) => ({ ...prev, [key]: errMsg }));
        setActiveRun((prev) => (prev && prev.runId === runId ? null : prev));
        const finishedInfo: AgentRunFinishedInfo = {
          projectId,
          chatId,
          status: "failed",
          error: errMsg,
        };
        setLastFinishedRun({ ...finishedInfo, runId });
        onRunFinishedRef.current?.(finishedInfo);
      };

      try {
        const res = await fetch(`/api/ai/runs/${runId}/events`, {
          signal: controller.signal,
        });
        if (!res.ok) {
          const detail = await readErrorDetail(res, "Agent event stream failed");
          failStream(detail);
          return;
        }
        if (!res.body) {
          failStream("Agent event stream unavailable");
          return;
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          const blocks = buffer.split(/\r?\n\r?\n/);
          buffer = blocks.pop() ?? "";
          for (const block of blocks) {
            const parsed = parseSseBlock(block);
            if (parsed) {
              handleSseEvent(runId, projectId, chatId, parsed);
            }
          }
        }

        buffer += decoder.decode();
        if (buffer.trim()) {
          const parsed = parseSseBlock(buffer);
          if (parsed) {
            handleSseEvent(runId, projectId, chatId, parsed);
          }
        }

        if (!terminalEmittedRunIdsRef.current.has(runId) && !controller.signal.aborted) {
          failStream("Agent event stream ended unexpectedly");
        }
      } catch (err) {
        if ((err as { name?: string })?.name === "AbortError" || controller.signal.aborted) return;
        const msg = err instanceof Error ? err.message : "Agent event stream disconnected";
        failStream(msg);
      }
    },
    [handleSseEvent]
  );

  useEffect(() => {
    mountedRef.current = true;
    if (disabled || typeof fetch !== "function") {
      return () => {
        mountedRef.current = false;
        streamAbortRef.current?.abort();
      };
    }

    const mountAbort = new AbortController();
    fetch("/api/ai/runs/active", { signal: mountAbort.signal })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (!mountedRef.current || !data?.run) return;
        const run = data.run;
        if (!run.run_id || run.status !== "running") return;

        setActiveRun({
          runId: run.run_id,
          projectId: run.project_id,
          chatId: run.chat_id,
          profileId: typeof run.profile_id === "string" ? run.profile_id : "",
          status: "running",
          lines: [],
          error: null,
        });
        void attachToStream(run.run_id, run.project_id, run.chat_id);
      })
      .catch(() => {});

    return () => {
      mountedRef.current = false;
      mountAbort.abort();
      streamAbortRef.current?.abort();
    };
  }, [disabled, attachToStream]);

  const startRun = useCallback(
    async (input: StartAgentRunInput): Promise<StartAgentRunResult> => {
      const key = chatScopeKey(input.projectId, input.chatId);
      setErrorByChat((prev) => removeKey(prev, key));

      if (typeof fetch !== "function") {
        return { ok: false, error: "fetch is unavailable" };
      }

      const csrf = getCsrfToken(input.csrfToken ?? optionCsrfToken);
      const body: Record<string, unknown> = {
        prompt: input.prompt,
        model: input.model,
        profile_id: input.profileId ?? "__auto__",
        active_file: input.activeFile ?? null,
        include_quarantined: Boolean(input.includeQuarantined),
      };
      if (input.selection) {
        body.selection = {
          text: input.selection.text,
          start_line: input.selection.startLine,
          end_line: input.selection.endLine,
        };
      }
      if (typeof input.cursorLine === "number") {
        body.cursor_line = input.cursorLine;
      }
      if (typeof input.bufferText === "string") {
        body.buffer_text = input.bufferText;
      }
      if (input.references && input.references.length > 0) {
        body.references = input.references;
        if (input.referenceBuffers) {
          body.reference_buffers = input.referenceBuffers;
        }
      }
      if (input.attachments && input.attachments.length > 0) {
        body.attachments = input.attachments;
      }

      try {
        const res = await fetch(
          `/api/projects/${input.projectId}/chats/${input.chatId}/runs`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRF-Token": csrf,
            },
            body: JSON.stringify(body),
          }
        );

        if (!res.ok) {
          const detail = await readErrorDetail(res, "Failed to start agent run");
          setErrorByChat((prev) => ({ ...prev, [key]: detail }));
          if (res.status === 409) {
            if (typeof fetch === "function") {
              try {
                const activeRes = await fetch("/api/ai/runs/active");
                if (activeRes.ok) {
                  const activeData = await activeRes.json();
                  if (mountedRef.current) {
                    const run = activeData?.run;
                    if (run && run.run_id && run.status === "running") {
                      setActiveRun((prev) =>
                        prev && prev.runId === run.run_id
                          ? prev
                          : {
                              runId: run.run_id,
                              projectId: run.project_id,
                              chatId: run.chat_id,
                              profileId: typeof run.profile_id === "string" ? run.profile_id : "",
                              status: "running",
                              lines: [],
                              error: null,
                            }
                      );
                      void attachToStream(run.run_id, run.project_id, run.chat_id);
                    } else {
                      setActiveRun(null);
                    }
                  }
                }
              } catch {
                // Keep existing activeRun if re-sync fails.
              }
            }
          } else {
            setActiveRun(null);
          }
          return { ok: false, status: res.status, error: detail };
        }

        const data = await res.json();
        const runId = String(data.run_id);
        const projectId = typeof data.project_id === "string" ? data.project_id : input.projectId;
        const chatId = typeof data.chat_id === "string" ? data.chat_id : input.chatId;
        const profileId =
          typeof data.profile_id === "string" ? data.profile_id : input.profileId || "";

        setPendingHunksByChat((prev) => removeKey(prev, key));
        setActiveRun({
          runId,
          projectId,
          chatId,
          profileId,
          status: "running",
          userPrompt: input.prompt,
          lines: [],
          error: null,
        });

        void attachToStream(runId, projectId, chatId);
        return { ok: true, runId, status: res.status };
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Failed to start agent run";
        setErrorByChat((prev) => ({ ...prev, [key]: msg }));
        setActiveRun(null);
        return { ok: false, error: msg };
      }
    },
    [optionCsrfToken, attachToStream]
  );

  const stopRun = useCallback(async (): Promise<void> => {
    const current = activeRunRef.current;
    if (!current || typeof fetch !== "function") return;

    const key = chatScopeKey(current.projectId, current.chatId);
    setPendingHunksByChat((prev) => removeKey(prev, key));

    const csrf = getCsrfToken(optionCsrfToken);
    try {
      await fetch(`/api/ai/runs/${current.runId}/stop`, {
        method: "POST",
        headers: {
          "X-CSRF-Token": csrf,
        },
      });
    } finally {
      streamAbortRef.current?.abort();
      setActiveRun((prev) => (prev && prev.runId === current.runId ? null : prev));
      if (!terminalEmittedRunIdsRef.current.has(current.runId)) {
        terminalEmittedRunIdsRef.current.add(current.runId);
        const finishedInfo: AgentRunFinishedInfo = {
          projectId: current.projectId,
          chatId: current.chatId,
          status: "stopped",
        };
        setLastFinishedRun({ ...finishedInfo, runId: current.runId });
        onRunFinishedRef.current?.(finishedInfo);
      }
    }
  }, [optionCsrfToken]);

  const clearChatHunks = useCallback((projectId: string, chatId: string) => {
    const key = chatScopeKey(projectId, chatId);
    setPendingHunksByChat((prev) => removeKey(prev, key));
  }, []);

  const resolveChatHunk = useCallback((projectId: string, chatId: string, hunkId: string) => {
    const key = chatScopeKey(projectId, chatId);
    setPendingHunksByChat((prev) => {
      const existing = prev[key];
      if (!existing) return prev;
      const remaining = existing.filter((h) => h.hunk_id !== hunkId);
      if (remaining.length === 0) {
        return removeKey(prev, key);
      }
      return { ...prev, [key]: remaining };
    });
  }, []);

  return {
    activeRun,
    pendingHunksByChat,
    errorByChat,
    lastFinishedRun,
    startRun,
    stopRun,
    clearChatHunks,
    resolveChatHunk,
  };
}
