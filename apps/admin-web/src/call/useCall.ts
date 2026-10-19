// One call over the gateway WebSocket: a ticket from admin-api, then the socket (docs/protocol.md
// "Connecting"). A dropped connection or a gateway restart reconnects with the same call_id;
// an utterance the server did not answer is sent again after the resume. `debug` asks for a
// debug ticket and runs the bot's saved draft (`?draft=true`): the editor's test console.
import { useCallback, useEffect, useReducer, useRef } from "react";

import type { ClientMessage } from "../../../../packages/contracts/ts/protocol.client";
import type { ServerMessage } from "../../../../packages/contracts/ts/protocol.server";
import { client, csrf as csrfHeader, unwrap } from "../api/client";
import { callReducer, initialCallState } from "./callState";

async function ticketUrl(csrf: string, botId: string, callId: string | null, debug: boolean): Promise<string> {
  const ticket = await unwrap(
    client.POST("/calls/ticket", {
      headers: csrfHeader(csrf),
      body: { bot_id: botId, call_id: callId, debug },
    }),
  );
  return debug ? `${ticket.ws_url}&draft=true` : ticket.ws_url;
}

// Close codes after which the same call can go on (protocol.md "Close codes"): a restart,
// backpressure, or a connection lost without a close frame.
const RESUMABLE = new Set([1001, 1006, 1011, 1012, 1013]);
const RETRY_DELAYS_MS = [500, 1500, 4000];

export function useCall(csrf: string, botId: string, { debug = false }: { debug?: boolean } = {}) {
  const [state, dispatch] = useReducer(callReducer, initialCallState);
  const socket = useRef<WebSocket | null>(null);
  // The last utterance without a turn_end yet; resent if a resumed history lacks it.
  const unanswered = useRef<string | null>(null);
  // Bumped by every start: a socket of an earlier call (a restart) is closed and its late
  // events and retries are ignored.
  const generation = useRef(0);

  const start = useCallback(() => {
    const mine = ++generation.current;
    const current = () => generation.current === mine;
    socket.current?.close();
    socket.current = null;

    const connect = async (callId: string | null, attempt: number): Promise<void> => {
      if (!current()) return;
      dispatch({ type: "connecting", callId });
      let url: string;
      try {
        url = await ticketUrl(csrf, botId, callId, debug);
      } catch {
        if (current()) dispatch({ type: "failed", message: "Не вдалося почати розмову. Спробуйте ще раз." });
        return;
      }
      if (!current()) return;
      const ws = new WebSocket(url);
      socket.current = ws;
      let started = false;
      let ended = false;
      let currentCallId = callId;

      ws.onmessage = (event: MessageEvent<string>) => {
        if (!current()) return;
        const message = JSON.parse(event.data) as ServerMessage;
        dispatch({ type: "server", message });
        if (message.type === "session_started") {
          started = true;
          currentCallId = message.call_id;
          // The resumed history replaced the lines; an utterance the server never got is
          // sent again and shown again (protocol.md "Close codes").
          const pending = unanswered.current;
          const received = message.history?.some((item) => item.role === "user" && item.text === pending);
          if (message.resumed && pending && !received) {
            ws.send(JSON.stringify({ type: "user_text", text: pending } satisfies ClientMessage));
            dispatch({ type: "user_text", text: pending });
          }
        } else if (message.type === "turn_end") {
          unanswered.current = null;
        } else if (message.type === "session_end") {
          ended = message.reason !== "shutdown";
        }
      };

      ws.onclose = (event: CloseEvent) => {
        if (socket.current === ws) socket.current = null;
        if (ended || !current()) return;
        const resumable = RESUMABLE.has(event.code) && currentCallId !== null;
        const delay = RETRY_DELAYS_MS[attempt];
        if (resumable && delay !== undefined && (started || attempt > 0)) {
          dispatch({ type: "reconnecting" });
          setTimeout(() => void connect(currentCallId, attempt + 1), delay);
          return;
        }
        dispatch({
          type: "failed",
          message:
            event.code === 4409
              ? "Цю розмову відкрито в іншій вкладці."
              : "З'єднання з асистентом втрачено. Почніть нову розмову.",
        });
      };
    };
    unanswered.current = null;
    void connect(null, 0);
  }, [csrf, botId, debug]);

  const send = useCallback((text: string) => {
    const ws = socket.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    unanswered.current = text;
    ws.send(JSON.stringify({ type: "user_text", text } satisfies ClientMessage));
    dispatch({ type: "user_text", text });
  }, []);

  const hangup = useCallback(() => {
    const ws = socket.current;
    if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "hangup" } satisfies ClientMessage));
  }, []);

  // Leaving the page ends the socket; the server keeps the state for a resume (FR-21).
  useEffect(() => () => socket.current?.close(), []);

  return { state, start, send, hangup };
}
