"use strict";
let token = "";
const $ = id => document.getElementById(id);
const number = value => value === null || value === undefined ? "—" : Number(value).toLocaleString(undefined, {maximumFractionDigits: 4});
async function refresh() {
  if (!token) return;
  try {
    const response = await fetch("/api/status", {headers: {Authorization: "Bearer " + token}, cache: "no-store"});
    if (!response.ok) throw new Error("Access denied or service unavailable");
    const data = await response.json(), s = data.snapshot, m = data.metrics;
    $("mode").textContent = (data.mode || "uninitialized").toUpperCase();
    $("notice").textContent = data.kill_switch ? "KILL SWITCH: " + data.kill_switch.reason : data.paused ? "PAUSED · Protective exits remain active" : "Monitoring · " + m.status;
    const values = [["BTC / USD", s.btc_price], ["Bid", s.bid], ["Ask", s.ask], ["Spread · bps", s.spread_bps],
      ["Book imbalance", s.imbalance], ["Regime", s.regime], ["Position · BTC", s.position_btc], ["Equity · USD", s.equity],
      ["Unrealized P&L", s.unrealized_pnl], ["Realized net P&L", m.net_profit], ["Net return · %", m.net_return_pct],
      ["Today P&L", m.daily_pnl[new Date().toISOString().slice(0,10)] || 0], ["Fees · USD", m.fees], ["Profit factor", m.profit_factor],
      ["Expectancy · USD", m.expectancy], ["Max drawdown · %", m.maximum_drawdown_pct], ["Closed trades", m.number_of_trades],
      ["Win rate · %", m.win_rate === null ? null : m.win_rate * 100], ["Maker fills · %", m.maker_percentage],
      ["WebSocket", data.websocket], ["Exchange", data.exchange_status], ["Heartbeat age · s", data.heartbeat_age_seconds]];
    $("cards").replaceChildren(...values.map(([label, value]) => {
      const card = document.createElement("div"), l = document.createElement("div"), v = document.createElement("div");
      card.className = "card"; l.className = "label"; v.className = "value";
      l.textContent = label; v.textContent = value !== null && value !== undefined && Number.isNaN(Number(value)) ? value : number(value);
      card.append(l, v); return card;
    }));
    $("detail").textContent = JSON.stringify({last_signal: s.last_signal, last_trade: data.last_trade, commands: data.commands}, null, 2);
  } catch (error) { $("notice").textContent = error.message; }
}
$("connect").addEventListener("click", () => { token = $("token").value; $("token").value = ""; refresh(); });
document.querySelectorAll("[data-action]").forEach(button => button.addEventListener("click", async () => {
  const action = button.dataset.action;
  let confirmation = "";
  if (["resume", "flatten", "kill", "reset-kill"].includes(action)) {
    confirmation = window.prompt("Type " + action.toUpperCase() + " to confirm this operation.") || "";
    if (confirmation !== action.toUpperCase()) return;
  }
  const response = await fetch("/api/command", {method: "POST", headers: {Authorization: "Bearer " + token, "Content-Type": "application/json"}, body: JSON.stringify({action, confirmation})});
  const body = await response.json();
  $("notice").textContent = response.ok ? "Command queued. Verify its acknowledgement below." : body.error;
  refresh();
}));
setInterval(refresh, 3000);
