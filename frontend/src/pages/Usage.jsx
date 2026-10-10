import { useEffect, useState } from "react";
import { api } from "../api";

const fmt = new Intl.NumberFormat("en-IN");

function Stat({ label, value, sub }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

function Bars({ rows }) {
  if (!rows.length) return <div className="muted pad">No usage yet.</div>;
  const max = Math.max(...rows.map((r) => Number(r.tokens) || 0), 1);
  return (
    <div className="bars">
      {rows.map((r) => {
        const h = Math.max(2, ((Number(r.tokens) || 0) / max) * 100);
        const cachedShare = r.chats ? (r.cached_chats / r.chats) * 100 : 0;
        return (
          <div className="bar-col" key={r.day} title={`${r.day}: ${fmt.format(r.tokens)} tokens, ${r.chats} chats (${r.cached_chats} cached)`}>
            <div className="bar-track">
              <div className="bar-fill" style={{ height: `${h}%` }}>
                <div className="bar-cached" style={{ height: `${cachedShare}%` }} />
              </div>
            </div>
            <div className="bar-label">{r.day.slice(5)}</div>
          </div>
        );
      })}
    </div>
  );
}

export default function Usage() {
  const [days, setDays] = useState(14);
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const load = () => api(`/api/admin/usage?days=${days}`).then(setData).catch((e) => setError(e.message));
    load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, [days]);

  if (error) return <section className="page"><div className="error">{error}</div></section>;
  if (!data) return <section className="page"><div className="spinner" /></section>;

  const totalCost = data.by_model.reduce((s, r) => s + Number(r.cost_usd || 0), 0);
  const totalTokens = data.daily.reduce((s, r) => s + Number(r.tokens || 0), 0);
  const budgetPct = Math.min(100, (data.budget.used_today / data.budget.daily_limit) * 100);

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h2>Usage and cost</h2>
          <p className="muted">From the usage ledger (written by the queue worker into monthly partitions).</p>
        </div>
        <div className="seg">
          {[7, 14, 30].map((d) => <button key={d} className={days === d ? "on" : ""} onClick={() => setDays(d)}>{d}d</button>)}
        </div>
      </header>
      <div className="stats">
        <Stat label={`Tokens (${days}d)`} value={fmt.format(totalTokens)} />
        <Stat label={`Estimated cost (${days}d)`} value={`$${totalCost.toFixed(4)}`} sub="list prices; stub and Ollama are free" />
        <Stat label="Cache hit rate" value={`${(data.cache_hit_rate * 100).toFixed(1)}%`} sub="chat answers served from Redis" />
        <Stat label="Chat latency" value={data.latency_ms.p50 ? `${Math.round(data.latency_ms.p50)} ms` : "-"} sub={data.latency_ms.p95 ? `p95 ${Math.round(data.latency_ms.p95)} ms` : ""} />
      </div>
      <div className="card">
        <div className="card-head">
          <h3>Daily AI budget</h3>
          <span className="muted small">fallback when exhausted: {data.budget.fallback}</span>
        </div>
        <div className="budget"><div style={{ width: `${budgetPct}%` }} className={budgetPct > 85 ? "hot" : ""} /></div>
        <div className="muted small">{fmt.format(data.budget.used_today)} of {fmt.format(data.budget.daily_limit)} tokens used today</div>
      </div>
      <div className="grid2">
        <div className="card">
          <div className="card-head"><h3>Tokens per day</h3><span className="legend"><i className="lg-a" /> all <i className="lg-b" /> cached share</span></div>
          <Bars rows={data.daily} />
        </div>
        <div className="card">
          <div className="card-head"><h3>System</h3></div>
          <dl className="kv">
            <dt>AI backend</dt><dd>{data.backend.ai}</dd>
            <dt>Embeddings</dt><dd>{data.backend.embeddings}</dd>
            <dt>Retriever</dt><dd>{data.backend.retriever}</dd>
            <dt>Agent</dt><dd>{data.backend.agent}</dd>
            {Object.entries(data.queues).flatMap(([q, n]) => [<dt key={`${q}-k`}>queue {q}</dt>, <dd key={`${q}-v`} className={q.endsWith("dlq") && n > 0 ? "warn" : ""}>{n}</dd>])}
            {Object.entries(data.documents).flatMap(([s, n]) => [<dt key={`${s}-k`}>docs {s}</dt>, <dd key={`${s}-v`}>{n}</dd>])}
          </dl>
        </div>
      </div>
      <div className="card">
        <div className="card-head"><h3>By model</h3></div>
        <table className="table">
          <thead><tr><th>Backend</th><th>Model</th><th>Kind</th><th>Calls</th><th>Input tokens</th><th>Output tokens</th><th>Cost (USD)</th></tr></thead>
          <tbody>
            {data.by_model.map((r, i) => (
              <tr key={i}>
                <td>{r.backend}</td><td className="mono small">{r.model}</td><td>{r.kind}</td>
                <td className="mono">{fmt.format(r.calls)}</td><td className="mono">{fmt.format(r.input_tokens)}</td><td className="mono">{fmt.format(r.output_tokens)}</td>
                <td className="mono">{Number(r.cost_usd).toFixed(6)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
