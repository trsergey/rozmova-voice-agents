// The admin session in the TanStack Query cache: `null` when logged out. Login, access-code
// login and logout replace it; every page reads the role and the CSRF token from here.
import { queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useContext } from "react";

import { ApiError, client, csrf, unwrap, type Session } from "../api/client";

export const sessionQuery = queryOptions({
  queryKey: ["session"],
  queryFn: async (): Promise<Session | null> => {
    try {
      return await unwrap(client.GET("/auth/me"));
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return null;
      throw error;
    }
  },
  staleTime: Infinity,
  retry: false,
});

export function useSession() {
  return useQuery(sessionQuery);
}

// RequireSession provides the session it checked, so every page below it renders with the
// same one; after logout the guard unmounts them before any sees no session.
export const SessionContext = createContext<Session | null>(null);

export function useCurrentSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useCurrentSession outside RequireSession");
  return session;
}

// Viewers see everything and change nothing (FR-14); the API enforces it, the UI hides the controls.
export function useCanEdit(): boolean {
  return useCurrentSession().user.role === "admin";
}

export function useLogin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { email: string; password: string }) => unwrap(client.POST("/auth/login", { body })),
    onSuccess: (session) => queryClient.setQueryData(sessionQuery.queryKey, session),
  });
}

export function useAccessCodeLogin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (code: string) => unwrap(client.POST("/auth/access-code", { body: { code } })),
    onSuccess: (session) => queryClient.setQueryData(sessionQuery.queryKey, session),
  });
}

export function useLogout() {
  const queryClient = useQueryClient();
  const session = useCurrentSession();
  return useMutation({
    mutationFn: () => unwrap(client.POST("/auth/logout", { headers: csrf(session.csrf_token) })),
    // Logged out either way: an expired session is as good as a deleted one. RequireSession
    // then sends the visitor to the login page; nothing of the old session's data stays cached.
    onSettled: () => {
      queryClient.setQueryData(sessionQuery.queryKey, null);
      queryClient.removeQueries({ predicate: (query) => query.queryKey[0] !== "session" });
    },
  });
}
