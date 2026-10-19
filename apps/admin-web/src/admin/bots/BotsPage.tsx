// The bot list (FR-12): live version and draft state at a glance; admins create bots here.
import { Dialog } from "radix-ui";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router";

import { formatDateTime } from "../format";
import { AdminOnly } from "../guards";
import { Badge, Button, ErrorText, Field, Input, PageHeader, Select } from "../ui";
import { useBots, useCreateBot, useProviders } from "./queries";

export function BotsPage() {
  const bots = useBots();

  return (
    <>
      <PageHeader title="Боти">
        <AdminOnly>
          <CreateBotDialog />
        </AdminOnly>
      </PageHeader>
      {bots.isPending && <p className="text-sm text-fg-muted">Завантаження…</p>}
      {bots.isError && <ErrorText>Не вдалося завантажити ботів.</ErrorText>}
      {bots.data?.length === 0 && <p className="text-sm text-fg-muted">Ботів ще немає.</p>}
      {bots.data && bots.data.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-line bg-surface">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-line text-fg-muted">
              <tr>
                <th className="px-4 py-2 font-medium">Назва</th>
                <th className="px-4 py-2 font-medium">Модель</th>
                <th className="px-4 py-2 font-medium">В ефірі</th>
                <th className="px-4 py-2 font-medium">Чернетка змінена</th>
              </tr>
            </thead>
            <tbody>
              {bots.data.map((bot) => (
                <tr key={bot.id} className="border-b border-line last:border-b-0 hover:bg-muted-bg">
                  <td className="px-4 py-3">
                    <Link to={bot.id} className="font-medium text-accent hover:underline">
                      {bot.name}
                    </Link>
                  </td>
                  <td className="px-4 py-3 text-fg-muted">
                    {bot.provider} · {bot.model}
                  </td>
                  <td className="px-4 py-3">
                    {bot.live_version != null ? (
                      <Badge tone="ok">v{bot.live_version}</Badge>
                    ) : (
                      <Badge tone="neutral">не опубліковано</Badge>
                    )}
                  </td>
                  <td className="px-4 py-3 text-fg-muted">
                    {bot.draft_updated_at ? formatDateTime(bot.draft_updated_at) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function CreateBotDialog() {
  const [open, setOpen] = useState(false);
  const providers = useProviders();
  const create = useCreateBot();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [provider, setProvider] = useState("openai");
  const [model, setModel] = useState("gpt-4.1-mini");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    create.mutate({ name, provider, model }, { onSuccess: (bot) => navigate(bot.id) });
  };

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>
        <Button>Новий бот</Button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/40" />
        <Dialog.Content className="fixed top-1/2 left-1/2 w-[calc(100%-2rem)] max-w-md -translate-x-1/2 -translate-y-1/2 rounded-lg border border-line bg-surface p-5 text-fg shadow-xl">
          <Dialog.Title className="text-lg font-semibold">Новий бот</Dialog.Title>
          <Dialog.Description className="mt-1 mb-4 text-sm text-fg-muted">
            Бот почнеться з порожньої чернетки; флоу редагується в редакторі.
          </Dialog.Description>
          <form className="grid gap-4" onSubmit={submit}>
            <Field label="Назва" htmlFor="bot-name">
              <Input id="bot-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={100} required />
            </Field>
            <Field label="Провайдер" htmlFor="bot-provider">
              <Select id="bot-provider" value={provider} onChange={(e) => setProvider(e.target.value)}>
                {(providers.data ?? [provider]).map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Модель" htmlFor="bot-model">
              <Input id="bot-model" value={model} onChange={(e) => setModel(e.target.value)} maxLength={100} required />
            </Field>
            {create.isError && <ErrorText>Не вдалося створити бота.</ErrorText>}
            <div className="flex justify-end gap-2">
              <Dialog.Close asChild>
                <Button variant="secondary">Скасувати</Button>
              </Dialog.Close>
              <Button type="submit" disabled={create.isPending || name.trim() === ""}>
                Створити
              </Button>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
