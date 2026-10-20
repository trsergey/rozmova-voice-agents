// The demo chat inside the admin (ROZ-89): the same chat a guest gets at /demo/, with the
// admin's sidebar kept, on the admin's own session. The guest page opens in a new tab.
import { Chat } from "../../demo/Chat";
import "../../demo/demo.css";
import { useBots } from "../bots/queries";
import { useCurrentSession } from "../session";
import { ErrorText, PageHeader } from "../ui";

export function DemoChatPage() {
  const { csrf_token } = useCurrentSession();
  const bots = useBots();
  const bot = bots.data?.find((candidate) => candidate.live_version_id !== null);

  return (
    <>
      <PageHeader title="Демо-чат">
        <a href="/demo/" target="_blank" rel="noreferrer" className="text-sm text-accent hover:underline">
          Сторінка для гостей ↗
        </a>
      </PageHeader>
      <p className="mb-4 max-w-2xl text-sm text-fg-muted">
        Те саме, що бачить абонент: опублікована версія бота, без службових подій.
      </p>
      {bots.isPending && <p className="text-sm text-fg-muted">Завантаження…</p>}
      {bots.isError && <ErrorText>Не вдалося завантажити ботів.</ErrorText>}
      {bots.data && !bot && <p className="text-sm text-fg-muted">Жоден бот ще не опублікований.</p>}
      {bot && (
        <div className="demo embedded max-w-2xl">
          <Chat csrf={csrf_token} bot={bot} />
        </div>
      )}
    </>
  );
}
