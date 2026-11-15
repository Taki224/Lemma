import { describe, it, expect } from "vitest";
import {
  describeSnapshot,
  groupByDay,
  type SnapshotItem,
} from "../utils/snapshotLabels";

function snap(
  sha: string,
  timestamp: string,
  trigger: SnapshotItem["trigger"] = "manual",
  message = "Snapshot",
): SnapshotItem {
  return { commit_sha: sha, timestamp, trigger, message };
}

// Local-time constructors keep tests timezone independent.
const iso = (y: number, mo: number, d: number, h = 12) =>
  new Date(y, mo - 1, d, h, 0, 0).toISOString();

describe("describeSnapshot", () => {
  it("test_label_per_trigger", () => {
    const labels = (
      ["compile_success", "ai_accept", "pre_restore", "manual"] as const
    ).map((t) => describeSnapshot(snap("a", iso(2026, 1, 1), t)).label);
    expect(labels).toEqual([
      "Compiled",
      "AI edit accepted",
      "Backup before restore",
      "Saved manually",
    ]);
  });

  it("test_detail_strips_generic_prefix", () => {
    expect(describeSnapshot(snap("a", iso(2026, 1, 1), "manual", "Snapshot")).detail).toBe("");
    expect(
      describeSnapshot(snap("a", iso(2026, 1, 1), "manual", "Snapshot: intro rewrite")).detail,
    ).toBe("intro rewrite");
    expect(
      describeSnapshot(snap("a", iso(2026, 1, 1), "ai_accept", "Fix abstract")).detail,
    ).toBe("Fix abstract");
    expect(describeSnapshot(snap("a", iso(2026, 1, 1), "manual", "Saved manually")).detail).toBe("");
  });
});

describe("groupByDay", () => {
  const now = new Date(2026, 5, 15, 15, 0, 0);

  it("test_group_today_yesterday_other", () => {
    const groups = groupByDay(
      [
        snap("t", iso(2026, 6, 15, 9)),
        snap("y", iso(2026, 6, 14, 23)),
        snap("o", iso(2026, 6, 1, 10)),
      ],
      now,
    );
    expect(groups.map((g) => g.dayLabel)).toEqual([
      "Today",
      "Yesterday",
      new Date(2026, 5, 1, 10).toLocaleDateString(),
    ]);
    expect(groups.map((g) => g.items.map((i) => i.commit_sha))).toEqual([["t"], ["y"], ["o"]]);
  });

  it("test_group_order_newest_first", () => {
    const groups = groupByDay(
      [
        snap("old", iso(2026, 6, 14, 8)),
        snap("new1", iso(2026, 6, 15, 8)),
        snap("new2", iso(2026, 6, 15, 11)),
      ],
      now,
    );
    expect(groups.map((g) => g.dayLabel)).toEqual(["Today", "Yesterday"]);
    expect(groups[0].items.map((i) => i.commit_sha)).toEqual(["new2", "new1"]);
  });

  it("test_invalid_timestamp_goes_to_unknown", () => {
    const groups = groupByDay(
      [snap("bad", "not-a-date"), snap("ok", iso(2026, 6, 15, 8))],
      now,
    );
    expect(groups.map((g) => g.dayLabel)).toEqual(["Today", "Unknown date"]);
    expect(groups[1].items.map((i) => i.commit_sha)).toEqual(["bad"]);
  });
});

describe("describeSnapshot prefix stripping", () => {
  const detail = (message: string) => describeSnapshot(snap("a", iso(2026, 1, 1), "manual", message)).detail;

  it("test_only_exact_snapshot_prefix_is_stripped", () => {
    expect(detail("Snapshot of references")).toBe("Snapshot of references");
    expect(detail("Snapshots archived")).toBe("Snapshots archived");
    expect(detail("Snapshot:")).toBe("");
    expect(detail("Snapshot: intro")).toBe("intro");
    expect(detail("snapshot - intro")).toBe("intro");
    expect(detail("Snapshot")).toBe("");
  });
});
