import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { test, expect, vi } from "vitest";
import { GitHubConnectorModal } from "../components/GitHubConnectorModal";

test("links a repository folder and confirms a pull that would remove local files", async () => {
  const pulls: Array<Record<string, unknown>> = [];
  const mockFetch = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method || "GET";
    if (method === "GET" && String(url).endsWith("/github")) {
      return { ok: true, status: 200, json: async () => ({ linked: false, has_token: false }) };
    }
    if (method === "PUT") {
      const body = JSON.parse(String(init?.body || "{}"));
      expect(body.token).toBe("ghp_test_token_value");
      expect(body.subpath).toBe("paper");
      return {
        ok: true,
        status: 200,
        json: async () => ({
          linked: true,
          owner: "acme",
          repo: "manuscript",
          branch: "main",
          subpath: "paper",
          last_synced_sha: null,
          has_token: true,
        }),
      };
    }
    if (method === "POST" && String(url).endsWith("/pull")) {
      const body = JSON.parse(String(init?.body || "{}")) as Record<string, unknown>;
      pulls.push(body);
      if (!body.confirm_overwrite) {
        return {
          ok: false,
          status: 409,
          json: async () => ({
            detail: {
              code: "confirm_required",
              message: "Pull would remove local files that are not in the GitHub folder.",
              paths: ["main.tex"],
            },
          }),
        };
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({
          commit_sha: "a".repeat(40),
          added: ["chapter.tex"],
          updated: [],
          deleted: ["main.tex"],
          unchanged: false,
        }),
      };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  });

  render(
    <GitHubConnectorModal
      isOpen
      projectId="manuscript"
      csrfToken="csrf-test"
      fetchImpl={mockFetch as unknown as typeof fetch}
    />
  );

  await screen.findByText(/Local version history stays in this workspace/i);
  fireEvent.change(screen.getByLabelText("GitHub token"), { target: { value: "ghp_test_token_value" } });
  fireEvent.change(screen.getByLabelText("GitHub owner"), { target: { value: "acme" } });
  fireEvent.change(screen.getByLabelText("GitHub repository"), { target: { value: "manuscript" } });
  fireEvent.change(screen.getByLabelText("Folder in the repository"), { target: { value: "paper" } });
  fireEvent.click(screen.getByRole("button", { name: "Save link" }));

  await screen.findByRole("button", { name: "Pull from GitHub" });
  fireEvent.click(screen.getByRole("button", { name: "Pull from GitHub" }));
  await screen.findByText("main.tex");
  fireEvent.click(screen.getByRole("button", { name: "Replace local files and pull" }));
  await screen.findByText(/Pulled 1 added, 0 updated, 1 removed/i);
  expect(pulls).toEqual([{ confirm_overwrite: false }, { confirm_overwrite: true }]);
});

test("notifies onLinkChange when linking and unlinking a repository", async () => {
  const onLinkChange = vi.fn();
  const mockFetch = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method || "GET";
    if (method === "GET" && String(url).endsWith("/github")) {
      return { ok: true, status: 200, json: async () => ({ linked: false, has_token: true }) };
    }
    if (method === "PUT") {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          linked: true,
          owner: "acme",
          repo: "manuscript",
          branch: "main",
          subpath: "",
          last_synced_sha: null,
          has_token: true,
        }),
      };
    }
    if (method === "DELETE") {
      return {
        ok: true,
        status: 200,
        json: async () => ({ linked: false, has_token: true }),
      };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  });

  render(
    <GitHubConnectorModal
      isOpen
      projectId="manuscript"
      csrfToken="csrf-test"
      onLinkChange={onLinkChange}
      fetchImpl={mockFetch as unknown as typeof fetch}
    />
  );

  await waitFor(() => {
    expect(onLinkChange).toHaveBeenCalledWith(false);
  });

  fireEvent.change(screen.getByLabelText("GitHub owner"), { target: { value: "acme" } });
  fireEvent.change(screen.getByLabelText("GitHub repository"), { target: { value: "manuscript" } });
  fireEvent.click(screen.getByRole("button", { name: "Save link" }));

  await screen.findByRole("button", { name: "Unlink" });
  expect(onLinkChange).toHaveBeenCalledWith(true);

  fireEvent.click(screen.getByRole("button", { name: "Unlink" }));
  await screen.findByText(/GitHub folder unlinked/i);
  expect(onLinkChange).toHaveBeenCalledTimes(3);
});

test("shows connected status card and toggles edit settings when already linked", async () => {
  const mockFetch = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method || "GET";
    if (method === "GET" && String(url).endsWith("/github")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          linked: true,
          owner: "acme",
          repo: "manuscript",
          branch: "main",
          subpath: "draft",
          last_synced_sha: "277b5a3deadbeef",
          has_token: true,
          token_source: "saved",
        }),
      };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  });

  render(
    <GitHubConnectorModal
      isOpen
      projectId="manuscript"
      csrfToken="csrf-test"
      fetchImpl={mockFetch as unknown as typeof fetch}
    />
  );

  await screen.findByTestId("github-connected-card");
  expect(screen.getByText("277b5a3")).toBeInTheDocument();
  expect(screen.queryByLabelText("GitHub owner")).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Edit settings" }));
  await screen.findByLabelText("GitHub owner");
  expect(screen.getByRole("button", { name: "Save changes" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Remove token" })).toBeInTheDocument();
});

