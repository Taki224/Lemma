// @vitest-environment jsdom
import { renderHook, act, waitFor } from "@testing-library/react";
import { test, expect, vi, beforeEach, afterEach } from "vitest";
import { useAgentRun, chatScopeKey } from "../hooks/useAgentRun";
import type { ProposedHunkItem } from "../components/DiffExtension";

function makeHunk(id: string, filePath = "main.tex"): ProposedHunkItem {
  return {
    hunk_id: id,
    file_path: filePath,
    base_etag: "etag-1",
    start_line: 1,
    end_line: 2,
    original_text: "old",
    replacement_text: "new",
    cite_keys: [],
    unknown_cite_keys: [],
    overlap_warnings: [],
    claim_evidence: [],
    injection_warning: false,
  };
}

function makeSseStream() {
  const encoder = new TextEncoder();
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  return {
    stream,
    pushEvent(eventType: string, payload: Record<string, unknown>) {
      const chunk = `event: ${eventType}\ndata: ${JSON.stringify(payload)}\n\n`;
      controller.enqueue(encoder.encode(chunk));
    },
    pushRaw(raw: string) {
      controller.enqueue(encoder.encode(raw));
    },
    close() {
      controller.close();
    },
  };
}

function sseResponse(stream: ReadableStream<Uint8Array>, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    body: stream,
    json: async () => ({}),
  } as unknown as Response;
}

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    body: null,
  } as unknown as Response;
}

const originalFetch = globalThis.fetch;

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("lemma_csrf_token", "csrf-test-token");
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

test("test_start_run_streams_status_lines_and_stores_chat_scoped_hunks", async () => {
  const sse = makeSseStream();
  const onRunFinished = vi.fn();

  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/ai/runs/active") {
      return jsonResponse({ run: null });
    }
    if (url === "/api/projects/p1/chats/c1/runs" && init?.method === "POST") {
      expect((init.headers as Record<string, string>)["X-CSRF-Token"]).toBe("csrf-test-token");
      return jsonResponse(
        {
          run_id: "run-1",
          project_id: "p1",
          chat_id: "c1",
          profile_id: "writing",
          status: "running",
        },
        201
      );
    }
    if (url === "/api/ai/runs/run-1/events") {
      return sseResponse(sse.stream);
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const { result } = renderHook(() => useAgentRun({ onRunFinished }));

  await waitFor(() => {
    expect(globalThis.fetch).toHaveBeenCalledWith("/api/ai/runs/active", expect.anything());
  });

  await act(async () => {
    const res = await result.current.startRun({
      projectId: "p1",
      chatId: "c1",
      prompt: "Improve the intro",
      model: "openai/gpt-4o",
      activeFile: "main.tex",
    });
    expect(res.ok).toBe(true);
    expect(res.runId).toBe("run-1");
  });

  expect(result.current.activeRun?.runId).toBe("run-1");
  expect(result.current.activeRun?.lines).toEqual([]);

  await act(async () => {
    sse.pushEvent("started", {
      type: "started",
      chat_id: "c1",
      user_message_id: "m-user",
      profile_id: "review",
      context: [{ path: "main.tex", detail: "active", kind: "active" }],
    });
  });

  await waitFor(() => {
    expect(result.current.activeRun?.profileId).toBe("review");
    expect(result.current.activeRun?.context).toEqual([
      { path: "main.tex", detail: "active", kind: "active" },
    ]);
  });

  await act(async () => {
    sse.pushEvent("status", {
      type: "status",
      message_id: "s1",
      content: "Reviewing main.tex",
      pill_file: null,
    });
  });

  await waitFor(() => {
    expect(result.current.activeRun?.lines).toEqual([
      { messageId: "s1", content: "Reviewing main.tex", pillFile: null },
    ]);
  });

  await act(async () => {
    sse.pushEvent("status", {
      type: "status",
      message_id: "s2",
      content: "Reading related section",
      pill_file: "sections/intro.tex",
    });
  });

  await waitFor(() => {
    expect(result.current.activeRun?.lines).toEqual([
      { messageId: "s1", content: "Reviewing main.tex", pillFile: null },
      { messageId: "s2", content: "Reading related section", pillFile: "sections/intro.tex" },
    ]);
  });

  const hunks = [makeHunk("h1")];
  await act(async () => {
    sse.pushEvent("done", {
      type: "done",
      run_id: "run-1",
      project_id: "p1",
      chat_id: "c1",
      reply: "Here is an improved intro.",
      profile_id: "review",
      context: [{ path: "main.tex", detail: "active", kind: "active" }],
      hunks,
    });
    sse.close();
  });

  await waitFor(() => {
    expect(result.current.activeRun).toBeNull();
    expect(result.current.pendingHunksByChat[chatScopeKey("p1", "c1")]).toEqual(hunks);
  });

  expect(onRunFinished).toHaveBeenCalledWith({
    projectId: "p1",
    chatId: "c1",
    status: "completed",
    reply: "Here is an improved intro.",
    profileId: "review",
    context: [{ path: "main.tex", detail: "active", kind: "active" }],
    hunks,
  });
});

test("test_stop_run_calls_stop_endpoint_and_clears_unaccepted_hunks", async () => {
  const sse = makeSseStream();
  const onRunFinished = vi.fn();

  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/ai/runs/active") {
      return jsonResponse({ run: null });
    }
    if (url === "/api/projects/p1/chats/c1/runs" && init?.method === "POST") {
      return jsonResponse(
        {
          run_id: "run-stop-1",
          project_id: "p1",
          chat_id: "c1",
          profile_id: "writing",
          status: "running",
        },
        201
      );
    }
    if (url === "/api/ai/runs/run-stop-1/events") {
      return sseResponse(sse.stream);
    }
    if (url === "/api/ai/runs/run-stop-1/stop" && init?.method === "POST") {
      expect((init.headers as Record<string, string>)["X-CSRF-Token"]).toBe("csrf-test-token");
      sse.pushEvent("stopped", {
        type: "stopped",
        run_id: "run-stop-1",
        project_id: "p1",
        chat_id: "c1",
      });
      sse.close();
      return jsonResponse({ run_id: "run-stop-1", status: "stopped" });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const { result } = renderHook(() => useAgentRun({ onRunFinished }));

  await act(async () => {
    await result.current.startRun({
      projectId: "p1",
      chatId: "c1",
      prompt: "Draft methods",
      model: "openai/gpt-4o",
    });
  });

  expect(result.current.activeRun?.runId).toBe("run-stop-1");

  await act(async () => {
    await result.current.stopRun();
  });

  await waitFor(() => {
    expect(result.current.activeRun).toBeNull();
  });
  expect(result.current.pendingHunksByChat[chatScopeKey("p1", "c1")]).toBeUndefined();
  expect(globalThis.fetch).toHaveBeenCalledWith(
    "/api/ai/runs/run-stop-1/stop",
    expect.objectContaining({ method: "POST" })
  );
  expect(onRunFinished).toHaveBeenCalledWith({
    projectId: "p1",
    chatId: "c1",
    status: "stopped",
  });
});

test("test_resumes_active_run_on_mount_from_active_endpoint", async () => {
  const sse = makeSseStream();

  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/ai/runs/active") {
      return jsonResponse({
        run: {
          run_id: "run-resume-1",
          project_id: "p2",
          chat_id: "c9",
          profile_id: "writing",
          status: "running",
          events: [],
        },
      });
    }
    if (url === "/api/ai/runs/run-resume-1/events") {
      return sseResponse(sse.stream);
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const { result } = renderHook(() => useAgentRun());

  await waitFor(() => {
    expect(result.current.activeRun?.runId).toBe("run-resume-1");
    expect(result.current.activeRun?.projectId).toBe("p2");
    expect(result.current.activeRun?.chatId).toBe("c9");
  });

  await act(async () => {
    sse.pushEvent("status", {
      type: "status",
      message_id: "s-replay",
      content: "Replayed status",
      pill_file: null,
    });
    sse.pushEvent("status", {
      type: "status",
      message_id: "s-live",
      content: "Live status",
      pill_file: "appendix.tex",
    });
  });

  await waitFor(() => {
    expect(result.current.activeRun?.lines).toEqual([
      { messageId: "s-replay", content: "Replayed status", pillFile: null },
      { messageId: "s-live", content: "Live status", pillFile: "appendix.tex" },
    ]);
  });

  await act(async () => {
    sse.close();
  });
});

test("test_error_and_conflict_keep_no_hunks_and_clear_active_run", async () => {
  const sse = makeSseStream();
  const onRunFinished = vi.fn();
  let postAttempt = 0;

  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/ai/runs/active") {
      return jsonResponse({ run: null });
    }
    if (url === "/api/projects/p1/chats/c1/runs" && init?.method === "POST") {
      postAttempt += 1;
      if (postAttempt === 1) {
        return jsonResponse({ detail: "Another chat run is already active" }, 409);
      }
      return jsonResponse(
        {
          run_id: "run-err-1",
          project_id: "p1",
          chat_id: "c1",
          profile_id: "writing",
          status: "running",
        },
        201
      );
    }
    if (url === "/api/ai/runs/run-err-1/events") {
      return sseResponse(sse.stream);
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const { result } = renderHook(() => useAgentRun({ onRunFinished }));

  // 1. 409 Conflict
  await act(async () => {
    const conflictRes = await result.current.startRun({
      projectId: "p1",
      chatId: "c1",
      prompt: "First attempt",
      model: "openai/gpt-4o",
    });
    expect(conflictRes.ok).toBe(false);
    expect(conflictRes.status).toBe(409);
  });

  expect(result.current.activeRun).toBeNull();
  expect(result.current.pendingHunksByChat[chatScopeKey("p1", "c1")]).toBeUndefined();
  expect(result.current.errorByChat[chatScopeKey("p1", "c1")]).toContain("Another chat run");

  // 2. Mid-run error SSE event
  await act(async () => {
    const okRes = await result.current.startRun({
      projectId: "p1",
      chatId: "c1",
      prompt: "Second attempt",
      model: "openai/gpt-4o",
    });
    expect(okRes.ok).toBe(true);
  });

  expect(result.current.activeRun?.runId).toBe("run-err-1");
  expect(result.current.errorByChat[chatScopeKey("p1", "c1")]).toBeUndefined();

  await act(async () => {
    sse.pushEvent("error", {
      type: "error",
      run_id: "run-err-1",
      project_id: "p1",
      chat_id: "c1",
      detail: "OpenRouter upstream timeout",
    });
    sse.close();
  });

  await waitFor(() => {
    expect(result.current.activeRun).toBeNull();
    expect(result.current.errorByChat[chatScopeKey("p1", "c1")]).toBe(
      "OpenRouter upstream timeout"
    );
  });
  expect(result.current.pendingHunksByChat[chatScopeKey("p1", "c1")]).toBeUndefined();
  expect(onRunFinished).toHaveBeenCalledWith({
    projectId: "p1",
    chatId: "c1",
    status: "failed",
    error: "OpenRouter upstream timeout",
  });
});

test("test_stream_failure_and_premature_end_clear_active_run_and_record_error", async () => {
  const sse = makeSseStream();
  let attempt = 0;

  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/ai/runs/active") {
      return jsonResponse({ run: null });
    }
    if (url === "/api/projects/p1/chats/c1/runs" && init?.method === "POST") {
      attempt += 1;
      return jsonResponse(
        { run_id: `run-stream-${attempt}`, project_id: "p1", chat_id: "c1", status: "running" },
        201
      );
    }
    if (url === "/api/ai/runs/run-stream-1/events") {
      return jsonResponse({ detail: "Stream unavailable" }, 500);
    }
    if (url === "/api/ai/runs/run-stream-2/events") {
      return sseResponse(sse.stream);
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const { result } = renderHook(() => useAgentRun());

  // 1. Non-ok /events response
  await act(async () => {
    await result.current.startRun({
      projectId: "p1",
      chatId: "c1",
      prompt: "First",
      model: "openai/gpt-4o",
    });
  });

  await waitFor(() => {
    expect(result.current.activeRun).toBeNull();
    expect(result.current.errorByChat[chatScopeKey("p1", "c1")]).toBe("Stream unavailable");
  });

  // 2. Stream closes without terminal done/stopped/error event
  await act(async () => {
    await result.current.startRun({
      projectId: "p1",
      chatId: "c1",
      prompt: "Second",
      model: "openai/gpt-4o",
    });
  });

  await act(async () => {
    sse.close();
  });

  await waitFor(() => {
    expect(result.current.activeRun).toBeNull();
    expect(result.current.errorByChat[chatScopeKey("p1", "c1")]).toBe(
      "Agent event stream ended unexpectedly"
    );
  });
});

test("test_conflict_409_preserves_hunks_and_resyncs_active_run_and_empty_hunks_omitted", async () => {
  const sse1 = makeSseStream();
  const sseOther = makeSseStream();
  let otherActive = false;

  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/ai/runs/active") {
      return jsonResponse(
        otherActive
          ? {
              run: {
                run_id: "run-other",
                project_id: "p2",
                chat_id: "c2",
                profile_id: "writing",
                status: "running",
              },
            }
          : { run: null }
      );
    }
    if (url === "/api/projects/p1/chats/c1/runs" && init?.method === "POST") {
      if (otherActive) {
        return jsonResponse({ detail: "Another chat run is already active" }, 409);
      }
      return jsonResponse(
        { run_id: "run-1", project_id: "p1", chat_id: "c1", status: "running" },
        201
      );
    }
    if (url === "/api/ai/runs/run-1/events") {
      return sseResponse(sse1.stream);
    }
    if (url === "/api/ai/runs/run-other/events") {
      return sseResponse(sseOther.stream);
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as typeof fetch;

  const { result } = renderHook(() => useAgentRun());

  // First run produces a hunk
  await act(async () => {
    await result.current.startRun({
      projectId: "p1",
      chatId: "c1",
      prompt: "Edit",
      model: "openai/gpt-4o",
    });
  });

  const h1 = [makeHunk("h1")];
  await act(async () => {
    sse1.pushEvent("done", {
      type: "done",
      run_id: "run-1",
      project_id: "p1",
      chat_id: "c1",
      reply: "Done",
      hunks: h1,
    });
    sse1.close();
  });

  await waitFor(() => {
    expect(result.current.pendingHunksByChat[chatScopeKey("p1", "c1")]).toEqual(h1);
  });

  // Now another run is active on server -> 409 must preserve hunks and re-sync activeRun
  otherActive = true;
  await act(async () => {
    const res = await result.current.startRun({
      projectId: "p1",
      chatId: "c1",
      prompt: "Second",
      model: "openai/gpt-4o",
    });
    expect(res.ok).toBe(false);
    expect(res.status).toBe(409);
  });

  await waitFor(() => {
    expect(result.current.activeRun?.runId).toBe("run-other");
  });
  expect(result.current.pendingHunksByChat[chatScopeKey("p1", "c1")]).toEqual(h1);

  // Empty hunks array on done should not store [] in pendingHunksByChat
  await act(async () => {
    sseOther.pushEvent("done", {
      type: "done",
      run_id: "run-other",
      project_id: "p2",
      chat_id: "c2",
      reply: "No edits",
      hunks: [],
    });
    sseOther.close();
  });

  await waitFor(() => {
    expect(result.current.activeRun).toBeNull();
  });
  expect(result.current.pendingHunksByChat[chatScopeKey("p2", "c2")]).toBeUndefined();
});
