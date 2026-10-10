import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { AssistantMessage } from "../components/Answer";
import { Icon } from "../components/Icon";

const STARTERS = ["How many leave days do I have left?", "My laptop screen is flickering, can you raise a ticket?", "What's the policy on working from abroad?"];

function newSession() {
  return crypto.randomUUID ? crypto.randomUUID() : `s-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
}

function LeaveCard({ leave }) {
  if (!leave || leave.error) return null;
  const pct = (used, total) => `${Math.min(100, (used / total) * 100)}%`;
  return (
    <div className="leave-card">
      <div className="leave-item">
        <div className="leave-label">Annual leave left</div>
        <div className="leave-value">{leave.annual_remaining}<span> / {leave.annual_total}</span></div>
        <div className="bar"><div style={{ width: pct(leave.annual_used, leave.annual_total) }} /></div>
      </div>
      <div className="leave-item">
        <div className="leave-label">Sick leave left</div>
        <div className="leave-value">{leave.sick_remaining}<span> / {leave.sick_total}</span></div>
        <div className="bar"><div style={{ width: pct(leave.sick_used, leave.sick_total) }} /></div>
      </div>
    </div>
  );
}

function Trace({ steps }) {
  if (!steps?.length) return null;
  return (
    <div className="trace">
      {steps.map((s, i) => (
        <span key={i} className="trace-step">
          <Icon name={s.step === "confirm" ? "shield" : "bolt"} size={12} />
          {s.name}
          {s.approved === false ? " (declined)" : ""}
        </span>
      ))}
    </div>
  );
}

function ConfirmCard({ m, onDecide, busy }) {
  const a = m.action;
  return (
    <div className="msg assistant">
      <div className="bubble confirm">
        <div className="confirm-head"><Icon name="shield" /> Confirm before I do this</div>
        <p className="muted small">{m.text}</p>
        <dl className="confirm-grid">
          <dt>Action</dt><dd>Raise IT ticket</dd>
          <dt>Category</dt><dd>{a.args.category}</dd>
          <dt>Priority</dt><dd><span className={`prio ${a.args.priority}`}>{a.args.priority}</span></dd>
          <dt>Summary</dt><dd>{a.args.summary}</dd>
        </dl>
        {m.decided ? (
          <div className="muted small">{m.decided === "approved" ? "Approved" : "Cancelled"}</div>
        ) : (
          <div className="confirm-actions">
            <button className="btn primary" disabled={busy} onClick={() => onDecide(true)}><Icon name="check" /> Raise ticket</button>
            <button className="btn ghost" disabled={busy} onClick={() => onDecide(false)}><Icon name="x" /> Cancel</button>
          </div>
        )}
      </div>
      <Trace steps={m.trace} />
    </div>
  );
}

export default function Assistant() {
  const [session, setSession] = useState(newSession);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [leave, setLeave] = useState(null);
  const endRef = useRef(null);

  const refreshLeave = () => api("/api/me/leave").then(setLeave).catch(() => {});
  useEffect(() => { refreshLeave(); }, []);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [messages]);

  function addReply(r) {
    setMessages((ms) => [...ms.filter((m) => !m.pending), r.type === "confirm" ? { role: "confirm", ...r } : { role: "assistant", ...r }]);
  }

  async function send(text) {
    const t = (text ?? input).trim();
    if (!t || busy) return;
    setInput("");
    setBusy(true);
    setMessages((ms) => [...ms.map((m) => (m.role === "confirm" && !m.decided ? { ...m, decided: "cancelled" } : m)), { role: "user", text: t }, { role: "assistant", text: "", pending: true }]);
    try {
      addReply(await api("/api/agent/message", { method: "POST", body: { text: t, session_id: session } }));
    } catch (err) {
      addReply({ type: "answer", text: "", error: err.message });
    } finally {
      setBusy(false);
    }
  }

  async function decide(index, approve) {
    const m = messages[index];
    setBusy(true);
    setMessages((ms) => ms.map((x, i) => (i === index ? { ...x, decided: approve ? "approved" : "cancelled" } : x)).concat({ role: "assistant", text: "", pending: true }));
    try {
      addReply(await api("/api/agent/confirm", { method: "POST", body: { session_id: session, action_id: m.action.id, approve } }));
    } catch (err) {
      addReply({ type: "answer", text: "", error: err.message });
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    await api("/api/agent/reset", { method: "POST", body: { session_id: session } }).catch(() => {});
    setSession(newSession());
    setMessages([]);
    refreshLeave();
  }

  return (
    <section className="chat solo">
      <header className="page-head">
        <div>
          <h2>Assistant</h2>
          <p className="muted">Searches policies, checks your leave and raises IT tickets. It always asks before creating anything.</p>
        </div>
        <button className="btn ghost" onClick={reset}><Icon name="plus" /> New session</button>
      </header>
      <LeaveCard leave={leave} />
      <div className="messages">
        {!messages.length && (
          <div className="empty">
            <div className="empty-icon"><Icon name="spark" size={28} /></div>
            <h3>How can I help?</h3>
            <div className="suggestions">{STARTERS.map((s) => <button key={s} className="chip" onClick={() => send(s)}>{s}</button>)}</div>
          </div>
        )}
        {messages.map((m, i) => {
          if (m.role === "user") return <div key={i} className="msg user"><div className="bubble">{m.text}</div></div>;
          if (m.role === "confirm") return <ConfirmCard key={i} m={m} busy={busy} onDecide={(ok) => decide(i, ok)} />;
          return (
            <div key={i}>
              <AssistantMessage m={m} />
              {m.executed?.ticket_id && <div className="ticket-done"><Icon name="ticket" size={14} /> Ticket #{m.executed.ticket_id} created</div>}
              <Trace steps={m.trace} />
            </div>
          );
        })}
        <div ref={endRef} />
      </div>
      <form className="composer" onSubmit={(e) => { e.preventDefault(); send(); }}>
        <input value={input} onChange={(e) => setInput(e.target.value)} placeholder="Ask a question or describe an IT problem..." maxLength={2000} />
        <button className="btn primary" disabled={busy || !input.trim()} aria-label="Send"><Icon name="send" /></button>
      </form>
    </section>
  );
}
