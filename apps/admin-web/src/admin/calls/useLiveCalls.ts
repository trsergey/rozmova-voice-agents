// `WS /calls/live` for the calls page: the calls in progress, and a refresh of the list when
// a call starts or ends. Reconnects with backoff; 4401/4403 (logged out, not an admin) end it.
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useReducer, useState } from "react";

import { liveReducer, parseLiveEvent } from "./live";
import { callKeys } from "./queries";

export type LiveStatus = "connecting" | "live" | "reconnecting" | "closed";

const BACKOFF_MS = [1000, 2000, 5000, 10000];

export function liveUrl(): string {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${location.host}/calls/live`;
}

export function useLiveCalls() {
  const queryClient = useQueryClient();
  const [calls, dispatch] = useReducer(liveReducer, []);
  const [status, setStatus] = useState<LiveStatus>("connecting");

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let attempt = 0;
    let stopped = false;

    const connect = () => {
      socket = new WebSocket(liveUrl());
      socket.onopen = () => {
        attempt = 0;
        setStatus("live");
      };
      socket.onmessage = (message: MessageEvent<string>) => {
        const event = parseLiveEvent(message.data);
        if (!event) return;
        dispatch(event);
        if (event.type === "session_started" || event.type === "session_end") {
          void queryClient.invalidateQueries({ queryKey: callKeys.all });
        }
      };
      socket.onclose = (event: CloseEvent) => {
        if (stopped) return;
        if (event.code === 4401 || event.code === 4403) {
          setStatus("closed");
          return;
        }
        setStatus("reconnecting");
        retry = setTimeout(connect, BACKOFF_MS[Math.min(attempt++, BACKOFF_MS.length - 1)]);
      };
    };

    connect();
    return () => {
      stopped = true;
      clearTimeout(retry);
      socket?.close();
    };
  }, [queryClient]);

  return { calls, status };
}
