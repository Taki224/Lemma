import React, { useEffect, useRef } from "react";

export interface PaneFindBarProps {
  paneLabel: string;
  query: string;
  onQueryChange: (value: string) => void;
  caseSensitive: boolean;
  onToggleCaseSensitive: () => void;
  matchCount: number;
  activeIndex: number;
  onNext: () => void;
  onPrev: () => void;
  onClose: () => void;
  focusNonce: number;
  placeholder?: string;
}

export function PaneFindBar({
  paneLabel,
  query,
  onQueryChange,
  caseSensitive,
  onToggleCaseSensitive,
  matchCount,
  activeIndex,
  onNext,
  onPrev,
  onClose,
  focusNonce,
  placeholder = "Find...",
}: PaneFindBarProps): React.ReactElement {
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    const input = inputRef.current;
    if (!input) return;
    input.focus();
    input.select();
  }, [focusNonce]);

  const statusText =
    query.length === 0
      ? ""
      : matchCount === 0
      ? "No results"
      : `${activeIndex + 1} of ${matchCount}`;

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      onClose();
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      e.stopPropagation();
      if (e.shiftKey) {
        onPrev();
      } else {
        onNext();
      }
    }
  };

  return (
    <div
      className="lemma-pane-find-bar"
      role="search"
      aria-label={`Find in ${paneLabel}`}
      data-testid={`find-bar-${paneLabel.toLowerCase().replace(/\s+/g, "-")}`}
    >
      <input
        ref={inputRef}
        type="text"
        className="lemma-pane-find-input"
        aria-label={`Find in ${paneLabel}`}
        placeholder={placeholder}
        value={query}
        onChange={(e) => onQueryChange(e.target.value)}
        onKeyDown={handleKeyDown}
      />
      {statusText && (
        <span className="lemma-pane-find-count" aria-live="polite" data-testid="find-match-count">
          {statusText}
        </span>
      )}
      <button
        type="button"
        className={`lemma-pane-find-btn${caseSensitive ? " is-active" : ""}`}
        aria-label="Match case"
        aria-pressed={caseSensitive}
        title="Match case"
        onClick={onToggleCaseSensitive}
      >
        Aa
      </button>
      <button
        type="button"
        className="lemma-pane-find-btn"
        aria-label="Previous match"
        title="Previous match (Shift+Enter)"
        disabled={matchCount === 0}
        onClick={onPrev}
      >
        ↑
      </button>
      <button
        type="button"
        className="lemma-pane-find-btn"
        aria-label="Next match"
        title="Next match (Enter)"
        disabled={matchCount === 0}
        onClick={onNext}
      >
        ↓
      </button>
      <button
        type="button"
        className="lemma-pane-find-btn"
        aria-label="Close find"
        title="Close (Escape)"
        onClick={onClose}
      >
        ×
      </button>
    </div>
  );
}
