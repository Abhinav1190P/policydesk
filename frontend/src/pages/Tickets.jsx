import { useEffect, useState } from "react";
import { api } from "../api";
import { useAuth } from "../auth";

export default function Tickets() {
  const { user } = useAuth();
  const admin = user.role === "admin";
  const [scope, setScope] = useState(admin ? "tenant" : "mine");
  const [rows, setRows] = useState([]);
  const [error, setError] = useState("");

  const load = () => api(`/api/tickets?scope=${scope}`).then(setRows).catch((e) => setError(e.message));
  useEffect(() => { load(); }, [scope]);

  async function setStatus(id, status) {
    await api(`/api/tickets/${id}`, { method: "PATCH", body: { status } });
    load();
  }

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h2>IT tickets</h2>
          <p className="muted">Tickets raised through the assistant.</p>
        </div>
        {admin && (
          <div className="seg">
            <button className={scope === "tenant" ? "on" : ""} onClick={() => setScope("tenant")}>All</button>
            <button className={scope === "mine" ? "on" : ""} onClick={() => setScope("mine")}>Mine</button>
          </div>
        )}
      </header>
      {error && <div className="error">{error}</div>}
      <div className="card">
        <table className="table">
          <thead>
            <tr><th>#</th>{scope === "tenant" && <th>Employee</th>}<th>Summary</th><th>Category</th><th>Priority</th><th>Status</th><th>Created</th></tr>
          </thead>
          <tbody>
            {rows.map((t) => (
              <tr key={t.id}>
                <td className="mono">{t.id}</td>
                {scope === "tenant" && <td>{t.user_name}</td>}
                <td className="wrap">{t.summary}</td>
                <td>{t.category}</td>
                <td><span className={`prio ${t.priority}`}>{t.priority}</span></td>
                <td>
                  {admin && scope === "tenant" ? (
                    <select value={t.status} onChange={(e) => setStatus(t.id, e.target.value)}>
                      <option value="open">open</option>
                      <option value="in_progress">in progress</option>
                      <option value="closed">closed</option>
                    </select>
                  ) : (
                    <span className={`status ${t.status}`}>{t.status.replace("_", " ")}</span>
                  )}
                </td>
                <td className="muted small">{new Date(t.created_at).toLocaleString()}</td>
              </tr>
            ))}
            {!rows.length && <tr><td colSpan={7} className="muted center pad">No tickets yet. Describe an IT problem to the assistant to raise one.</td></tr>}
          </tbody>
        </table>
      </div>
    </section>
  );
}
