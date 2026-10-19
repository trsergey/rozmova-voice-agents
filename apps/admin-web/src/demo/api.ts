// The admin API calls the demo page needs: session, bot list and call tickets.
import { client, csrf, unwrap, type Schemas, type Session } from "../api/client";

export { ApiError } from "../api/client";
export type { Session };
export type Bot = Schemas["BotOut"];
export type Ticket = Schemas["TicketOut"];

export const api = {
  me: () => unwrap(client.GET("/auth/me")),
  loginWithCode: (code: string) => unwrap(client.POST("/auth/access-code", { body: { code } })),
  logout: (token: string) => unwrap(client.POST("/auth/logout", { headers: csrf(token) })),
  bots: () => unwrap(client.GET("/bots")),
  ticket: (token: string, botId: string, callId?: string) =>
    unwrap(client.POST("/calls/ticket", { headers: csrf(token), body: { bot_id: botId, call_id: callId ?? null, debug: false } })),
};
