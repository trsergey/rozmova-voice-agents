// Login: an admin with email and password, or anyone with an access code (a viewer session,
// FR-14). Both start the same cookie session; `?next=` brings the visitor back where they were.
import { Tabs } from "radix-ui";
import { useState, type FormEvent } from "react";
import { Navigate, useSearchParams } from "react-router";

import { ApiError } from "../api/client";
import { useAccessCodeLogin, useLogin, useSession } from "./session";
import { Button, ErrorText, Field, Input } from "./ui";

// Only paths inside the admin: `next` comes from the URL, so it must not lead off the site.
function safeNext(next: string | null): string {
  return next && next.startsWith("/admin/") && !next.startsWith("/admin/login") ? next : "/admin/bots";
}

function errorText(error: unknown, invalid: string): string {
  if (error instanceof ApiError && error.status === 401) return invalid;
  if (error instanceof ApiError && error.status === 429) return "Забагато спроб. Зачекайте хвилину.";
  return "Не вдалося увійти. Спробуйте ще раз.";
}

export function LoginPage() {
  const { data: session } = useSession();
  const [params] = useSearchParams();
  const next = safeNext(params.get("next"));

  if (session) return <Navigate to={next} replace />;

  const tab = "rounded-md px-3 py-1.5 text-sm text-fg-muted data-[state=active]:bg-surface data-[state=active]:text-fg data-[state=active]:shadow-sm";

  return (
    <main className="flex min-h-screen items-start justify-center px-4 pt-[12vh]">
      <div className="w-full max-w-sm">
        <h1 className="mb-1 text-xl font-semibold">Rozmova</h1>
        <p className="mb-6 text-sm text-fg-muted">Адмінка голосового асистента</p>
        <Tabs.Root defaultValue="password" className="rounded-lg border border-line bg-surface p-4">
          <Tabs.List aria-label="Спосіб входу" className="mb-4 grid grid-cols-2 gap-1 rounded-md bg-muted-bg p-1">
            <Tabs.Trigger value="password" className={tab}>
              Адміністратор
            </Tabs.Trigger>
            <Tabs.Trigger value="code" className={tab}>
              Код доступу
            </Tabs.Trigger>
          </Tabs.List>
          <Tabs.Content value="password">
            <PasswordForm />
          </Tabs.Content>
          <Tabs.Content value="code">
            <AccessCodeForm />
          </Tabs.Content>
        </Tabs.Root>
      </div>
    </main>
  );
}

function PasswordForm() {
  const login = useLogin();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    login.mutate({ email, password });
  };

  return (
    <form className="grid gap-4" onSubmit={submit}>
      <Field label="Email" htmlFor="email">
        <Input id="email" type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required />
      </Field>
      <Field label="Пароль" htmlFor="password">
        <Input
          id="password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
      </Field>
      {login.isError && <ErrorText>{errorText(login.error, "Невірний email або пароль.")}</ErrorText>}
      <Button type="submit" disabled={login.isPending}>
        Увійти
      </Button>
    </form>
  );
}

function AccessCodeForm() {
  const login = useAccessCodeLogin();
  const [code, setCode] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    login.mutate(code.trim());
  };

  return (
    <form className="grid gap-4" onSubmit={submit}>
      <Field label="Код доступу" htmlFor="code">
        <Input id="code" autoComplete="off" value={code} onChange={(e) => setCode(e.target.value)} required />
      </Field>
      <p className="text-sm text-fg-muted">Код дає доступ лише на перегляд.</p>
      {login.isError && <ErrorText>{errorText(login.error, "Код недійсний або відкликаний.")}</ErrorText>}
      <Button type="submit" disabled={login.isPending || code.trim() === ""}>
        Увійти
      </Button>
    </form>
  );
}
