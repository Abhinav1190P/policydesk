import { Navigate, NavLink, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth";
import Login from "./pages/Login";
import Ask from "./pages/Ask";
import Assistant from "./pages/Assistant";
import Tickets from "./pages/Tickets";
import Documents from "./pages/Documents";
import Usage from "./pages/Usage";
import { Icon } from "./components/Icon";

function Shell({ children }) {
  const { user, config, signOut } = useAuth();
  const links = [
    { to: "/ask", label: "Ask policies", icon: "book" },
    { to: "/assistant", label: "Assistant", icon: "spark" },
    { to: "/tickets", label: "Tickets", icon: "ticket" },
  ];
  if (user.role === "admin") {
    links.push({ to: "/documents", label: "Documents", icon: "file" });
    links.push({ to: "/usage", label: "Usage & cost", icon: "chart" });
  }
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">P</div>
          <div>
            <div className="brand-name">PolicyDesk</div>
            <div className="brand-tenant">{user.tenant_name || user.tenant_slug}</div>
          </div>
        </div>
        <nav>
          {links.map((l) => (
            <NavLink key={l.to} to={l.to} className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
              <Icon name={l.icon} />
              <span>{l.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          {config && (
            <div className="backend-pill" title="Active AI configuration">
              <span className="dot" />
              {config.ai_backend} · {config.retriever === "bedrock_kb" ? "Bedrock KB" : "pgvector"}
            </div>
          )}
          <div className="me">
            <div className="avatar">{user.name.slice(0, 1)}</div>
            <div className="me-text">
              <div className="me-name">{user.name}</div>
              <div className="me-role">{user.role}</div>
            </div>
            <button className="icon-btn" onClick={signOut} title="Sign out" aria-label="Sign out">
              <Icon name="logout" />
            </button>
          </div>
        </div>
      </aside>
      <main className="main">{children}</main>
    </div>
  );
}

export default function App() {
  const { user, loading } = useAuth();
  if (loading) return <div className="center-screen"><div className="spinner" /></div>;
  if (!user) {
    return (
      <Routes>
        <Route path="*" element={<Login />} />
      </Routes>
    );
  }
  const admin = user.role === "admin";
  return (
    <Shell>
      <Routes>
        <Route path="/ask" element={<Ask />} />
        <Route path="/assistant" element={<Assistant />} />
        <Route path="/tickets" element={<Tickets />} />
        {admin && <Route path="/documents" element={<Documents />} />}
        {admin && <Route path="/usage" element={<Usage />} />}
        <Route path="*" element={<Navigate to="/ask" replace />} />
      </Routes>
    </Shell>
  );
}
