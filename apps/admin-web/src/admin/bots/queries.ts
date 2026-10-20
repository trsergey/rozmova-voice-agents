// Server state of the bot pages: the bot list, one bot, the provider list and its keys, and
// publishing: versions, diffs, the draft's validation, eval jobs, publish and rollback.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client, csrf, unwrap, type Schemas } from "../../api/client";
import { useCurrentSession } from "../session";

export type Bot = Schemas["BotOut"];
export type BotDetail = Schemas["BotDetailOut"];
export type Credential = Schemas["CredentialOut"];
export type Version = Schemas["VersionItemOut"];
export type FlowDiff = Schemas["FlowDiff"];
export type EvalJob = Schemas["EvalJobOut"];
// A side of a diff: a version number or the draft.
export type DiffSide = number | "draft";

// While an eval job runs, its status is polled this often.
export const EVAL_POLL_MS = 1000;

const keys = {
  bots: ["bots"] as const,
  bot: (id: string) => ["bots", id] as const,
  credentials: (id: string) => ["bots", id, "credentials"] as const,
  providers: ["providers"] as const,
  versions: (id: string) => ["bots", id, "versions"] as const,
  diff: (id: string, a: number, b: DiffSide) => ["bots", id, "diff", a, b] as const,
  // Keyed by the draft's save time: a saved edit is a new draft to check.
  validation: (id: string, savedAt: string | null) => ["bots", id, "validation", savedAt] as const,
  evalJob: (jobId: string) => ["evals", jobId] as const,
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

export function useVersions(id: string) {
  return useQuery({
    queryKey: keys.versions(id),
    queryFn: () => unwrap(client.GET("/bots/{bot_id}/versions", { params: { path: { bot_id: id } } })),
  });
}

export function useDiff(id: string, a: number | null, b: DiffSide) {
  return useQuery({
    queryKey: keys.diff(id, a ?? 0, b),
    queryFn: () =>
      unwrap(
        client.GET("/bots/{bot_id}/versions/{a}/diff/{b}", {
          params: { path: { bot_id: id, a: a ?? 0, b: String(b) } },
        }),
      ),
    enabled: a != null,
  });
}

// The engine's check of the saved draft. A POST, but it only reads: everyone may run it.
export function useValidation(id: string, savedAt: string | null) {
  const { csrf_token } = useCurrentSession();
  return useQuery({
    queryKey: keys.validation(id, savedAt),
    queryFn: () =>
      unwrap(client.POST("/bots/{bot_id}/validate", { headers: csrf(csrf_token), params: { path: { bot_id: id } } })),
  });
}

export function useStartEval(id: string) {
  const { csrf_token } = useCurrentSession();
  return useMutation({
    mutationFn: () =>
      unwrap(client.POST("/bots/{bot_id}/evals", { headers: csrf(csrf_token), params: { path: { bot_id: id } } })),
  });
}

// An eval job's status, polled until it is no longer running; then the bot card shows its
// report, so the bot is refetched.
export function useEvalJob(botId: string, jobId: string | null) {
  const queryClient = useQueryClient();
  return useQuery({
    queryKey: keys.evalJob(jobId ?? ""),
    queryFn: async () => {
      const job = await unwrap(client.GET("/evals/{job_id}", { params: { path: { job_id: jobId ?? "" } } }));
      if (job.state !== "running") await queryClient.invalidateQueries({ queryKey: keys.bot(botId), exact: true });
      return job;
    },
    enabled: jobId != null,
    refetchInterval: (query) => (query.state.data?.state === "running" ? EVAL_POLL_MS : false),
  });
}

function useAfterPublish() {
  const queryClient = useQueryClient();
  // The bot (live version), its versions and diffs, and the bot list all change.
  return () => queryClient.invalidateQueries({ queryKey: keys.bots });
}

export function usePublish(id: string) {
  const { csrf_token } = useCurrentSession();
  const refresh = useAfterPublish();
  return useMutation({
    mutationFn: (note: string) =>
      unwrap(
        client.POST("/bots/{bot_id}/publish", {
          headers: csrf(csrf_token),
          params: { path: { bot_id: id } },
          body: { note: note.trim() || null },
        }),
      ),
    onSuccess: refresh,
  });
}

export function useRollback(id: string) {
  const { csrf_token } = useCurrentSession();
  const refresh = useAfterPublish();
  return useMutation({
    mutationFn: (version: number) =>
      unwrap(
        client.POST("/bots/{bot_id}/rollback/{version}", {
          headers: csrf(csrf_token),
          params: { path: { bot_id: id, version } },
        }),
      ),
    onSuccess: refresh,
  });
}
