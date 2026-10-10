import { useEffect, useRef, useState } from "react";
import { api, streamChat } from "../api";
import { AssistantMessage } from "../components/Answer";
import { Icon } from "../components/Icon";

const SUGGESTIONS = [
  "How many days of annual leave do I get?",
  "How many unused leave days can I carry forward?",
  "What is the hotel limit when I travel?",
  "What should I do if I lose my laptop?",
];

export default function Ask() {
  const [conversations, setConversations] = useState([]);
  const [current, setCurrent] = useState(null);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef(null);
  const abortRef = useRef(null);

  const loadConversations = () => api("/api/conversations").then(setConversations).catch(() => {});

  useEffect(() => {
    loadConversations();
    return () => abortRef.current?.abort();
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  async function open(id) {
    abortRef.current?.abort();
    setCurrent(id);
    const rows = await api(`/api/conversations/${id}/messages`);
    setMessages(
      rows.map((r) => (r.role === "user" ? { role: "user", text: r.content } : { role: "assistant", text: r.content, citations: r.citations, backend: r.backend, cached: r.cached, latency_ms: r.latency_ms, guardrail: r.guardrail }))
    );
  }

  function newChat() {
    abortRef.current?.abort();
    setCurrent(null);
    setMessages([]);
  }

  async function remove(id, e) {
    e.stopPropagation();
    await api(`/api/conversations/${id}`, { method: "DELETE" });
    if (current === id) newChat();
    loadConversations();
  }

  function patchLast(fn) {
    setMessages((ms) => {
      const copy = ms.slice();
      copy[copy.length - 1] = fn(copy[copy.length - 1]);
      return copy;
    });
  }

  async function send(question) {
    const q = (question ?? input).trim();
    if (!q || busy) return;
    setInput("");
    setBusy(true);
    setMessages((ms) => [...ms, { role: "user", text: q }, { role: "assistant", text: "", citations: [] }]);
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await streamChat({
        question: q,
        conversationId: current,
        signal: controller.signal,
        onEvent: (ev) => {
          if (ev.type === "meta") {
            setCurrent(ev.conversation_id);
            patchLast((m) => ({ ...m, backend: ev.backend, fallback: ev.fallback, instance: ev.instance }));
          } else if (ev.type === "citations") patchLast((m) => ({ ...m, citations: ev.citations }));
          else if (ev.type === "token") patchLast((m) => ({ ...m, text: m.text + ev.text }));
          else if (ev.type === "done")
            patchLast((m) => ({ ...m, text: ev.answer, citations: ev.citations || m.citations, cached: ev.cached, latency_ms: ev.latency_ms, guardrail: ev.guardrail }));
          else if (ev.type === "error") patchLast((m) => ({ ...m, error: ev.message }));
        },
      });
    } catch (err) {
      if (err.name !== "AbortError") patchLast((m) => ({ ...m, error: err.message }));
    } finally {
      setBusy(false);
      loadConversations();
    }
  }

  return (
    <div className="ask">
      <div className="convo-list">
        <button className="btn ghost full" onClick={newChat}><Icon name="plus" /> New question</button>
        <div className="convo-items">
          {conversations.map((c) => (
            <div key={c.id} className={`convo${current === c.id ? " on" : ""}`} onClick={() => open(c.id)}>
              <span>{c.title}</span>
              <button className="icon-btn small" onClick={(e) => remove(c.id, e)} aria-label="Delete conversation"><Icon name="trash" size={14} /></button>
            </div>
          ))}
          {!conversations.length && <div className="muted small pad">Your questions will show up here.</div>}
        </div>
      </div>
      <section className="chat">
        <header className="page-head">
          <div>
            <h2>Ask your policies</h2>
            <p className="muted">Answers come only from your company's documents, with numbered sources.</p>
          </div>
        </header>
        <div className="messages">
          {!messages.length && (
            <div className="empty">
              <div className="empty-icon"><Icon name="book" size={28} /></div>
              <h3>What do you want to know?</h3>
              <div className="suggestions">
                {SUGGESTIONS.map((s) => <button key={s} className="chip" onClick={() => send(s)}>{s}</button>)}
              </div>
            </div>
          )}
          {messages.map((m, i) => (m.role === "user" ? <div key={i} className="msg user"><div className="bubble">{m.text}</div></div> : <AssistantMessage key={i} m={m} />))}
          <div ref={endRef} />
        </div>
        <form className="composer" onSubmit={(e) => { e.preventDefault(); send(); }}>
          <input value={input} onChange={(e) => setInput(e.target.value)} placeholder="Ask about leave, travel, devices..." maxLength={2000} />
          <button className="btn primary" disabled={busy || !input.trim()} aria-label="Send"><Icon name="send" /></button>
        </form>
      </section>
    </div>
  );
}
