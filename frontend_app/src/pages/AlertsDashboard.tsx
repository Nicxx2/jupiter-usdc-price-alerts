import { useEffect, useState, useRef } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input, Select } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "sonner";
import { Line } from "react-chartjs-2";
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

// ─── Small utils (NaN-safe + formatting) ─────────────────────────
const safe = (n: any, d = 0) => {
  const v = Number(n);
  return Number.isFinite(v) ? v : d;
};
const fmt = (n: any, digits = 2) => safe(n).toFixed(digits);

// ─── RSI helpers ─────────────────────────────────────────
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
    return resetEnabled ? "🟡 Waiting to Reactivate" : "🔴 Inactive";
  }
  return "🟢 Active";
}

function getAlertStatusWithCountdown(lastTime: string | undefined, resetMinutes: number): string {
  if (!lastTime) return "🟢 Active";
  try {
    const last = new Date(lastTime);
    if (isNaN(last.getTime())) return "🟢 Active";
    const now = new Date();
    const diff = now.getTime() - last.getTime();
    const minutesSince = diff / 60000;
    if (resetMinutes === 0) return minutesSince > 0 ? "🔴 Inactive" : "🟢 Active";
    if (minutesSince >= resetMinutes) return "🟢 Active";
    const remainingMs = alertResetMinutesToMs(resetMinutes) - diff;
    const remainingMin = Math.floor(remainingMs / 60000);
    const remainingSec = Math.floor((remainingMs % 60000) / 1000);
    return `🟡 Cooldown — ready in ${String(remainingMin).padStart(2, "0")}:${String(remainingSec).padStart(2, "0")}`;
  } catch {
    return "🟢 Active";
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
  const [latestBuyPrice, setLatestBuyPrice] = useState<number | null>(null);
  const [latestSellPrice, setLatestSellPrice] = useState<number | null>(null);

  // ─── Wallet Tracking State & Helpers ─────────────────────────
  const [wallets, setWallets] = useState<string[]>([]);
  const [walletRefresh, setWalletRefresh] = useState<number>(60); // kept in state but not displayed
  const [outputMint, setOutputMint] = useState<string>("");
  const [newWallet, setNewWallet] = useState("");
  const [selectedWallet, setSelectedWallet] = useState("all");
  const [pnlData, setPnlData] = useState<{ individual: Record<string, any>; aggregated?: any }>({ individual: {} });

  const delay = (ms: number) => new Promise((res) => setTimeout(res, ms));

  const [rsi, setRsi] = useState<number | null>(null);
  const [rsiAlerts, setRsiAlerts] = useState<Record<string, { triggered: boolean }>>({});
  const [rsiResetEnabled, setRsiResetEnabled] = useState(false);
  const [rsiInterval, setRsiInterval] = useState("1s");
  const [pendingInterval, setPendingInterval] = useState("1s");
  const [newRsiDir, setNewRsiDir] = useState<"above" | "below">("above");
  const [newRsiValue, setNewRsiValue] = useState("");

  const lastPnlFetch = useRef<number>(Date.now());

  // Sell % simulator state
  const [sellPercent, setSellPercent] = useState<number>(25);

  // ─── Dark mode detection for chart theming ───────────────────────
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
        setRsi(safe(data.latest_rsi, null) as any);
        // timestamp intentionally ignored/hidden in v2.3
        setRsiAlerts(data.alerts || {});
        setRsiInterval(data.interval || "1s");
        setPendingInterval(data.interval || "1s");
        setRsiResetEnabled(!!data.reset_enabled);
      })
      .catch(() => toast.error("Failed to load RSI"));
  };

  const fetchState = async () => {
    try {
      const stored = localStorage.getItem("selectedWallet");
      const res = await fetch("/api/state");
      const data = await res.json();

      setUsdAmount(safe(data.usd_amount, 100));
      setBuyAlerts(data.buy_alerts || []);
      setSellAlerts(data.sell_alerts || []);
      setLastBuyTimes(data.last_triggered_buy || {});
      setLastSellTimes(data.last_triggered_sell || {});

      setWallets(data.wallet_addresses || []);
      setWalletRefresh(safe(data.wallet_refresh_minutes, 60));
      setOutputMint(data.output_mint || "");
      setSelectedWallet(stored || "all");
      setAlertResetMinutes(safe(data.alert_reset_minutes, 0));
      setHistory(data.latest_prices || []);
      const last = data.latest_prices?.at?.(-1);
      setLatestBuyPrice(last?.buy_price ?? null);
      setLatestSellPrice(last?.sell_price ?? null);
    } catch {
      toast.error("Failed to load state");
    }
  };

  // ─── Fetch PnL for each wallet (with rate-limit & retry) ─────────────────
  async function fetchPnl() {
    if (!wallets.length || !outputMint) {
      setPnlData({ individual: {}, aggregated: undefined });
      lastPnlFetch.current = Date.now();
      return;
    }

    const prev = pnlData.individual;
    const tokenMint = outputMint;
    const indiv: Record<string, any> = {};
    const failed: string[] = [];

    const fetchTime = new Date().toLocaleString();

    // First pass
    for (const w of wallets) {
      try {
        const res = await fetch(`/api/pnl/${w}/${tokenMint}`);
        if (!res.ok) throw new Error();
        const data = await res.json();
        indiv[w] = { ...data, lastFetchedAt: fetchTime };
      } catch {
        failed.push(w);
        indiv[w] = { ...(prev[w] || {}) };
      }
      await delay(1100);
    }

    // Retry failures
    if (failed.length) {
      await delay(2000);
      const still: string[] = [];
      for (const w of failed) {
        try {
          const res = await fetch(`/api/pnl/${w}/${tokenMint}`);
          if (!res.ok) throw new Error();
          const retryData = await res.json();
          indiv[w] = { ...retryData, lastFetchedAt: fetchTime };
        } catch {
          still.push(w);
          indiv[w] = { ...(prev[w] || {}) };
        }
        await delay(1100);
      }
      if (still.length) toast.error(`Failed to load PnL for: ${still.join(", ")}`);
    }

    // Compute aggregate across all wallets
    let agg: any = {
      holding: 0,
      realized: 0,
      unrealized: 0,
      current_value: 0,
      cost_basis: 0,
      last_trade_time: null as string | null,
      lastFetchedAt: fetchTime,
      staleCount: 0,
    };
    let weightedCost = 0;
    let maxTs = 0;

    for (const [_w, d] of Object.entries(indiv)) {
      const h = safe((d as any).holding);
      const cv = safe((d as any).current_value);
      const u = safe((d as any).unrealized);
      const r = safe((d as any).realized);
      const cb = safe((d as any).cost_basis);

      agg.holding += h;
      agg.realized += r;
      agg.unrealized += u;
      agg.current_value += cv;
      weightedCost += cb * h;

      const t = Date.parse((d as any).last_trade_time || "");
      if (!isNaN(t) && t > maxTs) maxTs = t;
    }

    if (agg.holding > 0) {
      agg.cost_basis = weightedCost / agg.holding;
    }
    agg.last_trade_time = maxTs ? new Date(maxTs).toLocaleString() : null;

    // Track which wallets failed or have stale data
    const failedWallets: string[] = [];
    for (const [wallet, data] of Object.entries(indiv)) {
      const isStale = (data as any).lastFetchedAt !== fetchTime;
      const hasNoData =
        safe((data as any).holding) === 0 &&
        safe((data as any).realized) === 0 &&
        safe((data as any).unrealized) === 0 &&
        safe((data as any).cost_basis) === 0;

      if (isStale || (hasNoData && !(data as any).last_trade_time)) {
        failedWallets.push(wallet);
      }
    }
    agg.staleCount = failedWallets.length;
    agg.failedWallets = failedWallets;

    setPnlData({
      individual: indiv,
      aggregated: agg,
    });

    await fetch("/api/pnl", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ individual: indiv, aggregated: agg }),
    });

    lastPnlFetch.current = Date.now();
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

  // Load persisted PnL from the server
  useEffect(() => {
    fetch("/api/pnl")
      .then((res) => res.json())
      .then((serverPnl) => setPnlData(serverPnl))
      .catch(() => {
        // First-load failure is okay; next fetchPnl will fill
      });
  }, []);

  // Countdown ticker for the “🔁” timers
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

  const data = {
    labels: history.map((h) => h.timestamp || h.time || "-"),
    datasets: [
      {
        label: "Buy Price",
        data: history.map((h) => safe(h.buy_price ?? h.buy, 0)),
        borderColor: "#4ade80",
        fill: false,
      },
      {
        label: "Sell Price",
        data: history.map((h) => safe(h.sell_price ?? h.sell, 0)),
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
        },
        grid: {
          color: isDark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.06)",
        },
      },
      y: {
        ticks: {
          color: isDark ? "#d1d5db" : "#374151",
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

  return (
    <div className="relative p-6 max-w-4xl mx-auto space-y-6">
      <div className="absolute top-2 left-2 text-xs text-gray-500">v2.4</div>

      <h1 className="text-3xl font-bold mb-4">Jupiter USDC Price Alerts</h1>

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

      {/* ─── RSI Info Card ────────────────────────────────── */}
      <Card>
        <CardContent className="p-4 text-center">
          <h2 className="text-xl font-semibold">RSI ({rsiInterval})</h2>
          <p className="text-2xl font-bold" style={{ color: rsiColor(rsi) }}>
            {rsi !== null ? fmt(rsi, 2) : "--"}
          </p>
          <p className="italic text-sm text-gray-500">{rsiLabel(rsi)}</p>
          {/* RSI timestamp intentionally omitted in v2.3 */}
        </CardContent>
      </Card>

      {/* ─── RSI Alerts Card ───────────────────────────────── */}
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
                    <span>{key}</span> — <span className="font-semibold">{status}</span>
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

      {/* ─── RSI Interval Card ────────────────────────────── */}
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

      {/* ─── RSI Reset Mode Card ───────────────────────────── */}
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
                            {val} — <span className="font-semibold">{status}</span>
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
        <CardContent className="p-4">
          <h2 className="text-lg font-bold mb-2">Price Chart</h2>
          <div style={{ height: 320 }}>
            <Line data={data} options={options} />
          </div>
        </CardContent>
      </Card>

      {/* Wallets */}
      {wallets.length > 0 && (
        <Card>
          <CardContent className="space-y-4 p-4">
            <div className="flex justify-between items-center mb-2">
              <Label>Wallet Info</Label>
              <span className="text-sm text-gray-500">
                Latest update: {pnlData.aggregated?.lastFetchedAt || "—"}
                {pnlData.aggregated?.failedWallets?.length > 0 && (
                  <span className="text-orange-500">
                    {` (Failed: ${pnlData.aggregated.failedWallets
                      .map((w: string) => w.slice(0, 8) + "...")
                      .slice(0, 3)
                      .join(", ")})`}
                  </span>
                )}
                {/* refresh hint intentionally removed */}
              </span>
            </div>

            <div className="space-y-3">
              {/* Add wallet */}
              <div className="flex flex-col sm:flex-row gap-2">
                <Button
                  onClick={async () => {
                    if (!newWallet) return toast.error("Enter an address");
                    const res = await fetch("/api/wallets", {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ values: [newWallet] }),
                    });

                    if (res.ok) {
                      toast.success("Wallet added");
                      setNewWallet("");
                      fetchState();

                      setPnlData((prev) => ({
                        ...prev,
                        individual: {
                          ...prev.individual,
                          [newWallet]: { loading: true },
                        },
                      }));
                    }
                  }}
                  className="w-full sm:w-auto order-2 sm:order-1"
                >
                  Add
                </Button>
                <Input
                  placeholder="New wallet address"
                  value={newWallet}
                  onChange={(e) => setNewWallet(e.target.value)}
                  className="flex-grow order-1 sm:order-2"
                />
              </div>

              {/* Wallet selector */}
            <Select
                value={selectedWallet}
                onChange={(e) => {
                  setSelectedWallet(e.target.value);
                  localStorage.setItem("selectedWallet", e.target.value);
                }}
                className="border p-1 rounded w-full"
              >
                <option value="all">All</option>
                {wallets.slice(-5).map((w) => (
                  <option key={w} value={w}>
                    {w}
                  </option>
                ))}
            </Select>
            </div>

            <div className="overflow-x-auto">
              <ul className="min-w-full list-disc pl-5 space-y-1">
                {(
                  selectedWallet === "all"
                    ? pnlData.aggregated
                      ? [{ key: "Aggregated", ...pnlData.aggregated }]
                      : []
                    : pnlData.individual[selectedWallet]
                    ? [{ key: selectedWallet, ...pnlData.individual[selectedWallet] }]
                    : []
                ).map((item: any) => (
                  <li key={item.key} className="space-y-1">
                    <div className="flex justify-between items-center">
                      <strong>{item.key}</strong>
                      <span className="text-xs text-gray-500">{item.lastFetchedAt}</span>
                    </div>
                    {item.loading ? (
                      <div className="italic text-gray-500">click “Update All” to load</div>
                    ) : (
                      <div className="grid grid-cols-2 gap-2 text-sm">
                        <span>Holding:</span> <span>{fmt(item.holding, 4)}</span>
                        <span>Realized:</span> <span>{fmt(item.realized, 4)}</span>
                        <span>Unrealized:</span> <span>{fmt(item.unrealized, 4)}</span>
                        <span>Current Value:</span> <span>{fmt(item.current_value, 4)}</span>
                        <span>Cost Basis:</span> <span>{fmt(item.cost_basis, 6)}</span>
                        <span>Last Trade:</span> <span>{item.last_trade_time || "--"}</span>
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </div>

            <Button onClick={fetchPnl}>Update All</Button>
          </CardContent>
        </Card>
      )}

      {/* Sell % Simulator */}
      {wallets.length > 0 && (
        <Card>
          <CardContent className="space-y-4 p-4">
            <div className="flex justify-between items-center">
              <Label>Sell % Simulator</Label>
              <span className="text-xs text-gray-500">
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
                  onChange={(e) => setSellPercent(parseFloat(e.target.value))}
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
                      setSellPercent(Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : 0);
                    }}
                    inputMode="decimal"
                  />
                  <span className="text-sm text-gray-500">%</span>
                </div>
              </div>

              <div className="flex flex-wrap gap-2">
                {[25, 33, 50, 66, 75, 100].map((p) => (
                  <Button key={p} variant="outline" size="sm" onClick={() => setSellPercent(p)}>
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
                    <div className="flex justify-between">
                      <span>Percent to sell:</span>
                      <span className="font-semibold">{sellPercent.toFixed(1)}%</span>
                    </div>
                    <div className="flex justify-between">
                      <span>Current token price (est):</span>
                      <span className="font-semibold">{fmt(pricePerToken, 6)}</span>
                    </div>
                    <div className="flex justify-between">
                      <span>Tokens to sell:</span>
                      <span className="font-semibold">{fmt(tokensToSell, 6)}</span>
                    </div>
                    <div className="flex justify-between">
                      <span>Expected proceeds:</span>
                      <span className="font-semibold">{fmt(proceeds, 2)}</span>
                    </div>
                  </div>

                  <div className="space-y-1">
                    <div className="flex justify-between">
                      <span>From principal:</span>
                      <span className="font-semibold">{fmt(principalPart, 2)}</span>
                    </div>
                    <div className={`flex justify-between ${profitPart >= 0 ? "" : "text-red-600"}`}>
                      <span>{profitPart >= 0 ? "From unrealized profit:" : "Unrealized loss portion:"}</span>
                      <span className="font-semibold">{fmt(profitPart, 2)}</span>
                    </div>
                    <div className="flex justify-between text-xs text-gray-500">
                      <span>Split ratio:</span>
                      <span>
                        {fmt(splitPrincipalPct, 1)}% principal • {fmt(splitProfitPct, 1)}%{" "}
                        {profitPart >= 0 ? "profit" : "loss"}
                      </span>
                    </div>
                    <div className="flex justify-between text-xs text-gray-500 pt-1">
                      <span>Position principal (held):</span>
                      <span>{fmt(principalValue, 2)}</span>
                    </div>
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
