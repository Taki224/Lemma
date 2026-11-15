export interface SnapshotItem {
  commit_sha: string;
  timestamp: string;
  trigger: "compile_success" | "ai_accept" | "pre_restore" | "manual";
  message: string;
}

export interface RestorePreviewData {
  commit_sha: string;
  changed_files: string[];
  unified_diff: string;
  lost_bib_keys: string[];
  has_bib_loss_warning: boolean;
}

const TRIGGER_LABELS: Record<SnapshotItem["trigger"], string> = {
  compile_success: "Compiled",
  ai_accept: "AI edit accepted",
  pre_restore: "Backup before restore",
  manual: "Saved manually",
};

// Only a bare "Snapshot" or "Snapshot:" / "Snapshot - " lead-in: "Snapshot of references" keeps its words.
const GENERIC_PREFIX_RE = /^snapshot(?::\s*|\s+[-–—]\s+|\s*$)/i;

export function describeSnapshot(s: SnapshotItem): { label: string; detail: string } {
  const label = TRIGGER_LABELS[s.trigger] ?? TRIGGER_LABELS.manual;
  const detail = (s.message ?? "").trim().replace(GENERIC_PREFIX_RE, "").trim();
  if (detail.toLowerCase() === label.toLowerCase()) {
    return { label, detail: "" };
  }
  return { label, detail };
}

const UNKNOWN_DAY = "Unknown date";

function dayKey(d: Date): string {
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

export function groupByDay(
  snapshots: SnapshotItem[],
  now: Date = new Date(),
): { dayLabel: string; items: SnapshotItem[] }[] {
  const today = dayKey(now);
  const yesterday = dayKey(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1));

  const valid: { item: SnapshotItem; date: Date }[] = [];
  const unknown: SnapshotItem[] = [];
  for (const item of snapshots) {
    const date = new Date(item.timestamp);
    if (Number.isNaN(date.getTime())) unknown.push(item);
    else valid.push({ item, date });
  }
  valid.sort((a, b) => b.date.getTime() - a.date.getTime());

  const groups: { dayLabel: string; items: SnapshotItem[] }[] = [];
  let currentKey: string | null = null;
  for (const { item, date } of valid) {
    const key = dayKey(date);
    if (key !== currentKey) {
      const dayLabel =
        key === today ? "Today" : key === yesterday ? "Yesterday" : date.toLocaleDateString();
      groups.push({ dayLabel, items: [] });
      currentKey = key;
    }
    groups[groups.length - 1].items.push(item);
  }
  if (unknown.length > 0) groups.push({ dayLabel: UNKNOWN_DAY, items: unknown });
  return groups;
}
