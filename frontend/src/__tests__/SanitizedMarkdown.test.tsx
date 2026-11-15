import React from "react";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { test, expect, afterEach } from "vitest";
import { SanitizedMarkdown } from "../components/SanitizedMarkdown";

afterEach(() => {
  cleanup();
});

test("renders headings, horizontal rules, nested lists, and multi-backtick inline code", () => {
  const md = [
    "---",
    "",
    "### 1. Operational Phase States (The Phase Tracker)",
    "",
    "The output is classified into three states:",
    "",
    "1. **Reasoning State:**",
    " * **Definition:** Unconstrained internal monologue.",
    " * **Delimiters:** Explicit tags, such as `\\thinkopen` (`<think>`).",
    "",
    "2. **Structured-Action State:**",
    " * **Delimiters:** Code block fences (e.g., `` ``` ``).",
    "",
    "#### State Invariants and Transition Rules",
    "* **Initialisation:** Seeded from metadata.",
  ].join("\n");

  const { container } = render(<SanitizedMarkdown content={md} />);

  expect(container.querySelector("hr")).not.toBeNull();
  const h3 = container.querySelector("h3");
  expect(h3?.textContent).toBe("1. Operational Phase States (The Phase Tracker)");
  const h4 = container.querySelector("h4");
  expect(h4?.textContent).toBe("State Invariants and Transition Rules");

  const orderedLists = container.querySelectorAll("ol");
  expect(orderedLists.length).toBe(1);
  expect(orderedLists[0].querySelectorAll(":scope > li").length).toBe(2);

  const nestedUls = orderedLists[0].querySelectorAll("ul");
  expect(nestedUls.length).toBe(2);
  expect(nestedUls[0].querySelectorAll("li").length).toBe(2);

  const codePills = Array.from(container.querySelectorAll("code.lemma-inline-code")).map(
    (el) => el.textContent
  );
  expect(codePills).toContain("\\thinkopen");
  expect(codePills).toContain("<think>");
  expect(codePills).toContain("```");
  expect(container.textContent).not.toContain("### 1.");
  expect(container.textContent).not.toContain("#### State");
});

test("renders blockquotes, tables, fenced code blocks, and strikethrough", () => {
  const md = [
    "> Quoted **note** with ~~old~~ text",
    "",
    "| Metric | Value |",
    "| :--- | ---: |",
    "| MAE | $0.42$ |",
    "",
    "```python",
    "print(1 + 1)",
    "```",
  ].join("\n");

  const { container } = render(<SanitizedMarkdown content={md} />);

  expect(container.querySelector("blockquote strong")?.textContent).toBe("note");
  expect(container.querySelector("blockquote del")?.textContent).toBe("old");

  const table = container.querySelector("table");
  expect(table).not.toBeNull();
  expect(table?.querySelectorAll("th").length).toBe(2);
  expect(table?.querySelector("td .katex-html")).not.toBeNull();

  const pre = container.querySelector("pre.lemma-chat-code code");
  expect(pre?.textContent).toBe("print(1 + 1)");
});

test("preserves asterisks inside inline math and keeps underscores literal", () => {
  const md = "Compute $a * b * c$ and cite `smith_2024_energy` or smith_2024_energy.";
  const { container } = render(<SanitizedMarkdown content={md} />);

  expect(container.querySelectorAll(".katex-html").length).toBe(1);
  expect(container.querySelector("em")).toBeNull();
  expect(container.textContent).toContain("smith_2024_energy");
});

test("blocks unsafe html/images and renders external links as confirmation buttons inside lists", () => {
  const md = [
    "* Read [the paper](https://example.com/paper) for details.",
    '<script>alert(1)</script><img src="https://evil.example/a.png">',
    "![pixel](https://evil.example/pixel.png)",
  ].join("\n");

  const { container } = render(<SanitizedMarkdown content={md} />);

  expect(container.querySelectorAll("script, iframe, img").length).toBe(0);
  expect(container.textContent).toContain("[Remote image blocked: pixel]");
  const btn = screen.getByRole("button", { name: "External URL https://example.com/paper" });
  expect(btn.closest("li")).not.toBeNull();

  fireEvent.click(btn);
  expect(screen.getByRole("dialog", { name: "Confirm opening external URL" })).toBeTruthy();
});
