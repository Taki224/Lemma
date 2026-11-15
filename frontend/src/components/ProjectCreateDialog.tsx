import React, { useEffect, useRef, useState } from "react";

export interface ProjectCreateDialogProps {
  isOpen: boolean;
  error?: string | null;
  onSubmit: (name: string) => void | Promise<unknown>;
  onCancel: () => void;
}

export function ProjectCreateDialog({ isOpen, error, onSubmit, onCancel }: ProjectCreateDialogProps) {
  const [name, setName] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const createRef = useRef<HTMLButtonElement>(null);

  const [submitting, setSubmitting] = useState(false);
  const submittingRef = useRef(false);
  const openerRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (isOpen) {
      setName("");
      const active = typeof document !== "undefined" ? (document.activeElement as HTMLElement | null) : null;
      openerRef.current = active && typeof active.focus === "function" ? active : null;
      return;
    }
    setName("");
    const opener = openerRef.current;
    openerRef.current = null;
    if (opener && typeof opener.focus === "function") opener.focus();
  }, [isOpen]);

  if (!isOpen) return null;

  const trimmed = name.trim();
  const canSubmit = trimmed.length > 0 && !submitting;

  const submit = async () => {
    if (!canSubmit || submittingRef.current) return;
    submittingRef.current = true;
    setSubmitting(true);
    try {
      await onSubmit(trimmed);
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  };

  const handleKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onCancel();
      return;
    }
    if (event.key === "Enter" && event.target === inputRef.current) {
      event.preventDefault();
      submit();
      return;
    }
    if (event.key === "Tab") {
      // Tab order: input -> Cancel -> Create (only when enabled).
      const order = [inputRef.current, cancelRef.current, canSubmit ? createRef.current : null].filter(
        (el): el is HTMLInputElement | HTMLButtonElement => el !== null,
      );
      const first = order[0];
      const last = order[order.length - 1];
      if (event.shiftKey && event.target === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && event.target === last) {
        event.preventDefault();
        first.focus();
      }
    }
  };

  return (
    <div className="lemma-modal-backdrop">
      <div
        className="lemma-modal-dialog lemma-project-create-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="project-create-title"
        onKeyDown={handleKeyDown}
      >
        <h2 id="project-create-title">New project</h2>
        <div className="lemma-project-create-field">
          <label htmlFor="project-create-name">Project name</label>
          <input
            id="project-create-name"
            ref={inputRef}
            type="text"
            className="lemma-project-create-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoComplete="off"
            autoFocus
          />
        </div>
        {error ? (
          <div className="lemma-error-banner lemma-project-create-error" role="alert">
            {error}
          </div>
        ) : null}
        <div className="lemma-modal-actions">
          <button ref={cancelRef} type="button" className="lemma-btn lemma-btn-secondary" onClick={onCancel}>
            Cancel
          </button>
          <button
            ref={createRef}
            type="button"
            className="lemma-btn lemma-btn-primary"
            disabled={!canSubmit}
            onClick={submit}
          >
            Create
          </button>
        </div>
      </div>
    </div>
  );
}
