import React, { useEffect, useRef, useState } from "react";
import { nearestCorner, type Corner } from "../hooks/useChatBubble";

const DRAG_THRESHOLD_PX = 4;

interface Size {
  width: number;
  height: number;
}

interface ChatBubbleProps {
  /** Element whose rectangle bounds the bubble; defaults to the bubble's parent (the chat layer). */
  containerRef?: React.RefObject<Pick<HTMLElement, "getBoundingClientRect"> | null>;
  corner: Corner;
  pendingCount: number;
  onExpand: () => void;
  onCornerChange: (corner: Corner) => void;
}

// Keep in sync with `.lemma-chat-bubble` (width/height) and `.lemma-chat-bubble-{tl,tr,bl,br}` (corner inset) in index.css.
const BUBBLE_SIZE_PX = 48;
const BUBBLE_MARGIN_PX = 16;

/** Container-local top-left of the bubble when anchored at `corner` (mirrors the `.lemma-chat-bubble-*` CSS). */
function cornerOrigin(corner: Corner, size: Size): { x: number; y: number } {
  const far = BUBBLE_MARGIN_PX;
  return {
    x: corner[1] === "l" ? far : size.width - far - BUBBLE_SIZE_PX,
    y: corner[0] === "t" ? far : size.height - far - BUBBLE_SIZE_PX,
  };
}

function clampAxis(value: number, extent: number): number {
  const min = BUBBLE_MARGIN_PX;
  const max = Math.max(min, extent - BUBBLE_MARGIN_PX - BUBBLE_SIZE_PX);
  return Math.min(max, Math.max(min, value));
}

export function ChatBubble({ containerRef, corner, pendingCount, onExpand, onCornerChange }: ChatBubbleProps) {
  const startRef = useRef<{ x: number; y: number } | null>(null);
  const movedRef = useRef(false);
  // A click event follows pointerup; the pointer path already handled it, so swallow that one.
  const suppressClickRef = useRef(false);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const rawDeltaRef = useRef<{ dx: number; dy: number } | null>(null);
  const [offset, setOffset] = useState<{ dx: number; dy: number } | null>(null);

  // The layer the bubble lives in (the editor column), never the window.
  const getContainer = (): { left: number; top: number; width: number; height: number } => {
    const el = containerRef?.current ?? buttonRef.current?.parentElement ?? null;
    if (el && typeof el.getBoundingClientRect === "function") return el.getBoundingClientRect();
    return { left: 0, top: 0, width: window.innerWidth, height: window.innerHeight };
  };

  // Keep the dragged bubble inside the container: express the pointer delta as a clamped offset.
  const clampedDelta = (dx: number, dy: number) => {
    const rect = getContainer();
    const origin = cornerOrigin(corner, rect);
    return {
      dx: clampAxis(origin.x + dx, rect.width) - origin.x,
      dy: clampAxis(origin.y + dy, rect.height) - origin.y,
    };
  };

  // The observer outlives renders; it reads the latest clamp (corner may change) through this ref.
  const clampedDeltaRef = useRef(clampedDelta);
  clampedDeltaRef.current = clampedDelta;

  // Layer resized mid-drag: re-clamp. (At rest the CSS corner anchor already follows the layer.)
  useEffect(() => {
    const el = containerRef?.current ?? null;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      const raw = rawDeltaRef.current;
      if (raw) setOffset(clampedDeltaRef.current(raw.dx, raw.dy));
    });
    observer.observe(el as Element);
    return () => observer.disconnect();
  }, [containerRef]);

  const handlePointerDown = (e: React.PointerEvent<HTMLButtonElement>) => {
    if (e.button > 0) return;
    e.currentTarget.setPointerCapture?.(e.pointerId);
    startRef.current = { x: e.clientX, y: e.clientY };
    movedRef.current = false;
    rawDeltaRef.current = null;
  };

  const handlePointerMove = (e: React.PointerEvent<HTMLButtonElement>) => {
    const start = startRef.current;
    if (!start) return;
    const dx = e.clientX - start.x;
    const dy = e.clientY - start.y;
    if (!movedRef.current && Math.hypot(dx, dy) < DRAG_THRESHOLD_PX) return;
    movedRef.current = true;
    rawDeltaRef.current = { dx, dy };
    setOffset(clampedDelta(dx, dy));
  };

  // Let the bubble glide from the drop point to its new corner: re-express the drop point as an
  // offset from the new anchor, hold it for one frame (no transition), then clear it.
  const settleFrom = (dx: number, dy: number, next: Corner) => {
    const rect = getContainer();
    const from = cornerOrigin(corner, rect);
    const to = cornerOrigin(next, rect);
    if (typeof requestAnimationFrame === "undefined") {
      setOffset(null);
      return;
    }
    setOffset({ dx: dx + from.x - to.x, dy: dy + from.y - to.y });
    requestAnimationFrame(() => {
      void buttonRef.current?.offsetWidth; // flush style so the next change transitions
      setOffset(null);
    });
  };

  const handlePointerUp = (e: React.PointerEvent<HTMLButtonElement>) => {
    const start = startRef.current;
    if (!start) return;
    startRef.current = null;
    e.currentTarget.releasePointerCapture?.(e.pointerId);
    suppressClickRef.current = true;
    setTimeout(() => {
      suppressClickRef.current = false;
    }, 0);
    if (movedRef.current) {
      const rect = getContainer();
      const origin = cornerOrigin(corner, rect);
      const d = clampedDelta(e.clientX - start.x, e.clientY - start.y);
      // Snap by the bubble's centre, in container-local coordinates.
      const next = nearestCorner(
        { x: origin.x + d.dx + BUBBLE_SIZE_PX / 2, y: origin.y + d.dy + BUBBLE_SIZE_PX / 2 },
        rect,
      );
      rawDeltaRef.current = null;
      settleFrom(d.dx, d.dy, next);
      onCornerChange(next);
    } else {
      setOffset(null);
      onExpand();
    }
  };

  const handlePointerCancel = (e: React.PointerEvent<HTMLButtonElement>) => {
    if (!startRef.current) return;
    startRef.current = null;
    rawDeltaRef.current = null;
    e.currentTarget.releasePointerCapture?.(e.pointerId);
    setOffset(null);
  };

  // Assistive tech and synthetic clicks have no pointer sequence; pointer-driven ones are swallowed.
  const handleClick = () => {
    if (suppressClickRef.current) {
      suppressClickRef.current = false;
      return;
    }
    onExpand();
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLButtonElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onExpand();
    }
  };

  const label =
    pendingCount > 0
      ? `Open AI assistant, ${pendingCount} suggestions waiting`
      : "Open AI assistant";

  const className = [
    "lemma-chat-bubble",
    `lemma-chat-bubble-${corner}`,
    offset ? "lemma-chat-bubble-dragging" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <button
      type="button"
      className={className}
      aria-label={label}
      ref={buttonRef}
      // Dynamic drag offset is passed as CSS custom properties; the class applies the transform.
      style={
        offset
          ? ({ "--bubble-dx": `${offset.dx}px`, "--bubble-dy": `${offset.dy}px` } as React.CSSProperties)
          : undefined
      }
      onClick={handleClick}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerCancel={handlePointerCancel}
      onKeyDown={handleKeyDown}
    >
      <span aria-hidden="true">Ask</span>
      {pendingCount > 0 && <span className="lemma-chat-bubble-badge">{pendingCount}</span>}
    </button>
  );
}
