import { useState } from "react";

function Bold({ text }) {
  const bits = text.split(/(\*\*[^*]+\*\*)/g);
  return bits.map((b, i) => (b.startsWith("**") && b.endsWith("**") && b.length > 4 ? <strong key={i}>{b.slice(2, -2)}</strong> : <span key={i}>{b}</span>));
}

export function AnswerText({ text, onCite }) {
  const clean = text.replace(/【\s*(\d+)\s*】/g, "[$1]");
  const parts = clean.split(/(\[\d+\])/g);
  return (
    <p className="answer-text">
      {parts.map((p, i) => {
        const m = p.match(/^\[(\d+)\]$/);
        if (m) {
          return (
            <button key={i} className="cite-mark" onClick={() => onCite?.(Number(m[1]))}>
              {m[1]}
            </button>
          );
        }
        return <Bold key={i} text={p} />;
      })}
    </p>
  );
}

export function Citations({ items, active, setActive }) {
  const used = items.filter((c) => c.used !== false);
  if (!used.length) return null;
  const open = used.find((c) => c.n === active);
  return (
    <div className="citations">
      <div className="cite-row">
        {used.map((c) => (
          <button key={`${c.n}-${c.chunk_id || c.title}`} className={`cite-chip${active === c.n ? " on" : ""}`} onClick={() => setActive(active === c.n ? null : c.n)}>
            <span className="cite-n">{c.n}</span>
            <span className="cite-title">{c.title}</span>
          </button>
        ))}
      </div>
      {open && (
        <div className="cite-panel">
          <div className="cite-panel-head">{open.heading || open.title}</div>
          <div className="cite-panel-body">{open.content || open.snippet}</div>
          {open.similarity !== undefined && <div className="cite-meta">similarity {open.similarity} · {open.source}</div>}
        </div>
      )}
    </div>
  );
}

export function MessageMeta({ m }) {
  const tags = [];
  if (m.cached) tags.push(<span key="c" className="tag green">cached</span>);
  if (m.backend && m.backend !== "none") tags.push(<span key="b" className="tag">{m.backend}</span>);
  if (m.fallback) tags.push(<span key="f" className="tag amber">fallback: {m.fallback}</span>);
  if (m.guardrail) tags.push(<span key="g" className="tag amber">guardrail: {m.guardrail}</span>);
  if (m.latency_ms !== undefined && m.latency_ms !== null) tags.push(<span key="l" className="tag muted">{m.latency_ms} ms</span>);
  if (m.instance) tags.push(<span key="i" className="tag muted">{m.instance}</span>);
  if (!tags.length) return null;
  return <div className="msg-meta">{tags}</div>;
}

export function AssistantMessage({ m }) {
  const [active, setActive] = useState(null);
  return (
    <div className="msg assistant">
      <div className="bubble">
        {m.text ? <AnswerText text={m.text} onCite={setActive} /> : <div className="typing"><span /><span /><span /></div>}
        {m.error && <div className="error">{m.error}</div>}
        <Citations items={m.citations || []} active={active} setActive={setActive} />
      </div>
      <MessageMeta m={m} />
    </div>
  );
}
