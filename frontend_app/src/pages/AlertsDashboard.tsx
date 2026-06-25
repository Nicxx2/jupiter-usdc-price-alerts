import { useEffect, useMemo, useState, useRef } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input, Select } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "sonner";
import { Line } from "react-chartjs-2";
import { Cross2Icon, DotFilledIcon, GearIcon, QuestionMarkCircledIcon } from "@radix-ui/react-icons";
import {
  Chart as ChartJS,
  LineElement,
  PointElement,
  LinearScale,
  CategoryScale,
  Tooltip,
  Legend,
} from "chart.js";

ChartJS.register(LineElement, PointElement, LinearScale, CategoryScale, Tooltip, Legend);

// Small utils: NaN-safe parsing and formatting.
const safe = (n: any, d: any = 0) => {
  if (n === null || n === undefined || n === "") return d;
  const v = Number(n);
  return Number.isFinite(v) ? v : d;
};
const fmt = (n: any, digits = 2) => safe(n).toFixed(digits);

const pnlStatusText = (item: any) => {
  const status = item?.pnl_status;
  if (!status || status === "ok") return "";
  if (status === "holding_only") return item?.pnl_message || "Holding found; P&L not indexed yet";
  if (status === "indexing") return item?.pnl_message || "P&L is queued for indexing";
  if (status === "not_found") return item?.pnl_message || "No holding found for this token";
  if (status === "partial") return item?.pnl_message || "Some wallet P&L data is unavailable";
  return item?.pnl_error || "Wallet info update issue";
};

type RateLimitMode = "safe" | "custom" | "off";

type SettingsHelpLabelProps = {
  text: string;
  help: string;
  open: boolean;
  onToggle: () => void;
};

function SettingsHelpLabel({ text, help, open, onToggle }: SettingsHelpLabelProps) {
  return (
    <div className="relative flex items-center gap-1.5 text-sm font-medium text-gray-700 dark:text-gray-300 sm:min-h-10">
      <span>{text}</span>
      <button
        type="button"
        className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-gray-400 transition hover:bg-gray-100 hover:text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-400 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
        title={help}
        aria-label={`${text} help`}
        aria-expanded={open}
        onClick={(event) => {
          event.stopPropagation();
          onToggle();
        }}
      >
        <QuestionMarkCircledIcon className="h-3.5 w-3.5" />
      </button>
      {open && (
        <div className="absolute left-0 top-7 z-50 w-64 max-w-[calc(100vw-3rem)] rounded-md border border-gray-200 bg-white p-2 text-xs font-normal leading-relaxed text-gray-600 shadow-lg dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300">
          {help}
        </div>
      )}
    </div>
  );
}

function ThemeToggleButton() {
  const [isDark, setIsDark] = useState<boolean>(
    typeof document !== "undefined" && document.documentElement.classList.contains("dark")
  );

  useEffect(() => {
    const update = () => setIsDark(document.documentElement.classList.contains("dark"));
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);

  const toggleTheme = () => {
    const root = document.documentElement;
    const next = !isDark;
    root.classList.toggle("dark", next);
    try {
      localStorage.setItem("theme", next ? "dark" : "light");
    } catch {
      // Theme still changes for this session if local storage is unavailable.
    }
    setIsDark(next);
  };

  return (
    <Button
      variant="outline"
      size="icon"
      onClick={toggleTheme}
      className="h-10 w-10 rounded-full shadow"
      title={isDark ? "Switch to light mode" : "Switch to dark mode"}
      aria-label={isDark ? "Switch to light mode" : "Switch to dark mode"}
    >
      <span className="block h-5 w-5">
        {isDark ? (
          <svg viewBox="0 0 24 24" fill="currentColor" className="text-yellow-400">
            <path d="M6.76 4.84l-1.8-1.79-1.41 1.41 1.79 1.8 1.42-1.42zm10.48 0l1.79-1.79 1.41 1.41-1.79 1.8-1.41-1.42zM12 4V1h-0v3h0zm0 19v-3h0v3h0zM4 12H1v0h3v0zm19 0h-3v0h3v0zM6.76 19.16l-1.8 1.79 1.41 1.41 1.8-1.79-1.41-1.41zm10.48 0l1.41 1.41 1.79-1.79-1.41-1.41-1.79 1.79zM12 7a5 5 0 100 10 5 5 0 000-10z" />
          </svg>
        ) : (
          <svg viewBox="0 0 24 24" fill="currentColor" className="text-gray-700 dark:text-gray-200">
            <path d="M21.64 13a9 9 0 11-10.63-10.6 7 7 0 1010.63 10.6z" />
          </svg>
        )}
      </span>
    </Button>
  );
}

type TokenConfig = {
  mint: string;
  name?: string;
  enabled?: boolean;
  ntfy_topic?: string;
  check_interval?: number | null;
  rsi_check_interval?: number | null;
  alert_reset_minutes?: number | null;
  rsi_interval?: string | null;
  rsi_reset_enabled?: boolean | null;
  rsi_enabled?: boolean | null;
  wallet_addresses?: string[];
};

type TokenSummary = TokenConfig & {
  active?: boolean;
  buy_price?: number | null;
  sell_price?: number | null;
  rsi?: number | null;
  rsi_status?: string | null;
  last_checked?: string | null;
  next_check_at?: string | null;
  effective_check_interval?: number | null;
  effective_rsi_check_interval?: number | null;
  error?: string | null;
  ntfy_effective_topic?: string;
  ntfy_topic_source?: string;
};

type TokenDraft = {
  name: string;
  ntfy_topic: string;
  check_interval: string;
  rsi_check_interval: string;
  rsi_enabled: boolean;
};
const formatTokenTime = (value?: string | null) => {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
};

const formatChartTime = (value?: string | null) => {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  const time = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const today = new Date();
  return date.toDateString() === today.toDateString()
    ? time
    : `${date.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
};

const timestampMs = (value?: string | null) => {
  if (!value) return Number.POSITIVE_INFINITY;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : Number.POSITIVE_INFINITY;
};

const shortMint = (mint: string) => (mint ? `${mint.slice(0, 4)}...${mint.slice(-4)}` : "--");
const tokenName = (token?: TokenConfig | null) => token?.name || (token?.mint ? shortMint(token.mint) : "--");
const intervalDraft = (value?: number | null) => (value === null || value === undefined ? "" : String(value));
const topicSourceLabel = (source?: string) => {
  if (source === "custom") return "Custom topic";
  if (source === "inherited") return "Global topic";
  return "No topic";
};
const CHART_WINDOWS = [2, 4, 6, 12, 24];
const scopedPreferenceKey = (name: string, mint?: string) => mint ? `${name}:${mint}` : name;
const clampPercent = (value: any) => Math.max(0, Math.min(100, safe(value, 25)));

const getStoredValue = (key: string) => {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
};

const setStoredValue = (key: string, value: string) => {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Local storage is optional; the in-memory UI state still works for this session.
  }
};

const readStoredNumber = (key: string, fallback: number) => {
  const raw = getStoredValue(key);
  if (raw === null || raw.trim() === "") return fallback;
  const stored = Number(raw);
  return Number.isFinite(stored) ? stored : fallback;
};

const readStoredBool = (key: string, fallback: boolean) => {
  const raw = getStoredValue(key);
  if (raw === null || raw.trim() === "") return fallback;
  return raw === "true" ? true : raw === "false" ? false : fallback;
};

const readChartWindow = (mint?: string) => {
  const scoped = readStoredNumber(scopedPreferenceKey("chartWindowHours", mint), NaN);
  if (CHART_WINDOWS.includes(scoped)) return scoped;
  const global = readStoredNumber("chartWindowHours", 6);
  return CHART_WINDOWS.includes(global) ? global : 6;
};

const readSellPercent = (mint?: string) => {
  const scoped = readStoredNumber(scopedPreferenceKey("sellPercent", mint), NaN);
  if (Number.isFinite(scoped)) return clampPercent(scoped);
  return clampPercent(readStoredNumber("sellPercent", 25));
};

const readSelectedWallet = (mint: string | undefined, wallets: string[]) => {
  const scoped = getStoredValue(scopedPreferenceKey("selectedWallet", mint));
  const selected = scoped || "all";
  return selected === "all" || wallets.includes(selected) ? selected : "all";
};

async function responseDetail(res: Response, fallback: string) {
  try {
    const data = await res.json();
    return data?.detail || data?.error || fallback;
  } catch {
    return fallback;
  }
}

// RSI helpers.
function rsiLabel(value: number | null): string {
  if (value === null) return "";
  if (value >= 70) return "Overbought";
  if (value <= 30) return "Oversold";
  if (value >= 50) return "Bullish";
  return "Gray Zone";
}

function rsiColor(value: number | null): string {
  if (value === null) return "gray";
  if (value >= 70) return "red";
  if (value <= 30) return "blue";
  if (value >= 50) return "green";
  return "darkgray";
}

function getRsiStatus(triggered: boolean, resetEnabled: boolean): string {
  if (triggered) {
    return resetEnabled ? "Waiting" : "Inactive";
  }
  return "Active";
}

function friendlyRsiMessage(message?: string): string {
  if (!message) return "";
  const bars = message.match(/not enough bars for RSI\((\d+)\): got (\d+)/i);
  if (bars) return `Waiting for enough RSI candles (${bars[2]}/${Number(bars[1]) + 1} bars)`;
  const points = message.match(/not enough data for RSI: need >= (\d+) points, got (\d+)/i);
  if (points) return `Waiting for enough RSI candles (${points[2]}/${points[1]} bars)`;
  if (/no (valid )?rsi candles returned|no non-zero volume bars/i.test(message)) {
    return "Waiting for enough traded candles";
  }
  return message;
}

function isRsiWarmupMessage(message?: string): boolean {
  return /not enough (bars|data)|no (valid )?rsi candles returned|no non-zero volume bars/i.test(message || "");
}

function getRsiStatusMeta(status: string, message?: string) {
  const friendly = friendlyRsiMessage(message);
  if (status === "ok") return { color: "text-green-500", label: "RSI value is fresh" };
  if (status === "error" && isRsiWarmupMessage(message)) return { color: "text-amber-500", label: friendly || "Waiting for enough RSI candles" };
  if (status === "error") return { color: "text-red-500", label: friendly || "RSI fetch failed" };
  if (status === "stale") return { color: "text-amber-500", label: friendly || "RSI value is stale" };
  if (status === "disabled") return { color: "text-gray-400", label: friendly || "RSI disabled" };
  return { color: "text-amber-500", label: friendly || "Waiting for RSI" };
}

function getAlertStatusWithCountdown(lastTime: string | undefined, resetMinutes: number): string {
  if (!lastTime) return "Active";
  try {
    const last = new Date(lastTime);
    if (isNaN(last.getTime())) return "Active";
    const now = new Date();
    const diff = now.getTime() - last.getTime();
    const minutesSince = diff / 60000;
    if (resetMinutes === 0) return minutesSince > 0 ? "Inactive" : "Active";
    if (minutesSince >= resetMinutes) return "Active";
    const remainingMs = alertResetMinutesToMs(resetMinutes) - diff;
    const remainingMin = Math.floor(remainingMs / 60000);
    const remainingSec = Math.floor((remainingMs % 60000) / 1000);
    return `Cooldown - ready in ${String(remainingMin).padStart(2, "0")}:${String(remainingSec).padStart(2, "0")}`;
  } catch {
    return "Active";
  }
}
function alertResetMinutesToMs(min: number) {
  return min * 60 * 1000;
}

export default function AlertsDashboard() {
  const [usdAmount, setUsdAmount] = useState(100);
  const [buyAlerts, setBuyAlerts] = useState<number[]>([]);
  const [sellAlerts, setSellAlerts] = useState<number[]>([]);
  const [lastBuyTimes, setLastBuyTimes] = useState<Record<string, string>>({});
  const [lastSellTimes, setLastSellTimes] = useState<Record<string, string>>({});
  const [alertResetMinutes, setAlertResetMinutes] = useState(0);
  const [newBuy, setNewBuy] = useState("");
  const [newSell, setNewSell] = useState("");
  const [history, setHistory] = useState<any[]>([]);
  const [chartWindowHours, setChartWindowHours] = useState(() => readChartWindow());
  const [latestBuyPrice, setLatestBuyPrice] = useState<number | null>(null);
  const [latestSellPrice, setLatestSellPrice] = useState<number | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsHelpOpen, setSettingsHelpOpen] = useState<string | null>(null);
  const [checkInterval, setCheckInterval] = useState(60);
  const [rsiCheckInterval, setRsiCheckInterval] = useState(5);
  const [solanaTrackerRps, setSolanaTrackerRps] = useState(1);
  const [solanaTrackerRateLimitMode, setSolanaTrackerRateLimitMode] = useState<RateLimitMode>("safe");
  const [inputDecimals, setInputDecimals] = useState<number | "">("");
  const [outputDecimals, setOutputDecimals] = useState<number | "">("");
  const [solanaTrackerEnabled, setSolanaTrackerEnabled] = useState(false);
  const [solanaTrackerFeaturesEnabled, setSolanaTrackerFeaturesEnabled] = useState(true);
  const [solanaTrackerApiConfigured, setSolanaTrackerApiConfigured] = useState(false);
  const [ntfyConfigured, setNtfyConfigured] = useState(false);
  const [ntfyTopic, setNtfyTopic] = useState("");
  const [ntfyTopicSaved, setNtfyTopicSaved] = useState("");
  const [ntfyTopicEffective, setNtfyTopicEffective] = useState("");
  const [settingsSaving, setSettingsSaving] = useState(false);

  // Wallet tracking state and helpers.
  const [wallets, setWallets] = useState<string[]>([]);
  const [walletRefresh, setWalletRefresh] = useState<number>(60); // kept in state but not displayed
  const [outputMint, setOutputMint] = useState<string>("");
  const [tokens, setTokens] = useState<TokenConfig[]>([]);
  const [tokenSummaries, setTokenSummaries] = useState<TokenSummary[]>([]);
  const [activeTokenMint, setActiveTokenMint] = useState("");
  const [newTokenMint, setNewTokenMint] = useState("");
  const [newTokenName, setNewTokenName] = useState("");
  const [newTokenTopic, setNewTokenTopic] = useState("");
  const [tokenDrafts, setTokenDrafts] = useState<Record<string, TokenDraft>>({});
  const [tokenSaving, setTokenSaving] = useState(false);
  const [newWallet, setNewWallet] = useState("");
  const [selectedWallet, setSelectedWallet] = useState("all");
  const [walletCopySourceMint, setWalletCopySourceMint] = useState("");
  const [pnlData, setPnlData] = useState<{ individual: Record<string, any>; aggregated?: any; token_mint?: string }>({ individual: {} });
  const [pnlLoading, setPnlLoading] = useState(false);
  const [sellPercent, setSellPercent] = useState<number>(() => readSellPercent());
  const [tokenOverviewExpanded, setTokenOverviewExpanded] = useState(() => readStoredBool("tokenOverviewExpanded", false));
  const [tokenManagerExpanded, setTokenManagerExpanded] = useState(() => readStoredBool("tokenManagerExpanded", false));
  const [addTokenExpanded, setAddTokenExpanded] = useState(false);
  const [editingTokenMint, setEditingTokenMint] = useState<string | null>(null);


  const [rsi, setRsi] = useState<number | null>(null);
  const [rsiStatus, setRsiStatus] = useState("waiting");
  const [rsiMessage, setRsiMessage] = useState("");
  const [rsiAlerts, setRsiAlerts] = useState<Record<string, { triggered: boolean }>>({});
  const [rsiResetEnabled, setRsiResetEnabled] = useState(false);
  const [rsiInterval, setRsiInterval] = useState("1s");
  const [pendingInterval, setPendingInterval] = useState("1s");
  const [newRsiDir, setNewRsiDir] = useState<"above" | "below">("above");
  const [newRsiValue, setNewRsiValue] = useState("");

  const lastPnlFetch = useRef<number>(Date.now());
  const tokenDraftDirty = useRef(false);
  const pnlFetchInFlight = useRef(false);
  const outputMintRef = useRef("");
  const ntfyTopicDirty = useRef(false);

  const effectiveSolanaTrackerRps = useMemo(() => {
    if (solanaTrackerRateLimitMode === "off") return null;
    if (solanaTrackerRateLimitMode === "safe") return 1;
    return Math.max(0.1, safe(solanaTrackerRps, 1));
  }, [solanaTrackerRateLimitMode, solanaTrackerRps]);

  const walletRefreshLabel = pnlLoading
    ? `Updating ${wallets.length} wallet${wallets.length === 1 ? "" : "s"}...`
    : "Update All";
  const settingsHelpProps = (key: string) => ({
    open: settingsHelpOpen === key,
    onToggle: () => setSettingsHelpOpen((current) => current === key ? null : key),
  });

  const resetNtfyTopicDraft = () => {
    ntfyTopicDirty.current = false;
    setNtfyTopic(ntfyTopicSaved || ntfyTopicEffective);
  };

  const closeSettingsPanel = () => {
    setSettingsHelpOpen(null);
    resetNtfyTopicDraft();
    setSettingsOpen(false);
  };

  const toggleSettingsPanel = () => {
    setSettingsHelpOpen(null);
    resetNtfyTopicDraft();
    setSettingsOpen((open) => !open);
  };

  useEffect(() => {
    if (!settingsOpen || typeof document === "undefined") return;
    const previousBodyOverflow = document.body.style.overflow;
    const previousRootOverflow = document.documentElement.style.overflow;
    document.body.style.overflow = "hidden";
    document.documentElement.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousBodyOverflow;
      document.documentElement.style.overflow = previousRootOverflow;
    };
  }, [settingsOpen]);

  const estimatedRsiUsage = useMemo(() => {
    if (!solanaTrackerFeaturesEnabled || !solanaTrackerApiConfigured) return { calls: 0, tokens: 0 };
    const summaries = new Map(tokenSummaries.map((summary) => [summary.mint, summary]));
    let calls = 0;
    let enabledTokens = 0;

    for (const token of tokens) {
      if (token.enabled === false || token.rsi_enabled === false) continue;
      const summary = summaries.get(token.mint);
      const interval = Math.max(1, Math.floor(safe(token.rsi_check_interval ?? summary?.effective_rsi_check_interval ?? summary?.rsi_check_interval ?? rsiCheckInterval, rsiCheckInterval)));
      calls += Math.ceil((30 * 24 * 60) / interval);
      enabledTokens += 1;
    }

    return { calls, tokens: enabledTokens };
  }, [rsiCheckInterval, solanaTrackerApiConfigured, solanaTrackerFeaturesEnabled, tokenSummaries, tokens]);

  const solanaTrackerRateLabel = solanaTrackerRateLimitMode === "off" ? "off" : `${effectiveSolanaTrackerRps} request/sec`;
  const rsiDefaultIntervalLabel = `${Math.max(1, Math.floor(safe(rsiCheckInterval, 5)))} min`;

  const activeToken = useMemo(
    () => tokens.find((token) => token.mint === activeTokenMint) || tokens[0] || null,
    [activeTokenMint, tokens]
  );

  useEffect(() => {
    outputMintRef.current = outputMint;
  }, [outputMint]);

  useEffect(() => {
    if (!activeTokenMint) return;
    setChartWindowHours(readChartWindow(activeTokenMint));
    setSellPercent(readSellPercent(activeTokenMint));
  }, [activeTokenMint]);

  const updateSellPercent = (value: number) => {
    const next = clampPercent(value);
    setSellPercent(next);
    setStoredValue(scopedPreferenceKey("sellPercent", activeTokenMint), String(next));
  };

  const toggleTokenOverview = () => {
    setTokenOverviewExpanded((current) => {
      const next = !current;
      setStoredValue("tokenOverviewExpanded", String(next));
      return next;
    });
  };

  const toggleTokenManager = () => {
    setTokenManagerExpanded((current) => {
      const next = !current;
      setStoredValue("tokenManagerExpanded", String(next));
      return next;
    });
  };

  const tokenRows = useMemo(() => {
    const summaries = new Map(tokenSummaries.map((summary) => [summary.mint, summary]));
    return tokens.map<TokenSummary>((token) => ({
      ...token,
      ...(summaries.get(token.mint) || {}),
      active: token.mint === activeTokenMint,
    }));
  }, [activeTokenMint, tokenSummaries, tokens]);

  const activeTokenSummary = useMemo(
    () => tokenRows.find((row) => row.mint === activeTokenMint) || tokenRows[0] || null,
    [activeTokenMint, tokenRows]
  );

  const activeTokenRsiEnabled = (activeTokenSummary?.rsi_enabled ?? activeToken?.rsi_enabled ?? true) !== false;
  const solanaTrackerUnavailableReason = !solanaTrackerFeaturesEnabled
    ? "SolanaTracker is disabled in settings."
    : !solanaTrackerApiConfigured
      ? "SolanaTracker API key is not configured."
      : "";
  const rsiFeaturesVisible = solanaTrackerEnabled && activeTokenRsiEnabled;
  const activeRsiText = !solanaTrackerFeaturesEnabled
    ? "Off"
    : !solanaTrackerApiConfigured
      ? "--"
      : !activeTokenRsiEnabled
        ? "Off"
        : rsi !== null
          ? fmt(rsi, 2)
          : "--";

  const walletCopySources = useMemo(
    () => tokens.filter((token) => token.mint !== activeTokenMint && (token.wallet_addresses || []).length > 0),
    [activeTokenMint, tokens]
  );

  useEffect(() => {
    if (editingTokenMint && !tokens.some((token) => token.mint === editingTokenMint)) {
      setEditingTokenMint(null);
    }
  }, [editingTokenMint, tokens]);

  const tokenOverviewRows = useMemo(() => {
    return [...tokenRows].sort((a, b) => {
      if (a.active !== b.active) return a.active ? -1 : 1;
      if (Boolean(a.error) !== Boolean(b.error)) return a.error ? -1 : 1;
      const aNext = timestampMs(a.next_check_at);
      const bNext = timestampMs(b.next_check_at);
      if (aNext !== bNext) return aNext - bNext;
      return tokenName(a).localeCompare(tokenName(b));
    });
  }, [tokenRows]);

  const tokenOverviewStats = useMemo(() => ({
    total: tokenRows.length,
    issues: tokenRows.filter((row) => row.error).length,
    customTopics: tokenRows.filter((row) => row.ntfy_topic_source === "custom").length,
  }), [tokenRows]);


  // Dark mode detection for chart theming.
  const [isDark, setIsDark] = useState<boolean>(
    typeof document !== "undefined" && document.documentElement.classList.contains("dark")
  );
  useEffect(() => {
    const update = () => setIsDark(document.documentElement.classList.contains("dark"));
    const obs = new MutationObserver(update);
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => obs.disconnect();
  }, []);

  const fetchRSI = () => {
    fetch("/api/rsi")
      .then((res) => res.json())
      .then((data) => {
        setRsi(data.latest_rsi === null || data.latest_rsi === undefined ? null : (safe(data.latest_rsi, null) as any));
        // timestamp intentionally ignored/hidden
        setRsiAlerts(data.alerts || {});
        setRsiInterval(data.interval || "1s");
        setPendingInterval(data.interval || "1s");
        setRsiResetEnabled(!!data.reset_enabled);
        setRsiStatus(data.status || "waiting");
        setRsiMessage(data.message || "");
        if ("solanatracker_features_enabled" in data) setSolanaTrackerFeaturesEnabled(data.solanatracker_features_enabled !== false);
        if ("solanatracker_api_key_configured" in data) setSolanaTrackerApiConfigured(!!data.solanatracker_api_key_configured);
      })
      .catch(() => toast.error("Failed to load RSI"));
  };

  const fetchState = async () => {
    try {
      const res = await fetch("/api/state");
      const data = await res.json();
      const storedWallets = Array.isArray(data.wallet_addresses) ? data.wallet_addresses : [];

      setUsdAmount(safe(data.usd_amount, 100));
      setBuyAlerts(data.buy_alerts || []);
      setSellAlerts(data.sell_alerts || []);
      setLastBuyTimes(data.last_triggered_buy || {});
      setLastSellTimes(data.last_triggered_sell || {});

      setWallets(storedWallets);
      setWalletRefresh(safe(data.wallet_refresh_minutes, 60));
      const nextTokens: TokenConfig[] = data.tokens || [];
      const nextSummaries: TokenSummary[] = data.token_summaries || [];
      const nextActiveMint = nextTokens.length ? data.active_token_mint || data.output_mint || nextTokens[0]?.mint || "" : "";
      const summariesByMint = new Map(nextSummaries.map((summary) => [summary.mint, summary]));
      const nextDrafts = nextTokens.reduce<Record<string, TokenDraft>>((acc, token) => {
        const summary = summariesByMint.get(token.mint);
        acc[token.mint] = {
          name: token.name || summary?.name || "",
          ntfy_topic: token.ntfy_topic || summary?.ntfy_topic || "",
          check_interval: intervalDraft(token.check_interval ?? summary?.check_interval),
          rsi_check_interval: intervalDraft(token.rsi_check_interval ?? summary?.rsi_check_interval),
          rsi_enabled: (token.rsi_enabled ?? summary?.rsi_enabled ?? true) !== false,
        };
        return acc;
      }, {});
      setTokens(nextTokens);
      setTokenSummaries(nextSummaries);
      if (!tokenDraftDirty.current) setTokenDrafts(nextDrafts);
      setActiveTokenMint(nextActiveMint);
      setOutputMint(nextActiveMint || data.output_mint || "");
      setSelectedWallet(readSelectedWallet(nextActiveMint, storedWallets));
      setWalletCopySourceMint((current) => {
        const sources = nextTokens.filter((token) => token.mint !== nextActiveMint && (token.wallet_addresses || []).length > 0);
        return current && sources.some((token) => token.mint === current) ? current : sources[0]?.mint || "";
      });
      setAlertResetMinutes(safe(data.alert_reset_minutes, 0));
      setCheckInterval(safe(data.check_interval, 60));
      setRsiCheckInterval(safe(data.rsi_check_interval, 5));
      setRsiInterval(data.rsi_interval || "1s");
      setPendingInterval(data.rsi_interval || "1s");
      setRsiResetEnabled(!!data.rsi_reset_enabled);
      setSolanaTrackerRps(safe(data.solanatracker_requests_per_second, 1));
      setSolanaTrackerRateLimitMode(["safe", "custom", "off"].includes(data.solanatracker_rate_limit_mode) ? data.solanatracker_rate_limit_mode : "safe");
      setInputDecimals(data.input_decimals ?? "");
      setOutputDecimals(data.output_decimals ?? "");
      setSolanaTrackerEnabled(!!data.solanatracker_enabled);
      setSolanaTrackerFeaturesEnabled(data.solanatracker_features_enabled !== false);
      setSolanaTrackerApiConfigured(!!data.solanatracker_api_key_configured);
      const savedNtfyTopic = data.ntfy_topic || "";
      const effectiveNtfyTopic = data.ntfy_effective_topic || savedNtfyTopic || "";
      setNtfyTopicSaved(savedNtfyTopic);
      setNtfyTopicEffective(effectiveNtfyTopic);
      if (!ntfyTopicDirty.current) setNtfyTopic(savedNtfyTopic || effectiveNtfyTopic);
      setNtfyConfigured(!!data.ntfy_configured);
      if (data.rsi_status) setRsiStatus(data.rsi_status);
      if (data.rsi_error) setRsiMessage(data.rsi_error);
      const nextHistory = Array.isArray(data.active_price_history)
        ? data.active_price_history
        : Array.isArray(data.latest_prices)
          ? data.latest_prices
          : [];
      setHistory(nextHistory);
      const last = nextHistory?.at?.(-1);
      setLatestBuyPrice(last?.buy_price ?? null);
      setLatestSellPrice(last?.sell_price ?? null);
    } catch {
      toast.error("Failed to load state");
    }
  };

  // Fetch PnL for the active token. Backend handles SolanaTracker rate limiting, chunking, and holdings fallback.
  async function fetchPnl() {
    if (!solanaTrackerEnabled) {
      setPnlData({ individual: {}, aggregated: undefined, token_mint: outputMint || activeTokenMint });
      return toast.error(solanaTrackerUnavailableReason || "SolanaTracker is not available");
    }
    if (pnlFetchInFlight.current) return;
    pnlFetchInFlight.current = true;
    setPnlLoading(true);

    try {
      if (!wallets.length || !outputMint) {
        setPnlData({ individual: {}, aggregated: undefined, token_mint: outputMint || activeTokenMint });
        lastPnlFetch.current = Date.now();
        return;
      }

      const prev = pnlData.individual;
      const tokenMint = outputMint;
      const indiv: Record<string, any> = {};
      const fetchTime = new Date().toLocaleString();

      try {
        const res = await fetch("/api/pnl/batch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ wallets, token: tokenMint }),
        });
        if (!res.ok) throw new Error(await res.text());
        const payload = await res.json();
        const batch = payload?.individual && typeof payload.individual === "object" ? payload.individual : {};

        for (const wallet of wallets) {
          const data = batch[wallet];
          if (!data) {
            indiv[wallet] = { ...(prev[wallet] || {}), pnl_status: "error", pnl_error: "No wallet result" };
          } else if (data.pnl_status === "error" && prev[wallet]) {
            indiv[wallet] = { ...prev[wallet], pnl_status: "error", pnl_error: data.pnl_error || "Wallet info update issue" };
          } else {
            indiv[wallet] = { ...data, lastFetchedAt: fetchTime };
          }
        }
      } catch (error: any) {
        const message = String(error?.message || error || "Wallet info update failed").slice(0, 160);
        for (const wallet of wallets) {
          indiv[wallet] = { ...(prev[wallet] || {}), pnl_status: "error", pnl_error: message };
        }
        toast.error("Failed to load wallet info");
      }

      if (outputMintRef.current && outputMintRef.current !== tokenMint) return;

      let agg: any = {
        holding: 0,
        realized: 0,
        unrealized: 0,
        current_value: 0,
        cost_basis: 0,
        cost_basis_total: 0,
        last_trade_time: null as string | null,
        lastFetchedAt: fetchTime,
        staleCount: 0,
        limitedPnlCount: 0,
      };
      let weightedCost = 0;
      let maxTs = 0;

      for (const [_w, d] of Object.entries(indiv)) {
        const h = safe((d as any).holding);
        const cv = safe((d as any).current_value);
        const u = safe((d as any).unrealized);
        const r = safe((d as any).realized);
        const cb = safe((d as any).cost_basis);
        const status = (d as any).pnl_status;
        if (["holding_only", "indexing"].includes(status) && (h > 0 || cv > 0)) agg.limitedPnlCount += 1;

        agg.holding += h;
        agg.realized += r;
        agg.unrealized += u;
        agg.current_value += cv;
        agg.cost_basis_total += safe((d as any).cost_basis_total);
        weightedCost += cb * h;

        const t = Date.parse((d as any).last_trade_time || "");
        if (!isNaN(t) && t > maxTs) maxTs = t;
      }

      if (agg.holding > 0) {
        agg.cost_basis = weightedCost / agg.holding;
      }
      agg.last_trade_time = maxTs ? new Date(maxTs).toLocaleString() : null;

      const failedWallets: string[] = [];
      for (const [wallet, data] of Object.entries(indiv)) {
        const isStale = Boolean((data as any).lastFetchedAt) && (data as any).lastFetchedAt !== fetchTime;
        const status = (data as any).pnl_status;
        if (status === "error" || isStale) {
          failedWallets.push(wallet);
        }
      }
      agg.staleCount = failedWallets.length;
      agg.failedWallets = failedWallets;
      if (agg.limitedPnlCount > 0) {
        agg.pnl_status = "partial";
        agg.pnl_message = `${agg.limitedPnlCount} wallet${agg.limitedPnlCount === 1 ? "" : "s"} included with holdings only`;
      }

      setPnlData({ individual: indiv, aggregated: agg, token_mint: tokenMint });

      try {
        await fetch("/api/pnl", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ individual: indiv, aggregated: agg, token_mint: tokenMint }),
        });
      } catch {
        // The visible wallet result is still usable if persistence fails.
      }

      lastPnlFetch.current = Date.now();
    } finally {
      pnlFetchInFlight.current = false;
      setPnlLoading(false);
    }
  }

  // Poll state every 60 s
  useEffect(() => {
    fetchState();
    const id = setInterval(fetchState, 60_000);
    return () => clearInterval(id);
  }, []);

  // Poll RSI every 60 s
  useEffect(() => {
    fetchRSI();
    const id = setInterval(fetchRSI, 60_000);
    return () => clearInterval(id);
  }, []);

  // Load persisted PnL for the active token from the server.
  useEffect(() => {
    if (!outputMint) return;
    let cancelled = false;
    fetch(`/api/pnl?token=${encodeURIComponent(outputMint)}`)
      .then((res) => res.json())
      .then((serverPnl) => {
        if (cancelled || outputMintRef.current !== outputMint) return;
        if (serverPnl?.token_mint && serverPnl.token_mint !== outputMint) return;
        setPnlData(serverPnl?.individual ? serverPnl : { individual: {}, aggregated: undefined, token_mint: outputMint });
      })
      .catch(() => {
        if (!cancelled && outputMintRef.current === outputMint) {
          setPnlData({ individual: {}, aggregated: undefined, token_mint: outputMint });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [outputMint]);


  // Countdown ticker for refresh timers.
  useEffect(() => {
    const id = setInterval(() => {
      setLastBuyTimes((prev) => ({ ...prev }));
      setLastSellTimes((prev) => ({ ...prev }));
    }, 1_000);
    return () => clearInterval(id);
  }, []);

  const applyUsdAmount = async () => {
    const amount = parseFloat(String(usdAmount));
    if (isNaN(amount) || amount <= 0) return toast.error("Invalid USD amount");
    const res = await fetch("/api/usd", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: amount }),
    });
    if (res.ok) {
      toast.success("USD amount updated");
      setHistory([]); // reset chart
    }
  };

  const applyResetMinutes = async () => {
    const res = await fetch("/api/reset-minutes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ minutes: alertResetMinutes }),
    });
    if (res.ok) {
      toast.success("Reset minutes updated");
      fetchState();
    }
  };


  const applyRuntimeSettings = async () => {
    setSettingsSaving(true);
    const ntfyDraft = ntfyTopic.trim();
    const inheritedNtfyUnchanged = !ntfyTopicDirty.current && !ntfyTopicSaved && Boolean(ntfyTopicEffective) && ntfyDraft === ntfyTopicEffective;
    const payload: Record<string, number | string | boolean | null> = {
      check_interval: Math.max(5, Math.floor(safe(checkInterval, 60))),
      rsi_check_interval: Math.max(1, Math.floor(safe(rsiCheckInterval, 5))),
      solanatracker_rate_limit_mode: solanaTrackerRateLimitMode,
      solanatracker_requests_per_second: Math.max(0.1, safe(solanaTrackerRps, 1)),
      solanatracker_features_enabled: solanaTrackerFeaturesEnabled,
      ntfy_topic: inheritedNtfyUnchanged ? "" : ntfyDraft,
    };

    payload.input_decimals = inputDecimals === "" ? null : safe(inputDecimals, 6);
    payload.output_decimals = outputDecimals === "" ? null : safe(outputDecimals, 6);

    try {
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error();
      toast.success("Settings updated");
      ntfyTopicDirty.current = false;
      fetchState();
    } catch {
      toast.error("Failed to update settings");
    } finally {
      setSettingsSaving(false);
    }
  };

  const sendTestNotification = async () => {
    try {
      const res = await fetch("/api/notify/test", { method: "POST" });
      if (!res.ok) throw new Error();
      toast.success("Test notification sent");
    } catch {
      toast.error("Test notification failed");
    }
  };

  const clearWalletPnl = () => {
    setPnlData({ individual: {}, aggregated: undefined, token_mint: outputMint || activeTokenMint });
  };

  const addWallet = async () => {
    if (pnlLoading) return;
    const wallet = newWallet.trim();
    if (!wallet) return toast.error("Enter an address");
    if (wallets.includes(wallet)) return toast.error("Wallet already tracked for this token");

    const res = await fetch("/api/wallets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values: [wallet] }),
    });

    if (res.ok) {
      toast.success("Wallet added");
      setNewWallet("");
      setWallets((current) => current.includes(wallet) ? current : [...current, wallet]);
      setPnlData((prev) => ({
        ...prev,
        aggregated: undefined,
        token_mint: outputMint || activeTokenMint,
        individual: {
          ...prev.individual,
          [wallet]: { loading: true },
        },
      }));
      fetchState();
    } else {
      toast.error("Wallet could not be added");
    }
  };

  const removeWallet = async (wallet: string) => {
    if (pnlLoading) return;
    if (!wallet) return;
    if (!window.confirm(`Remove ${shortMint(wallet)} from ${tokenName(activeToken)}?`)) return;

    const res = await fetch("/api/wallets", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: wallet }),
    });

    if (res.ok) {
      toast.success("Wallet removed");
      const nextWallets = wallets.filter((w) => w !== wallet);
      setWallets(nextWallets);
      if (selectedWallet === wallet) {
        setSelectedWallet("all");
        setStoredValue(scopedPreferenceKey("selectedWallet", activeTokenMint), "all");
      }
      setPnlData((prev) => {
        const nextIndividual = { ...prev.individual };
        delete nextIndividual[wallet];
        return { individual: nextIndividual, aggregated: undefined, token_mint: outputMint || activeTokenMint };
      });
      fetchState();
    } else {
      toast.error("Wallet could not be removed");
    }
  };

  const copyWalletsFromToken = async () => {
    if (pnlLoading) return;
    const selectedSourceMint = walletCopySourceMint || walletCopySources[0]?.mint || "";
    const source = walletCopySources.find((token) => token.mint === selectedSourceMint) || walletCopySources[0];
    const sourceWallets = source?.wallet_addresses || [];
    const newWallets = sourceWallets.filter((wallet) => !wallets.includes(wallet));
    if (!source || sourceWallets.length === 0) return toast.error("Choose a token with wallets");
    if (newWallets.length === 0) return toast.error("No new wallets to copy");

    const res = await fetch("/api/wallets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values: newWallets }),
    });

    if (res.ok) {
      toast.success(`Copied ${newWallets.length} wallet${newWallets.length === 1 ? "" : "s"}`);
      setWallets((current) => [...current, ...newWallets.filter((wallet) => !current.includes(wallet))]);
      clearWalletPnl();
      fetchState();
    } else {
      toast.error("Wallets could not be copied");
    }
  };

  const refreshAfterTokenChange = async (nextMint?: string) => {
    const mintForState = nextMint || outputMint || activeTokenMint;
    if (mintForState) outputMintRef.current = mintForState;
    setHistory([]);
    setLatestBuyPrice(null);
    setLatestSellPrice(null);
    setPnlData({ individual: {}, aggregated: undefined, token_mint: mintForState });
    await fetchState();
    fetchRSI();
  };

  const switchActiveToken = async (mint: string) => {
    if (!mint || mint === activeTokenMint) return;
    setTokenSaving(true);
    try {
      const res = await fetch("/api/tokens/active", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mint }),
      });
      if (!res.ok) throw new Error(await responseDetail(res, "Failed to switch token"));
      toast.success("Active token updated");
      tokenDraftDirty.current = false;
      await refreshAfterTokenChange(mint);
    } catch (err: any) {
      toast.error(err?.message || "Failed to switch token");
    } finally {
      setTokenSaving(false);
    }
  };

  const addTrackedToken = async () => {
    const mint = newTokenMint.trim();
    if (!mint) return toast.error("Enter a token mint");
    setTokenSaving(true);
    try {
      const res = await fetch("/api/tokens", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mint, name: newTokenName.trim() || undefined, ntfy_topic: newTokenTopic.trim() || undefined }),
      });
      if (!res.ok) throw new Error(await responseDetail(res, "Token could not be added"));
      toast.success("Token added");
      setNewTokenMint("");
      setNewTokenName("");
      setNewTokenTopic("");
      setAddTokenExpanded(false);
      tokenDraftDirty.current = false;
      await fetchState();
    } catch (err: any) {
      toast.error(err?.message || "Token could not be added");
    } finally {
      setTokenSaving(false);
    }
  };

  const draftFromTokenRow = (row: TokenSummary): TokenDraft => ({
    name: row.name || "",
    ntfy_topic: row.ntfy_topic || "",
    check_interval: intervalDraft(row.check_interval),
    rsi_check_interval: intervalDraft(row.rsi_check_interval),
    rsi_enabled: row.rsi_enabled !== false,
  });

  const resetTokenDraft = (row: TokenSummary) => {
    setTokenDrafts((current) => ({
      ...current,
      [row.mint]: draftFromTokenRow(row),
    }));
  };

  const openTokenEditor = (row: TokenSummary) => {
    const previousRow = editingTokenMint ? tokenRows.find((token) => token.mint === editingTokenMint) : null;
    if (editingTokenMint === row.mint) {
      resetTokenDraft(row);
      setEditingTokenMint(null);
      tokenDraftDirty.current = false;
      return;
    }

    setTokenDrafts((current) => {
      const next = { ...current, [row.mint]: draftFromTokenRow(row) };
      if (previousRow) next[previousRow.mint] = draftFromTokenRow(previousRow);
      return next;
    });
    setEditingTokenMint(row.mint);
    tokenDraftDirty.current = false;
  };

  const cancelTokenEdit = (row: TokenSummary) => {
    resetTokenDraft(row);
    setEditingTokenMint(null);
    tokenDraftDirty.current = false;
  };

  const updateTokenDraft = (mint: string, changes: Partial<TokenDraft>) => {
    tokenDraftDirty.current = true;
    setTokenDrafts((current) => ({
      ...current,
      [mint]: {
        name: current[mint]?.name || "",
        ntfy_topic: current[mint]?.ntfy_topic || "",
        check_interval: current[mint]?.check_interval || "",
        rsi_check_interval: current[mint]?.rsi_check_interval || "",
        rsi_enabled: current[mint]?.rsi_enabled ?? true,
        ...changes,
      },
    }));
  };

  const intervalPayload = (value: string, minimum: number, label: string): number | null | undefined => {
    const cleaned = value.trim();
    if (!cleaned) return null;
    const number = Number(cleaned);
    if (!Number.isInteger(number) || number < minimum) {
      toast.error(`${label} must be ${minimum} or higher`);
      return undefined;
    }
    return number;
  };

  const saveTokenSettings = async (row: TokenSummary) => {
    const draft = tokenDrafts[row.mint] || {
      name: row.name || "",
      ntfy_topic: row.ntfy_topic || "",
      check_interval: intervalDraft(row.check_interval),
      rsi_check_interval: intervalDraft(row.rsi_check_interval),
      rsi_enabled: row.rsi_enabled !== false,
    };
    const priceInterval = intervalPayload(draft.check_interval, 5, "Price interval seconds");
    if (priceInterval === undefined) return;
    const rsiIntervalValue = intervalPayload(draft.rsi_check_interval, 1, "RSI interval minutes");
    if (rsiIntervalValue === undefined) return;

    setTokenSaving(true);
    try {
      const res = await fetch(`/api/tokens/${encodeURIComponent(row.mint)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: draft.name.trim(),
          ntfy_topic: draft.ntfy_topic.trim(),
          check_interval: priceInterval,
          rsi_check_interval: rsiIntervalValue,
          rsi_enabled: draft.rsi_enabled !== false,
        }),
      });
      if (!res.ok) throw new Error(await responseDetail(res, "Token settings could not be saved"));
      toast.success("Token settings saved");
      setEditingTokenMint(null);
      tokenDraftDirty.current = false;
      await fetchState();
    } catch (err: any) {
      toast.error(err?.message || "Token settings could not be saved");
    } finally {
      setTokenSaving(false);
    }
  };

  const testTokenNotification = async (mint: string) => {
    setTokenSaving(true);
    try {
      const res = await fetch(`/api/tokens/${encodeURIComponent(mint)}/notify/test`, { method: "POST" });
      if (!res.ok) throw new Error(await responseDetail(res, "Token alert test failed"));
      toast.success("Token alert test sent");
    } catch (err: any) {
      toast.error(err?.message || "Token alert test failed");
    } finally {
      setTokenSaving(false);
    }
  };

  const removeTrackedToken = async (mint: string) => {
    if (tokens.length <= 1) return toast.error("At least one token is required");
    if (!window.confirm(`Remove ${shortMint(mint)}?`)) return;
    const deletingActive = mint === activeTokenMint;
    const nextMint = deletingActive ? tokens.find((token) => token.mint !== mint)?.mint || "" : outputMint || activeTokenMint;
    if (deletingActive) outputMintRef.current = "";
    setTokenSaving(true);
    try {
      const res = await fetch(`/api/tokens/${encodeURIComponent(mint)}`, { method: "DELETE" });
      if (!res.ok) throw new Error(await responseDetail(res, "Token could not be removed"));
      toast.success("Token removed");
      if (editingTokenMint === mint) setEditingTokenMint(null);
      tokenDraftDirty.current = false;
      await refreshAfterTokenChange(nextMint);
    } catch (err: any) {
      if (deletingActive) outputMintRef.current = outputMint;
      toast.error(err?.message || "Token could not be removed");
    } finally {
      setTokenSaving(false);
    }
  };

  const addAlert = async (type: "buy" | "sell", value: string) => {
    const num = parseFloat(value);
    if (isNaN(num) || num <= 0) return toast.error("Invalid price value");
    const res = await fetch(`/api/${type}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values: [num] }),
    });
    if (res.ok) {
      toast.success(`${type} alert added`);
      type === "buy" ? setNewBuy("") : setNewSell("");
      fetchState();
    }
  };

  const removeAlert = async (type: "buy" | "sell", value: number) => {
    const res = await fetch(`/api/${type}`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    });
    if (res.ok) {
      toast.success(`${type} alert removed`);
      fetchState();
    }
  };

  const resetAlert = async (type: "buy" | "sell", value: number) => {
    const res = await fetch(`/api/reset-alert`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ side: type, price: value }),
    });
    if (res.ok) {
      toast.success(`Reset ${type} alert`);
      fetchState();
    } else {
      toast.error("Failed to reset alert");
    }
  };

  const filteredHistory = useMemo(() => {
    const cutoff = Date.now() - chartWindowHours * 60 * 60 * 1000;
    return history.filter((point) => {
      const timestamp = Date.parse(point.timestamp || point.time || "");
      return Number.isFinite(timestamp) ? timestamp >= cutoff : true;
    });
  }, [history, chartWindowHours]);

  const updateChartWindow = (hours: number) => {
    setChartWindowHours(hours);
    setStoredValue(scopedPreferenceKey("chartWindowHours", activeTokenMint), String(hours));
  };

  const data = {
    labels: filteredHistory.map((h) => formatChartTime(h.timestamp || h.time)),
    datasets: [
      {
        label: "Buy Price",
        data: filteredHistory.map((h) => (h.buy_price ?? h.buy) == null ? null : safe(h.buy_price ?? h.buy)),
        borderColor: "#4ade80",
        fill: false,
      },
      {
        label: "Sell Price",
        data: filteredHistory.map((h) => (h.sell_price ?? h.sell) == null ? null : safe(h.sell_price ?? h.sell)),
        borderColor: "#f87171",
        fill: false,
      },
    ],
  };

  const options = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: {
        display: true,
        position: "top" as const,
        labels: {
          color: isDark ? "#e5e7eb" : "#111827",
        },
      },
      tooltip: {
        backgroundColor: isDark ? "rgba(31,41,55,0.9)" : "rgba(255,255,255,0.9)",
        titleColor: isDark ? "#f9fafb" : "#111827",
        bodyColor: isDark ? "#e5e7eb" : "#111827",
        borderColor: isDark ? "#374151" : "#e5e7eb",
        borderWidth: 1,
      },
    },
    scales: {
      x: {
        ticks: {
          color: isDark ? "#d1d5db" : "#374151",
          autoSkip: true,
          maxTicksLimit: 6,
          maxRotation: 0,
        },
        grid: {
          color: isDark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.06)",
        },
      },
      y: {
        ticks: {
          color: isDark ? "#d1d5db" : "#374151",
          autoSkip: true,
          maxTicksLimit: 6,
          maxRotation: 0,
          callback: function (value: any) {
            return safe(value).toFixed(8);
          },
        },
        grid: {
          color: isDark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.06)",
        },
      },
    },
  } as const;

  const rsiMeta = getRsiStatusMeta(rsiStatus, rsiMessage);

  return (
    <div className="relative p-6 max-w-4xl mx-auto space-y-6">
      <div className="absolute top-2 left-2 text-xs text-gray-500">v3.1</div>

      <div className="fixed right-3 top-3 z-50 flex items-center gap-2">
        <Button
          variant="outline"
          size="icon"
          onClick={toggleSettingsPanel}
          className="h-10 w-10 rounded-full shadow"
          title="Settings"
          aria-label="Open settings"
          aria-expanded={settingsOpen}
        >
          <GearIcon />
        </Button>
        <ThemeToggleButton />
      </div>

      {settingsOpen && (
        <div className="fixed inset-0 z-40 overflow-y-auto overscroll-contain px-3 pb-4 pt-14" onClick={closeSettingsPanel}>
          <div
            className="relative ml-auto max-h-[calc(100dvh-4.5rem)] w-full max-w-md overflow-y-auto overscroll-contain rounded-lg border border-gray-200 bg-white p-4 shadow-xl dark:border-gray-700 dark:bg-gray-800"
            onClick={(e) => { e.stopPropagation(); setSettingsHelpOpen(null); }}
          >
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-base font-semibold">Settings</h2>
              <Button variant="ghost" size="icon" onClick={closeSettingsPanel} aria-label="Close settings">
                <Cross2Icon />
              </Button>
            </div>

            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <SettingsHelpLabel {...settingsHelpProps("price-check")} text="Price check (sec)" help="How often the app checks Jupiter prices for tracked tokens. Minimum is 5 seconds." />
              <Input type="number" min={5} value={checkInterval} onChange={(e) => setCheckInterval(safe(e.target.value, 60))} />

              <SettingsHelpLabel {...settingsHelpProps("solanatracker-features")} text="SolanaTracker" help="Enables SolanaTracker-only features: RSI, wallet info, and the sell simulator. Jupiter price checks and price alerts keep running when this is off." />
              <Select value={solanaTrackerFeaturesEnabled ? "true" : "false"} onChange={(e) => setSolanaTrackerFeaturesEnabled(e.target.value === "true")}>
                <option value="true">Enabled</option>
                <option value="false">Disabled</option>
              </Select>

              {solanaTrackerFeaturesEnabled && (
                <>
                  <SettingsHelpLabel {...settingsHelpProps("rsi-check")} text="RSI check (min)" help="How often RSI is refreshed from SolanaTracker. More tokens and lower minutes create more API calls." />
                  <Input type="number" min={1} value={rsiCheckInterval} onChange={(e) => setRsiCheckInterval(safe(e.target.value, 5))} />

                  <SettingsHelpLabel {...settingsHelpProps("solanatracker-limit")} text="SolanaTracker limit" help="Safe protects the free API by pacing requests. Custom uses your requests/sec value. Off removes this app delay for paid or private limits." />
                  <Select value={solanaTrackerRateLimitMode} onChange={(e) => setSolanaTrackerRateLimitMode(e.target.value as RateLimitMode)}>
                    <option value="safe">Safe</option>
                    <option value="custom">Custom</option>
                    <option value="off">Off</option>
                  </Select>

                  {solanaTrackerRateLimitMode === "custom" && (
                    <>
                      <SettingsHelpLabel {...settingsHelpProps("requests-sec")} text="Requests/sec" help="Used only in Custom mode. Keep this near 1 for the free SolanaTracker plan unless your account allows more." />
                      <Input
                        type="number"
                        min={0.1}
                        step={0.1}
                        value={solanaTrackerRps}
                        onChange={(e) => setSolanaTrackerRps(safe(e.target.value, 1))}
                      />
                    </>
                  )}
                </>
              )}

              <SettingsHelpLabel {...settingsHelpProps("global-ntfy-topic")} text="Global ntfy topic" help="Default alert topic for tokens without their own ntfy topic. The Docker/ENV topic is shown here until you edit and save an app override." />
              <Input
                value={ntfyTopic}
                placeholder="Docker/ENV default"
                onChange={(e) => {
                  ntfyTopicDirty.current = true;
                  setNtfyTopic(e.target.value);
                }}
              />
              <div className="text-xs text-gray-500 sm:col-span-2 -mt-2">
                {ntfyTopicSaved
                  ? "Using saved app topic."
                  : ntfyTopicEffective
                    ? "Using Docker/ENV topic."
                    : "No global ntfy topic set."}
              </div>

              <SettingsHelpLabel {...settingsHelpProps("output-decimals")} text="Output decimals" help="Decimals for the token you track. Auto is recommended unless token amounts or quotes look wrong." />
              <Input
                type="number"
                min={0}
                max={12}
                value={outputDecimals}
                placeholder="Auto"
                onChange={(e) => setOutputDecimals(e.target.value === "" ? "" : safe(e.target.value, 6))}
              />

              <SettingsHelpLabel {...settingsHelpProps("input-decimals")} text="Input decimals" help="Decimals for the input token, usually USDC. Auto is recommended for normal USDC tracking." />
              <Input
                type="number"
                min={0}
                max={12}
                value={inputDecimals}
                placeholder="Auto"
                onChange={(e) => setInputDecimals(e.target.value === "" ? "" : safe(e.target.value, 6))}
              />

              <div className="text-xs text-gray-500 sm:col-span-2">
                {solanaTrackerFeaturesEnabled
                  ? solanaTrackerApiConfigured
                    ? `SolanaTracker rate limit: ${solanaTrackerRateLabel}. RSI check default: every ${rsiDefaultIntervalLabel}. Estimate: ${estimatedRsiUsage.calls.toLocaleString()}/month across ${estimatedRsiUsage.tokens} token${estimatedRsiUsage.tokens === 1 ? "" : "s"}.`
                    : "SolanaTracker API key not configured."
                  : "SolanaTracker disabled - RSI, wallet info, and sell simulator hidden."}
              </div>
            </div>

            <div className="sticky bottom-0 -mx-4 mt-4 flex flex-wrap gap-2 border-t border-gray-200 bg-white px-4 pb-2 pt-3 dark:border-gray-700 dark:bg-gray-800">
              <Button onClick={applyRuntimeSettings} disabled={settingsSaving}>
                {settingsSaving ? "Saving" : "Save"}
              </Button>
              <Button
                variant="outline"
                onClick={sendTestNotification}
                disabled={!ntfyConfigured}
                title={ntfyConfigured ? "Send a test ntfy alert using the saved global or Docker/ENV topic" : "Save a global ntfy topic before testing alerts"}
                aria-label={ntfyConfigured ? "Send a test ntfy alert using the saved global or Docker/ENV topic" : "Save a global ntfy topic before testing alerts"}
              >
                Test alert
              </Button>
            </div>
          </div>
        </div>
      )}

      <h1 className="text-3xl font-bold mb-4">Jupiter USDC Price Alerts</h1>

      <Card>
        <CardContent className="space-y-3 p-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0 space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <Label>Token Overview</Label>
                <span className="text-xs text-gray-500">{tokenOverviewStats.total} token{tokenOverviewStats.total === 1 ? "" : "s"}</span>
                <span className={tokenOverviewStats.issues ? "text-xs text-red-500" : "text-xs text-gray-500"}>
                  {tokenOverviewStats.issues} issue{tokenOverviewStats.issues === 1 ? "" : "s"}
                </span>
                <span className="text-xs text-gray-500">{tokenOverviewStats.customTopics} custom topic{tokenOverviewStats.customTopics === 1 ? "" : "s"}</span>
              </div>
              <div className="flex flex-wrap gap-2 text-xs text-gray-500">
                <span>Active {tokenName(activeTokenSummary || activeToken)}</span>
                <span>Buy {latestBuyPrice !== null ? fmt(latestBuyPrice, 8) : "--"}</span>
                <span>Sell {latestSellPrice !== null ? fmt(latestSellPrice, 8) : "--"}</span>
                <span>RSI {activeRsiText}</span>
                <span>{topicSourceLabel(activeTokenSummary?.ntfy_topic_source)}</span>
              </div>
            </div>
            <Button size="sm" variant="outline" onClick={toggleTokenOverview}>
              {tokenOverviewExpanded ? "Hide" : "Show"}
            </Button>
          </div>

          {tokenOverviewExpanded && (
            <div className="overflow-hidden rounded border border-gray-200 dark:border-gray-700">
              {tokenOverviewRows.length === 0 ? (
                <div className="p-3 text-sm text-gray-500">No tokens configured</div>
              ) : (
                tokenOverviewRows.map((row) => {
                  const hasBuy = row.buy_price !== null && row.buy_price !== undefined;
                  const hasSell = row.sell_price !== null && row.sell_price !== undefined;
                  const hasRsi = row.rsi !== null && row.rsi !== undefined;
                  const rowRsiEnabled = row.rsi_enabled !== false;
                  const rowRsiText = !solanaTrackerFeaturesEnabled
                    ? "Off"
                    : !solanaTrackerApiConfigured
                      ? "--"
                      : !rowRsiEnabled
                        ? "Off"
                        : hasRsi
                          ? fmt(row.rsi, 2)
                          : "--";
                  const nextLabel = formatTokenTime(row.next_check_at);
                  const statusLabel = row.error ? "Issue" : row.active ? "Active" : row.rsi_status === "stale" ? "Stale" : "Watching";
                  const dotClass = row.error ? "bg-red-500" : row.active ? "bg-green-500" : row.rsi_status === "stale" ? "bg-yellow-500" : "bg-gray-400";
                  return (
                    <button
                      key={row.mint}
                      type="button"
                      onClick={() => !row.active && switchActiveToken(row.mint)}
                      disabled={tokenSaving || row.active}
                      className="grid w-full grid-cols-2 gap-2 border-t border-gray-200 p-3 text-left text-sm first:border-t-0 hover:bg-gray-50 disabled:cursor-default disabled:hover:bg-transparent dark:border-gray-700 dark:hover:bg-gray-700/35 dark:disabled:hover:bg-transparent sm:grid-cols-[minmax(0,1.3fr)_repeat(5,minmax(5.2rem,1fr))]"
                      title={row.error || row.mint}
                    >
                      <span className="col-span-2 flex min-w-0 items-center gap-2 sm:col-span-1">
                        <span className={`h-2.5 w-2.5 flex-shrink-0 rounded-full ${dotClass}`} />
                        <span className="min-w-0 truncate font-medium">{tokenName(row)}</span>
                        <span className="flex-shrink-0 font-mono text-xs text-gray-500">{shortMint(row.mint)}</span>
                      </span>
                      <span className="min-w-0 truncate">Buy <strong>{hasBuy ? fmt(row.buy_price, 8) : "--"}</strong></span>
                      <span className="min-w-0 truncate">Sell <strong>{hasSell ? fmt(row.sell_price, 8) : "--"}</strong></span>
                      <span className="min-w-0 truncate">RSI <strong>{rowRsiText}</strong></span>
                      <span className="min-w-0 truncate">{topicSourceLabel(row.ntfy_topic_source)}</span>
                      <span className="min-w-0 truncate">{nextLabel ? `Next ${nextLabel}` : statusLabel}</span>
                    </button>
                  );
                })
              )}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardContent className="space-y-4 p-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0 space-y-1">
              <Label>Active Token</Label>
              <div className="flex flex-wrap items-center gap-2">
                <span className="min-w-0 truncate text-lg font-semibold">{tokenName(activeToken)}</span>
                <span className="rounded border border-gray-200 px-2 py-0.5 font-mono text-xs text-gray-500 dark:border-gray-700">
                  {shortMint(activeTokenMint || outputMint)}
                </span>
              </div>
              <div className="flex flex-wrap gap-2 text-xs text-gray-500">
                <span>{tokens.length} token{tokens.length === 1 ? "" : "s"}</span>
                <span>{topicSourceLabel(activeTokenSummary?.ntfy_topic_source)}</span>
                <span>Buy {latestBuyPrice !== null ? fmt(latestBuyPrice, 8) : "--"}</span>
                <span>Sell {latestSellPrice !== null ? fmt(latestSellPrice, 8) : "--"}</span>
                <span>RSI {rsi !== null ? fmt(rsi, 2) : "--"}</span>
                <span>Price check {activeTokenSummary?.effective_check_interval ?? activeTokenSummary?.check_interval ?? checkInterval}s</span>
                <span>RSI check {activeTokenSummary?.effective_rsi_check_interval ?? activeTokenSummary?.rsi_check_interval ?? rsiCheckInterval}m</span>
              </div>
            </div>

            <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row">
              <Select
                value={activeTokenMint}
                onChange={(e) => switchActiveToken(e.target.value)}
                disabled={tokenSaving || tokens.length === 0}
                className="w-full sm:w-64"
              >
                {tokens.length === 0 ? (
                  <option value="">No token configured</option>
                ) : (
                  tokens.map((token) => (
                    <option key={token.mint} value={token.mint}>
                      {tokenName(token)} ({shortMint(token.mint)})
                    </option>
                  ))
                )}
              </Select>
              <Button size="sm" variant="outline" onClick={toggleTokenManager} className="whitespace-nowrap" aria-expanded={tokenManagerExpanded}>
                {tokenManagerExpanded ? "Hide" : "Manage"}
              </Button>
            </div>
          </div>

          {tokenManagerExpanded && (
            <div className="space-y-4">
              <div className="rounded border border-gray-200 dark:border-gray-700">
                <div className="flex flex-col gap-2 p-3 sm:flex-row sm:items-center sm:justify-between">
                  <div className="min-w-0">
                    <Label>Add Token</Label>
                  </div>
                  <Button size="sm" variant="outline" onClick={() => setAddTokenExpanded((value) => !value)} aria-expanded={addTokenExpanded}>
                    {addTokenExpanded ? "Close" : "+ Add Token"}
                  </Button>
                </div>

                {addTokenExpanded && (
                  <div className="grid grid-cols-1 gap-2 border-t border-gray-200 p-3 dark:border-gray-700 sm:grid-cols-[minmax(0,1.4fr)_minmax(8rem,12rem)_minmax(8rem,12rem)_auto]">
                    <Input
                      placeholder="Token mint"
                      value={newTokenMint}
                      onChange={(e) => setNewTokenMint(e.target.value)}
                      className="min-w-0"
                    />
                    <Input
                      placeholder="Name"
                      value={newTokenName}
                      onChange={(e) => setNewTokenName(e.target.value)}
                      className="min-w-0"
                    />
                    <Input
                      placeholder="ntfy topic (optional)"
                      value={newTokenTopic}
                      onChange={(e) => setNewTokenTopic(e.target.value)}
                      className="min-w-0"
                    />
                    <Button onClick={addTrackedToken} disabled={tokenSaving} className="whitespace-nowrap">
                      {tokenSaving ? "Checking" : "Add Token"}
                    </Button>
                  </div>
                )}
              </div>

              <div className="space-y-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <Label>Tracked Tokens</Label>
                  </div>
                  <span className="text-xs text-gray-500">{tokenRows.length} configured</span>
                </div>

                {tokenRows.length === 0 ? (
                  <div className="rounded border border-gray-200 p-3 text-sm text-gray-500 dark:border-gray-700">No tokens configured</div>
                ) : (
                  <div className="overflow-hidden rounded border border-gray-200 dark:border-gray-700">
                    {tokenRows.map((row) => {
                      const hasBuy = row.buy_price !== null && row.buy_price !== undefined;
                      const hasSell = row.sell_price !== null && row.sell_price !== undefined;
                      const hasRsi = row.rsi !== null && row.rsi !== undefined;
                      const rowRsiEnabled = row.rsi_enabled !== false;
                      const rowRsiText = !solanaTrackerFeaturesEnabled
                        ? "Off"
                        : !solanaTrackerApiConfigured
                          ? "--"
                          : !rowRsiEnabled
                            ? "Off"
                            : hasRsi
                              ? fmt(row.rsi, 2)
                              : "--";
                      const isEditing = editingTokenMint === row.mint;
                      const rowStatus = row.error ? "Issue" : row.active ? "Active" : row.rsi_status === "stale" ? "Stale" : "Watching";
                      const dotClass = row.error ? "bg-red-500" : row.active ? "bg-green-500" : row.rsi_status === "stale" ? "bg-yellow-500" : "bg-gray-400";
                      const statusClass = row.error
                        ? "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300"
                        : row.active
                          ? "bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300"
                          : row.rsi_status === "stale"
                            ? "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/40 dark:text-yellow-300"
                            : "bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300";
                      const draft = tokenDrafts[row.mint] || {
                        name: row.name || "",
                        ntfy_topic: row.ntfy_topic || "",
                        check_interval: intervalDraft(row.check_interval),
                        rsi_check_interval: intervalDraft(row.rsi_check_interval),
                        rsi_enabled: row.rsi_enabled !== false,
                      };
                      const topicTitle = row.ntfy_effective_topic
                        ? `${topicSourceLabel(row.ntfy_topic_source)}: ${row.ntfy_effective_topic}`
                        : "No ntfy topic configured";
                      const effectivePriceInterval = row.effective_check_interval ?? safe(draft.check_interval || checkInterval, checkInterval);
                      const effectiveRsiInterval = row.effective_rsi_check_interval ?? safe(draft.rsi_check_interval || rsiCheckInterval, rsiCheckInterval);
                      const lastCheckedLabel = formatTokenTime(row.last_checked);
                      const nextCheckLabel = formatTokenTime(row.next_check_at);
                      return (
                        <div key={row.mint} className="border-t border-gray-200 p-3 first:border-t-0 dark:border-gray-700">
                          <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                            <div className="min-w-0 space-y-2">
                              <div className="flex min-w-0 flex-wrap items-center gap-2">
                                <span className={`h-2.5 w-2.5 flex-shrink-0 rounded-full ${dotClass}`} title={row.error || row.rsi_status || undefined} />
                                <span className="truncate font-medium">{tokenName(row)}</span>
                                <span className={`rounded px-2 py-0.5 text-xs ${statusClass}`}>{rowStatus}</span>
                                <span className="min-w-0 truncate font-mono text-xs text-gray-500" title={row.mint}>{shortMint(row.mint)}</span>
                              </div>

                              <div className="grid grid-cols-3 gap-2 text-xs text-gray-500 sm:text-sm">
                                <span>Buy <strong className="text-gray-900 dark:text-gray-100">{hasBuy ? fmt(row.buy_price, 8) : "--"}</strong></span>
                                <span>Sell <strong className="text-gray-900 dark:text-gray-100">{hasSell ? fmt(row.sell_price, 8) : "--"}</strong></span>
                                <span>RSI <strong className="text-gray-900 dark:text-gray-100">{rowRsiText}</strong></span>
                              </div>

                              <div className="flex flex-wrap items-center gap-2 text-xs text-gray-500">
                                <span title={topicTitle}>{topicSourceLabel(row.ntfy_topic_source)}</span>
                                <span>Price check {effectivePriceInterval ? `${effectivePriceInterval}s` : "global"}</span>
                                {solanaTrackerFeaturesEnabled && rowRsiEnabled ? (
                                  <>
                                    <span>RSI check {effectiveRsiInterval ? `${effectiveRsiInterval}m` : "global"}</span>
                                    <span>{row.rsi_interval || "1s"} {row.rsi_reset_enabled ? "reset" : "one-shot"}</span>
                                  </>
                                ) : (
                                  <span>RSI off</span>
                                )}
                                <span>Alert reset {row.alert_reset_minutes ?? alertResetMinutes}m</span>
                                {lastCheckedLabel && <span title={row.last_checked || undefined}>Last {lastCheckedLabel}</span>}
                                {nextCheckLabel && <span title={row.next_check_at || undefined}>Next {nextCheckLabel}</span>}
                              </div>
                            </div>

                            <div className="flex flex-wrap items-center gap-2 md:justify-end">
                              {!row.active && (
                                <Button
                                  size="sm"
                                  variant="outline"
                                  disabled={tokenSaving}
                                  onClick={() => switchActiveToken(row.mint)}
                                >
                                  Use
                                </Button>
                              )}
                              <Button size="sm" variant="outline" onClick={() => openTokenEditor(row)} aria-expanded={isEditing}>
                                {isEditing ? "Close" : "Edit"}
                              </Button>
                            </div>
                          </div>

                          {isEditing && (
                            <div className="mt-3 rounded border border-gray-200 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-900/30">
                              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-5">
                                <Input
                                  aria-label={`${tokenName(row)} name`}
                                  placeholder="Name"
                                  value={draft.name}
                                  onChange={(e) => updateTokenDraft(row.mint, { name: e.target.value })}
                                  className="min-w-0"
                                />
                                <Input
                                  aria-label={`${tokenName(row)} ntfy topic`}
                                  placeholder="ntfy topic"
                                  value={draft.ntfy_topic}
                                  onChange={(e) => updateTokenDraft(row.mint, { ntfy_topic: e.target.value })}
                                  className="min-w-0"
                                  title={topicTitle}
                                />
                                <Input
                                  aria-label={`${tokenName(row)} price interval seconds`}
                                  type="number"
                                  min={5}
                                  placeholder="Price check (sec)"
                                  value={draft.check_interval}
                                  onChange={(e) => updateTokenDraft(row.mint, { check_interval: e.target.value })}
                                  className="min-w-0"
                                />
                                <Input
                                  aria-label={`${tokenName(row)} RSI interval minutes`}
                                  type="number"
                                  min={1}
                                  placeholder="RSI check (min)"
                                  value={draft.rsi_check_interval}
                                  onChange={(e) => updateTokenDraft(row.mint, { rsi_check_interval: e.target.value })}
                                  disabled={!draft.rsi_enabled}
                                  className="min-w-0"
                                />
                                <Select
                                  aria-label={`${tokenName(row)} RSI checks`}
                                  value={draft.rsi_enabled ? "true" : "false"}
                                  onChange={(e) => updateTokenDraft(row.mint, { rsi_enabled: e.target.value === "true" })}
                                  className="min-w-0"
                                >
                                  <option value="true">RSI on</option>
                                  <option value="false">RSI off</option>
                                </Select>
                              </div>
                              <div className="mt-3 flex flex-wrap items-center gap-2">
                                <Button
                                  size="sm"
                                  disabled={tokenSaving}
                                  onClick={() => saveTokenSettings(row)}
                                >
                                  Save
                                </Button>
                                <Button
                                  size="sm"
                                  variant="outline"
                                  disabled={tokenSaving || row.ntfy_topic_source === "disabled"}
                                  onClick={() => testTokenNotification(row.mint)}
                                  title={row.ntfy_topic_source === "disabled" ? "Add an ntfy topic before testing this token alert" : `Send a test ntfy alert for ${tokenName(row)}`}
                                  aria-label={row.ntfy_topic_source === "disabled" ? `Add an ntfy topic before testing alerts for ${tokenName(row)}` : `Send a test ntfy alert for ${tokenName(row)}`}
                                >
                                  Test alert
                                </Button>
                                <Button size="sm" variant="outline" disabled={tokenSaving} onClick={() => cancelTokenEdit(row)}>
                                  Cancel
                                </Button>
                                <Button
                                  size="sm"
                                  variant="outline"
                                  disabled={tokenSaving || tokens.length <= 1}
                                  onClick={() => removeTrackedToken(row.mint)}
                                >
                                  Remove
                                </Button>
                              </div>
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Real-time Prices */}
      <div className="grid grid-cols-2 gap-4">
        <Card>
          <CardContent className="p-4 text-center">
            <h2 className="text-xl font-semibold">Buy Price</h2>
            <p className="text-2xl font-bold text-green-600">
              {latestBuyPrice !== null ? fmt(latestBuyPrice, 8) : "--"}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4 text-center">
            <h2 className="text-xl font-semibold">Sell Price</h2>
            <p className="text-2xl font-bold text-red-500">
              {latestSellPrice !== null ? fmt(latestSellPrice, 8) : "--"}
            </p>
          </CardContent>
        </Card>
      </div>

      {rsiFeaturesVisible && (
        <>
      {/* RSI Info Card */}
      <Card>
        <CardContent className="p-4 text-center">
          <h2 className="inline-flex items-center justify-center gap-1 text-xl font-semibold">
            RSI ({rsiInterval})
            <span className={rsiMeta.color} title={rsiMeta.label} aria-label={rsiMeta.label}>
              <DotFilledIcon />
            </span>
          </h2>
          <p className="text-2xl font-bold" style={{ color: rsiColor(rsi) }}>
            {rsi !== null ? fmt(rsi, 2) : "--"}
          </p>
          <p className="italic text-sm text-gray-500">{rsiLabel(rsi) || rsiMeta.label}</p>
          {/* RSI timestamp intentionally omitted */}
        </CardContent>
      </Card>

      {/* RSI Alerts Card */}
      <Card>
        <CardContent className="space-y-2 p-4">
          <Label>RSI Alerts</Label>
          <div className="flex flex-wrap items-center gap-2">
            <Select
              value={newRsiDir}
              onChange={(e) => setNewRsiDir(e.target.value as "above" | "below")}
              className="flex-shrink-0"
            >
              <option value="above">Above</option>
              <option value="below">Below</option>
            </Select>
            <Input
              value={newRsiValue}
              onChange={(e) => setNewRsiValue(e.target.value)}
              placeholder="Threshold"
              className="flex-grow min-w-0"
              inputMode="decimal"
            />
            <Button
              onClick={async () => {
                const num = parseFloat(newRsiValue);
                if (!Number.isFinite(num) || num < 0) return toast.error("Invalid RSI value");
                await fetch("/api/rsi", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ values: [`${newRsiDir}:${num.toFixed(2)}`] }),
                });
                setNewRsiValue("");
                fetchRSI();
              }}
              className="flex-shrink-0"
            >
              Add
            </Button>
          </div>
          <ul className="list-disc pl-5">
            {Object.entries(rsiAlerts).map(([key, { triggered }]) => {
              const status = getRsiStatus(triggered, rsiResetEnabled);
              return (
                <li key={key} className="flex justify-between items-center gap-2">
                  <div>
                    <span>{key}</span> - <span className="font-semibold">{status}</span>
                  </div>
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={async () => {
                        await fetch("/api/rsi/reset-alert", {
                          method: "POST",
                          headers: { "Content-Type": "application/json" },
                          body: JSON.stringify({ key }),
                        });
                        fetchRSI();
                      }}
                    >
                      Reset
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={async () => {
                        await fetch("/api/rsi", {
                          method: "DELETE",
                          headers: { "Content-Type": "application/json" },
                          body: JSON.stringify({ key }),
                        });
                        fetchRSI();
                      }}
                    >
                      Remove
                    </Button>
                  </div>
                </li>
              );
            })}
          </ul>
        </CardContent>
      </Card>

      {/* RSI Interval Card */}
      <Card>
        <CardContent className="space-y-2 p-4">
          <Label>RSI Interval</Label>
          <div className="flex gap-2">
            <Select
              value={pendingInterval}
              onChange={(e) => setPendingInterval(e.target.value)}
              className="border p-1 rounded"
            >
              {["1s", "1m", "5m", "15m", "1h", "4h"].map((opt) => (
                <option key={opt} value={opt}>
                  {opt}
                </option>
              ))}
            </Select>
            <Button
              onClick={async () => {
                setRsi(null);
                await fetch("/api/rsi/interval", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ interval: pendingInterval }),
                });
                setRsiInterval(pendingInterval);
                fetchRSI();
              }}
            >
              Update
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* RSI Reset Mode Card */}
      <Card>
        <CardContent className="space-y-2 p-4">
          <Label>RSI Reset Mode</Label>
          <div className="flex gap-2">
            <Select
              value={rsiResetEnabled ? "true" : "false"}
              onChange={(e) => setRsiResetEnabled(e.target.value === "true")}
              className="border p-1 rounded"
            >
              <option value="true">Re-trigger on cross-back</option>
              <option value="false">One-time only</option>
            </Select>
            <Button
              onClick={async () => {
                await fetch("/api/rsi/reset-mode", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ enabled: rsiResetEnabled }),
                });
                fetchRSI();
              }}
            >
              Update
            </Button>
          </div>
        </CardContent>
      </Card>

        </>
      )}

      {/* Simulated USD */}
      <Card>
        <CardContent className="space-y-2 p-4">
          <Label>Simulated USD Amount</Label>
          <div className="flex gap-2">
            <Input
              type="number"
              value={usdAmount}
              onChange={(e) => setUsdAmount(parseFloat(e.target.value))}
              inputMode="decimal"
            />
            <Button onClick={applyUsdAmount}>Update</Button>
          </div>
        </CardContent>
      </Card>

      {/* Alert Reset Minutes */}
      <Card>
        <CardContent className="space-y-2 p-4">
          <Label>Alert Reset Minutes (0 disables reset)</Label>
          <div className="flex gap-2">
            <Input
              type="number"
              value={alertResetMinutes}
              onChange={(e) => setAlertResetMinutes(parseInt(e.target.value))}
              inputMode="numeric"
            />
            <Button onClick={applyResetMinutes}>Update</Button>
          </div>
        </CardContent>
      </Card>

      {/* Alerts */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {[["Buy", buyAlerts, lastBuyTimes], ["Sell", sellAlerts, lastSellTimes]].map(
          ([label, alerts, times]: any[]) => (
            <Card key={label}>
              <CardContent className="space-y-2 p-4">
                <Label>{label} Alerts</Label>
                <div className="flex gap-2">
                  <Input
                    value={label === "Buy" ? newBuy : newSell}
                    onChange={(e) => (label === "Buy" ? setNewBuy(e.target.value) : setNewSell(e.target.value))}
                    inputMode="decimal"
                  />
                  <Button onClick={() => addAlert(label.toLowerCase(), label === "Buy" ? newBuy : newSell)}>
                    Add
                  </Button>
                </div>
                <ul className="list-disc pl-5">
                  {alerts.map((val: number, i: number) => {
                    const key = safe(val).toFixed(8);
                    const lastTime = (times as any)[key];
                    const status = getAlertStatusWithCountdown(lastTime, alertResetMinutes);
                    return (
                      <li key={`${val}-${i}`} className="flex justify-between items-center gap-2">
                        <div className="flex flex-col">
                          <span>
                            {val} - <span className="font-semibold">{status}</span>
                          </span>
                          {lastTime && (
                            <span className="text-xs text-gray-500">
                              Last triggered: {new Date(lastTime).toLocaleString()}
                            </span>
                          )}
                        </div>
                        <div className="flex gap-2">
                          <Button size="sm" variant="outline" onClick={() => resetAlert(label.toLowerCase(), val)}>
                            Reset
                          </Button>
                          <Button size="sm" variant="outline" onClick={() => removeAlert(label.toLowerCase(), val)}>
                            Remove
                          </Button>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              </CardContent>
            </Card>
          )
        )}
      </div>

      {/* Chart */}
      <Card>
        <CardContent className="space-y-3 p-4">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
            <h2 className="text-lg font-bold">Price Chart</h2>
            <div className="flex flex-wrap gap-2">
              {CHART_WINDOWS.map((hours) => (
                <Button
                  key={hours}
                  size="sm"
                  variant={chartWindowHours === hours ? "default" : "outline"}
                  onClick={() => updateChartWindow(hours)}
                >
                  {hours}h
                </Button>
              ))}
            </div>
          </div>
          <div className="text-xs text-gray-500">
            Showing {filteredHistory.length} of {history.length} saved points for {tokenName(activeToken)}.
          </div>
          <div style={{ height: 320 }}>
            <Line data={data} options={options} />
          </div>
        </CardContent>
      </Card>

      {solanaTrackerEnabled && (
      <>
      {/* Wallets */}
      <Card>
        <CardContent className="space-y-5 p-4">
          <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0">
              <Label>Wallets</Label>
              <div className="truncate text-xs text-gray-500">
                {wallets.length} wallet{wallets.length === 1 ? "" : "s"} for {tokenName(activeToken)}
              </div>
            </div>
            <span className="text-xs text-gray-500 sm:text-right">
              Latest update: {pnlData.aggregated?.lastFetchedAt || "--"}
              {pnlData.aggregated?.failedWallets?.length > 0 && (
                <span className="text-orange-500">
                  {` (Failed: ${pnlData.aggregated.failedWallets
                    .map((w: string) => w.slice(0, 8) + "...")
                    .slice(0, 3)
                    .join(", ")})`}
                </span>
              )}
            </span>
          </div>

          <section className="space-y-2 border-t border-gray-200 pt-4 dark:border-gray-700">
            <div className="text-sm font-medium text-gray-700 dark:text-gray-300">Add wallet</div>
            <div className="flex flex-col gap-2 sm:flex-row">
              <Input
                placeholder="Wallet address"
                value={newWallet}
                onChange={(e) => setNewWallet(e.target.value)}
                disabled={pnlLoading}
                className="flex-grow"
              />
              <Button onClick={addWallet} disabled={pnlLoading} className="w-full whitespace-nowrap sm:w-auto">
                Add
              </Button>
            </div>
          </section>

          {walletCopySources.length > 0 && (
            <section className="space-y-2 border-t border-gray-200 pt-4 dark:border-gray-700">
              <div className="text-sm font-medium text-gray-700 dark:text-gray-300">Reuse wallets</div>
              <div className="flex flex-col gap-2 sm:flex-row">
                <Select
                  value={walletCopySourceMint || walletCopySources[0]?.mint || ""}
                  onChange={(e) => setWalletCopySourceMint(e.target.value)}
                  disabled={pnlLoading}
                  className="w-full"
                >
                  {walletCopySources.map((token) => (
                    <option key={token.mint} value={token.mint}>
                      {tokenName(token)} ({(token.wallet_addresses || []).length})
                    </option>
                  ))}
                </Select>
                <Button variant="outline" onClick={copyWalletsFromToken} disabled={pnlLoading} className="w-full whitespace-nowrap sm:w-auto">
                  Copy
                </Button>
              </div>
            </section>
          )}

          <section className="space-y-3 border-t border-gray-200 pt-4 dark:border-gray-700">
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <div className="text-sm font-medium text-gray-700 dark:text-gray-300">Current wallets</div>
                <div className="text-xs text-gray-500">Tracked only for {tokenName(activeToken)}</div>
              </div>
              <Button onClick={fetchPnl} disabled={pnlLoading || wallets.length === 0} className="w-full whitespace-nowrap sm:w-auto">
                {walletRefreshLabel}
              </Button>
            </div>

            {wallets.length > 0 ? (
              <div className="flex flex-wrap gap-2">
                {wallets.map((wallet) => (
                  <span
                    key={wallet}
                    className="inline-flex max-w-full items-center gap-2 rounded border border-gray-200 px-2 py-1 text-xs dark:border-gray-700"
                    title={wallet}
                  >
                    <span className="truncate font-mono">{shortMint(wallet)}</span>
                    <button
                      type="button"
                      className="text-gray-500 hover:text-red-500 disabled:opacity-50"
                      disabled={pnlLoading}
                      onClick={() => removeWallet(wallet)}
                      aria-label={`Remove wallet ${wallet}`}
                    >
                      Remove
                    </button>
                  </span>
                ))}
              </div>
            ) : (
              <div className="text-sm text-gray-500">No wallets tracked for this token.</div>
            )}
          </section>

          <section className="space-y-3 border-t border-gray-200 pt-4 dark:border-gray-700">
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <div className="text-sm font-medium text-gray-700 dark:text-gray-300">Wallet info</div>
                <div className="text-xs text-gray-500">PnL and holdings for the selected source</div>
              </div>
              {solanaTrackerEnabled && wallets.length > 0 && (
                <Select
                  value={selectedWallet}
                  onChange={(e) => {
                    setSelectedWallet(e.target.value);
                    setStoredValue(scopedPreferenceKey("selectedWallet", activeTokenMint), e.target.value);
                  }}
                  className="w-full sm:w-48"
                >
                  <option value="all">All wallets</option>
                  {wallets.map((w) => (
                    <option key={w} value={w}>
                      {shortMint(w)}
                    </option>
                  ))}
                </Select>
              )}
            </div>

            <div className="overflow-x-auto">
              <ul className="min-w-full space-y-2">
                {wallets.length === 0 && (
                  <li className="list-none text-sm text-gray-500">Add a wallet to load wallet info.</li>
                )}
                {wallets.length > 0 && selectedWallet === "all" && !pnlData.aggregated && (
                  <li className="list-none text-sm text-gray-500">Click update to load wallet info for all wallets.</li>
                )}
                {wallets.length > 0 && selectedWallet !== "all" && !pnlData.individual[selectedWallet] && (
                  <li className="list-none text-sm text-gray-500">Click update to load wallet info for this wallet.</li>
                )}
                {(
                  selectedWallet === "all"
                    ? pnlData.aggregated
                      ? [{ key: "Aggregated", ...pnlData.aggregated }]
                      : []
                    : pnlData.individual[selectedWallet]
                    ? [{ key: selectedWallet, ...pnlData.individual[selectedWallet] }]
                    : []
                ).map((item: any) => (
                  <li key={item.key} className="space-y-2 rounded border border-gray-200 p-3 dark:border-gray-700">
                    <div className="flex items-center justify-between gap-3">
                      <strong className="min-w-0 truncate" title={item.key}>{item.key === "Aggregated" ? item.key : shortMint(item.key)}</strong>
                      <span className="flex-shrink-0 text-xs text-gray-500">{item.lastFetchedAt}</span>
                    </div>
                    {pnlStatusText(item) && (
                      <div className="rounded border border-amber-200 bg-amber-50 px-2 py-1 text-xs text-amber-700 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-300">
                        {pnlStatusText(item)}
                      </div>
                    )}
                    {item.loading ? (
                      <div className="text-sm italic text-gray-500">Click update to load</div>
                    ) : (
                      <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-sm">
                        <span className="text-gray-500">Holding</span> <span>{fmt(item.holding, 4)}</span>
                        <span className="text-gray-500">Realized</span> <span>{fmt(item.realized, 4)}</span>
                        <span className="text-gray-500">Unrealized</span> <span>{fmt(item.unrealized, 4)}</span>
                        <span className="text-gray-500">Current value</span> <span>{fmt(item.current_value, 4)}</span>
                        <span className="text-gray-500">Cost basis</span> <span>{fmt(item.cost_basis, 6)}</span>
                        <span className="text-gray-500">Last trade</span> <span>{item.last_trade_time || "--"}</span>
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          </section>
        </CardContent>
      </Card>
      </>
      )}
      {/* Sell % Simulator */}
      {solanaTrackerEnabled && wallets.length > 0 && (
        <Card>
          <CardContent className="space-y-4 p-4">
            <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
              <Label>Sell % Simulator</Label>
              <span className="truncate text-xs text-gray-500 sm:text-right" title={selectedWallet === "all" ? "Aggregated" : selectedWallet || ""}>
                Source: {selectedWallet === "all" ? "Aggregated" : selectedWallet || "--"}
              </span>
            </div>

            {/* Slider + number input + quick chips */}
            <div className="space-y-2">
              <div className="flex items-center gap-3">
                <input
                  type="range"
                  min={0}
                  max={100}
                  step={1}
                  value={sellPercent}
                  onChange={(e) => updateSellPercent(parseFloat(e.target.value))}
                  className="w-full"
                />
                <div className="flex items-center gap-2 w-32">
                  <Input
                    type="number"
                    min={0}
                    max={100}
                    step={0.1}
                    value={sellPercent}
                    onChange={(e) => {
                      const n = parseFloat(e.target.value);
                      updateSellPercent(Number.isFinite(n) ? n : 0);
                    }}
                    inputMode="decimal"
                  />
                  <span className="text-sm text-gray-500">%</span>
                </div>
              </div>

              <div className="flex flex-wrap gap-2">
                {[25, 33, 50, 66, 75, 100].map((p) => (
                  <Button key={p} variant="outline" size="sm" onClick={() => updateSellPercent(p)}>
                    {p}%
                  </Button>
                ))}
              </div>
            </div>

            {/* Computation + display */}
            {(() => {
              const src: any = selectedWallet === "all" ? pnlData.aggregated : pnlData.individual[selectedWallet];
              const holding = safe(src?.holding);
              if (!src || holding <= 0) {
                return <div className="text-sm text-gray-500">No holdings found for the selected source.</div>;
              }

              const pct = sellPercent / 100;
              const currentValue = safe(src.current_value);
              const unrealized = safe(src.unrealized);
              const costBasis = safe(src.cost_basis);
              const pnlLimited = ["holding_only", "indexing", "partial"].includes(src?.pnl_status);

              const pricePerToken = holding > 0 ? currentValue / holding : 0;
              const tokensToSell = holding * pct;
              const proceeds = currentValue * pct;
              const principalValue = holding * costBasis; // cost of current position
              const principalPart = principalValue * pct;
              const profitPart = unrealized * pct;

              const splitPrincipalPct = proceeds > 0 ? (principalPart / proceeds) * 100 : 0;
              const splitProfitPct = proceeds > 0 ? (profitPart / proceeds) * 100 : 0;

              return (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm">
                  <div className="space-y-1">
                    <div className="flex justify-between gap-3">
                      <span>Percent to sell:</span>
                      <span className="font-semibold">{sellPercent.toFixed(1)}%</span>
                    </div>
                    <div className="flex justify-between gap-3">
                      <span>Current token price (est):</span>
                      <span className="font-semibold">{fmt(pricePerToken, 6)}</span>
                    </div>
                    <div className="flex justify-between gap-3">
                      <span>Tokens to sell:</span>
                      <span className="font-semibold">{fmt(tokensToSell, 6)}</span>
                    </div>
                    <div className="flex justify-between gap-3">
                      <span>Expected proceeds:</span>
                      <span className="font-semibold">{fmt(proceeds, 2)}</span>
                    </div>
                  </div>

                  <div className="space-y-1">
                    {pnlLimited ? (
                      <div className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-300">
                        Holding value is available, but P&L split is not indexed yet.
                      </div>
                    ) : (
                      <>
                        <div className="flex justify-between gap-3">
                          <span>From principal:</span>
                          <span className="font-semibold">{fmt(principalPart, 2)}</span>
                        </div>
                        <div className={`flex justify-between gap-3 ${profitPart >= 0 ? "" : "text-red-600"}`}>
                          <span>{profitPart >= 0 ? "From unrealized profit:" : "Unrealized loss portion:"}</span>
                          <span className="font-semibold">{fmt(profitPart, 2)}</span>
                        </div>
                        <div className="flex justify-between gap-3 text-xs text-gray-500">
                          <span>Split ratio:</span>
                          <span>
                            {fmt(splitPrincipalPct, 1)}% principal | {fmt(splitProfitPct, 1)}%{" "}
                            {profitPart >= 0 ? "profit" : "loss"}
                          </span>
                        </div>
                        <div className="flex justify-between gap-3 text-xs text-gray-500 pt-1">
                          <span>Position principal (held):</span>
                          <span>{fmt(principalValue, 2)}</span>
                        </div>
                      </>
                    )}
                  </div>

                  <div className="text-xs text-gray-500 sm:col-span-2">
                    Note: Proceeds use current price; actual execution may vary due to price impact and slippage.
                  </div>
                </div>
              );
            })()}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
