import { useEffect, useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
    const remainingMs = resetMinutes * 60 * 1000 - diff;
    const remainingMin = Math.floor(remainingMs / 60000);
    const remainingSec = Math.floor((remainingMs % 60000) / 1000);
    return `🟡 Cooldown — ready in ${String(remainingMin).padStart(2, "0")}:${String(remainingSec).padStart(2, "0")}`;
  } catch {
    return "🟢 Active";
  }
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
  const [rsi, setRsi] = useState<number | null>(null);
  const [rsiTime, setRsiTime] = useState<string>("");
  const [rsiAlerts, setRsiAlerts] = useState<Record<string, { triggered: boolean }>>({});
  const [rsiResetEnabled, setRsiResetEnabled] = useState(false);
  // the *applied* interval (shown in the top card)
  const [rsiInterval, setRsiInterval] = useState("1s");
  // the *pending* interval (driven by the dropdown)
  const [pendingInterval, setPendingInterval] = useState("1s");
  // new state for RSI form
  const [newRsiDir,  setNewRsiDir]  = useState<"above"|"below">("above");
  const [newRsiValue, setNewRsiValue] = useState("");

  const fetchRSI = () => {
    fetch("/api/rsi")
    .then((res) => res.json())
    .then((data) => {
        setRsi(data.latest_rsi);
        setRsiTime(data.timestamp);
        setRsiAlerts(data.alerts || {});
        setRsiInterval(data.interval || "1s");
        setPendingInterval(data.interval || "1s");
        setRsiResetEnabled(data.reset_enabled || false);
      })
      .catch(() => toast.error("Failed to load RSI"));
  };

  const fetchState = () => {
    fetch("/api/state")
      .then((res) => res.json())
      .then((data) => {
        setUsdAmount(data.usd_amount || 100);
        setBuyAlerts(data.buy_alerts || []);
        setSellAlerts(data.sell_alerts || []);
        setLastBuyTimes(data.last_triggered_buy || {});
        setLastSellTimes(data.last_triggered_sell || {});
        setAlertResetMinutes(data.alert_reset_minutes || 0);
        setHistory(data.latest_prices || []);
        const last = data.latest_prices?.at(-1);
        setLatestBuyPrice(last?.buy_price ?? null);
        setLatestSellPrice(last?.sell_price ?? null);
      })
      .catch(() => toast.error("Failed to load state"));
  };

  useEffect(() => {
    fetchState();
    fetchRSI();
    const rsiIntervalTimer = setInterval(fetchRSI, 60000);
    const interval = setInterval(fetchState, 60000);
    const refreshCountdown = setInterval(() => {
      setLastBuyTimes((prev) => ({ ...prev }));
      setLastSellTimes((prev) => ({ ...prev }));
    }, 1000);

    return () => {
      clearInterval(rsiIntervalTimer);
      clearInterval(interval);
      clearInterval(refreshCountdown);
    };
  }, []);

  const applyUsdAmount = async () => {
    const amount = parseFloat(usdAmount.toString());
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
        data: history.map((h) => h.buy_price || h.buy || 0),
        borderColor: "#4ade80",
        fill: false,
      },
      {
        label: "Sell Price",
        data: history.map((h) => h.sell_price || h.sell || 0),
        borderColor: "#f87171",
        fill: false,
      },
    ],
  };

  return (
    <div className="p-6 max-w-4xl mx-auto space-y-6">
      <h1 className="text-3xl font-bold mb-4">Jupiter USDC Price Alerts</h1>

      {/* Real-time Prices */}
      <div className="grid grid-cols-2 gap-4">
        <Card>
          <CardContent className="p-4 text-center">
            <h2 className="text-xl font-semibold">Buy Price</h2>
            <p className="text-2xl font-bold text-green-600">
              {latestBuyPrice !== null ? latestBuyPrice.toFixed(8) : "--"}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4 text-center">
            <h2 className="text-xl font-semibold">Sell Price</h2>
            <p className="text-2xl font-bold text-red-500">
              {latestSellPrice !== null ? latestSellPrice.toFixed(8) : "--"}
            </p>
          </CardContent>
        </Card>
      </div>

      {/* ─── RSI Info Card ────────────────────────────────── */}
      <Card>
        <CardContent className="p-4 text-center">
          <h2 className="text-xl font-semibold">RSI ({rsiInterval})</h2>
          <p className="text-2xl font-bold" style={{ color: rsiColor(rsi) }}>
            {rsi !== null ? rsi.toFixed(2) : "--"}
          </p>
          <p className="italic text-sm text-gray-500">{rsiLabel(rsi)}</p>
        </CardContent>
      </Card>


      {/* ─── RSI Alerts Card ───────────────────────────────── */}
      <Card>
        <CardContent className="space-y-2 p-4">
          <Label>RSI Alerts</Label>
          <div className="flex gap-2 items-center">
            <select
              value={newRsiDir}
              onChange={e => setNewRsiDir(e.target.value as any)}
              className="border p-1 rounded"
            >
              <option value="above">Above</option>
              <option value="below">Below</option>
            </select>
            <Input
              value={newRsiValue}
              onChange={e => setNewRsiValue(e.target.value)}
              placeholder="Threshold"
            />
            <Button
              onClick={async () => {
                const num = parseFloat(newRsiValue);
                if (isNaN(num) || num < 0) return toast.error("Invalid RSI value");
                // we send the raw number; backend will treat it as float threshold
                await fetch("/api/rsi", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ values: [`${newRsiDir}:${num.toFixed(2)}`] }),
                });
                setNewRsiValue("");
                fetchRSI();
              }}
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
                          body: JSON.stringify({ key }),     // <-- pass the alert key string
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
            <select
              value={pendingInterval}
              onChange={(e) => setPendingInterval(e.target.value)}
              className="border p-1 rounded"
            >
              {["1s", "1m", "5m", "15m", "1h", "4h"].map((opt) => (
                <option key={opt} value={opt}>
                  {opt}
                </option>
              ))}
            </select>
            <Button
              onClick={async () => {
                // clear out the old RSI so we dont show stale,
                // then apply & re‐fetch immediately
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
            <select
              value={rsiResetEnabled ? "true" : "false"}
              onChange={(e) => setRsiResetEnabled(e.target.value === "true")}
              className="border p-1 rounded"
            >
              <option value="true">Re-trigger on cross-back</option>
              <option value="false">One-time only</option>
            </select>
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

      {/* Rest of the UI remains unchanged */}
      <Card>
        <CardContent className="space-y-2 p-4">
          <Label>Simulated USD Amount</Label>
          <div className="flex gap-2">
            <Input type="number" value={usdAmount} onChange={(e) => setUsdAmount(parseFloat(e.target.value))} />
            <Button onClick={applyUsdAmount}>Update</Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="space-y-2 p-4">
          <Label>Alert Reset Minutes (0 disables reset)</Label>
          <div className="flex gap-2">
            <Input
              type="number"
              value={alertResetMinutes}
              onChange={(e) => setAlertResetMinutes(parseInt(e.target.value))}
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
                    onChange={(e) =>
                      label === "Buy" ? setNewBuy(e.target.value) : setNewSell(e.target.value)
                    }
                  />
                  <Button onClick={() => addAlert(label.toLowerCase(), label === "Buy" ? newBuy : newSell)}>
                    Add
                  </Button>
                </div>
                <ul className="list-disc pl-5">
                  {alerts.map((val: number, i: number) => {
                    const key = val.toFixed(8);
                    const lastTime = times[key];
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
          <Line
            data={data}
            options={{
              responsive: true,
              plugins: {
                tooltip: {
                  callbacks: {
                    label: function (ctx) {
                      return `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(8)}`;
                    },
                  },
                },
                legend: {
                  display: true,
                  position: "top",
                },
              },
              scales: {
                y: {
                  ticks: {
                    callback: function (value) {
                      return Number(value).toFixed(8);
                    },
                  },
                },
              },
            }}
          />
        </CardContent>
      </Card>
    </div>
  );
}
