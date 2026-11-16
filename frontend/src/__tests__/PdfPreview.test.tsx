import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { test, expect, vi } from "vitest";
import {
  PdfPreview,
  formatPdfStatisticsSummary,
  pdfDocumentLoader,
  statsFromTextItems,
} from "../components/PdfPreview";
import * as pdfjsLib from "pdfjs-dist";

test("test_compile_error_dropdown_renders_diagnostics_and_propose_fix_button", () => {
  const onJumpToLine = vi.fn();
  const onProposeFix = vi.fn();

  const diagnostics = [
    {
      severity: "error" as const,
      file_path: "chapters/intro.tex",
      line: 42,
      message: "Undefined control sequence \\nonexistentmacro",
      context_lines: "l.42 \\nonexistentmacro",
    },
  ];

  render(
    <PdfPreview
      projectId="manuscript-2026"
      status="failed"
      pdfUrl="/api/projects/manuscript-2026/pdf"
      diagnostics={diagnostics}
      onJumpToLine={onJumpToLine}
      onProposeFix={onProposeFix}
    />
  );

  // Status badge shows "1 Error"
  const badgeBtn = screen.getByRole("button", { name: /1 Error/i });
  fireEvent.click(badgeBtn);

  expect(screen.getByText(/Undefined control sequence/i)).toBeInTheDocument();

  const jumpBtn = screen.getByRole("button", { name: /Jump to line/i });
  fireEvent.click(jumpBtn);
  expect(onJumpToLine).toHaveBeenCalledWith("chapters/intro.tex", 42);

  const fixBtn = screen.getByRole("button", { name: /Fix in chat/i });
  fireEvent.click(fixBtn);
  expect(onProposeFix).toHaveBeenCalledWith(diagnostics[0]);
});

test("test_compile_diagnostics_ignore_warnings_toggle_and_propose_fix_error", async () => {
  localStorage.removeItem("lemma_ignore_compile_warnings");
  const onProposeFix = vi.fn().mockRejectedValueOnce(new Error("Model unavailable"));
  const diagnostics = [
    {
      severity: "typesetting" as const,
      file_path: "template.tex",
      line: 168,
      message: "Overfull \\hbox (2.32pt too wide)",
      context_lines: "",
    },
    {
      severity: "warning" as const,
      file_path: "template.tex",
      line: 20,
      message: "Reference `sec:foo' undefined",
      context_lines: "",
    },
  ];

  render(
    <PdfPreview
      projectId="manuscript-2026"
      status="success"
      pdfUrl="/api/projects/manuscript-2026/pdf"
      diagnostics={diagnostics}
      onProposeFix={onProposeFix}
    />
  );

  fireEvent.click(screen.getByRole("button", { name: /Compiled/i }));
  expect(screen.getByText(/Overfull/i)).toBeInTheDocument();
  expect(screen.getByText(/Reference `sec:foo' undefined/i)).toBeInTheDocument();

  const fixBtns = screen.getAllByRole("button", { name: /Fix in chat/i });
  fireEvent.click(fixBtns[0]);
  await waitFor(() => {
    expect(screen.getByRole("alert")).toHaveTextContent("Model unavailable");
  });

  const ignoreCheckbox = screen.getByRole("checkbox", { name: /Ignore warnings/i });
  fireEvent.click(ignoreCheckbox);
  expect(screen.queryByText(/Overfull/i)).toBeNull();
  expect(screen.getByText(/No compile errors \(2 warnings ignored\)/i)).toBeInTheDocument();
  localStorage.removeItem("lemma_ignore_compile_warnings");
});

test("test_pdf_click_triggers_inverse_synctex_and_fallback_hint", async () => {
  const onJumpToLine = vi.fn();
  const fetchMock = vi.fn(async (url: string) => {
    if (url.includes("/synctex/inverse")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          file_path: "chapters/methodology.tex",
          line: 19,
          column: 1,
          exact: false,
          hint: "No exact source mapping — jumped to nearest page position",
        }),
      };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  });

  vi.stubGlobal("fetch", fetchMock);

  render(
    <PdfPreview
      projectId="manuscript-2026"
      status="success"
      pdfUrl="/api/projects/manuscript-2026/pdf"
      diagnostics={[]}
      forwardHighlight={{
        page: 2,
        x: 72,
        y: 240,
        h: 72,
        v: 252,
        width: 0,
        height: 0,
        nonce: 1,
        exact: false,
        hint: "No exact source mapping — jumped to nearest page position",
      }}
      onJumpToLine={onJumpToLine}
    />
  );

  // Verify fallback toast hint is displayed when exact === false
  expect(
    screen.getByText("No exact source mapping — jumped to nearest page position")
  ).toBeInTheDocument();

  // Double-click the rendered PDF page surface to trigger inverse SyncTeX
  const pageSurface = screen.getByTestId("pdf-page-surface-1");
  fireEvent.doubleClick(pageSurface, { clientX: 144, clientY: 300 });

  await waitFor(() => {
    expect(onJumpToLine).toHaveBeenCalledWith("chapters/methodology.tex", 19);
  });
});

test("test_pdf_preview_configures_pdfjs_worker_src_before_rendering", () => {
  // Real pdf.js getDocument() rejects with "No GlobalWorkerOptions.workerSrc
  // specified" unless workerSrc is set; PdfPreview swallows that rejection, so the
  // canvases are never drawn (blank 300x150 boxes).
  render(
    <PdfPreview
      projectId="manuscript-2026"
      status="success"
      pdfUrl="/api/projects/manuscript-2026/pdf"
    />
  );

  expect(typeof pdfjsLib.GlobalWorkerOptions.workerSrc).toBe("string");
  expect(pdfjsLib.GlobalWorkerOptions.workerSrc.length).toBeGreaterThan(0);
});

test("test_pdf_canvas_backing_store_scales_with_device_pixel_ratio_for_crisp_rendering", async () => {
  // On HiDPI screens the canvas bitmap must be devicePixelRatio times the
  // logical (CSS) size, otherwise the browser upscales it and the PDF is blurry.
  const originalDpr = (globalThis as any).devicePixelRatio;
  (globalThis as any).devicePixelRatio = 2;

  // Keep the render result (not a destructured `container`): the stub exposes the
  // current root via a getter and re-mounts the tree on every state change.
  const view = render(
    <PdfPreview
      projectId="manuscript-2026"
      status="success"
      pdfUrl="/api/projects/manuscript-2026/pdf"
      diagnostics={[]}
    />
  );

  try {
    const zoomScale = 1.0;
    const logicalWidth = 612 * zoomScale;

    // The test DOM stub only matches bare tag selectors; make it resolve the
    // `canvas[data-page-number="N"]` lookup the component performs. Scoped to
    // this test and restored afterwards.
    const firstCanvas = view.container.querySelector("canvas") as any;
    const proto = Object.getPrototypeOf(firstCanvas);
    const originalQuerySelector = proto.querySelector;
    proto.querySelector = function (selector: string) {
      const m = /^canvas\[data-page-number="(\d+)"\]$/.exec(selector);
      if (m) {
        return (
          this.querySelectorAll("canvas").find(
            (n: any) => String(n.props["data-page-number"]) === m[1]
          ) || null
        );
      }
      return originalQuerySelector.call(this, selector);
    };

    try {
      // Re-query on every poll: the component re-renders once the document loads.
      const getCanvas = () => view.container.querySelector("canvas") as HTMLCanvasElement;

      await waitFor(() => {
        expect(getCanvas().width).toBeDefined();
      });
      expect(getCanvas().width).toBe(logicalWidth * 2);
      expect(getCanvas().style.width).toBe(`${logicalWidth}px`);
    } finally {
      proto.querySelector = originalQuerySelector;
    }
  } finally {
    (globalThis as any).devicePixelRatio = originalDpr;
  }
});

test("test_pdf_canvas_supersamples_1x_displays_and_rerenders_when_pixel_ratio_changes", async () => {
  const originalOpen = pdfDocumentLoader.open;
  const originalMatchMedia = window.matchMedia;
  const originalDpr = window.devicePixelRatio;
  let dpr = 1;
  const resolutionListeners: Array<() => void> = [];
  Object.defineProperty(window, "devicePixelRatio", { configurable: true, get: () => dpr });
  window.matchMedia = ((query: string) => ({
    media: query,
    matches: true,
    addEventListener: (_type: string, fn: () => void) => {
      resolutionListeners.push(fn);
    },
    removeEventListener: (_type: string, fn: () => void) => {
      const index = resolutionListeners.indexOf(fn);
      if (index >= 0) resolutionListeners.splice(index, 1);
    },
  })) as typeof window.matchMedia;
  const page = {
    getViewport({ scale }: { scale: number }) {
      return { width: 612 * scale, height: 792 * scale };
    },
    render() {
      return { promise: Promise.resolve(), cancel() {} };
    },
  };
  let releasePage: (value: typeof page) => void = () => {};
  const pageGate = new Promise<typeof page>((resolve) => {
    releasePage = resolve;
  });
  pdfDocumentLoader.open = () => ({
    promise: Promise.resolve({
      numPages: 1,
      destroy() {},
      getPage: () => pageGate,
    }),
  });

  const view = render(
    <PdfPreview projectId="manuscript-2026" status="success" pdfUrl="/api/projects/manuscript-2026/pdf" />
  );

  const firstCanvas = view.container.querySelector("canvas") as any;
  const proto = Object.getPrototypeOf(firstCanvas);
  const originalQuerySelector = proto.querySelector;
  proto.querySelector = function (selector: string) {
    const m = /^canvas\[data-page-number="(\d+)"\]$/.exec(selector);
    if (m) {
      return (
        this.querySelectorAll("canvas").find(
          (n: any) => String(n.props["data-page-number"]) === m[1]
        ) || null
      );
    }
    return originalQuerySelector.call(this, selector);
  };

  try {
    releasePage(page);
    const canvas = () => view.container.querySelector("canvas") as HTMLCanvasElement;
    await waitFor(() => {
      expect(canvas().width).toBe(612 * 2);
    });
    expect(canvas().style.width).toBe("612px");

    dpr = 3;
    for (const listener of resolutionListeners.slice()) listener();
    await waitFor(() => {
      expect(canvas().width).toBe(612 * 3);
    });
    expect(canvas().style.width).toBe("612px");
  } finally {
    proto.querySelector = originalQuerySelector;
    pdfDocumentLoader.open = originalOpen;
    window.matchMedia = originalMatchMedia;
    Object.defineProperty(window, "devicePixelRatio", { configurable: true, value: originalDpr });
  }
});

test("test_download_is_styled_button_with_href_when_pdf", () => {
  render(
    <PdfPreview
      projectId="manuscript-2026"
      status="success"
      pdfUrl="/api/projects/manuscript-2026/pdf"
    />
  );

  const link = screen.getByRole("link", { name: "Download PDF" });
  expect(link.getAttribute("href")).toBe("/api/projects/manuscript-2026/pdf");
  expect(link.getAttribute("download")).toBe("manuscript-2026.pdf");
  const classes = String(link.getAttribute("class")).split(/\s+/);
  for (const c of ["lemma-btn", "lemma-btn-secondary", "lemma-btn-xs"]) {
    expect(classes.includes(c)).toBe(true);
  }
});

test("test_download_disabled_with_tooltip_when_no_pdf", () => {
  render(<PdfPreview projectId="manuscript-2026" status="idle" pdfUrl={null} />);

  expect(screen.queryByRole("link", { name: "Download PDF" })).toBeNull();
  const btn = screen.getByRole("button", { name: "Download PDF" });
  expect(btn.getAttribute("title")).toBe("Compile first");
  expect(btn.getAttribute("aria-disabled")).toBe("true");
  expect(btn.hasAttribute("disabled")).toBe(true);
  const classes = String(btn.getAttribute("class")).split(/\s+/);
  for (const c of ["lemma-btn", "lemma-btn-secondary", "lemma-btn-xs"]) {
    expect(classes.includes(c)).toBe(true);
  }
});

const baseBox = {
  page: 1,
  x: 72,
  y: 240,
  h: 72,
  v: 252,
  width: 100,
  height: 20,
  nonce: 1,
  exact: true,
};

function renderWithHighlight(box: any) {
  return render(
    <PdfPreview
      projectId="manuscript-2026"
      status="success"
      pdfUrl="/api/projects/manuscript-2026/pdf"
      forwardHighlight={box}
    />
  );
}

test("test_overlay_uses_width_height_scaled_by_zoom", () => {
  renderWithHighlight(baseBox);

  let box = screen.getByTestId("synctex-highlight-box") as any;
  expect(box.style.left).toBe("72px");
  expect(box.style.top).toBe("232px");
  expect(box.style.width).toBe("100px");
  expect(box.style.height).toBe("20px");

  fireEvent.click(screen.getByRole("button", { name: "Zoom In" }));

  box = screen.getByTestId("synctex-highlight-box") as any;
  expect(box.style.left).toBe("90px");
  expect(box.style.top).toBe("290px");
  expect(box.style.width).toBe("125px");
  expect(box.style.height).toBe("25px");
});

test("test_overlay_positioned_from_h_and_baseline_v_not_x_y", () => {
  // Real synctex output: x:171.13 y:134.76 h:133.77 v:134.76 W:343.71 H:6.92
  renderWithHighlight({
    ...baseBox,
    x: 171.13,
    y: 134.76,
    h: 133.77,
    v: 134.76,
    width: 343.71,
    height: 20,
  });
  const box = screen.getByTestId("synctex-highlight-box") as any;
  expect(box.style.left).toBe("133.77px");
  expect(box.style.top).toBe(`${134.76 - 20}px`);
});

test("test_overlay_enforces_minimum_size", () => {
  renderWithHighlight({ ...baseBox, width: 0, height: 0 });

  const box = screen.getByTestId("synctex-highlight-box") as any;
  expect(box.style.width).toBe("4px");
  expect(box.style.height).toBe("10px");
});

test("test_scrolls_target_page_into_view_on_new_nonce", () => {
  const proto = Object.getPrototypeOf(renderAndGetPage());
  const calls: Array<{ testId: string | null; opts: any }> = [];
  const original = proto.scrollIntoView;
  proto.scrollIntoView = function (opts: any) {
    calls.push({ testId: this.getAttribute("data-testid"), opts });
  };
  try {
    const view = renderWithHighlight({ ...baseBox, nonce: 1 });
    expect(calls.length).toBe(1);
    expect(calls[0].testId).toBe("pdf-page-surface-1");
    expect(calls[0].opts).toEqual({ block: "center", behavior: "smooth" });

    view.rerender(
      <PdfPreview
        projectId="manuscript-2026"
        status="success"
        pdfUrl="/api/projects/manuscript-2026/pdf"
        forwardHighlight={{ ...baseBox, nonce: 2 }}
      />
    );
    expect(calls.length).toBe(2);
  } finally {
    proto.scrollIntoView = original;
  }
});

test("test_same_nonce_does_not_rescroll", () => {
  const proto = Object.getPrototypeOf(renderAndGetPage());
  let count = 0;
  const original = proto.scrollIntoView;
  proto.scrollIntoView = function () {
    count++;
  };
  try {
    const view = renderWithHighlight({ ...baseBox, nonce: 5 });
    expect(count).toBe(1);
    view.rerender(
      <PdfPreview
        projectId="manuscript-2026"
        status="success"
        pdfUrl="/api/projects/manuscript-2026/pdf"
        forwardHighlight={{ ...baseBox, nonce: 5 }}
      />
    );
    fireEvent.click(screen.getByRole("button", { name: "Zoom In" }));
    expect(count).toBe(1);
  } finally {
    proto.scrollIntoView = original;
  }
});

test("test_overlay_clears_after_timeout", () => {
  // The harness has no fake timers: capture the 3 s timer instead of waiting.
  const realSetTimeout = globalThis.setTimeout;
  const realClearTimeout = globalThis.clearTimeout;
  const pending = new Map<number, () => void>();
  let nextId = 1000;
  const cleared: number[] = [];
  globalThis.setTimeout = ((fn: () => void, ms?: number, ...rest: any[]) => {
    if (ms === 3000) {
      const id = nextId++;
      pending.set(id, fn);
      return id as any;
    }
    return realSetTimeout(fn, ms, ...rest);
  }) as any;
  globalThis.clearTimeout = ((id: any) => {
    if (pending.has(id)) {
      pending.delete(id);
      cleared.push(id);
      return;
    }
    realClearTimeout(id);
  }) as any;
  try {
    const view = renderWithHighlight({ ...baseBox, nonce: 1 });
    expect(screen.queryByTestId("synctex-highlight-box") !== null).toBe(true);
    expect(pending.size).toBe(1);

    // A new highlight cancels the previous timer and starts its own.
    view.rerender(
      <PdfPreview
        projectId="manuscript-2026"
        status="success"
        pdfUrl="/api/projects/manuscript-2026/pdf"
        forwardHighlight={{ ...baseBox, nonce: 2 }}
      />
    );
    expect(cleared.length).toBe(1);
    expect(pending.size).toBe(1);
    expect(screen.queryByTestId("synctex-highlight-box") !== null).toBe(true);

    for (const fn of [...pending.values()]) fn();
    expect(screen.queryByTestId("synctex-highlight-box")).toBeNull();
  } finally {
    globalThis.setTimeout = realSetTimeout;
    globalThis.clearTimeout = realClearTimeout;
  }
});

test("test_overlay_hidden_when_rotated", () => {
  renderWithHighlight(baseBox);
  expect(screen.queryByTestId("synctex-highlight-box") !== null).toBe(true);

  fireEvent.click(screen.getByRole("button", { name: "Rotate Page" }));
  expect(screen.queryByTestId("synctex-highlight-box")).toBeNull();
});

function renderAndGetPage() {
  render(
    <PdfPreview
      projectId="manuscript-2026"
      status="success"
      pdfUrl="/api/projects/manuscript-2026/pdf"
    />
  );
  return screen.getByTestId("pdf-page-surface-1");
}

// --- Task 3.1: PDF-only pinch / Ctrl+wheel zoom -------------------------------
// The fake DOM has no addEventListener: install a recording one on the node
// prototype for the duration of a test.
function installListenerRecorder() {
  const proto = Object.getPrototypeOf(renderAndGetPage());
  const original = {
    add: proto.addEventListener,
    remove: proto.removeEventListener,
  };
  const added: Array<{ node: any; type: string; handler: (e: any) => void; options: any }> = [];
  const removed: Array<{ node: any; type: string; handler: (e: any) => void }> = [];
  proto.addEventListener = function (type: string, handler: (e: any) => void, options?: any) {
    added.push({ node: this, type, handler, options });
  };
  proto.removeEventListener = function (type: string, handler: (e: any) => void) {
    removed.push({ node: this, type, handler });
  };
  return {
    added,
    removed,
    restore() {
      proto.addEventListener = original.add;
      proto.removeEventListener = original.remove;
    },
  };
}

function fakeWheel(init: { ctrlKey: boolean; deltaY: number; deltaMode?: number }) {
  let prevented = false;
  return {
    ...init,
    preventDefault() {
      prevented = true;
    },
    get defaultPrevented() {
      return prevented;
    },
  };
}

function renderPdf(pdfUrl: string | null = "/api/projects/manuscript-2026/pdf") {
  return render(<PdfPreview projectId="manuscript-2026" status="success" pdfUrl={pdfUrl} />);
}

test("test_ctrl_wheel_zooms_and_prevents_default", () => {
  const rec = installListenerRecorder();
  try {
    renderPdf();
    const wheel = rec.added.filter((a) => a.type === "wheel");
    expect(wheel.length).toBe(1);
    expect(wheel[0].options).toEqual({ passive: false });
    // Attached to the whole pane so pinching over the toolbar is also kept from zooming the page.
    expect(String(wheel[0].node.getAttribute("class"))).toBe("lemma-pdf-preview-pane");

    const ev = fakeWheel({ ctrlKey: true, deltaY: -10 });
    wheel[0].handler(ev);
    expect(ev.defaultPrevented).toBe(true);
    // 1.0 * exp(0.1) = 1.105 -> 1.11
    expect(screen.getByTestId("pdf-zoom-level").textContent).toBe("111%");
  } finally {
    rec.restore();
  }
});

test("test_plain_wheel_not_prevented", () => {
  const rec = installListenerRecorder();
  try {
    renderPdf();
    const handler = rec.added.filter((a) => a.type === "wheel")[0].handler;
    const ev = fakeWheel({ ctrlKey: false, deltaY: -50 });
    handler(ev);
    expect(ev.defaultPrevented).toBe(false);
    expect(screen.getByTestId("pdf-zoom-level").textContent).toBe("100%");
  } finally {
    rec.restore();
  }
});

test("test_zoom_clamped_to_bounds", () => {
  const rec = installListenerRecorder();
  try {
    renderPdf();
    const handler = rec.added.filter((a) => a.type === "wheel")[0].handler;
    // Each event is capped, so reaching the bounds takes several events.
    for (let i = 0; i < 6; i++) handler(fakeWheel({ ctrlKey: true, deltaY: -1000 }));
    expect(screen.getByTestId("pdf-zoom-level").textContent).toBe("300%");
    for (let i = 0; i < 12; i++) handler(fakeWheel({ ctrlKey: true, deltaY: 1000 }));
    expect(screen.getByTestId("pdf-zoom-level").textContent).toBe("50%");
  } finally {
    rec.restore();
  }
});

test("test_wheel_delta_is_capped_per_event", () => {
  const rec = installListenerRecorder();
  try {
    renderPdf();
    const handler = rec.added.filter((a) => a.type === "wheel")[0].handler;
    handler(fakeWheel({ ctrlKey: true, deltaY: -100000 }));
    // One huge event must not jump straight to the max: 1.0 * exp(0.5) = 1.65 -> 165%.
    expect(screen.getByTestId("pdf-zoom-level").textContent).toBe("165%");
  } finally {
    rec.restore();
  }
});

test("test_wheel_delta_mode_lines_is_normalised_to_pixels", () => {
  const rec = installListenerRecorder();
  try {
    renderPdf();
    const handler = rec.added.filter((a) => a.type === "wheel")[0].handler;
    // Firefox reports lines (deltaMode 1): 1 line = 16 px -> exp(0.16) = 1.17 -> 117%.
    handler(fakeWheel({ ctrlKey: true, deltaY: -1, deltaMode: 1 }));
    expect(screen.getByTestId("pdf-zoom-level").textContent).toBe("117%");
  } finally {
    rec.restore();
  }
});

test("test_listener_removed_on_unmount", () => {
  const rec = installListenerRecorder();
  try {
    const view = renderPdf();
    const wheel = rec.added.filter((a) => a.type === "wheel");
    expect(wheel.length).toBe(1);
    // Only the PDF viewport gets a wheel listener, nothing else.
    expect(rec.added.every((a) => a.type === "wheel" && a.node === wheel[0].node)).toBe(true);
    expect(rec.removed.length).toBe(0);

    // The harness's unmount() does not run effect cleanups; removing the PDF
    // (viewport leaves the tree) is what triggers the same cleanup.
    view.rerender(<PdfPreview projectId="manuscript-2026" status="idle" pdfUrl={null} />);
    expect(rec.removed.length).toBe(1);
    expect(rec.removed[0].type).toBe("wheel");
    expect(rec.removed[0].handler).toBe(wheel[0].handler);
  } finally {
    rec.restore();
  }
});

// --- Task 3.2: rotated PDF disables click-to-source ---------------------------
function stubInverseFetch() {
  const fetchMock = vi.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => ({ file_path: "main.tex", line: 7, exact: true }),
  }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

test("test_double_click_ignored_when_rotated_and_shows_notice", async () => {
  const fetchMock = stubInverseFetch();
  const onJumpToLine = vi.fn();
  render(
    <PdfPreview
      projectId="manuscript-2026"
      status="success"
      pdfUrl="/api/projects/manuscript-2026/pdf"
      onJumpToLine={onJumpToLine}
    />
  );
  fireEvent.click(screen.getByRole("button", { name: "Rotate Page" }));

  const page = screen.getByTestId("pdf-page-surface-1");
  fireEvent.doubleClick(page, { clientX: 10, clientY: 10 });

  await waitFor(() => {
    expect(screen.getByText("Reset rotation to jump to source")).toBeInTheDocument();
  });
  expect(fetchMock).not.toHaveBeenCalled();
  expect(onJumpToLine).not.toHaveBeenCalled();
  expect(screen.getByTestId("pdf-page-surface-1").getAttribute("title")).toMatch(/rotation/i);
});

test("test_double_click_works_at_zero_rotation", async () => {
  const fetchMock = stubInverseFetch();
  const onJumpToLine = vi.fn();
  render(
    <PdfPreview
      projectId="manuscript-2026"
      status="success"
      pdfUrl="/api/projects/manuscript-2026/pdf"
      onJumpToLine={onJumpToLine}
    />
  );
  const page = screen.getByTestId("pdf-page-surface-1");
  expect(page.getAttribute("title")).toBeNull();
  fireEvent.doubleClick(page, { clientX: 10, clientY: 10 });

  await waitFor(() => {
    expect(onJumpToLine).toHaveBeenCalledWith("main.tex", 7);
  });
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(screen.queryByText("Reset rotation to jump to source")).toBeNull();
});

// --- Rotation notice lifecycle -----------------------------------------------------
async function showRotationNotice() {
  stubInverseFetch();
  render(<PdfPreview projectId="manuscript-2026" status="success" pdfUrl="/api/projects/manuscript-2026/pdf" />);
  fireEvent.click(screen.getByRole("button", { name: "Rotate Page" }));
  fireEvent.doubleClick(screen.getByTestId("pdf-page-surface-1"), { clientX: 10, clientY: 10 });
  await waitFor(() => {
    expect(screen.getByText("Reset rotation to jump to source")).toBeInTheDocument();
  });
}

test("test_rotation_notice_clears_when_rotation_returns_to_zero", async () => {
  await showRotationNotice();
  for (let i = 0; i < 3; i++) fireEvent.click(screen.getByRole("button", { name: "Rotate Page" }));
  expect(screen.queryByText("Reset rotation to jump to source")).toBeNull();
});

test("test_rotation_notice_auto_dismisses_after_four_seconds", async () => {
  const realSetTimeout = globalThis.setTimeout;
  const pending: Array<() => void> = [];
  globalThis.setTimeout = ((fn: () => void, ms?: number, ...rest: any[]) => {
    if (ms === 4000) {
      pending.push(fn);
      return pending.length as any;
    }
    return realSetTimeout(fn, ms, ...rest);
  }) as any;
  try {
    await showRotationNotice();
    expect(pending.length).toBeGreaterThan(0);
    for (const fn of pending) fn();
    expect(screen.queryByText("Reset rotation to jump to source")).toBeNull();
  } finally {
    globalThis.setTimeout = realSetTimeout;
  }
});

test("test_double_click_uses_onSyncJump_instead_of_onJumpToLine_when_provided", async () => {
  stubInverseFetch();
  const onJumpToLine = vi.fn();
  const onSyncJump = vi.fn();
  render(
    <PdfPreview
      projectId="manuscript-2026"
      status="success"
      pdfUrl="/api/projects/manuscript-2026/pdf"
      onJumpToLine={onJumpToLine}
      onSyncJump={onSyncJump}
    />
  );
  fireEvent.doubleClick(screen.getByTestId("pdf-page-surface-1"), { clientX: 10, clientY: 10 });
  await waitFor(() => {
    expect(onSyncJump).toHaveBeenCalledWith("main.tex", 7);
  });
  expect(onJumpToLine).not.toHaveBeenCalled();
});

test("test_pdf_text_stats_count_words_lines_and_characters", () => {
  const stats = statsFromTextItems([
    [
      { str: "Hello world", hasEOL: true },
      { str: "Second line" },
    ],
    [{ str: "One" }, { str: " " }, { str: "two." }],
  ]);
  // "Hello world" + "Second line" are split by the end-of-line marker.
  // The marker is a word break and is not included in the character totals.
  expect(stats).toEqual({
    words: 6,
    lines: 3,
    characters: 30,
    charactersExcludingSpaces: 27,
  });
});

test("test_pdf_text_stats_count_a_blank_line_and_ignore_empty_items", () => {
  expect(statsFromTextItems([[{ hasEOL: true }, {}]])).toEqual({
    words: 0,
    lines: 1,
    characters: 0,
    charactersExcludingSpaces: 0,
  });
});

test("test_pdf_statistics_summary_includes_word_and_character_counts", () => {
  expect(formatPdfStatisticsSummary(1, null)).toBe("1 page");
  expect(formatPdfStatisticsSummary(2, null)).toBe("2 pages");
  expect(
    formatPdfStatisticsSummary(2, {
      words: 1200,
      lines: 40,
      characters: 6800,
      charactersExcludingSpaces: 5600,
    })
  ).toBe("2 pages · 1,200 words · 6,800 characters");
  expect(
    formatPdfStatisticsSummary(1, {
      words: 1,
      lines: 1,
      characters: 1,
      charactersExcludingSpaces: 1,
    })
  ).toBe("1 page · 1 word · 1 character");
});

test("test_pdf_statistics_panel_shows_counts_from_pdf_text", async () => {
  const originalOpen = pdfDocumentLoader.open;
  pdfDocumentLoader.open = () => ({
    promise: Promise.resolve({
      numPages: 2,
      destroy() {},
      getPage: async (pageNum: number) => ({
        getViewport() {
          return { width: 612, height: 792 };
        },
        render() {
          return { promise: Promise.resolve(), cancel() {} };
        },
        getTextContent: async () => ({
          items:
            pageNum === 1
              ? [{ str: "Hello world", hasEOL: true }]
              : [{ str: "More text" }],
        }),
      }),
    }),
  });

  try {
    render(
      <PdfPreview
        projectId="manuscript-2026"
        status="success"
        pdfUrl="/api/projects/manuscript-2026/pdf"
      />
    );

    await waitFor(() => {
      const summary = screen.getByTestId("pdf-statistics").textContent || "";
      expect(summary).toContain("2 pages");
      expect(summary).toContain("4 words");
      expect(summary).toContain("20 characters");
    });

    fireEvent.click(screen.getByTestId("pdf-statistics"));
    const panel = screen.getByTestId("pdf-statistics-panel");
    expect(panel.textContent).toContain("Lines");
    expect(panel.textContent).toContain("2");
    expect(panel.textContent).toContain("Characters excluding spaces");
    expect(panel.textContent).toContain("18");
  } finally {
    pdfDocumentLoader.open = originalOpen;
  }
});

test("test_pdf_statistics_read_no_pdf_until_a_file_exists", () => {
  render(<PdfPreview projectId="manuscript-2026" status="idle" pdfUrl={null} />);
  expect(screen.getByTestId("pdf-statistics").textContent).toBe("No PDF");
  expect(screen.queryByTestId("pdf-statistics-panel")).toBeNull();
});

test("test_ctrl_f_opens_pdf_find_bar_and_highlights_matches", async () => {
  const originalOpen = pdfDocumentLoader.open;
  pdfDocumentLoader.open = () => ({
    promise: Promise.resolve({
      numPages: 1,
      destroy() {},
      getPage: async () => ({
        getViewport() {
          return {
            width: 612,
            height: 792,
            convertToViewportRectangle: ([x1, y1, x2, y2]: [number, number, number, number]) => [
              x1,
              792 - y2,
              x2,
              792 - y1,
            ],
          };
        },
        render() {
          return { promise: Promise.resolve(), cancel() {} };
        },
        getTextContent: async () => ({
          items: [
            { str: "Theorem one and theorem two", transform: [1, 0, 0, 1, 50, 700], width: 200, height: 12 },
          ],
        }),
      }),
    }),
  });

  try {
    const { container } = render(
      <PdfPreview
        projectId="manuscript-2026"
        status="success"
        pdfUrl="/api/projects/manuscript-2026/pdf"
      />
    );

    await waitFor(() => {
      expect(screen.getByTestId("pdf-statistics").textContent).toContain("5 words");
    });

    const pane = container.querySelector(".lemma-pdf-preview-pane") as HTMLElement;
    fireEvent.keyDown(pane, { key: "f", ctrlKey: true });

    const input = screen.getByRole("textbox", { name: "Find in PDF" });
    fireEvent.change(input, { target: { value: "theorem" } });

    expect(screen.getByTestId("find-match-count").textContent).toBe("1 of 2");
    expect(screen.getAllByTestId("pdf-search-highlight-active")).toHaveLength(1);
    expect(screen.getAllByTestId("pdf-search-highlight")).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "Next match" }));
    expect(screen.getByTestId("find-match-count").textContent).toBe("2 of 2");

    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByTestId("find-bar-pdf")).toBeNull();
  } finally {
    pdfDocumentLoader.open = originalOpen;
  }
});
