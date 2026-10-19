// A flow node on the canvas: its kind, id and a one-line summary, an inlet on top and an
// outlet per route at the bottom, so an edge drawn from a route outlet carries that route.
// Nodes with validator errors get a red frame and the errors on hover.
import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";

import { hasInlet, ISSUE_TEXT, NODE_KINDS, outlets, summary, type FlowIssue, type FlowNode } from "./graph";

export type CanvasNode = Node<{ node: FlowNode; issues: FlowIssue[]; active: boolean }, "flow">;

export function FlowNodeView({ data, selected }: NodeProps<CanvasNode>) {
  const { node, issues, active } = data;
  const kind = NODE_KINDS[node.type];
  const exits = outlets(node);
  const frame = issues.length
    ? "border-danger ring-2 ring-danger/30"
    : active
      ? "border-accent ring-4 ring-accent/30"
      : selected
        ? "border-accent"
        : "border-line";

  return (
    <div
      className={`w-56 rounded-lg border border-t-4 bg-surface text-fg shadow-sm ${kind.accent} ${frame}`}
      title={issues.map((issue) => ISSUE_TEXT[issue.code] ?? issue.message).join("\n") || undefined}
      data-testid={`node-${node.id}`}
    >
      {hasInlet(node) && <Handle type="target" position={Position.Top} className="!size-2.5 !bg-fg-muted" />}
      <div className="px-3 py-2">
        <p className="flex items-center justify-between gap-2 text-xs">
          <span className="font-semibold">{kind.label}</span>
          <code className="truncate font-mono text-fg-muted">{node.id}</code>
        </p>
        <p className="mt-1 line-clamp-2 text-xs text-fg-muted">{summary(node)}</p>
        {issues.length > 0 && (
          <p className="mt-1 text-xs font-medium text-danger">
            {issues.length === 1 ? "1 помилка" : `помилок: ${issues.length}`}
          </p>
        )}
      </div>
      {exits.length > 0 && (
        <div className="flex justify-around gap-1 border-t border-line px-2 pt-1 pb-2">
          {exits.map((exit) => (
            <div key={exit.id} className="relative flex min-w-0 flex-col items-center">
              {exit.label && <span className="max-w-24 truncate text-[10px] text-fg-muted">{exit.label}</span>}
              <Handle
                id={exit.id}
                type="source"
                position={Position.Bottom}
                className="!static !mt-1 !size-2.5 !translate-x-0 !translate-y-0 !bg-accent"
              />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
