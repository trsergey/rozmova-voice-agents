// One bot: what is live, the draft and its last eval, publishing and the version history
// (./Publishing), and the provider keys (FR-17), which are write-only: the page shows their
// last 4 characters and never gets a key back.
import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router";

import { ApiError } from "../../api/client";
import { formatDateTime } from "../format";
import { AdminOnly } from "../guards";
import { Badge, Button, Card, ErrorText, Field, Input, PageHeader, Select } from "../ui";
import { PublishCard, VersionsCard } from "./Publishing";
import { useBot, useCredentials, useProviders, usePutCredential, type BotDetail } from "./queries";

export function BotPage() {
  const { botId = "" } = useParams();
  const bot = useBot(botId);

  if (bot.isPending) return <p className="text-sm text-fg-muted">Завантаження…</p>;
  if (bot.isError) {
    const missing = bot.error instanceof ApiError && (bot.error.status === 404 || bot.error.status === 422);
    return (
      <>
        <PageHeader title={missing ? "Бота не знайдено" : "Помилка"} />
        <BackLink />
      </>
    );
  }
  return (
    <>
      <BackLink />
      <PageHeader title={bot.data.name}>
        <span className="flex flex-wrap items-center gap-3 text-sm text-fg-muted">
          {bot.data.provider} · {bot.data.model}
          <Link
            to="editor"
            className="rounded-md bg-accent px-3 py-2 font-medium text-accent-fg hover:bg-accent/90"
          >
            Редактор флоу
          </Link>
        </span>
      </PageHeader>
      <div className="grid gap-4 md:grid-cols-3">
        <Card title="В ефірі">
          {bot.data.live_version != null ? (
            <p className="text-2xl font-semibold">v{bot.data.live_version}</p>
          ) : (
            <p className="text-sm text-fg-muted">Ще не опубліковано</p>
          )}
        </Card>
        <Card title="Чернетка">
          <p className="text-sm">
            {bot.data.draft_updated_at ? `Змінена ${formatDateTime(bot.data.draft_updated_at)}` : "Ще не змінювалась"}
          </p>
        </Card>
        <Card title="Останній eval">
          <LastEval bot={bot.data} />
        </Card>
      </div>
      <div className="mt-6 grid gap-4 lg:grid-cols-2">
        <PublishCard bot={bot.data} />
        <Credentials botId={botId} botProvider={bot.data.provider} />
      </div>
      <div className="mt-6">
        <VersionsCard botId={botId} />
      </div>
    </>
  );
}

function BackLink() {
  return (
    <Link to="/admin/bots" className="mb-3 inline-block text-sm text-accent hover:underline">
      ← Усі боти
    </Link>
  );
}

function LastEval({ bot }: { bot: BotDetail }) {
  const run = bot.last_eval;
  if (!run) return <p className="text-sm text-fg-muted">Ще не запускався</p>;
  return (
    <div className="grid gap-1.5 text-sm">
      <p className="flex flex-wrap items-center gap-2">
        <Badge tone={run.passed ? "ok" : "bad"}>{run.passed ? "пройдено" : "не пройдено"}</Badge>
        <span>
          {run.cases_passed} з {run.cases} кейсів
        </span>
      </p>
      <p className="text-fg-muted">{formatDateTime(run.finished_at)}</p>
      {!run.current && <p className="text-warn-fg">Чернетка змінилась після прогону.</p>}
    </div>
  );
}

function Credentials({ botId, botProvider }: { botId: string; botProvider: string }) {
  const credentials = useCredentials(botId);

  return (
    <Card title="Ключі провайдера">
      {credentials.isError && <ErrorText>Не вдалося завантажити ключі.</ErrorText>}
      {credentials.data?.length === 0 && (
        <p className="text-sm text-fg-muted">Власного ключа немає: бот працює на демо-ключі платформи.</p>
      )}
      {credentials.data && credentials.data.length > 0 && (
        <ul className="grid gap-2 text-sm">
          {credentials.data.map((credential) => (
            <li key={credential.provider} className="flex flex-wrap items-center justify-between gap-2">
              <span>
                {credential.provider} <span className="font-mono text-fg-muted">…{credential.last4}</span>
              </span>
              <span className="text-fg-muted">{formatDateTime(credential.created_at)}</span>
            </li>
          ))}
        </ul>
      )}
      <AdminOnly>
        <CredentialForm botId={botId} botProvider={botProvider} />
      </AdminOnly>
    </Card>
  );
}

function CredentialForm({ botId, botProvider }: { botId: string; botProvider: string }) {
  const providers = useProviders();
  const save = usePutCredential(botId);
  const [provider, setProvider] = useState(botProvider);
  const [apiKey, setApiKey] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    // The key leaves the page state as soon as it is sent: nothing keeps it after the request.
    save.mutate({ provider, api_key: apiKey.trim() }, { onSettled: () => setApiKey("") });
  };

  const error =
    save.error instanceof ApiError && save.error.code === "credentials_unavailable"
      ? "Сервер не налаштований для зберігання ключів (немає майстер-ключа)."
      : save.error instanceof ApiError && save.error.status === 422
        ? "Ключ закороткий або провайдер невідомий."
        : "Не вдалося зберегти ключ.";

  return (
    <form className="mt-4 grid gap-3 border-t border-line pt-4" onSubmit={submit}>
      <div className="grid gap-3 sm:grid-cols-[10rem_1fr]">
        <Field label="Провайдер" htmlFor="credential-provider">
          <Select id="credential-provider" value={provider} onChange={(e) => setProvider(e.target.value)}>
            {(providers.data ?? [provider]).map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Новий ключ" htmlFor="credential-key">
          <Input
            id="credential-key"
            type="password"
            autoComplete="off"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            minLength={8}
            required
          />
        </Field>
      </div>
      <p className="text-xs text-fg-muted">Ключ шифрується на сервері; після збереження видно лише останні 4 символи.</p>
      {save.isError && <ErrorText>{error}</ErrorText>}
      {save.isSuccess && (
        <p role="status" className="text-sm text-ok-fg">
          Ключ збережено: …{save.data.last4}
        </p>
      )}
      <div>
        <Button type="submit" disabled={save.isPending || apiKey.trim().length < 8}>
          Зберегти ключ
        </Button>
      </div>
    </form>
  );
}
