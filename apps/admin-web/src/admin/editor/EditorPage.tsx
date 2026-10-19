// The flow editor (FR-10): the bot's draft on a canvas. Admins add nodes from the palette,
// connect outlets, move and delete, undo and redo, and save; every load and save runs the
// validator, whose errors frame their nodes. Viewers see the same canvas read-only.
import { useMutation, useQuery } from "@tanstack/react-query";
import { AlertDialog } from "radix-ui";
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { Link, useBlocker, useParams } from "react-router";

import { client, csrf, unwrap } from "../../api/client";
import { useBot } from "../bots/queries";
import { formatTime } from "../format";
import { useCanEdit, useCurrentSession } from "../session";
import { Button, ErrorText } from "../ui";
import { Canvas, PALETTE_MIME } from "./Canvas";
import { ISSUE_TEXT, LAYOUT, NODE_KINDS, PALETTE, toFlowGraph, type FlowIssue, type NodeType } from "./graph";
// The forms bring zod and React Hook Form; they load when a node is first selected.
const NodeForm = lazy(() => import("./NodeForm").then((m) => ({ default: m.NodeForm })));
import { selectDirty, useEditor } from "./store";

export function EditorPage() {
  const { botId = "" } = useParams();
  const bot = useBot(botId);
  const canEdit = useCanEdit();
  const { csrf_token } = useCurrentSession();
  const draft = useQuery({
    queryKey: ["bots", botId, "draft"],
    queryFn: () => unwrap(client.GET("/bots/{bot_id}/draft", { params: { path: { bot_id: botId } } })),
    staleTime: Infinity,
  });
  const dirty = useEditor(selectDirty);
  const [warnings, setWarnings] = useState<FlowIssue[]>([]);
  const [savedAt, setSavedAt] = useState<string | null>(null);

  const validate = useMutation({
    mutationFn: () =>
      unwrap(
        client.POST("/bots/{bot_id}/validate", { headers: csrf(csrf_token), params: { path: { bot_id: botId } } }),
      ),
    onSuccess: (result) => {
      useEditor.getState().setIssues(result.errors ?? []);
      setWarnings(result.warnings ?? []);
    },
  });

  const save = useMutation({
    mutationFn: () => {
      const { graph } = useEditor.getState();
      return unwrap(
        client.PUT("/bots/{bot_id}/draft", {
          headers: csrf(csrf_token),
          params: { path: { bot_id: botId } },
          body: toFlowGraph(graph),
        }),
      ).then((out) => ({ out, graph }));
    },
    onSuccess: ({ out, graph }) => {
      useEditor.getState().markSaved(graph);
      setSavedAt(out.updated_at ?? null);
      validate.mutate();
    },
  });

  // Load the draft into the editor once per bot, and check it right away.
  const loadedFor = useRef<string | null>(null);
  const runValidate = validate.mutate;
  useEffect(() => {
    if (!draft.data || loadedFor.current === botId) return;
    loadedFor.current = botId;
    useEditor.getState().load(draft.data.flow);
    setSavedAt(draft.data.updated_at ?? null);
    runValidate();
  }, [draft.data, botId, runValidate]);

  useShortcuts(canEdit, () => {
    if (selectDirty(useEditor.getState()) && !save.isPending) save.mutate();
  });
  const blocker = useBlocker(({ currentLocation, nextLocation }) => dirty && currentLocation.pathname !== nextLocation.pathname);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  if (draft.isPending || bot.isPending) return <p className="text-sm text-fg-muted">Завантаження…</p>;
  if (draft.isError || bot.isError) return <ErrorText>Не вдалося завантажити чернетку.</ErrorText>;

  return (
    <div className="flex h-[calc(100vh-8.5rem)] min-h-[28rem] flex-col gap-3">
      <Toolbar
        botId={botId}
        botName={bot.data.name}
        canEdit={canEdit}
        dirty={dirty}
        saving={save.isPending}
        savedAt={savedAt}
        onSave={() => save.mutate()}
      />
      {save.isError && <ErrorText>Не вдалося зберегти чернетку.</ErrorText>}
      <div className="flex min-h-0 flex-1 gap-3">
        {canEdit && <Palette />}
        <div className="min-w-0 flex-1 overflow-hidden rounded-lg border border-line bg-surface" aria-label="Холст флоу">
          <Canvas editable={canEdit} />
        </div>
        <SidePanel editable={canEdit} warnings={warnings} checking={validate.isPending} />
      </div>
      <LeaveDialog
        open={blocker.state === "blocked"}
        onStay={() => blocker.reset?.()}
        onLeave={() => blocker.proceed?.()}
      />
    </div>
  );
}

function Toolbar(props: {
  botId: string;
  botName: string;
  canEdit: boolean;
  dirty: boolean;
  saving: boolean;
  savedAt: string | null;
  onSave: () => void;
}) {
  const canUndo = useEditor((state) => state.past.length > 0);
  const canRedo = useEditor((state) => state.future.length > 0);
  const { undo, redo } = useEditor.getState();

  return (
    <div className="flex flex-wrap items-center gap-3">
      <Link to={`/admin/bots/${props.botId}`} className="text-sm text-accent hover:underline">
        ← {props.botName}
      </Link>
      <h1 className="text-lg font-semibold">Редактор флоу</h1>
      <span className="text-sm text-fg-muted" role="status">
        {props.dirty
          ? "Є незбережені зміни"
          : props.savedAt
            ? `Збережено о ${formatTime(props.savedAt)}`
            : "Чернетку ще не зберігали"}
      </span>
      {props.canEdit && (
        <div className="ml-auto flex gap-2">
          <Button variant="secondary" onClick={undo} disabled={!canUndo} title="Ctrl/⌘+Z">
            Скасувати
          </Button>
          <Button variant="secondary" onClick={redo} disabled={!canRedo} title="Ctrl/⌘+Shift+Z">
            Повторити
          </Button>
          <Button onClick={props.onSave} disabled={!props.dirty || props.saving} title="Ctrl/⌘+S">
            {props.saving ? "Збереження…" : "Зберегти чернетку"}
          </Button>
        </div>
      )}
    </div>
  );
}

function Palette() {
  const add = (type: NodeType) => {
    const { graph, addNode, select } = useEditor.getState();
    const bottom = Math.max(-LAYOUT.rowGap, ...graph.nodes.map((node) => node.ui?.y ?? 0));
    select(addNode(type, 0, bottom + LAYOUT.rowGap));
  };

  return (
    <aside aria-label="Палітра" className="hidden w-44 shrink-0 flex-col gap-1.5 overflow-y-auto md:flex">
      <p className="text-xs text-fg-muted">Перетягніть на холст або натисніть:</p>
      {PALETTE.map((type) => (
        <button
          key={type}
          type="button"
          draggable
          onDragStart={(event) => {
            event.dataTransfer.setData(PALETTE_MIME, type);
            event.dataTransfer.effectAllowed = "move";
          }}
          onClick={() => add(type)}
          className={`rounded-md border border-t-4 border-line bg-surface px-2 py-1.5 text-left text-sm hover:bg-muted-bg ${NODE_KINDS[type].accent}`}
        >
          <span className="block font-medium">{NODE_KINDS[type].label}</span>
          <span className="block text-xs text-fg-muted">{NODE_KINDS[type].hint}</span>
        </button>
      ))}
    </aside>
  );
}

// The selected node's form, or the whole flow's check when nothing is selected.
function SidePanel({ editable, warnings, checking }: { editable: boolean; warnings: FlowIssue[]; checking: boolean }) {
  const node = useEditor((state) => state.graph.nodes.find((n) => n.id === state.selectedNodeId));
  const issues = useEditor((state) => state.issues);
  if (!node) return <IssuesPanel warnings={warnings} checking={checking} />;
  return (
    <aside aria-label="Налаштування вузла" className="w-80 shrink-0 overflow-y-auto rounded-lg border border-line bg-surface p-3">
      <Suspense fallback={<p className="text-sm text-fg-muted">Завантаження…</p>}>
        <NodeForm node={node} issues={issues.filter((issue) => issue.node_id === node.id)} editable={editable} />
      </Suspense>
    </aside>
  );
}

function IssuesPanel({ warnings, checking }: { warnings: FlowIssue[]; checking: boolean }) {
  const errors = useEditor((state) => state.issues);
  const dirty = useEditor(selectDirty);
  const line = (issue: FlowIssue, index: number) => (
    <li key={index} className="text-xs">
      {issue.node_id && (
        <button type="button" className="mr-1 font-mono underline" onClick={() => useEditor.getState().select(issue.node_id)}>
          {issue.node_id}
        </button>
      )}
      {ISSUE_TEXT[issue.code] ?? issue.message}
    </li>
  );

  return (
    <aside aria-label="Перевірка" className="hidden w-80 shrink-0 overflow-y-auto rounded-lg border border-line bg-surface p-3 lg:block">
      <h2 className="mb-2 text-sm font-medium">
        Перевірка{checking && <span className="font-normal text-fg-muted"> · триває…</span>}
      </h2>
      {dirty && <p className="mb-2 text-xs text-fg-muted">Перевіряється збережена чернетка.</p>}
      {errors.length === 0 && warnings.length === 0 && !checking && (
        <p className="text-sm text-ok-fg">Помилок немає, флоу можна публікувати.</p>
      )}
      {errors.length > 0 && (
        <>
          <p className="mb-1 text-sm text-danger">Помилок: {errors.length}</p>
          <ul className="mb-3 grid gap-1 text-danger">{errors.map(line)}</ul>
        </>
      )}
      {warnings.length > 0 && (
        <>
          <p className="mb-1 text-sm text-warn-fg">Попереджень: {warnings.length}</p>
          <ul className="grid gap-1 text-fg-muted">{warnings.map(line)}</ul>
        </>
      )}
    </aside>
  );
}

function LeaveDialog({ open, onStay, onLeave }: { open: boolean; onStay: () => void; onLeave: () => void }) {
  return (
    <AlertDialog.Root open={open} onOpenChange={(next) => !next && onStay()}>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="fixed inset-0 bg-black/40" />
        <AlertDialog.Content className="fixed top-1/2 left-1/2 w-[calc(100%-2rem)] max-w-sm -translate-x-1/2 -translate-y-1/2 rounded-lg border border-line bg-surface p-5 text-fg shadow-xl">
          <AlertDialog.Title className="text-lg font-semibold">Піти без збереження?</AlertDialog.Title>
          <AlertDialog.Description className="mt-2 text-sm text-fg-muted">
            У чернетці є незбережені зміни; вони пропадуть.
          </AlertDialog.Description>
          <div className="mt-5 flex justify-end gap-2">
            <AlertDialog.Cancel asChild>
              <Button variant="secondary">Залишитись</Button>
            </AlertDialog.Cancel>
            <AlertDialog.Action asChild>
              <Button className="bg-danger text-white hover:bg-danger/90" onClick={onLeave}>
                Піти
              </Button>
            </AlertDialog.Action>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}

// Ctrl/⌘+Z undo, Ctrl/⌘+Shift+Z or Ctrl+Y redo, Ctrl/⌘+S save; not while typing in a field.
function useShortcuts(enabled: boolean, onSave: () => void) {
  const saveRef = useRef(onSave);
  useEffect(() => {
    saveRef.current = onSave;
  });
  useEffect(() => {
    if (!enabled) return;
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest("input, textarea, select, [contenteditable=true]")) return;
      if (!(event.metaKey || event.ctrlKey)) return;
      const key = event.key.toLowerCase();
      const { undo, redo } = useEditor.getState();
      if (key === "z" && !event.shiftKey) undo();
      else if ((key === "z" && event.shiftKey) || key === "y") redo();
      else if (key === "s") saveRef.current();
      else return;
      event.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [enabled]);
}
