import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Icon } from "../components/Icon";

const CATEGORIES = ["hr", "it", "finance", "general"];

function formatSize(n) {
  return n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
}

export default function Documents() {
  const [docs, setDocs] = useState([]);
  const [file, setFile] = useState(null);
  const [title, setTitle] = useState("");
  const [category, setCategory] = useState("hr");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  const inputRef = useRef(null);

  const load = () => api("/api/documents").then(setDocs).catch((e) => setError(e.message));
  useEffect(() => { load(); }, []);

  const pending = docs.some((d) => d.status === "queued" || d.status === "processing");
  useEffect(() => {
    if (!pending) return;
    const t = setInterval(load, 1500);
    return () => clearInterval(t);
  }, [pending]);

  async function upload(e) {
    e.preventDefault();
    if (!file) return;
    setBusy(true);
    setError("");
    const form = new FormData();
    form.append("file", file);
    if (title.trim()) form.append("title", title.trim());
    form.append("category", category);
    try {
      await api("/api/documents", { method: "POST", form });
      setFile(null);
      setTitle("");
      if (inputRef.current) inputRef.current.value = "";
      load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function remove(id) {
    await api(`/api/documents/${id}`, { method: "DELETE" });
    load();
  }

  async function retry(id) {
    await api(`/api/documents/${id}/retry`, { method: "POST" });
    load();
  }

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h2>Policy documents</h2>
          <p className="muted">Uploads go to a queue; a worker extracts, chunks and embeds them. Only your company can search them.</p>
        </div>
      </header>
      <form className={`card upload${drag ? " drag" : ""}`} onSubmit={upload} onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)} onDrop={(e) => { e.preventDefault(); setDrag(false); setFile(e.dataTransfer.files[0] || null); }}>
        <div className="drop" onClick={() => inputRef.current?.click()}>
          <Icon name="upload" size={22} />
          <div>{file ? <b>{file.name}</b> : <>Drop a PDF, Markdown or text file, or <u>browse</u></>}</div>
          <div className="muted small">Up to 5 MB</div>
          <input ref={inputRef} type="file" accept=".pdf,.md,.markdown,.txt" hidden onChange={(e) => setFile(e.target.files[0] || null)} />
        </div>
        <div className="upload-fields">
          <label>Title<input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Defaults to the file's heading" /></label>
          <label>Category<select value={category} onChange={(e) => setCategory(e.target.value)}>{CATEGORIES.map((c) => <option key={c}>{c}</option>)}</select></label>
          <button className="btn primary" disabled={!file || busy}>{busy ? "Uploading..." : "Upload"}</button>
        </div>
        {error && <div className="error">{error}</div>}
      </form>
      <div className="card">
        <table className="table">
          <thead><tr><th>Document</th><th>Category</th><th>Status</th><th>Chunks</th><th>Strategy</th><th>Size</th><th /></tr></thead>
          <tbody>
            {docs.map((d) => (
              <tr key={d.id}>
                <td className="wrap"><div className="doc-title">{d.title}</div><div className="muted small">{d.filename}</div></td>
                <td>{d.category}</td>
                <td>
                  <span className={`status ${d.status}`}>{d.status}</span>
                  {d.error && <div className="muted small err-text" title={d.error}>{d.error}</div>}
                </td>
                <td className="mono">{d.chunk_count}</td>
                <td className="muted">{d.chunk_strategy}</td>
                <td className="muted">{formatSize(d.size_bytes)}</td>
                <td className="actions">
                  {d.status === "failed" && <button className="icon-btn" title="Retry" onClick={() => retry(d.id)}><Icon name="retry" /></button>}
                  <button className="icon-btn" title="Delete" onClick={() => remove(d.id)}><Icon name="trash" /></button>
                </td>
              </tr>
            ))}
            {!docs.length && <tr><td colSpan={7} className="muted center pad">No documents yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </section>
  );
}
