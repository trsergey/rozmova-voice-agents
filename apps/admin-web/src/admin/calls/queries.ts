// Server state of the call log: pages of the list and one call with its timeline.
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { client, unwrap, type Schemas } from "../../api/client";

export type CallRow = Schemas["CallOut"];
export type CallDetail = Schemas["CallDetailOut"];
export type CallEvent = Schemas["CallEventOut"];
export type Outcome = "hangup" | "timeout" | "shutdown" | "limit" | "handoff" | "completed" | "abandoned" | "open";

export interface Filters {
  bot_id?: string;
  outcome?: Outcome;
  since?: string;
  until?: string;
}

export const callKeys = {
  list: (filters: Filters) => ["calls", "list", filters] as const,
  all: ["calls", "list"] as const,
  call: (id: string) => ["calls", id] as const,
};

export function useCalls(filters: Filters) {
  return useInfiniteQuery({
    queryKey: callKeys.list(filters),
    queryFn: ({ pageParam }) =>
      unwrap(client.GET("/calls", { params: { query: { ...filters, cursor: pageParam ?? undefined } } })),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor,
  });
}

export function useCall(id: string) {
  return useQuery({
    queryKey: callKeys.call(id),
    queryFn: () => unwrap(client.GET("/calls/{call_id}", { params: { path: { call_id: id } } })),
  });
}
