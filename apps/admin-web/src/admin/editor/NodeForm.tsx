// The selected node's settings (FR-10): a React Hook Form per node type, validated by the
// zod schemas generated from the FlowGraph contract (api/flow.zod.ts). Every valid change
// goes to the editor store at once, so the canvas and the saved draft follow the form.
// Text fields that hold JSON (tool arguments, condition rules) are parsed first and then
// checked by the same schemas. The validator's issues for the node show under their field.
import { zodResolver } from "@hookform/resolvers/zod";
import { useQuery } from "@tanstack/react-query";
import { useEffect, type ReactNode } from "react";
import {
  useFieldArray,
  useForm,
  useWatch,
  type DefaultValues,
  type FieldValues,
  type Resolver,
  type UseFormReturn,
} from "react-hook-form";
import { z } from "zod";

import { client, unwrap, type Schemas } from "../../api/client";
import {
  ConditionDataSchema,
  EndDataSchema,
  HandoffDataSchema,
  KBAnswerDataSchema,
  LLMPromptDataSchema,
  StartDataSchema,
  ToolDataSchema,
} from "../../api/flow.zod";
import { Button, Input } from "../ui";
import { ISSUE_TEXT, NODE_KINDS, sameJson, type FlowIssue, type FlowNode } from "./graph";
import { useEditor } from "./store";

type Tool = Schemas["ToolOut"];

export function useTools() {
  return useQuery({ queryKey: ["tools"], queryFn: () => unwrap(client.GET("/tools")), staleTime: Infinity });
}

// The field an issue is about, when the validator names one ("'prompt' must not be empty.").
export function issueField(issue: FlowIssue): string | null {
  if (issue.code === "unknown_tool") return "tool";
  if (issue.code === "duplicate_route") return "routes";
  return /^'(\w+)'/.exec(issue.message)?.[1] ?? null;
}

export function NodeForm({ node, issues, editable }: { node: FlowNode; issues: FlowIssue[]; editable: boolean }) {
  const remove = useEditor((state) => state.remove);
  const general = issues.filter((issue) => issueField(issue) === null);

  return (
    <div className="grid gap-3">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold">{NODE_KINDS[node.type].label}</h2>
        <code className="truncate font-mono text-xs text-fg-muted">{node.id}</code>
      </div>
      {general.length > 0 && (
        <ul className="grid gap-1 rounded-md bg-danger/10 px-3 py-2 text-xs text-danger" aria-label="Помилки вузла">
          {general.map((issue, i) => (
            <li key={i}>{ISSUE_TEXT[issue.code] ?? issue.message}</li>
          ))}
        </ul>
      )}
      <fieldset disabled={!editable} className="grid gap-3">
        {/* Keyed by node, so switching nodes starts a fresh form. */}
        <TypeForm key={node.id} node={node} issues={issues} />
      </fieldset>
      {editable && (
        <Button variant="ghost" className="justify-self-start !text-danger" onClick={() => remove([node.id], [])}>
          Видалити вузол
        </Button>
      )}
    </div>
  );
}

function TypeForm({ node, issues }: { node: FlowNode; issues: FlowIssue[] }) {
  switch (node.type) {
    case "start":
      return <TextForm node={node} field="greeting" label="Вітання" schema={StartDataSchema} issues={issues} />;
    case "handoff":
      return <TextForm node={node} field="reason" label="Причина передачі" schema={HandoffDataSchema} issues={issues} />;
    case "end":
      return <TextForm node={node} field="farewell" label="Прощання" schema={EndDataSchema} issues={issues} />;
    case "llm_prompt":
      return <LLMPromptForm node={node} issues={issues} />;
    case "kb_answer":
      return <KBAnswerForm node={node} issues={issues} />;
    case "tool":
      return <ToolForm node={node} issues={issues} />;
    case "condition":
      return <ConditionForm node={node} issues={issues} />;
  }
}

// --- the plumbing every form shares ------------------------------------------------------

// A node's form: starts from the node's data, pushes every change that converts to valid
// data into the store, and resets only when the data changed elsewhere (undo, redo), so
// typing never re-creates the fields under the cursor.
function useNodeForm<V extends FieldValues>(
  node: FlowNode,
  toValues: (data: FlowNode["data"]) => V,
  toData: (values: V) => unknown,
  schema: z.ZodType,
  resolver?: Resolver<V>,
): UseFormReturn<V> {
  const updateData = useEditor((state) => state.updateData);
  const form = useForm<V>({ defaultValues: toValues(node.data) as DefaultValues<V>, resolver, mode: "onChange" });
  const nodeId = node.id;

  useEffect(() => {
    let current: unknown;
    try {
      current = toData(form.getValues());
    } catch {
      current = undefined;
    }
    if (!sameJson(current, node.data)) form.reset(toValues(node.data));
  }, [node.data, form, toData, toValues]);

  const values = useWatch({ control: form.control }) as V;
  useEffect(() => {
    let data: unknown;
    try {
      data = toData(values);
    } catch {
      return; // a JSON field that does not parse yet; its own rule reports it
    }
    const parsed = schema.safeParse(data);
    if (parsed.success) updateData(nodeId, parsed.data as FlowNode["data"]);
  }, [values, nodeId, toData, schema, updateData]);

  return form;
}

function Row({ label, htmlFor, error, server, children }: { label: string; htmlFor: string; error?: string; server: FlowIssue[]; children: ReactNode }) {
  return (
    <div className="grid gap-1">
      <label htmlFor={htmlFor} className="text-xs font-medium">
        {label}
      </label>
      {children}
      {error && (
        <p role="alert" className="text-xs text-danger">
          {error}
        </p>
      )}
      {server.map((issue, i) => (
        <p key={i} className="text-xs text-danger">
          {ISSUE_TEXT[issue.code] ?? issue.message}
        </p>
      ))}
    </div>
  );
}

const textarea =
  "w-full min-w-0 rounded-md border border-line bg-surface px-3 py-2 text-sm text-fg placeholder:text-fg-muted focus:border-accent focus:outline-none disabled:opacity-70";
const onField = (issues: FlowIssue[], field: string) => issues.filter((issue) => issueField(issue) === field);

// --- start, handoff, end: one text --------------------------------------------------------

function TextForm<S extends z.ZodType<Record<string, string>>>({
  node,
  field,
  label,
  schema,
  issues,
}: {
  node: FlowNode;
  field: string;
  label: string;
  schema: S;
  issues: FlowIssue[];
}) {
  const form = useNodeForm<Record<string, string>>(
    node,
    asValues,
    identity,
    schema,
    zodResolver(schema as z.ZodType<Record<string, string>, Record<string, string>>),
  );
  const id = `${node.id}-${field}`;
  return (
    <Row label={label} htmlFor={id} error={form.formState.errors[field]?.message} server={onField(issues, field)}>
      <textarea id={id} rows={4} className={textarea} {...form.register(field)} />
    </Row>
  );
}

const identity = <T,>(value: T) => value;
const asValues = (data: FlowNode["data"]) => data as Record<string, string>;

// --- llm_prompt: prompt, tools, routes -------------------------------------------------------

interface LLMValues {
  prompt: string;
  tools: string[];
  routes: { name: string }[];
}

const llmValues = (data: FlowNode["data"]): LLMValues => {
  const d = data as Extract<FlowNode, { type: "llm_prompt" }>["data"];
  return { prompt: d.prompt, tools: d.tools ?? [], routes: (d.routes ?? []).map((name) => ({ name })) };
};

const llmToData = (values: LLMValues) => ({
  prompt: values.prompt,
  tools: values.tools ?? [],
  routes: (values.routes ?? []).map((route) => route.name),
});

function LLMPromptForm({ node, issues }: { node: Extract<FlowNode, { type: "llm_prompt" }>; issues: FlowIssue[] }) {
  const tools = useTools();
  const form = useNodeForm(node, llmValues, llmToData, LLMPromptDataSchema);
  const routes = useFieldArray({ control: form.control, name: "routes" });
  const id = (field: string) => `${node.id}-${field}`;

  return (
    <>
      <Row label="Промпт вузла" htmlFor={id("prompt")} server={onField(issues, "prompt")}>
        <textarea id={id("prompt")} rows={7} className={textarea} {...form.register("prompt")} />
      </Row>
      <fieldset className="grid gap-1">
        <legend className="mb-1 text-xs font-medium">Дії, які може викликати модель</legend>
        {/* The registry plus whatever the node already names, so an unknown tool is shown
            (and flagged) rather than silently dropped by the next edit. */}
        {[
          ...(tools.data ?? []),
          ...(node.data.tools ?? [])
            .filter((name) => !tools.data?.some((tool) => tool.name === name))
            .map((name) => ({ name, description: "", parameters: {}, side_effect: false, unknown: true })),
        ].map((tool) => (
          <ToolCheckbox key={tool.name} tool={tool} register={form.register("tools")} />
        ))}
        {onField(issues, "tool").map((issue, i) => (
          <p key={i} className="text-xs text-danger">
            {ISSUE_TEXT[issue.code]}
          </p>
        ))}
      </fieldset>
      <fieldset className="grid gap-1.5">
        <legend className="mb-1 text-xs font-medium">Маршрути</legend>
        <p className="text-xs text-fg-muted">Модель переходить маршрутом сама; кожен маршрут — окреме ребро на холсті.</p>
        {routes.fields.map((field, index) => (
          <div key={field.id} className="flex gap-2">
            <Input aria-label={`Маршрут ${index + 1}`} className="font-mono" {...form.register(`routes.${index}.name`)} />
            <Button variant="ghost" aria-label={`Прибрати маршрут ${index + 1}`} onClick={() => routes.remove(index)}>
              ×
            </Button>
          </div>
        ))}
        {onField(issues, "routes").map((issue, i) => (
          <p key={i} className="text-xs text-danger">
            {ISSUE_TEXT[issue.code] ?? issue.message}
          </p>
        ))}
        <Button variant="secondary" className="justify-self-start" onClick={() => routes.append({ name: `route_${routes.fields.length + 1}` })}>
          Додати маршрут
        </Button>
      </fieldset>
    </>
  );
}

function ToolCheckbox({
  tool,
  register,
}: {
  tool: Tool & { unknown?: boolean };
  register: ReturnType<UseFormReturn<LLMValues>["register"]>;
}) {
  return (
    <label className="flex items-start gap-2 text-sm" title={tool.description}>
      <input type="checkbox" value={tool.name} className="mt-1" {...register} />
      <span>
        <code className="font-mono text-xs">{tool.name}</code>
        {tool.side_effect && <span className="ml-1 text-xs text-warn-fg">змінює дані</span>}
        {tool.unknown && <span className="ml-1 text-xs text-danger">немає в реєстрі</span>}
      </span>
    </label>
  );
}

// --- kb_answer: numbers and the not-found reply ----------------------------------------------

type KBValues = z.input<typeof KBAnswerDataSchema>;
const kbValues = (data: FlowNode["data"]) => data as KBValues;

function KBAnswerForm({ node, issues }: { node: Extract<FlowNode, { type: "kb_answer" }>; issues: FlowIssue[] }) {
  const form = useNodeForm<KBValues>(node, kbValues, identity, KBAnswerDataSchema, zodResolver(KBAnswerDataSchema));
  const errors = form.formState.errors;
  const id = (field: string) => `${node.id}-${field}`;

  return (
    <>
      <div className="grid grid-cols-2 gap-3">
        <Row label="Фрагментів (top k)" htmlFor={id("top_k")} error={errors.top_k && "Ціле число від 1 до 20."} server={onField(issues, "top_k")}>
          <Input id={id("top_k")} type="number" min={1} max={20} step={1} {...form.register("top_k", { valueAsNumber: true })} />
        </Row>
        <Row label="Поріг схожості" htmlFor={id("min_similarity")} error={errors.min_similarity && "Число від 0 до 1."} server={onField(issues, "min_similarity")}>
          <Input id={id("min_similarity")} type="number" min={0} max={1} step={0.05} {...form.register("min_similarity", { valueAsNumber: true })} />
        </Row>
      </div>
      <Row label="Відповідь, коли в базі нічого немає" htmlFor={id("not_found_reply")} server={onField(issues, "not_found_reply")}>
        <textarea id={id("not_found_reply")} rows={3} className={textarea} {...form.register("not_found_reply")} />
      </Row>
    </>
  );
}

// --- tool: which tool, its arguments (JSON), where the result goes --------------------------------

const jsonObject = (text: string): Record<string, unknown> => {
  const value: unknown = JSON.parse(text || "{}");
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error("not an object");
  return value as Record<string, unknown>;
};
const isJsonObject = (text: string) => {
  try {
    jsonObject(text);
    return true;
  } catch {
    return false;
  }
};

const ToolValuesSchema = z.object({
  tool: z.string(),
  args: z.string().refine(isJsonObject, "Потрібен JSON-об'єкт, напр. {\"service_id\": \"{{slots.service}}\"}."),
  save_as: z.string(),
});
type ToolValues = z.infer<typeof ToolValuesSchema>;

const toolValues = (data: FlowNode["data"]): ToolValues => {
  const d = data as Extract<FlowNode, { type: "tool" }>["data"];
  return { tool: d.tool, args: JSON.stringify(d.args ?? {}, null, 2), save_as: d.save_as ?? "" };
};

const toolToData = (values: ToolValues) => ({
  tool: values.tool,
  args: jsonObject(values.args),
  save_as: values.save_as.trim() || null,
});

function ToolForm({ node, issues }: { node: Extract<FlowNode, { type: "tool" }>; issues: FlowIssue[] }) {
  const tools = useTools();
  const form = useNodeForm(node, toolValues, toolToData, ToolDataSchema, zodResolver(ToolValuesSchema));
  const toolName = useWatch({ control: form.control, name: "tool" });
  const chosen = tools.data?.find((tool) => tool.name === toolName);
  const id = (field: string) => `${node.id}-${field}`;

  return (
    <>
      <Row label="Дія" htmlFor={id("tool")} server={onField(issues, "tool")}>
        <select id={id("tool")} className={textarea} {...form.register("tool")}>
          <option value="">— оберіть —</option>
          {tools.data?.map((tool) => (
            <option key={tool.name} value={tool.name}>
              {tool.name}
            </option>
          ))}
        </select>
      </Row>
      {chosen && <p className="text-xs text-fg-muted">{chosen.description}</p>}
      <Row label="Аргументи (JSON; рядки — шаблони над слотами)" htmlFor={id("args")} error={form.formState.errors.args?.message} server={onField(issues, "args")}>
        <textarea id={id("args")} rows={5} className={`${textarea} font-mono text-xs`} {...form.register("args")} />
      </Row>
      <Row label="Зберегти результат у слот" htmlFor={id("save_as")} server={onField(issues, "save_as")}>
        <Input id={id("save_as")} className="font-mono" placeholder="необов'язково" {...form.register("save_as")} />
      </Row>
    </>
  );
}

// --- condition: cases of route + JSONLogic rule ------------------------------------------------

const ConditionValuesSchema = z.object({
  cases: z.array(
    z.object({
      route: z.string(),
      when: z.string().refine(isJsonObject, "Потрібне правило JSONLogic, напр. {\"==\": [{\"var\": \"plan\"}, \"basic\"]}."),
    }),
  ),
});
type ConditionValues = z.infer<typeof ConditionValuesSchema>;

const conditionValues = (data: FlowNode["data"]): ConditionValues => {
  const d = data as Extract<FlowNode, { type: "condition" }>["data"];
  return { cases: (d.cases ?? []).map((c) => ({ route: c.route, when: JSON.stringify(c.when) })) };
};

const conditionToData = (values: ConditionValues) => ({
  cases: (values.cases ?? []).map((c) => ({ route: c.route, when: jsonObject(c.when) })),
});

function ConditionForm({ node, issues }: { node: Extract<FlowNode, { type: "condition" }>; issues: FlowIssue[] }) {
  const form = useNodeForm(node, conditionValues, conditionToData, ConditionDataSchema, zodResolver(ConditionValuesSchema));
  const cases = useFieldArray({ control: form.control, name: "cases" });

  return (
    <fieldset className="grid gap-3">
      <legend className="mb-1 text-xs font-medium">Умови по черзі; перша, що справджується, веде своїм маршрутом</legend>
      {cases.fields.map((field, index) => (
        <div key={field.id} className="grid gap-1.5 rounded-md border border-line p-2">
          <div className="flex gap-2">
            <Input aria-label={`Маршрут умови ${index + 1}`} className="font-mono" {...form.register(`cases.${index}.route`)} />
            <Button variant="ghost" aria-label={`Прибрати умову ${index + 1}`} onClick={() => cases.remove(index)}>
              ×
            </Button>
          </div>
          <textarea aria-label={`Правило умови ${index + 1}`} rows={2} className={`${textarea} font-mono text-xs`} {...form.register(`cases.${index}.when`)} />
          {form.formState.errors.cases?.[index]?.when && (
            <p role="alert" className="text-xs text-danger">
              {form.formState.errors.cases[index]?.when?.message}
            </p>
          )}
        </div>
      ))}
      {onField(issues, "cases").map((issue, i) => (
        <p key={i} className="text-xs text-danger">
          {ISSUE_TEXT[issue.code] ?? issue.message}
        </p>
      ))}
      <Button
        variant="secondary"
        className="justify-self-start"
        onClick={() => cases.append({ route: `case_${cases.fields.length + 1}`, when: '{"==": [{"var": "slot"}, "value"]}' })}
      >
        Додати умову
      </Button>
    </fieldset>
  );
}
