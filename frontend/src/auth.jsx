import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api, getToken, setToken } from "./api";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [config, setConfig] = useState(null);
  const [loading, setLoading] = useState(Boolean(getToken()));

  useEffect(() => {
    api("/api/config").then(setConfig).catch(() => setConfig(null));
    if (!getToken()) return;
    api("/api/auth/me")
      .then(setUser)
      .catch(() => setToken(null))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    const onLogout = () => setUser(null);
    window.addEventListener("pd:logout", onLogout);
    return () => window.removeEventListener("pd:logout", onLogout);
  }, []);

  const signIn = useCallback(async (email, password) => {
    const data = await api("/api/auth/login", { method: "POST", body: { email, password } });
    setToken(data.token);
    setUser(data.user);
  }, []);

  const register = useCallback(async (payload) => {
    const data = await api("/api/auth/register", { method: "POST", body: payload });
    setToken(data.token);
    setUser(data.user);
  }, []);

  const signOut = useCallback(() => {
    setToken(null);
    setUser(null);
  }, []);

  return <AuthContext.Provider value={{ user, config, loading, signIn, register, signOut }}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  return useContext(AuthContext);
}
