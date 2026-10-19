// The chat with one bot: streamed replies, cited knowledge-base sources, handoff and end.
import { useEffect, useRef, useState, type FormEvent } from "react";

import type { Bot } from "./api";
import type { BotLine } from "./callState";
import { useCall } from "./useCall";

interface Props {
  csrf: string;
  bot: Bot;
  onLogout: () => void;
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
          <button type="button" className="link" onClick={onLogout}>
            Вийти
          </button>
        </span>
      </div>

      <div className="lines" aria-live="polite">
        {state.lines.map((line, index) =>
          line.role === "user" ? (
            <p key={index} className="line user">
              {line.text}
            </p>
          ) : (
            <BotReply key={index} line={line} />
          ),
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
        {line.text}
        {line.interrupted && <span className="muted"> (перервано)</span>}
      </p>
      {line.sources.length > 0 && (
        <ol className="sources" aria-label="Джерела">
          {line.sources.map((source) => (
            <li key={source.marker} value={source.marker}>
              {source.document_title}
              {source.section && ` — ${source.section}`}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
