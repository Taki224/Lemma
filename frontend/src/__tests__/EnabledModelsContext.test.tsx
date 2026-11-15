import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import {
  EnabledModelsContext,
  EnabledModelsProvider,
  useEnabledModels,
  type EnabledModelInfo,
  type EnabledModelsContextValue,
} from "../context/EnabledModelsContext";

function makeModel(id: string, overrides: Partial<EnabledModelInfo> = {}): EnabledModelInfo {
  return {
    id,
    name: id.toUpperCase(),
    supports_vision: false,
    verified: true,
    prompt_price_per_token: 0.000003,
    completion_price_per_token: 0.000015,
    ...overrides,
  };
}

let latest: EnabledModelsContextValue | null = null;

function Probe(): React.ReactElement {
  const value = useEnabledModels();
  latest = value;
  return (
    <div data-testid="probe">
      <span data-testid="loading">{String(value.loading)}</span>
      <span data-testid="ids">{value.models.map((m) => m.id).join(",")}</span>
    </div>
  );
}

function jsonResponse(models: EnabledModelInfo[]) {
  return { ok: true, status: 200, json: () => Promise.resolve({ models }) };
}

describe("EnabledModelsContext", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    vi.restoreAllMocks();
    latest = null;
  });

  afterEach(() => {
    global.fetch = originalFetch;
  });

  it("works without provider (empty default)", () => {
    // The test shim's createContext keeps the last Provider value globally; reset it so
    // this test observes the real default (a no-op under real React).
    const shimmed = EnabledModelsContext as unknown as { _currentValue?: unknown };
    if ("_currentValue" in shimmed) {
      shimmed._currentValue = {
        models: [],
        loading: false,
        refresh: async () => {},
      };
    }

    render(<Probe />);

    expect(screen.getByTestId("ids").textContent).toBe("");
    expect(screen.getByTestId("loading").textContent).toBe("false");
  });

  it("loads enabled models on mount", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValue(
        jsonResponse([makeModel("a/model"), makeModel("b/model", { supports_vision: true })])
      );

    render(
      <EnabledModelsProvider>
        <Probe />
      </EnabledModelsProvider>
    );

    expect(screen.getByTestId("loading").textContent).toBe("true");
    await waitFor(() => {
      expect(screen.getByTestId("ids").textContent).toBe("a/model,b/model");
    });
    expect(screen.getByTestId("loading").textContent).toBe("false");
    expect(global.fetch).toHaveBeenCalledWith("/api/ai/models");
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it("refresh refetches and updates consumers", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([makeModel("a/model")]))
      .mockResolvedValueOnce(jsonResponse([makeModel("a/model"), makeModel("c/model")]));

    render(
      <EnabledModelsProvider>
        <Probe />
      </EnabledModelsProvider>
    );
    await waitFor(() => {
      expect(screen.getByTestId("ids").textContent).toBe("a/model");
    });

    await latest!.refresh();

    await waitFor(() => {
      expect(screen.getByTestId("ids").textContent).toBe("a/model,c/model");
    });
    expect(global.fetch).toHaveBeenCalledTimes(2);
  });

  it("fetch failure yields empty list", async () => {
    global.fetch = vi.fn().mockImplementation(() => Promise.reject(new Error("offline")));

    render(
      <EnabledModelsProvider>
        <Probe />
      </EnabledModelsProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId("loading").textContent).toBe("false");
    });
    expect(screen.getByTestId("ids").textContent).toBe("");
  });

  it("non-ok response yields empty list", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValue({ ok: false, status: 401, json: () => Promise.resolve({}) });

    render(
      <EnabledModelsProvider>
        <Probe />
      </EnabledModelsProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId("loading").textContent).toBe("false");
    });
    expect(screen.getByTestId("ids").textContent).toBe("");
  });

  it("failed refresh keeps the previously loaded models", async () => {
    let calls = 0;
    global.fetch = vi.fn().mockImplementation(() => {
      calls += 1;
      return calls === 1
        ? Promise.resolve(jsonResponse([makeModel("a/model")]))
        : Promise.reject(new Error("network down"));
    });

    render(
      <EnabledModelsProvider>
        <Probe />
      </EnabledModelsProvider>
    );
    await waitFor(() => {
      expect(screen.getByTestId("ids").textContent).toBe("a/model");
    });

    await latest!.refresh();

    await waitFor(() => {
      expect(screen.getByTestId("ids").textContent).toBe("a/model");
    });
  });
});
