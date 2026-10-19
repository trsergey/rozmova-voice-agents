// Server state of the bot pages: the bot list, one bot, the provider list and its keys.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client, csrf, unwrap, type Schemas } from "../../api/client";
import { useCurrentSession } from "../session";

export type Bot = Schemas["BotOut"];
export type BotDetail = Schemas["BotDetailOut"];
export type Credential = Schemas["CredentialOut"];

const keys = {
  bots: ["bots"] as const,
  bot: (id: string) => ["bots", id] as const,
  credentials: (id: string) => ["bots", id, "credentials"] as const,
  providers: ["providers"] as const,
};

export function useBots() {
  return useQuery({ queryKey: keys.bots, queryFn: () => unwrap(client.GET("/bots")) });
}

export function useBot(id: string) {
  return useQuery({
    queryKey: keys.bot(id),
    queryFn: () => unwrap(client.GET("/bots/{bot_id}", { params: { path: { bot_id: id } } })),
  });
}

export function useProviders() {
  return useQuery({ queryKey: keys.providers, queryFn: () => unwrap(client.GET("/providers")), staleTime: Infinity });
}

export function useCredentials(id: string) {
  return useQuery({
    queryKey: keys.credentials(id),
    queryFn: () => unwrap(client.GET("/bots/{bot_id}/credentials", { params: { path: { bot_id: id } } })),
  });
}

export function useCreateBot() {
  const queryClient = useQueryClient();
  const { csrf_token } = useCurrentSession();
  return useMutation({
    mutationFn: (body: Schemas["BotIn"]) => unwrap(client.POST("/bots", { headers: csrf(csrf_token), body })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.bots, exact: true }),
  });
}

export function usePutCredential(id: string) {
  const queryClient = useQueryClient();
  const { csrf_token } = useCurrentSession();
  return useMutation({
    mutationFn: (body: Schemas["CredentialIn"]) =>
      unwrap(
        client.PUT("/bots/{bot_id}/credentials", { headers: csrf(csrf_token), params: { path: { bot_id: id } }, body }),
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.credentials(id) }),
  });
}
