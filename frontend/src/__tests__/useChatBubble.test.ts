import React from "react";
import { describe, it, expect } from "vitest";
import { render, cleanup } from "@testing-library/react";
import { useChatBubble, nearestCorner } from "../hooks/useChatBubble";

const KEY = "lemma_chat_bubble_v1";

type Bubble = ReturnType<typeof useChatBubble>;
let latest: Bubble | null = null;

function Probe(): React.ReactElement {
  latest = useChatBubble();
  return React.createElement("div", null);
}

function mount(): void {
  latest = null;
  render(React.createElement(Probe));
}

describe("useChatBubble", () => {
  it("test_defaults", () => {
    mount();
    expect(latest!.minimised).toBe(false);
    expect(latest!.corner).toBe("br");
  });

  it("test_persists_minimised_and_corner", () => {
    mount();
    latest!.setMinimised(true);
    latest!.setCorner("tl");
    expect(latest!.minimised).toBe(true);
    expect(latest!.corner).toBe("tl");
    expect(JSON.parse(localStorage.getItem(KEY)!)).toEqual({ minimised: true, corner: "tl" });

    cleanup();
    mount();
    expect(latest!.minimised).toBe(true);
    expect(latest!.corner).toBe("tl");
  });

  it("test_corrupt_storage_falls_back", () => {
    for (const bad of ["{not json", "null", '{"minimised":"yes","corner":"br"}', '{"minimised":true,"corner":"zz"}']) {
      localStorage.setItem(KEY, bad);
      cleanup();
      mount();
      expect(latest!.minimised).toBe(false);
      expect(latest!.corner).toBe("br");
    }
  });

  it("test_nearestCorner_for_each_quadrant", () => {
    const bounds = { width: 1000, height: 800 };
    expect(nearestCorner({ x: 100, y: 100 }, bounds)).toBe("tl");
    expect(nearestCorner({ x: 900, y: 100 }, bounds)).toBe("tr");
    expect(nearestCorner({ x: 100, y: 700 }, bounds)).toBe("bl");
    expect(nearestCorner({ x: 900, y: 700 }, bounds)).toBe("br");
  });
});
