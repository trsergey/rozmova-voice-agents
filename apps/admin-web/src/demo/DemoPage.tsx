// The public demo page at /demo/ (ROZ-68): an access-code login, then a chat with the first
// published bot (the seeded demo bot). Its styles are scoped under `.demo`. An admin who
// lands here gets a link back to the admin, where the same chat sits in the admin layout.
import { useEffect, useState, type FormEvent } from "react";

import { api, ApiError, type Bot, type Session } from "./api";
import { Chat } from "./Chat";
import "./demo.css";

type Loaded = { status: "loading" } | { status: "anonymous" } | { status: "ready"; session: Session; bot: Bot | null };

async function load(session: Session): Promise<Loaded> {
  const bots = await api.bots();
  return { status: "ready", session, bot: bots.find((bot) => bot.live_version_id !== null) ?? null };
}

export function DemoPage() {
  const [state, setState] = useState<Loaded>({ status: "loading" });

  useEffect(() => {
    document.title = "Hvylia Mobile — демо асистента";
  }, []);

  useEffect(() => {
    api
      .me()
      .then(load)
      .then(setState)
      .catch(() => setState({ status: "anonymous" }));
  }, []);

  const logout = async (csrf: string) => {
    await api.logout(csrf).catch(() => undefined);
    setState({ status: "anonymous" });
  };

  const admin = state.status === "ready" && state.session.user.role === "admin";
  return (
    <main className={`demo page${state.status === "anonymous" ? " signin" : ""}`}>
      <header className="header">
        {admin && (
          <a className="back" href="/admin/">
            ← Адмінка
          </a>
        )}
        <h1>Hvylia Mobile</h1>
        <p className="subtitle">Демо голосового асистента підтримки, поки що текстом</p>
      </header>
      {state.status === "loading" && <p className="muted">Завантаження…</p>}
      {state.status === "anonymous" && <AccessCodeForm onLogin={(session) => load(session).then(setState)} />}
      {state.status === "ready" &&
        (state.bot ? (
          <Chat csrf={state.session.csrf_token} bot={state.bot} onLogout={() => logout(state.session.csrf_token)} />
        ) : (
          <p className="muted">Демо-бот ще не опублікований.</p>
        ))}
    </main>
  );
}

function AccessCodeForm({ onLogin }: { onLogin: (session: Session) => Promise<void> }) {
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await onLogin(await api.loginWithCode(code));
    } catch (error) {
      setError(
        error instanceof ApiError && error.status === 401
          ? "Код недійсний або відкликаний."
          : "Не вдалося увійти. Спробуйте ще раз.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="card login" onSubmit={submit}>
      <label htmlFor="code">Код доступу</label>
      <input
        id="code"
        value={code}
        onChange={(event) => setCode(event.target.value)}
        autoComplete="off"
        autoFocus
        required
      />
      <button type="submit" disabled={busy || code.trim() === ""}>
        Увійти
      </button>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}
