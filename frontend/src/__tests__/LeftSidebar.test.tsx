import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { test, expect, vi } from "vitest";
import { LeftSidebar } from "../components/LeftSidebar";

test("test_file_tree_and_outline_click_triggers_file_and_line_navigation", () => {
  const onSelectProject = vi.fn();
  const onSelectFile = vi.fn();
  const onJumpToLine = vi.fn();

  const projects = [
    { project_id: "manuscript-2026", name: "Quantum Paper", root_tex: "main.tex" },
    { project_id: "paper-2026", name: "Conference Paper", root_tex: "paper.tex" },
  ];

  const files = [
    { path: "main.tex", name: "main.tex", is_dir: false, size_bytes: 512 },
    { path: "chapters/methods.tex", name: "methods.tex", is_dir: false, size_bytes: 1024 },
    { path: "references.bib", name: "references.bib", is_dir: false, size_bytes: 256 },
  ];

  const outline = [
    {
      title: "Introduction",
      level: "chapter" as const,
      file_path: "main.tex",
      line_number: 10,
      children: [
        {
          title: "Experimental Setup",
          level: "section" as const,
          file_path: "chapters/methods.tex",
          line_number: 24,
          children: [],
        },
      ],
    },
  ];

  render(
    <LeftSidebar
      projects={projects}
      activeProject={projects[0]}
      onSelectProject={onSelectProject}
      activeFile="main.tex"
      files={files}
      onSelectFile={onSelectFile}
      outline={outline}
      onJumpToLine={onJumpToLine}
    />
  );

  // Verify Files / Chats tablist exists and supports arrow key navigation
  const tablist = screen.getByRole("tablist");
  expect(tablist).toBeInTheDocument();

  // Clicking a file in the file tree selects that file
  const bibItem = screen.getByRole("button", { name: "references.bib" });
  fireEvent.click(bibItem);
  expect(onSelectFile).toHaveBeenCalledWith("references.bib");

  // Clicking a nested outline item in another chapter opens that file and jumps to line 24
  const outlineNode = screen.getByRole("button", { name: /Experimental Setup/i });
  fireEvent.click(outlineNode);
  expect(onSelectFile).toHaveBeenCalledWith("chapters/methods.tex");
  expect(onJumpToLine).toHaveBeenCalledWith("chapters/methods.tex", 24);
});

test("test_sync_repo_button_hidden_when_git_not_connected_and_shown_with_tooltip_when_connected", () => {
  const onSyncRepo = vi.fn();
  const project = { project_id: "manuscript-2026", name: "Quantum Paper", root_tex: "main.tex" };

  const { unmount } = render(
    <LeftSidebar
      activeProject={project}
      activeFile="main.tex"
      onSelectFile={vi.fn()}
      onJumpToLine={vi.fn()}
      githubLinked={false}
    />
  );
  expect(screen.queryByRole("button", { name: "Sync repo" })).toBeNull();
  unmount();

  render(
    <LeftSidebar
      activeProject={project}
      activeFile="main.tex"
      onSelectFile={vi.fn()}
      onJumpToLine={vi.fn()}
      githubLinked={true}
      onSyncRepo={onSyncRepo}
    />
  );

  const syncBtn = screen.getByRole("button", { name: "Sync repo" });
  expect(syncBtn).toBeInTheDocument();
  expect(syncBtn.getAttribute("title")).toBe("Sync repo");
  fireEvent.click(syncBtn);
  expect(onSyncRepo).toHaveBeenCalledTimes(1);
});

const sidebarProjects = [{ project_id: "manuscript-2026", name: "Quantum Paper", root_tex: "main.tex" }];

function renderSidebar(props: Partial<React.ComponentProps<typeof LeftSidebar>> = {}) {
  return render(
    <LeftSidebar
      projects={sidebarProjects}
      activeProject={sidebarProjects[0]}
      onSelectProject={vi.fn()}
      onCreateProject={vi.fn(async () => true)}
      activeFile="main.tex"
      onSelectFile={vi.fn()}
      onJumpToLine={vi.fn()}
      {...props}
    />
  );
}

test("test_no_inline_name_input_or_new_project_button", () => {
  renderSidebar();
  expect(screen.queryByLabelText("New Project Name")).toBeNull();
  // The fake renderer matches accessible names case-insensitively, so assert on visible text instead.
  expect(screen.queryByText("New Project")).toBeNull();
  expect(screen.getByRole("button", { name: "New project" })).toBeInTheDocument();
});

test("test_plus_shown_when_no_projects", () => {
  renderSidebar({ projects: [], activeProject: null });
  expect(screen.getByRole("button", { name: "New project" })).toBeInTheDocument();
});

test("test_plus_opens_dialog", () => {
  renderSidebar();
  expect(screen.queryByRole("dialog")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "New project" }));
  expect(screen.getByRole("dialog")).toBeInTheDocument();
});

test("test_submit_calls_onCreateProject_and_closes", async () => {
  const onCreateProject = vi.fn(async () => true);
  renderSidebar({ onCreateProject });
  fireEvent.click(screen.getByRole("button", { name: "New project" }));
  fireEvent.change(screen.getByLabelText("Project name"), { target: { value: "  My Paper " } });
  fireEvent.click(screen.getByRole("button", { name: /create/i }));
  expect(onCreateProject).toHaveBeenCalledWith("My Paper");
  await waitFor(() => {
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

test("test_failure_keeps_dialog_open_with_error", async () => {
  const onCreateProject = vi.fn(async () => false);
  renderSidebar({ onCreateProject, createProjectError: "Project already exists" });
  fireEvent.click(screen.getByRole("button", { name: "New project" }));
  fireEvent.change(screen.getByLabelText("Project name"), { target: { value: "Dup" } });
  fireEvent.click(screen.getByRole("button", { name: /create/i }));
  await waitFor(() => {
    expect(onCreateProject).toHaveBeenCalledWith("Dup");
    // Create re-enabled => the in-flight submit has settled.
    expect(screen.getByRole("button", { name: /create/i }).getAttribute("disabled")).toBe("false");
  });
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  expect(screen.getByText("Project already exists")).toBeInTheDocument();
});

test("test_new_project_open_and_cancel_notify_dismiss", () => {
  const onCreateProjectDismiss = vi.fn();
  renderSidebar({ onCreateProjectDismiss });
  fireEvent.click(screen.getByRole("button", { name: "New project" }));
  expect(onCreateProjectDismiss).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
  expect(onCreateProjectDismiss).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("test_chats_pin_rename_and_delete_actions", async () => {
  const onPinChat = vi.fn(async () => null);
  const onRenameChat = vi.fn(async () => null);
  const onDeleteChat = vi.fn(async () => null);
  render(
    <LeftSidebar
      activeProject={{ project_id: "p1", name: "Paper", root_tex: "main.tex" }}
      activeFile="main.tex"
      files={[{ path: "main.tex", name: "main.tex", is_dir: false, size_bytes: 10 }]}
      onSelectFile={vi.fn()}
      onJumpToLine={vi.fn()}
      chats={[
        { chat_id: "c1", title: "Draft Intro", updated_at: "2026-11-20T12:00:00Z", pinned: false },
        { chat_id: "c2", title: "Pinned Chat", updated_at: "2026-11-20T11:00:00Z", pinned: true },
      ]}
      activeChatId="c1"
      onSelectChat={vi.fn()}
      onPinChat={onPinChat}
      onRenameChat={onRenameChat}
      onDeleteChat={onDeleteChat}
    />
  );

  fireEvent.click(screen.getByRole("tab", { name: "Chats" }));

  // Pin unpinned chat
  fireEvent.click(screen.getByRole("button", { name: "More actions for Draft Intro" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Pin" }));
  expect(onPinChat).toHaveBeenCalledWith("c1", true);

  // Unpin pinned chat
  fireEvent.click(screen.getByRole("button", { name: "More actions for Pinned Chat" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Unpin" }));
  expect(onPinChat).toHaveBeenCalledWith("c2", false);

  // Rename chat
  fireEvent.click(screen.getByRole("button", { name: "More actions for Draft Intro" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Rename" }));
  const renameInput = screen.getByLabelText("Rename chat Draft Intro");
  fireEvent.change(renameInput, { target: { value: "Final Intro" } });
  fireEvent.keyDown(renameInput, { key: "Enter" });
  await waitFor(() => {
    expect(onRenameChat).toHaveBeenCalledWith("c1", "Final Intro");
  });

  // Delete chat with confirmation modal
  fireEvent.click(screen.getByRole("button", { name: "More actions for Draft Intro" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Delete" }));
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));
  await waitFor(() => {
    expect(onDeleteChat).toHaveBeenCalledWith("c1");
  });
});
