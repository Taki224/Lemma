import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { test, expect, vi } from "vitest";
import { ProjectCreateDialog } from "../components/ProjectCreateDialog";

// Note: the test renderer re-creates DOM nodes on every state change, so
// elements are re-queried after interactions that update state.
function setup(props: Partial<React.ComponentProps<typeof ProjectCreateDialog>> = {}) {
  const onSubmit = vi.fn();
  const onCancel = vi.fn();
  const view = render(<ProjectCreateDialog isOpen onSubmit={onSubmit} onCancel={onCancel} {...props} />);
  return { onSubmit, onCancel, view };
}

const nameInput = () => screen.getByLabelText("Project name");
const createButton = () => screen.getByRole("button", { name: /create/i });

test("test_submit_trims_name", () => {
  const { onSubmit } = setup();
  fireEvent.change(nameInput(), { target: { value: "  My Paper  " } });
  fireEvent.click(createButton());
  expect(onSubmit).toHaveBeenCalledTimes(1);
  expect(onSubmit).toHaveBeenCalledWith("My Paper");
});

test("test_blank_name_disables_submit", () => {
  const { onSubmit } = setup();
  expect(createButton().getAttribute("disabled")).toBe("true");
  fireEvent.change(nameInput(), { target: { value: "   " } });
  expect(createButton().getAttribute("disabled")).toBe("true");
  expect(onSubmit).not.toHaveBeenCalled();
  fireEvent.change(nameInput(), { target: { value: "x" } });
  expect(createButton().getAttribute("disabled")).toBe("false");
});

test("test_enter_submits", () => {
  const { onSubmit } = setup();
  fireEvent.change(nameInput(), { target: { value: "Paper" } });
  fireEvent.keyDown(nameInput(), { key: "Enter" });
  expect(onSubmit).toHaveBeenCalledWith("Paper");
});

test("test_enter_with_blank_name_does_not_submit", () => {
  const { onSubmit } = setup();
  fireEvent.keyDown(nameInput(), { key: "Enter" });
  expect(onSubmit).not.toHaveBeenCalled();
});

test("test_escape_cancels", () => {
  const { onCancel } = setup();
  fireEvent.keyDown(nameInput(), { key: "Escape" });
  expect(onCancel).toHaveBeenCalledTimes(1);
});

test("test_renders_error", () => {
  setup({ error: "Project already exists" });
  const banner = screen.getByText("Project already exists");
  expect(banner.getAttribute("class")).toContain("lemma-error-banner");
});

test("test_closed_renders_nothing", () => {
  const view = render(<ProjectCreateDialog isOpen={false} onSubmit={vi.fn()} onCancel={vi.fn()} />);
  expect(view.container).toBeNull();
});

test("test_dialog_semantics_and_autofocus", () => {
  setup();
  const dialog = screen.getByRole("dialog");
  expect(dialog.getAttribute("aria-modal")).toBe("true");
  expect(dialog.getAttribute("aria-labelledby")).toBe("project-create-title");
  expect(nameInput().getAttribute("autoFocus")).toBe("true");
});

test("test_tab_focus_is_trapped_in_dialog", () => {
  setup();
  fireEvent.change(nameInput(), { target: { value: "x" } });

  const input = nameInput();
  const cancel = screen.getByRole("button", { name: /cancel/i });
  const create = createButton();
  const inputFocus = vi.fn();
  const createFocus = vi.fn();
  input.focus = inputFocus;
  create.focus = createFocus;

  // Tab on last control wraps to the first.
  fireEvent.keyDown(create, { key: "Tab" });
  expect(inputFocus).toHaveBeenCalledTimes(1);

  // Shift+Tab on first control wraps to the last.
  fireEvent.keyDown(input, { key: "Tab", shiftKey: true });
  expect(createFocus).toHaveBeenCalledTimes(1);

  // Tab from a middle control is left to the browser.
  fireEvent.keyDown(cancel, { key: "Tab" });
  expect(inputFocus).toHaveBeenCalledTimes(1);
  expect(createFocus).toHaveBeenCalledTimes(1);
});

test("test_cancel_click_calls_onCancel", () => {
  const { onCancel } = setup();
  fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
  expect(onCancel).toHaveBeenCalledTimes(1);
});

test("test_name_resets_on_reopen", () => {
  const onSubmit = vi.fn();
  const view = render(<ProjectCreateDialog isOpen onSubmit={onSubmit} onCancel={vi.fn()} />);
  fireEvent.change(nameInput(), { target: { value: "Draft" } });
  view.rerender(<ProjectCreateDialog isOpen={false} onSubmit={onSubmit} onCancel={vi.fn()} />);
  view.rerender(<ProjectCreateDialog isOpen onSubmit={onSubmit} onCancel={vi.fn()} />);
  expect(nameInput().getAttribute("value") ?? (nameInput() as any).value).toBe("");
  expect(createButton().getAttribute("disabled")).toBe("true");
});

test("test_double_submit_is_ignored_until_promise_settles", async () => {
  let resolveSubmit: (v: boolean) => void = () => {};
  const onSubmit = vi.fn(
    () =>
      new Promise<boolean>((resolve) => {
        resolveSubmit = resolve;
      }),
  );
  setup({ onSubmit });
  fireEvent.change(nameInput(), { target: { value: "Paper" } });
  fireEvent.click(createButton());
  fireEvent.click(createButton());
  fireEvent.keyDown(nameInput(), { key: "Enter" });
  expect(onSubmit).toHaveBeenCalledTimes(1);
  expect(createButton().getAttribute("disabled")).toBe("true");

  resolveSubmit(false);
  await waitFor(() => {
    expect(createButton().getAttribute("disabled")).toBe("false");
  });
  fireEvent.click(createButton());
  expect(onSubmit).toHaveBeenCalledTimes(2);
});

test("test_focus_returns_to_opener_on_close", () => {
  const opener = { focus: vi.fn() };
  const g = globalThis as any;
  const hadDocument = "document" in g;
  const previous = g.document;
  g.document = { activeElement: opener };
  try {
    const onSubmit = vi.fn();
    const view = render(<ProjectCreateDialog isOpen onSubmit={onSubmit} onCancel={vi.fn()} />);
    expect(opener.focus).not.toHaveBeenCalled();
    view.rerender(<ProjectCreateDialog isOpen={false} onSubmit={onSubmit} onCancel={vi.fn()} />);
    expect(opener.focus).toHaveBeenCalledTimes(1);
  } finally {
    if (hadDocument) g.document = previous;
    else delete g.document;
  }
});
