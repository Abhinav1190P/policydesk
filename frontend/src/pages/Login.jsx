import { useState } from "react";
import { useAuth } from "../auth";

const DEMO = [
  { label: "Acme employee", email: "employee@acme.example" },
  { label: "Acme admin", email: "admin@acme.example" },
  { label: "Globex employee", email: "employee@globex.example" },
  { label: "Globex admin", email: "admin@globex.example" },
];

export default function Login() {
  const { signIn, register } = useAuth();
  const [mode, setMode] = useState("login");
  const [form, setForm] = useState({ email: "", password: "", name: "", tenant_slug: "", invite_code: "" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  async function submit(e) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      if (mode === "login") await signIn(form.email, form.password);
      else await register(form);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-page">
      <section className="auth-hero">
        <div className="brand big">
          <div className="brand-mark">P</div>
          <div className="brand-name">PolicyDesk</div>
        </div>
        <h1>Answers from your company's own policies, with sources.</h1>
        <p>Ask about leave, travel, devices and more. Check your leave balance or raise an IT ticket through the assistant. Every company only ever sees its own documents.</p>
        <ul className="hero-points">
          <li>Retrieval-augmented answers with citations</li>
          <li>Agent with tools and confirm-before-action</li>
          <li>Runs on Amazon Bedrock, Groq, Ollama or offline</li>
        </ul>
      </section>
      <section className="auth-card">
        <div className="tabs">
          <button className={mode === "login" ? "tab active" : "tab"} onClick={() => setMode("login")}>Sign in</button>
          <button className={mode === "register" ? "tab active" : "tab"} onClick={() => setMode("register")}>Join your company</button>
        </div>
        <form onSubmit={submit} className="form">
          {mode === "register" && (
            <>
              <label>Full name<input value={form.name} onChange={set("name")} required /></label>
              <div className="row2">
                <label>Company code<input value={form.tenant_slug} onChange={set("tenant_slug")} placeholder="acme" required /></label>
                <label>Invite code<input value={form.invite_code} onChange={set("invite_code")} placeholder="From your admin" required /></label>
              </div>
            </>
          )}
          <label>Work email<input type="email" value={form.email} onChange={set("email")} autoComplete="username" required /></label>
          <label>Password<input type="password" value={form.password} onChange={set("password")} autoComplete={mode === "login" ? "current-password" : "new-password"} minLength={mode === "register" ? 8 : 1} required /></label>
          {error && <div className="error">{error}</div>}
          <button className="btn primary" disabled={busy}>{busy ? "Please wait..." : mode === "login" ? "Sign in" : "Create account"}</button>
        </form>
        {mode === "login" && (
          <div className="demo">
            <div className="demo-title">Demo accounts (password Password123!)</div>
            <div className="demo-grid">
              {DEMO.map((d) => (
                <button key={d.email} className="chip" onClick={() => setForm({ ...form, email: d.email, password: "Password123!" })}>{d.label}</button>
              ))}
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
