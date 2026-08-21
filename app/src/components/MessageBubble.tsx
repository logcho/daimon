import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { fmtDuration } from "../lib/format";
import type { ChatMessage, Step } from "../types";
import { ThinkingIndicator } from "./ThinkingIndicator";
import { categoryParts, MAX_EXPANDED, StepSummary, summarise } from "./StepSummary";

/** A tool call. Shows what it was called *with*, not just its name — twelve
 *  `read_file` lines say nothing; twelve paths say what the agent read. */
function StepLine({ step, indented }: { step: Step; indented?: boolean }) {
  const name = step.tool ?? step.label;
  // Only worth showing once it's long enough to notice.
  const elapsed = step.elapsed_ms && step.elapsed_ms >= 100 ? fmtDuration(step.elapsed_ms / 1000) : null;

  const mark =
    step.status === "done" ? (
      <span className="text-emerald-400">✓</span>
    ) : step.status === "error" ? (
      <span className="text-red-400">✕</span>
    ) : step.status === "running" ? (
      <span className="block h-3 w-3 animate-spin rounded-full border-2 border-[#4f8dff]/25 border-t-[#4f8dff]" />
    ) : (
      <span className="block h-2 w-2 rounded-full border border-neutral-600" />
    );

  return (
    <span className={`flex items-baseline gap-2 font-mono text-xs ${indented ? "ml-4" : ""}`}>
      <span className="shrink-0 self-center">{mark}</span>
      <span className={step.status === "running" ? "text-[#4f8dff]" : "text-neutral-300"}>{name}</span>
      {step.detail && <span className="truncate text-neutral-500">{step.detail}</span>}
      {elapsed && <span className="shrink-0 text-neutral-600">{elapsed}</span>}
    </span>
  );
}

/** The rows behind a disclosure, capped. Expanding a 200-step run should not
 *  lock the webview, and past a certain length nobody is reading anyway. */
function StepRows({ steps, indented }: { steps: Step[]; indented?: boolean }) {
  return (
    <ul className="space-y-1">
      {steps.slice(0, MAX_EXPANDED).map((step) => (
        <li key={step.id}>
          <StepLine step={step} indented={indented} />
        </li>
      ))}
      {steps.length > MAX_EXPANDED && (
        <li className={`font-mono text-xs text-neutral-600 ${indented ? "ml-4" : ""}`}>
          … {steps.length - MAX_EXPANDED} more
        </li>
      )}
    </ul>
  );
}

/** The collapsed line for a run of plain tool calls.
 *
 *  While the run is live it names the call happening *now* — that is the whole
 *  reason to show anything at all. Once it has moved on, the individual calls
 *  are detail and the shape of the run is the useful part, so it becomes the
 *  same coarse tally a finished turn gets. Failures are never folded away
 *  silently: a run with an error says so on its collapsed line. */
function RunLabel({ steps }: { steps: Step[] }) {
  const last = steps[steps.length - 1];
  const running = last.status === "running";
  const failed = steps.filter((s) => s.status === "error").length;
  // Never folded away silently, and not only when the run has stopped: a call
  // that failed three tools ago still matters while the next one is spinning.
  const failures = failed > 0 && (
    <span className="shrink-0 text-red-400">✕ {failed} failed</span>
  );

  if (running) {
    return (
      <span className="flex min-w-0 items-baseline gap-2">
        <span className="block h-3 w-3 shrink-0 self-center animate-spin rounded-full border-2 border-[#4f8dff]/25 border-t-[#4f8dff]" />
        <span className="shrink-0">{steps.length} tools ·</span>
        <span className="shrink-0 text-[#4f8dff]">{last.tool ?? last.label}</span>
        {last.detail && <span className="truncate text-neutral-500">{last.detail}</span>}
        {failures}
      </span>
    );
  }

  return (
    <span className="flex min-w-0 items-baseline gap-2">
      <span className="truncate">
        {[`${steps.length} tools`, ...categoryParts(steps)].join(" · ")}
      </span>
      {failures}
    </span>
  );
}

/** A consecutive run of plain tool calls, as one row.
 *
 *  A single call renders as itself — hiding one line behind a disclosure costs
 *  a click and saves nothing. */
function ToolRun({ steps, indented }: { steps: Step[]; indented?: boolean }) {
  if (steps.length === 1) return <StepLine step={steps[0]} indented={indented} />;
  return (
    <StepSummary label={<RunLabel steps={steps} />} indented={indented}>
      <StepRows steps={steps} indented />
    </StepSummary>
  );
}

/** One sub-agent's line for a finished spawn: what it was asked, how much work
 *  it did, how long it took — the same facts `render.subagent_line` shows in
 *  the CLI. Its own steps hang behind the disclosure. */
function subagentLabel(step: Step, children: Step[]): string {
  const parts = [step.agent_label ?? step.label];
  if (step.subagent_query ?? step.detail) parts.push(String(step.subagent_query ?? step.detail));
  const stats: string[] = [];
  if (children.length) stats.push(`${children.length} tool${children.length === 1 ? "" : "s"}`);
  if (step.elapsed_ms) stats.push(fmtDuration(step.elapsed_ms / 1000));
  return stats.length ? `${parts.join(": ")} · ${stats.join(" · ")}` : parts.join(": ");
}

type Group = { kind: "run"; steps: Step[] } | { kind: "agent"; step: Step };

/** Steps grouped so a turn reads as *what was delegated* rather than as every
 *  call it took to get there.
 *
 *  Sub-agents are the structure worth seeing: each spawn keeps its own block,
 *  grouped by `agent_id` (an earlier version matched on the literal string
 *  "research:", which never covered `task` spawns). Everything else — the
 *  parent's own reads, edits and shells — collapses into one row per
 *  consecutive run. Grouping in sequence rather than partitioning keeps the
 *  order honest: a run that happened after a spawn still renders after it.
 *
 *  A *finished* sub-agent collapses to one line whether or not the turn itself
 *  is done: once it has reported back, its children are detail, not progress.
 *  A running one shows a rolling row of what it is doing, because that is the
 *  only sign it is getting anywhere. */
function StepList({ steps }: { steps: Step[] }) {
  const topLevel = steps.filter((s) => s.parent_step_id === undefined);
  const childrenOf = (id: string) => steps.filter((s) => s.parent_step_id === id);

  const groups: Group[] = [];
  for (const step of topLevel) {
    if (step.agent_id) {
      groups.push({ kind: "agent", step });
      continue;
    }
    const last = groups[groups.length - 1];
    if (last?.kind === "run") last.steps.push(step);
    else groups.push({ kind: "run", steps: [step] });
  }

  return (
    <ul className="space-y-1">
      {groups.map((group) => {
        if (group.kind === "run") {
          return (
            <li key={`run-${group.steps[0].id}`}>
              <ToolRun steps={group.steps} />
            </li>
          );
        }
        const step = group.step;
        const children = step.agent_id ? childrenOf(step.agent_id) : [];
        if (children.length === 0) {
          return (
            <li key={step.id}>
              <StepLine step={step} />
            </li>
          );
        }
        return (
          <li key={step.id}>
            {step.status === "running" ? (
              <>
                <StepLine step={step} />
                <div className="mt-1">
                  <ToolRun steps={children} indented />
                </div>
              </>
            ) : (
              <StepSummary label={subagentLabel(step, children)}>
                <StepRows steps={children} indented />
              </StepSummary>
            )}
          </li>
        );
      })}
    </ul>
  );
}

interface Props {
  message: ChatMessage;
}

export function MessageBubble({ message }: Props) {
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-br-md border border-[#4f8dff]/30 bg-[#4f8dff]/[0.18] px-3.5 py-2 text-sm leading-relaxed text-neutral-50 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.15)] backdrop-blur-sm">
          {message.content}
        </div>
      </div>
    );
  }

  const empty = message.content === "";
  // Live steps are what the user watches; a finished turn's are a record, and
  // a record does not need forty lines. Left uncollapsed, one session's
  // transcript grew by every tool call the agent had ever made.
  const live = message.thinking;
  const steps = message.steps;

  return (
    <div className="flex justify-start">
      <div className="liquid-glass-subtle max-w-[85%] space-y-2 rounded-2xl rounded-bl-md px-3.5 py-2.5">
        {steps.length > 0 &&
          (live ? (
            <StepList steps={steps} />
          ) : (
            <StepSummary label={summarise(steps, message.elapsedMs)}>
              <StepList steps={steps} />
            </StepSummary>
          ))}
        {message.notices?.map((notice) => (
          <p key={notice.id} className="font-mono text-xs text-neutral-500">
            {notice.kind === "continuation" ? "↻" : notice.kind === "retry" ? "↺" : "⌘"}{" "}
            {notice.text}
          </p>
        ))}
        {empty && message.thinking && <ThinkingIndicator />}
        {!empty && (
          <div className="daimon-prose prose prose-invert prose-sm max-w-none text-sm leading-relaxed text-white">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
          </div>
        )}
        {message.error && (
          <p className="whitespace-pre-wrap text-sm leading-relaxed text-red-400">{message.error}</p>
        )}
      </div>
    </div>
  );
}
