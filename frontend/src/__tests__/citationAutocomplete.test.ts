import { EditorState } from "@codemirror/state";
import { CompletionContext } from "@codemirror/autocomplete";
import { describe, expect, test } from "vitest";
import { formatBibDetail, parseBibFile, type BibEntry } from "../utils/bibParser";
import {
  createCitationCompletionSource,
  matchCitationContext,
} from "../components/citationAutocomplete";

describe("parseBibFile", () => {
  test("parses standard BibTeX and BibLaTeX entries with nested braces and quotes", () => {
    const bib = `
% Leading comment
@preamble{"ignored"}
@string{neurips = "NeurIPS"}

@inproceedings{vaswani2017,
  author = {Vaswani, Ashish and Shazeer, Noam and Parmar, Niki},
  title = {{Attention} Is All You Need},
  year = {2017},
}

@article{einstein1905,
  author = "Albert Einstein",
  title = "Zur Elektrodynamik bewegter K{\\"o}rper",
  date = {1905-06-30}
}

@book{knuth1984,
  author = {Donald E. Knuth},
  title = {The TeXbook},
  year = 1984
}
`;
    const entries = parseBibFile(bib);
    expect(entries).toHaveLength(3);
    expect(entries[0]).toEqual({
      key: "vaswani2017",
      entryType: "inproceedings",
      title: "Attention Is All You Need",
      author: "Vaswani, Ashish and Shazeer, Noam and Parmar, Niki",
      year: "2017",
    });
    expect(entries[1].key).toBe("einstein1905");
    expect(entries[1].year).toBe("1905");
    expect(entries[2].key).toBe("knuth1984");
    expect(entries[2].year).toBe("1984");
  });

  test("formats compact author/year/title details", () => {
    expect(
      formatBibDetail({
        key: "vaswani2017",
        entryType: "inproceedings",
        author: "Vaswani, Ashish and Shazeer, Noam and Parmar, Niki",
        title: "Attention Is All You Need",
        year: "2017",
      })
    ).toBe("Vaswani (2017) — Attention Is All You Need".replace("Vaswani", "Vaswani et al."));

    expect(
      formatBibDetail({
        key: "two2020",
        entryType: "article",
        author: "Alice Smith and Bob Jones",
        title: "Dual Methods",
        year: "2020",
      })
    ).toBe("Smith & Jones (2020) — Dual Methods");
  });
});

describe("matchCitationContext", () => {
  test("matches \\cite{ and variants with optional arguments", () => {
    expect(matchCitationContext("See \\cite{")).toEqual({
      query: "",
      fromOffset: 10,
      alreadyCited: new Set(),
    });
    expect(matchCitationContext("See \\citep[see][p.~4]{vas")).toEqual({
      query: "vas",
      fromOffset: 22,
      alreadyCited: new Set(),
    });
    expect(matchCitationContext("\\citet*{knuth")).toEqual({
      query: "knuth",
      fromOffset: 8,
      alreadyCited: new Set(),
    });
  });

  test("handles comma-separated multi-citations and excludes prior keys", () => {
    const res = matchCitationContext("As shown in \\cite{vaswani2017, ein");
    expect(res).not.toBeNull();
    expect(res!.query).toBe("ein");
    expect(res!.fromOffset).toBe(31);
    expect(res!.alreadyCited).toEqual(new Set(["vaswani2017"]));
  });

  test("ignores closed braces and commented-out lines", () => {
    expect(matchCitationContext("\\cite{vaswani2017} more text")).toBeNull();
    expect(matchCitationContext("% \\cite{vas")).toBeNull();
    expect(matchCitationContext("100\\% \\cite{vas")).not.toBeNull();
  });
});

describe("createCitationCompletionSource", () => {
  const sampleEntries: BibEntry[] = [
    {
      key: "vaswani2017",
      entryType: "inproceedings",
      author: "Vaswani, Ashish and Shazeer, Noam and Parmar, Niki",
      title: "Attention Is All You Need",
      year: "2017",
    },
    {
      key: "einstein1905",
      entryType: "article",
      author: "Albert Einstein",
      title: "Special Relativity",
      year: "1905",
    },
  ];

  function completeAtEnd(doc: string, disabled = false) {
    const state = EditorState.create({ doc });
    const context = new CompletionContext(state, doc.length, false);
    const source = createCitationCompletionSource(
      () => sampleEntries,
      () => disabled
    );
    return source(context);
  }

  test("returns all entries immediately on \\cite{", async () => {
    const result = await completeAtEnd("We build on \\cite{");
    expect(result).not.toBeNull();
    expect(result!.from).toBe("We build on \\cite{".length);
    expect(result!.options.map((o) => o.label)).toEqual([
      "vaswani2017",
      "einstein1905",
    ]);
    expect(result!.options[0].detail).toContain("Attention Is All You Need");
  });

  test("filters by paper title or author as well as key", async () => {
    const byTitle = await completeAtEnd("\\cite{relativity");
    expect(byTitle!.options.map((o) => o.label)).toEqual(["einstein1905"]);

    const byAuthor = await completeAtEnd("\\cite{vaswani");
    expect(byAuthor!.options.map((o) => o.label)).toEqual(["vaswani2017"]);
  });

  test("omits already-cited keys after a comma", async () => {
    const result = await completeAtEnd("\\cite{vaswani2017, ");
    expect(result!.options.map((o) => o.label)).toEqual(["einstein1905"]);
  });

  test("returns null when disabled", async () => {
    expect(await completeAtEnd("\\cite{", true)).toBeNull();
  });
});
