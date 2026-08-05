import type { ChatStreamEvent, ToolResultMetadata } from "@/lib/api";

export interface StreamToolCall<TMetadata = ToolResultMetadata> {
  id: string;
  name: string;
  arguments: string;
  result?: string;
  metadata?: TMetadata;
}

export interface StreamMessage<TMetadata = ToolResultMetadata> {
  id: string;
  role: "user" | "agent" | "tool-group";
  content: string;
  timestamp: number;
  toolCalls?: StreamToolCall<TMetadata>[];
  _stream?: {
    turnId?: string;
    messageId?: string;
    round?: number;
    seq?: number;
  };
}

function streamKey(evt: ChatStreamEvent, fallback: string): string {
  return evt.data?.messageId || fallback;
}

export function reduceChatStreamEvents<T extends StreamMessage>(messages: T[], events: ChatStreamEvent[]): T[] {
  let next = messages;
  const lastSeqByTurn = new Map<string, number>();
  for (const message of messages) {
    const stream = message._stream;
    if (stream?.turnId && stream.seq !== undefined) {
      lastSeqByTurn.set(stream.turnId, Math.max(lastSeqByTurn.get(stream.turnId) ?? -1, stream.seq));
    }
  }

  events.forEach((evt, eventIndex) => {
    const data = evt.data ?? {};
    const turnId = data.turnId;
    if (turnId && data.seq !== undefined) {
      if ((lastSeqByTurn.get(turnId) ?? -1) >= data.seq) return;
      lastSeqByTurn.set(turnId, data.seq);
    }
    if (evt.type === "done") return;

    const fallbackId = `stream-${messages.length}-${eventIndex}`;
    const key = streamKey(evt, fallbackId);
    const stream = { turnId, messageId: data.messageId, round: data.round, seq: data.seq };
    const exactIndex = data.messageId
      ? next.findIndex((message) => message._stream?.messageId === data.messageId || message.id === data.messageId)
      : -1;

    if (evt.type === "content_delta" || evt.type === "content") {
      const content = evt.type === "content_delta" ? data.delta ?? data.content ?? "" : data.content ?? "";
      let index = exactIndex;
      if (index < 0 && !data.messageId && evt.type === "content") {
        const last = next[next.length - 1];
        if (last?.role === "agent" && last._stream?.messageId === undefined) index = next.length - 1;
      }
      if (index >= 0) {
        const current = next[index];
        const nextStream = { ...stream, seq: Math.max(current._stream?.seq ?? -1, data.seq ?? -1) };
        const updated = {
          ...current,
          content: evt.type === "content_delta" ? current.content + content : content,
          _stream: nextStream,
        } as T;
        next = [...next.slice(0, index), updated, ...next.slice(index + 1)];
      } else {
        const created = {
          id: data.messageId || key,
          role: "agent",
          content,
          timestamp: Date.now(),
          _stream: stream,
        } as T;
        next = [...next, created];
      }
      return;
    }

    if (evt.type === "tool_call") {
      let index = exactIndex;
      if (index < 0 && !data.messageId) {
        const last = next[next.length - 1];
        if (last?.role === "agent" || (last?.role === "tool-group" && last.toolCalls?.some((tool) => tool.result === undefined))) {
          index = next.length - 1;
        }
      }
      const call: StreamToolCall = {
        id: data.id ?? "",
        name: data.name ?? "",
        arguments: data.arguments ?? "{}",
      };
      if (index >= 0) {
        const current = next[index];
        const existing = current.toolCalls ?? [];
        const callIndex = existing.findIndex((tool) => tool.id === call.id);
        const toolCalls = callIndex >= 0
          ? existing.map((tool, i) => i === callIndex ? { ...tool, ...call } : tool)
          : [...existing, call];
        const nextStream = { ...stream, seq: Math.max(current._stream?.seq ?? -1, data.seq ?? -1) };
        const updated = {
          ...current,
          role: "tool-group",
          toolCalls,
          _stream: nextStream,
        } as T;
        next = [...next.slice(0, index), updated, ...next.slice(index + 1)];
      } else {
        const created = {
          id: data.messageId || key,
          role: "tool-group",
          content: "",
          timestamp: Date.now(),
          toolCalls: [call],
          _stream: stream,
        } as T;
        next = [...next, created];
      }
      return;
    }

    if (evt.type === "tool_result") {
      let index = exactIndex;
      if (index < 0 || !next[index]?.toolCalls?.some((tool) => tool.id === data.id)) {
        index = next.findIndex((message) => message.toolCalls?.some((tool) => tool.id === data.id));
      }
      if (index < 0) return;
      const current = next[index];
      const nextStream = {
        ...stream,
        turnId: current._stream?.turnId ?? stream.turnId,
        messageId: current._stream?.messageId ?? stream.messageId,
        round: current._stream?.round ?? stream.round,
        seq: Math.max(current._stream?.seq ?? -1, data.seq ?? -1),
      };
      next = [...next.slice(0, index), {
        ...current,
        toolCalls: current.toolCalls?.map((tool) => tool.id === data.id
          ? { ...tool, result: data.result ?? "", metadata: data.metadata }
          : tool),
        _stream: nextStream,
      } as T, ...next.slice(index + 1)];
      return;
    }

    if (evt.type === "error") {
      next = [...next, {
        id: data.messageId ? `${data.messageId}-error` : key,
        role: "agent",
        content: `Error: ${data.message ?? "Unknown error"}`,
        timestamp: Date.now(),
        _stream: stream,
      } as T];
    }
  });
  return next;
}

export interface ChatStreamBatcher {
  enqueue(event: ChatStreamEvent): void;
  flush(): void;
  cancel(): void;
}

export function createChatStreamBatcher(onFlush: (events: ChatStreamEvent[]) => void): ChatStreamBatcher {
  let queued: ChatStreamEvent[] = [];
  let frame: number | null = null;
  const flush = () => {
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null;
    if (queued.length === 0) return;
    const events = queued;
    queued = [];
    onFlush(events);
  };
  return {
    enqueue(event) {
      if (event.type === "done") return flush();
      queued.push(event);
      if (event.type === "error") return flush();
      if (frame === null) frame = requestAnimationFrame(flush);
    },
    flush,
    cancel() {
      if (frame !== null) cancelAnimationFrame(frame);
      frame = null;
      queued = [];
    },
  };
}
