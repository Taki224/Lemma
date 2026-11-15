import React, { useRef, useState } from "react";

const KEYBOARD_STEP_PX = 16;

interface ResizeHandleProps {
  /** May return the delta actually applied (after clamping); the drag origin then advances by that amount only. */
  onDrag: (deltaPx: number) => number | void;
  onToggleCollapse: () => void;
  ariaLabel: string;
  disabled?: boolean;
  /** Optional current/min/max pane width in px, exposed as aria-valuenow/min/max. */
  width?: { now: number; min: number; max: number };
  /** "horizontal" is a top/bottom edge handle: delta is the pointer Y change (positive = down). Default "vertical". */
  orientation?: "vertical" | "horizontal";
  /** Same as `width`, for horizontal handles (panel height in px). */
  height?: { now: number; min: number; max: number };
}

export function ResizeHandle({ onDrag, onToggleCollapse, ariaLabel, disabled = false, width, orientation = "vertical", height }: ResizeHandleProps) {
  const horizontal = orientation === "horizontal";
  const aria = width ?? height;
  const lastPointerRef = useRef<number | null>(null);
  const [dragging, setDragging] = useState(false);

  const endDrag = (e: React.PointerEvent<HTMLDivElement>) => {
    if (lastPointerRef.current === null) return;
    lastPointerRef.current = null;
    e.currentTarget.releasePointerCapture?.(e.pointerId);
    setDragging(false);
  };

  const handlePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (disabled || e.button > 0) return;
    e.currentTarget.setPointerCapture?.(e.pointerId);
    lastPointerRef.current = horizontal ? e.clientY : e.clientX;
    setDragging(true);
  };

  const handlePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (disabled || lastPointerRef.current === null) return;
    // The button was released where the handle never saw a pointerup (capture lost).
    if (e.buttons === 0) {
      endDrag(e);
      return;
    }
    const delta = (horizontal ? e.clientY : e.clientX) - lastPointerRef.current;
    if (delta === 0) return;
    const applied = onDrag(delta);
    // Advance only by what was applied so a clamped drag does not drift from the pointer.
    lastPointerRef.current += typeof applied === "number" ? applied : delta;
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (disabled) return;
    const [dec, inc] = horizontal ? ["ArrowUp", "ArrowDown"] : ["ArrowLeft", "ArrowRight"];
    if (e.key === dec) {
      e.preventDefault();
      onDrag(-KEYBOARD_STEP_PX);
    } else if (e.key === inc) {
      e.preventDefault();
      onDrag(KEYBOARD_STEP_PX);
    } else if (horizontal && (e.key === "Home" || e.key === "Enter")) {
      // Keyboard equivalent of the double-click reset.
      e.preventDefault();
      onToggleCollapse();
    }
  };

  const handleDoubleClick = () => {
    if (disabled) return;
    onToggleCollapse();
  };

  const className = [
    "lemma-resize-handle",
    horizontal ? "lemma-resize-handle-horizontal" : "",
    dragging ? "lemma-resize-handle-active" : "",
    disabled ? "lemma-resize-handle-disabled" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      role="separator"
      aria-orientation={orientation}
      aria-label={ariaLabel}
      aria-valuenow={aria?.now}
      aria-valuemin={aria?.min}
      aria-valuemax={aria?.max}
      aria-disabled={disabled ? "true" : undefined}
      tabIndex={disabled ? -1 : 0}
      className={className}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onLostPointerCapture={endDrag}
      onKeyDown={handleKeyDown}
      onDoubleClick={handleDoubleClick}
    />
  );
}
