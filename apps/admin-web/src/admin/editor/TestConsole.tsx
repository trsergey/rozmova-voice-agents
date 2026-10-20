// The editor's test console (FR-11): a debug call that runs the bot's saved draft, with the
// streamed replies, the tools the bot called (arguments and results), the knowledge-base
// sources, and the active node, which the editor highlights on the canvas. The call itself
// lives in EditorPage (useCall), so the canvas and this panel read the same state.
import { useEffect, useRef, useState, type FormEvent } from "react";

import type { BotLine, CallState, ToolLine } from "../../call/callState";
import { withSuperscripts } from "../../call/citations";
import { Button, Input } from "../ui";

interface Call {
  state: CallState;
  start: () => void;
  send: (text: string) => void;
  hangup: () => void;
}

const PHASE: Record<CallState["phase"], string> = {
  idle: "не почато",
  connecting: "підключення…",
  live: "триває",
  reconnecting: "відновлення…",
  ended: "завершено",
};

export function TestConsole({ call, dirty, onSaveAndStart }: { call: Call; dirty: boolean; onSaveAndStart: () => void }) {
  const { state, start, send, hangup } = call;
  const [text, setText] = useState("");
  const bottom = useRef<HTMLDivElement>(null);
  const live = state.phase === "live";
  const running = state.phase !== "idle" && state.phase !== "ended";

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
    <aside aria-label="Тестова консоль" className="flex w-96 shrink-0 flex-col rounded-lg border border-line bg-surface">
      <div className="flex items-center justify-between gap-2 border-b border-line px-3 py-2">
        <h2 className="text-sm font-semibold">
          Тест чернетки <span className="font-normal text-fg-muted">· {PHASE[state.phase]}</span>
        </h2>
        <div className="flex gap-1">
          {running && (
            <Button variant="ghost" onClick={hangup}>
              Завершити
            </Button>
          )}
          <Button variant="secondary" onClick={start}>
            {state.phase === "idle" ? "Почати" : "Почати заново"}
          </Button>
        </div>
      </div>

      {dirty && (
        <div className="flex items-center justify-between gap-2 bg-warn-bg px-3 py-2 text-xs text-warn-fg">
          <span>Тест іде на збереженій чернетці; незбережених змін у ньому немає.</span>
          <Button variant="secondary" onClick={onSaveAndStart}>
            Зберегти й почати
          </Button>
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2" aria-live="polite">
        {state.phase === "idle" && (
          <p className="text-sm text-fg-muted">
            Почніть розмову, щоб поговорити з чернеткою. Активний вузол підсвічується на холсті.
          </p>
        )}
        <ol className="grid gap-2" aria-label="Розмова">
          {state.lines.map((line, index) => (
            <li key={index}>
              {line.role === "user" ? (
                <p className="ml-8 rounded-lg bg-accent/10 px-3 py-2 text-sm whitespace-pre-wrap">{line.text}</p>
              ) : line.role === "assistant" ? (
                <BotReply line={line} />
              ) : (
                <ToolCall line={line} />
              )}
            </li>
          ))}
        </ol>
        {state.error && (
          <p role="alert" className="mt-2 text-sm text-danger">
            {state.error}
          </p>
        )}
        {state.ended && <p className="mt-2 text-center text-sm text-fg-muted">{state.ended}</p>}
        <div ref={bottom} />
      </div>

      <form onSubmit={submit} className="flex gap-2 border-t border-line p-2">
        <Input
          aria-label="Репліка абонента"
          placeholder={live ? "Напишіть як абонент…" : "Спершу почніть розмову"}
          value={text}
          onChange={(event) => setText(event.target.value)}
          disabled={!live}
          maxLength={2000}
        />
        <Button type="submit" disabled={!live || text.trim() === ""}>
          Надіслати
        </Button>
      </form>
    </aside>
  );
}

function BotReply({ line }: { line: BotLine }) {
  return (
    <div className="mr-8 rounded-lg bg-muted-bg px-3 py-2 text-sm">
      <p className="whitespace-pre-wrap">
        {withSuperscripts(line.text, line.sources)}
        {!line.done && <span className="text-fg-muted">▍</span>}
        {line.interrupted && <span className="text-fg-muted"> (перервано)</span>}
      </p>
      {line.sources.length > 0 && (
        <ol aria-label="Джерела" className="mt-1.5 list-decimal pl-5 text-xs text-fg-muted">
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

function ToolCall({ line }: { line: ToolLine }) {
  const pending = line.result === undefined;
  return (
    <details className="rounded-md border border-line px-2 py-1 text-xs">
      <summary className="cursor-pointer font-mono">
        ⚙ {line.name}
        <span className="text-fg-muted">{pending ? " · виконується…" : " · готово"}</span>
      </summary>
      <pre className="mt-1 overflow-x-auto">{JSON.stringify(line.args, null, 2)}</pre>
      {!pending && <pre className="mt-1 overflow-x-auto border-t border-line pt-1">{JSON.stringify(line.result, null, 2)}</pre>}
    </details>
  );
}
