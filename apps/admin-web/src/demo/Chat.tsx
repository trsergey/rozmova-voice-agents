// The chat with one bot as a subscriber sees it: streamed replies without citation markers,
// the sources as a line of titles, starter prompts, handoff and end. The public demo page and
// the admin's demo page both show it.
import { useEffect, useRef, useState, type FormEvent } from "react";

import type { Bot } from "./api";
import type { BotLine } from "../call/callState";
import { sourceTitles, withoutMarkers } from "../call/citations";
import { useCall } from "../call/useCall";

// What a visitor can try first: S1, S2, S3 and the handoff.
export const STARTERS = [
  "Скільки грошей у мене на рахунку?",
  "Що входить у тариф «Безлім»?",
  "Підключіть роумінг Європа",
  "З'єднайте мене з оператором",
];

interface Props {
  csrf: string;
  bot: Bot;
  // Absent in the admin, where the session is the admin's own.
  onLogout?: () => void;
}

export function Chat({ csrf, bot, onLogout }: Props) {
  const { state, start, send, hangup } = useCall(csrf, bot.id);
  const [text, setText] = useState("");
  const bottom = useRef<HTMLDivElement>(null);
  const live = state.phase === "live";

  useEffect(() => {
    bottom.current?.scrollIntoView?.({ block: "end" });
  }, [state.lines]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const utterance = text.trim();
    if (!utterance) return;
    send(utterance);
    setText("");
  };
  const asked = state.lines.some((line) => line.role === "user");

  return (
    <section className="card chat">
      <div className="chat-bar">
        <span>
          {bot.name}
          {bot.live_version !== null && <span className="muted"> · v{bot.live_version}</span>}
        </span>
        <span className="chat-actions">
          {live && (
            <button type="button" className="link" onClick={hangup}>
              Завершити
            </button>
          )}
          {onLogout && (
            <button type="button" className="link" onClick={onLogout}>
              Вийти
            </button>
          )}
        </span>
      </div>

      <div className="lines" aria-live="polite">
        {state.lines.map((line, index) =>
          line.role === "user" ? (
            <p key={index} className="line user">
              {line.text}
            </p>
          ) : line.role === "assistant" ? (
            <BotReply key={index} line={line} />
          ) : null, // tool lines come with debug calls only, never to the demo
        )}
        {(state.phase === "connecting" || state.phase === "reconnecting") && (
          <p className="muted">{state.phase === "connecting" ? "Підключення…" : "Відновлюємо з'єднання…"}</p>
        )}
        {state.error && (
          <p className="error" role="alert">
            {state.error}
          </p>
        )}
        {state.ended && <p className="status">{state.ended}</p>}
        {live && !asked && (
          <div className="starters" aria-label="Що можна спитати">
            {STARTERS.map((starter) => (
              <button key={starter} type="button" className="starter" onClick={() => send(starter)}>
                {starter}
              </button>
            ))}
          </div>
        )}
        <div ref={bottom} />
      </div>

      {state.phase === "idle" || state.phase === "ended" ? (
        <button type="button" onClick={start}>
          {state.phase === "idle" ? "Почати розмову" : "Нова розмова"}
        </button>
      ) : (
        <form className="composer" onSubmit={submit}>
          <input
            aria-label="Повідомлення"
            placeholder="Напишіть повідомлення…"
            value={text}
            onChange={(event) => setText(event.target.value)}
            disabled={!live}
            maxLength={2000}
            autoFocus
          />
          <button type="submit" disabled={!live || text.trim() === ""}>
            Надіслати
          </button>
        </form>
      )}
    </section>
  );
}

function BotReply({ line }: { line: BotLine }) {
  return (
    <div className={`line bot${line.done ? "" : " streaming"}`}>
      <p>
        {withoutMarkers(line.text, !line.done)}
        {line.interrupted && <span className="muted"> (перервано)</span>}
      </p>
      {line.sources.length > 0 && (
        <p className="sources">
          <span className="sources-label">Джерела:</span> {sourceTitles(line.sources).join(" · ")}
        </p>
      )}
    </div>
  );
}
