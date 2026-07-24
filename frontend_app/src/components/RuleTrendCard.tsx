import { useEffect, useMemo, useState } from "react";
import { Line } from "react-chartjs-2";


type RuleUnit = "count" | "usd" | "percent" | "ratio";
type RuleStatus = "pass" | "fail" | "unknown" | "disabled";
type TrendWindow = "24h" | "7d" | "30d" | "90d";

type RuleCardItem = {
  type: string;
  label?: string;
  operator?: ">=" | "<=";
  unit?: RuleUnit;
  current?: number | null;
  target: number;
  status?: RuleStatus;
  reason?: string;
};

type TrendPoint = {
  kind: "point" | "gap" | "target";
  timestamp: string | null;
  source_timestamp: string | null;
  value: number | null;
  target: number;
  status: "pass" | "fail" | null;
  operator: string;
  unit: RuleUnit;
  scenario_key: string;
  scenario_label: string;
  reason: string | null;
};

type TrendPayload = {
  mint: string;
  rule_type: string;
  window: TrendWindow;
  retention_days: number;
  points: TrendPoint[];
  latest_valid: TrendPoint | null;
  total_events: number;
  sampled: boolean;
};

type RuleTrendCardProps = {
  mint: string;
  item: RuleCardItem;
  isDark: boolean;
  refreshKey?: string | null;
};

const WINDOWS: TrendWindow[] = ["24h", "7d", "30d", "90d"];

const localKey = (mint: string, ruleType: string, name: string) =>
  `ruleTrend:${name}:${mint}:${ruleType}`;

const readStored = (key: string) => {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
};

const writeStored = (key: string, value: string) => {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Browser storage is optional; the current session still works.
  }
};

const finite = (value: unknown): number | null => {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};

const isValidTrendPoint = (point?: TrendPoint) =>
  point?.kind === "point" && finite(point.value) !== null;

const trendPointRadius = (points: TrendPoint[], index: number, validPointCount: number) => {
  if (!isValidTrendPoint(points[index])) return 0;
  if (validPointCount <= 120) return 1.5;

  const hasPreviousPoint = isValidTrendPoint(points[index - 1]);
  const hasNextPoint = isValidTrendPoint(points[index + 1]);
  if (!hasPreviousPoint && !hasNextPoint) return 3;
  if (!hasPreviousPoint || !hasNextPoint) return 1.5;
  return 0;
};

const formatValue = (value: unknown, unit?: RuleUnit) => {
  const number = finite(value);
  if (number === null) return "--";
  if (unit === "usd") {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: "USD",
      notation: "compact",
      maximumFractionDigits: 2,
    }).format(number);
  }
  if (unit === "percent") return `${number.toLocaleString(undefined, { maximumFractionDigits: 2 })}%`;
  if (unit === "ratio") return `${number.toLocaleString(undefined, { maximumFractionDigits: 2 })}x`;
  return number.toLocaleString(undefined, { maximumFractionDigits: 0 });
};

const formatTime = (value?: string | null, window: TrendWindow = "7d") => {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  if (window === "24h") {
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  if (window === "7d") {
    return date.toLocaleString([], {
      weekday: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
  }
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
};

const fullTime = (value?: string | null) => {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "-" : date.toLocaleString();
};

function TrendIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <path d="M4 18V6M4 18h16" strokeLinecap="round" />
      <path d="m7 15 3.25-3.5 3 2 4.75-6" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="7" cy="15" r="1" fill="currentColor" stroke="none" />
      <circle cx="10.25" cy="11.5" r="1" fill="currentColor" stroke="none" />
      <circle cx="13.25" cy="13.5" r="1" fill="currentColor" stroke="none" />
      <circle cx="18" cy="7.5" r="1" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function RuleTrendCard({ mint, item, isDark, refreshKey }: RuleTrendCardProps) {
  const openStorageKey = localKey(mint, item.type, "open");
  const windowStorageKey = localKey(mint, item.type, "window");
  const [trendOpen, setTrendOpen] = useState(() => readStored(openStorageKey) === "true");
  const [window, setWindow] = useState<TrendWindow>(() => {
    const saved = readStored(windowStorageKey) as TrendWindow | null;
    return saved && WINDOWS.includes(saved) ? saved : "7d";
  });
  const [payload, setPayload] = useState<TrendPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [retryNonce, setRetryNonce] = useState(0);

  useEffect(() => {
    if (!trendOpen) return;
    const controller = new AbortController();
    let active = true;
    setLoading(true);
    setError("");
    const query = new URLSearchParams({ rule_type: item.type, window });
    fetch(`/api/tokens/${encodeURIComponent(mint)}/rule-history?${query.toString()}`, {
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) {
          let detail = "Rule history could not be loaded";
          try {
            const body = await response.json();
            detail = body?.detail || detail;
          } catch {
            // Keep the safe fallback message.
          }
          throw new Error(detail);
        }
        return response.json();
      })
      .then((data) => {
        if (active) setPayload(data);
      })
      .catch((reason) => {
        if (active && reason?.name !== "AbortError") {
          setError(String(reason?.message || reason || "Rule history could not be loaded"));
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [item.type, mint, refreshKey, retryNonce, trendOpen, window]);

  const points = Array.isArray(payload?.points) ? payload!.points : [];
  const validPointCount = points.filter(isValidTrendPoint).length;
  const hasValidPoints = validPointCount > 0;
  const latestScenario = [...points].reverse().find((point) => point.scenario_label)?.scenario_label
    || payload?.latest_valid?.scenario_label
    || "";

  const chartData = useMemo(() => ({
    labels: points.map((point) => formatTime(point.timestamp, window)),
    datasets: [
      {
        label: "Actual",
        data: points.map((point) => point.kind === "point" ? finite(point.value) : null),
        borderColor: "#3b82f6",
        backgroundColor: "#3b82f6",
        borderWidth: 2,
        pointRadius: (context: any) =>
          trendPointRadius(points, Number(context.dataIndex), validPointCount),
        pointHoverRadius: 4,
        pointHitRadius: 8,
        spanGaps: false,
        tension: 0.22,
      },
      {
        label: "Target",
        data: points.map((point) => finite(point.target)),
        borderColor: isDark ? "#cbd5e1" : "#64748b",
        backgroundColor: isDark ? "#cbd5e1" : "#64748b",
        borderWidth: 1.5,
        borderDash: [5, 4],
        pointRadius: 0,
        spanGaps: true,
        stepped: true as const,
      },
    ],
  }), [isDark, points, validPointCount, window]);

  const chartOptions = useMemo(() => ({
    responsive: true,
    maintainAspectRatio: false,
    animation: false as const,
    normalized: true,
    interaction: {
      mode: "index" as const,
      intersect: false,
    },
    plugins: {
      legend: {
        display: true,
        position: "top" as const,
        align: "start" as const,
        labels: {
          color: isDark ? "#e5e7eb" : "#374151",
          boxWidth: 20,
          boxHeight: 2,
          padding: 12,
          font: { size: 11 },
        },
      },
      tooltip: {
        backgroundColor: isDark ? "rgba(17,24,39,0.96)" : "rgba(255,255,255,0.96)",
        titleColor: isDark ? "#f9fafb" : "#111827",
        bodyColor: isDark ? "#e5e7eb" : "#111827",
        footerColor: isDark ? "#9ca3af" : "#4b5563",
        borderColor: isDark ? "#374151" : "#d1d5db",
        borderWidth: 1,
        callbacks: {
          label: (context: any) => {
            const point = points[context.dataIndex];
            const value = context.parsed?.y ?? context.raw;
            return `${context.dataset?.label || "Value"}: ${formatValue(value, point?.unit || item.unit)}`;
          },
          footer: (contexts: any[]) => {
            const index = contexts?.[0]?.dataIndex;
            const point = Number.isInteger(index) ? points[index] : null;
            if (!point) return "";
            const notes = [];
            if (point.source_timestamp) notes.push(`Source: ${fullTime(point.source_timestamp)}`);
            if (point.scenario_label) notes.push(point.scenario_label);
            if (point.reason) notes.push(point.reason);
            return notes;
          },
        },
      },
    },
    scales: {
      x: {
        ticks: {
          color: isDark ? "#9ca3af" : "#6b7280",
          autoSkip: true,
          maxTicksLimit: 4,
          maxRotation: 0,
          font: { size: 10 },
        },
        grid: { display: false },
      },
      y: {
        grace: "8%",
        ticks: {
          color: isDark ? "#9ca3af" : "#6b7280",
          maxTicksLimit: 5,
          font: { size: 10 },
          callback: (value: string | number) => formatValue(value, item.unit),
        },
        grid: {
          color: isDark ? "rgba(255,255,255,0.07)" : "rgba(0,0,0,0.06)",
        },
      },
    },
  }), [isDark, item.unit, points]);

  const itemStyle = item.status === "pass"
    ? "border-emerald-200 bg-emerald-50/70 dark:border-emerald-900 dark:bg-emerald-950/20"
    : item.status === "fail"
      ? "border-red-200 bg-red-50/70 dark:border-red-900 dark:bg-red-950/20"
      : "border-amber-200 bg-amber-50/70 dark:border-amber-900 dark:bg-amber-950/20";
  const itemLabel = item.status === "pass" ? "Pass" : item.status === "fail" ? "Not passed" : "Unknown";

  const toggleTrend = () => {
    const next = !trendOpen;
    setTrendOpen(next);
    writeStored(openStorageKey, String(next));
  };

  const chooseWindow = (next: TrendWindow) => {
    if (next === window) return;
    setPayload(null);
    setWindow(next);
    writeStored(windowStorageKey, next);
  };

  return (
    <div className={`min-w-0 rounded-lg border p-3 transition-colors ${trendOpen ? "" : "sm:min-h-48"} ${itemStyle}`}>
      <div className="flex min-w-0 items-start gap-2">
        <span className="min-w-0 flex-1 font-medium">{item.label}</span>
        <span className="flex-shrink-0 rounded bg-white/70 px-2 py-0.5 text-xs font-semibold dark:bg-gray-900/50">
          {itemLabel}
        </span>
        <button
          type="button"
          className={`inline-flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-md border transition focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 ${
            trendOpen
              ? "border-blue-400 bg-blue-600 text-white"
              : "border-gray-300 bg-white/70 text-gray-600 hover:border-blue-400 hover:text-blue-600 dark:border-gray-700 dark:bg-gray-900/50 dark:text-gray-300"
          }`}
          onClick={toggleTrend}
          aria-pressed={trendOpen}
          aria-label={`${trendOpen ? "Show current value for" : "Show trend for"} ${item.label}`}
          title={`${trendOpen ? "Show current value" : "Show trend"} - ${item.label}`}
        >
          <span className="h-5 w-5"><TrendIcon /></span>
        </button>
      </div>

      {!trendOpen ? (
        <>
          <div className="mt-3 grid grid-cols-[1fr_auto_1fr] items-end gap-2 text-sm">
            <div className="min-w-0">
              <div className="text-xs uppercase tracking-wide text-gray-500">Right now</div>
              <div className="truncate text-lg font-bold">{formatValue(item.current, item.unit)}</div>
            </div>
            <span className="pb-1 text-gray-400">{item.operator}</span>
            <div className="min-w-0 text-right">
              <div className="text-xs uppercase tracking-wide text-gray-500">Target</div>
              <div className="truncate text-lg font-bold">{formatValue(item.target, item.unit)}</div>
            </div>
          </div>
          {item.reason && <p className="mt-2 break-words text-xs text-gray-500">{item.reason}</p>}
        </>
      ) : (
        <div className="mt-3 min-w-0 space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-xs font-medium text-gray-600 dark:text-gray-300">Rule Trend</span>
            <div className="flex flex-wrap gap-1" aria-label="Rule trend range">
              {WINDOWS.map((option) => (
                <button
                  key={option}
                  type="button"
                  onClick={() => chooseWindow(option)}
                  className={`min-h-8 rounded px-2 py-1 text-xs font-semibold transition focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 ${
                    window === option
                      ? "bg-blue-600 text-white"
                      : "border border-gray-300 bg-white/70 text-gray-600 dark:border-gray-700 dark:bg-gray-900/50 dark:text-gray-300"
                  }`}
                >
                  {option}
                </button>
              ))}
            </div>
          </div>

          {loading && !payload ? (
            <div className="flex h-44 items-center justify-center text-xs text-gray-500" role="status">
              Loading trend…
            </div>
          ) : error ? (
            <div className="flex h-44 flex-col items-center justify-center gap-2 text-center text-xs text-red-600 dark:text-red-400">
              <span className="max-w-full break-words">{error}</span>
              <button
                type="button"
                className="rounded border border-current px-2 py-1 font-semibold"
                onClick={() => setRetryNonce((current) => current + 1)}
              >
                Retry
              </button>
            </div>
          ) : hasValidPoints ? (
            <div className="h-44 min-w-0">
              <Line data={chartData} options={chartOptions} />
            </div>
          ) : (
            <div className="flex h-44 items-center justify-center rounded-md border border-dashed border-gray-300 p-3 text-center text-xs text-gray-500 dark:border-gray-700">
              History begins after the first fresh valid reading. Unknown values are never plotted as zero.
            </div>
          )}

          <div className="space-y-1 text-[11px] leading-relaxed text-gray-500">
            {payload?.latest_valid ? (
              <div>
                Last valid: <span className="font-semibold text-gray-700 dark:text-gray-300">
                  {formatValue(payload.latest_valid.value, item.unit)}
                </span>
                {" · "}{fullTime(payload.latest_valid.timestamp)}
              </div>
            ) : (
              <div>No valid reading has been saved yet.</div>
            )}
            {latestScenario && <div>Price-impact scenario: {latestScenario}</div>}
            <div>
              Gaps mean the rule was disabled, unavailable, stale, or collection paused.
              {payload?.sampled ? " Older points are safely downsampled." : ""}
              {loading && payload ? " Refreshing…" : ""}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
