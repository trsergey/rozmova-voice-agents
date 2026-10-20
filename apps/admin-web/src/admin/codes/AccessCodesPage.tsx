// Demo access codes (FR-14), admins only: issue one per recipient with a label, see when it
// was last used, revoke it. The code itself is shown once, right after it is issued: the
// server keeps only its hash.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertDialog } from "radix-ui";
import { useState, type FormEvent } from "react";

import { client, csrf, unwrap, type Schemas } from "../../api/client";
import { formatDateTime } from "../format";
import { useCurrentSession } from "../session";
import { Badge, Button, ErrorText, Field, Input, PageHeader } from "../ui";

type Code = Schemas["AccessCodeOut"];
type Issued = Schemas["IssuedAccessCode"];

const key = ["access-codes"] as const;

export function AccessCodesPage() {
  const codes = useQuery({ queryKey: key, queryFn: () => unwrap(client.GET("/access-codes")) });
  const [issued, setIssued] = useState<Issued | null>(null);
  // Revoked codes stay in the API for the record; the list shows the ones that work.
  const [showRevoked, setShowRevoked] = useState(false);
  const revoked = codes.data?.filter((code) => code.revoked_at).length ?? 0;
  const shown = codes.data?.filter((code) => showRevoked || !code.revoked_at) ?? [];

  return (
    <>
      <PageHeader title="Коди доступу" />
      <div className="mb-6 grid max-w-xl gap-4">
        <IssueForm onIssued={setIssued} />
        {issued && <IssuedCode issued={issued} onDone={() => setIssued(null)} />}
      </div>
      {codes.isError && <ErrorText>Не вдалося завантажити коди.</ErrorText>}
      {codes.data?.length === 0 && <p className="text-sm text-fg-muted">Кодів ще немає.</p>}
      {revoked > 0 && (
        <label className="mb-3 flex items-center gap-2 text-sm">
          <input type="checkbox" checked={showRevoked} onChange={(event) => setShowRevoked(event.target.checked)} />
          Показати відкликані ({revoked})
        </label>
      )}
      {codes.data && codes.data.length > 0 && shown.length === 0 && (
        <p className="text-sm text-fg-muted">Діючих кодів немає.</p>
      )}
      {shown.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-line bg-surface">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-line text-fg-muted">
              <tr>
                <th className="px-4 py-2 font-medium">Для кого</th>
                <th className="px-4 py-2 font-medium">Виданий</th>
                <th className="px-4 py-2 font-medium">Останній вхід</th>
                <th className="px-4 py-2 font-medium">Стан</th>
                <th className="px-4 py-2" />
              </tr>
            </thead>
            <tbody>
              {shown.map((code) => (
                <tr key={code.id} className="border-b border-line last:border-b-0">
                  <td className="px-4 py-3 font-medium">{code.label}</td>
                  <td className="px-4 py-3 text-fg-muted">{formatDateTime(code.created_at)}</td>
                  <td className="px-4 py-3 text-fg-muted">{code.last_used_at ? formatDateTime(code.last_used_at) : "—"}</td>
                  <td className="px-4 py-3">
                    {code.revoked_at ? <Badge tone="bad">відкликаний</Badge> : <Badge tone="ok">діє</Badge>}
                  </td>
                  <td className="px-4 py-3 text-right">{!code.revoked_at && <RevokeButton code={code} />}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function IssueForm({ onIssued }: { onIssued: (issued: Issued) => void }) {
  const queryClient = useQueryClient();
  const { csrf_token } = useCurrentSession();
  const [label, setLabel] = useState("");
  const issue = useMutation({
    mutationFn: (label: string) => unwrap(client.POST("/access-codes", { headers: csrf(csrf_token), body: { label } })),
    onSuccess: (issued) => {
      onIssued(issued);
      setLabel("");
      void queryClient.invalidateQueries({ queryKey: key });
    },
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    issue.mutate(label.trim());
  };

  return (
    <form onSubmit={submit} className="grid gap-3 rounded-lg border border-line bg-surface p-4">
      <Field label="Для кого (компанія або людина)" htmlFor="code-label">
        <Input id="code-label" value={label} onChange={(e) => setLabel(e.target.value)} maxLength={100} required />
      </Field>
      {issue.isError && <ErrorText>Не вдалося видати код.</ErrorText>}
      <div>
        <Button type="submit" disabled={issue.isPending || label.trim() === ""}>
          Видати код
        </Button>
      </div>
    </form>
  );
}

function IssuedCode({ issued, onDone }: { issued: Issued; onDone: () => void }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    await navigator.clipboard?.writeText(issued.code);
    setCopied(true);
  };

  return (
    <div role="status" className="rounded-lg border border-accent/40 bg-accent/5 p-4">
      <p className="text-sm">
        Код для <span className="font-medium">{issued.label}</span>. Скопіюйте його зараз: більше його не буде видно.
      </p>
      <p className="my-3 font-mono text-2xl tracking-wider select-all">{issued.code}</p>
      <div className="flex gap-2">
        <Button variant="secondary" onClick={copy}>
          {copied ? "Скопійовано" : "Скопіювати"}
        </Button>
        <Button variant="ghost" onClick={onDone}>
          Готово
        </Button>
      </div>
    </div>
  );
}

function RevokeButton({ code }: { code: Code }) {
  const queryClient = useQueryClient();
  const { csrf_token } = useCurrentSession();
  const revoke = useMutation({
    mutationFn: () =>
      unwrap(client.DELETE("/access-codes/{code_id}", { headers: csrf(csrf_token), params: { path: { code_id: code.id } } })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: key }),
  });

  return (
    <AlertDialog.Root>
      <AlertDialog.Trigger asChild>
        <Button variant="ghost" disabled={revoke.isPending}>
          Відкликати
        </Button>
      </AlertDialog.Trigger>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="fixed inset-0 bg-black/40" />
        <AlertDialog.Content className="fixed top-1/2 left-1/2 w-[calc(100%-2rem)] max-w-sm -translate-x-1/2 -translate-y-1/2 rounded-lg border border-line bg-surface p-5 text-fg shadow-xl">
          <AlertDialog.Title className="text-lg font-semibold">Відкликати код?</AlertDialog.Title>
          <AlertDialog.Description className="mt-2 text-sm text-fg-muted">
            «{code.label}» більше не зможе увійти, а відкриті сесії з цим кодом завершаться одразу.
          </AlertDialog.Description>
          <div className="mt-5 flex justify-end gap-2">
            <AlertDialog.Cancel asChild>
              <Button variant="secondary">Скасувати</Button>
            </AlertDialog.Cancel>
            <AlertDialog.Action asChild>
              <Button className="bg-danger text-white hover:bg-danger/90" onClick={() => revoke.mutate()}>
                Відкликати
              </Button>
            </AlertDialog.Action>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}
