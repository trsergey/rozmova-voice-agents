// "Скидання демо" (ROZ-45): three admin-only buttons, each clearing one area and leaving the
// others alone, so the demo can start from a clean sheet. Each asks twice, like a rollback.
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { client, csrf, unwrap } from "../../api/client";
import { AdminOnly } from "../guards";
import { useCurrentSession } from "../session";
import { Button, Card, ErrorText } from "../ui";

type Area = "calls" | "demo-bot" | "sessions";

const AREAS: { area: Area; title: string; what: string }[] = [
  { area: "calls", title: "Дзвінки", what: "Усі дзвінки та їхні події в журналі. Боти й версії лишаються." },
  {
    area: "demo-bot",
    title: "Демо-бот",
    what: "Схема, назва й модель демо-бота — як у seed; seed стає версією в ефірі. Історія версій лишається.",
  },
  {
    area: "sessions",
    title: "Сесії",
    what: "Збережений стан розмов для перепідключення. Поточні дзвінки тривають, але не відновляться після обриву.",
  },
];

export function DemoReset() {
  return (
    <AdminOnly>
      <Card title="Скидання демо">
        <ul className="grid gap-3 md:grid-cols-3">
          {AREAS.map((item) => (
            <ResetArea key={item.area} {...item} />
          ))}
        </ul>
      </Card>
    </AdminOnly>
  );
}

function ResetArea({ area, title, what }: { area: Area; title: string; what: string }) {
  const { csrf_token } = useCurrentSession();
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const reset = useMutation({
    mutationFn: async (): Promise<string> => {
      const headers = csrf(csrf_token);
      if (area === "calls") {
        const { deleted } = await unwrap(client.POST("/ops/reset/calls", { headers }));
        return `Видалено дзвінків: ${deleted}`;
      }
      if (area === "sessions") {
        const { deleted } = await unwrap(client.POST("/ops/reset/sessions", { headers }));
        return `Видалено сесій: ${deleted}`;
      }
      const bot = await unwrap(client.POST("/ops/reset/demo-bot", { headers }));
      return bot.republished ? `Seed опубліковано як v${bot.live_version}` : `Seed уже в ефірі (v${bot.live_version})`;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: area === "demo-bot" ? ["bots"] : ["calls"] }),
    onSettled: () => setConfirming(false),
  });

  return (
    <li aria-label={title} className="flex flex-col gap-2 rounded-md border border-line p-3">
      <h3 className="font-medium">{title}</h3>
      <p className="flex-1 text-sm text-fg-muted">{what}</p>
      {confirming ? (
        <div className="flex flex-wrap gap-2">
          <Button onClick={() => reset.mutate()} disabled={reset.isPending}>
            Так, скинути
          </Button>
          <Button variant="secondary" onClick={() => setConfirming(false)} disabled={reset.isPending}>
            Скасувати
          </Button>
        </div>
      ) : (
        <Button variant="secondary" onClick={() => setConfirming(true)}>
          Скинути: {title.toLowerCase()}
        </Button>
      )}
      {reset.isError && <ErrorText>Не вдалося скинути. Спробуйте ще раз.</ErrorText>}
      {reset.isSuccess && (
        <p role="status" className="text-sm text-ok-fg">
          {reset.data}
        </p>
      )}
    </li>
  );
}
