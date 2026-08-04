"use client";

import { useEffect, useState, useRef, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { getStatus, getChatHistory, getChatSessions, sendChatStream, type AgentInfo, type ChatHistoryMessage } from "@/lib/api";
import { createChatStreamBatcher, reduceChatStreamEvents, type StreamMessage } from "@/lib/chat-stream";
import { useAgentName } from "@/hooks/use-agent-name";
import { Bot, Send, Copy, Check, SquarePen, MessageSquare, Wrench, ChevronDown, ChevronRight, Square } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface ChatMessage extends StreamMessage {
  toolCalls?: { id: string; name: string; arguments: string; result?: string }[];
}

interface ChatSession {
  id: string;
  preview: string;
}

function generateSessionId() {
  return `s-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

/** Convert raw history messages into UI ChatMessages, grouping tool calls with results. */
function buildChatMessages(history: ChatHistoryMessage[]): ChatMessage[] {
  const msgs: ChatMessage[] = [];
  let i = 0;
  while (i < history.length) {
    const h = history[i];
    if (h.role === "user") {
      msgs.push({ id: `h-${i}`, role: "user", content: h.content || "", timestamp: 0 });
      i++;
    } else if (h.role === "assistant" && h.toolCalls && h.toolCalls.length > 0) {
      // Group: assistant tool_calls + following tool results + final assistant content
      const calls = h.toolCalls.map((tc) => ({ ...tc, result: undefined as string | undefined }));
      i++;
      // Collect tool results
      while (i < history.length && history[i].role === "tool") {
        const toolMsg = history[i];
        const call = calls.find((c) => c.id === toolMsg.toolCallId);
        if (call) call.result = toolMsg.content;
        i++;
      }
      // Show as tool-group
      msgs.push({
        id: `h-tool-${i}`,
        role: "tool-group",
        content: h.content || "",
        timestamp: 0,
        toolCalls: calls,
      });
      // A following assistant is final text only when it does not start the
      // next tool round. Leave tool-calling assistants for the outer loop.
      const nextMessage = history[i];
      if (nextMessage?.role === "assistant" && nextMessage.content && (!nextMessage.toolCalls || nextMessage.toolCalls.length === 0)) {
        msgs.push({ id: `h-${i}`, role: "agent", content: nextMessage.content, timestamp: 0 });
        i++;
      }
    } else if (h.role === "assistant") {
      msgs.push({ id: `h-${i}`, role: "agent", content: h.content || "", timestamp: 0 });
      i++;
    } else {
      i++; // skip unexpected
    }
  }
  return msgs;
}

export default function ChatPage() {
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [selectedAgent, setSelectedAgent] = useState<string>("");
  const [sessionId, setSessionId] = useState<string>(() => generateSessionId());
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const historyAbortRef = useRef<AbortController | null>(null);
  const streamGenerationRef = useRef(0);
  const sessionsGenerationRef = useRef(0);

  const abortStream = useCallback(() => {
    abortRef.current?.abort();
  }, []);
  const invalidateStream = useCallback(() => {
    streamGenerationRef.current++;
    abortRef.current?.abort();
  }, []);

  useEffect(() => invalidateStream, [selectedAgent, sessionId, invalidateStream]);

  // Load agents on mount
  useEffect(() => {
    const controller = new AbortController();
    getStatus(controller.signal)
      .then((status) => {
        if (status.agents?.length > 0) {
          setAgents(status.agents);
          setSelectedAgent(status.agents[0].id);
        }
      })
      .catch(() => {});
    return () => controller.abort();
  }, []);

  // Load sessions when agent changes
  const loadSessions = useCallback(async (agentId: string, signal?: AbortSignal) => {
    const generation = ++sessionsGenerationRef.current;
    try {
      const list = await getChatSessions(agentId, signal);
      if (!signal?.aborted && sessionsGenerationRef.current === generation) {
        setSessions(list || []);
      }
    } catch {
      if (!signal?.aborted && sessionsGenerationRef.current === generation) {
        setSessions([]);
      }
    }
  }, []);

  useEffect(() => {
    if (!selectedAgent) return;
    const controller = new AbortController();
    void loadSessions(selectedAgent, controller.signal);
    return () => controller.abort();
  }, [selectedAgent, loadSessions]);

  // Load history when session changes
  useEffect(() => {
    if (!selectedAgent || !sessionId) return;
    const controller = new AbortController();
    historyAbortRef.current?.abort();
    historyAbortRef.current = controller;
    getChatHistory(selectedAgent, sessionId, controller.signal)
      .then((history) => {
        if (controller.signal.aborted) return;
        if (!history || history.length === 0) {
          setMessages([]);
          return;
        }
        setMessages(buildChatMessages(history));
      })
      .catch(() => {
        if (!controller.signal.aborted) setMessages([]);
      });
    return () => {
      if (historyAbortRef.current === controller) historyAbortRef.current = null;
      controller.abort();
    };
  }, [selectedAgent, sessionId]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    const el = textareaRef.current;
    if (el) {
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 200) + "px";
    }
  }, [input]);

  const handleNewChat = useCallback(() => {
    setSessionId(generateSessionId());
    setMessages([]);
  }, []);

  const handleSend = useCallback(async () => {
    const text = input.trim();
    if (!text || !selectedAgent || sending) return;

    historyAbortRef.current?.abort();
    setInput("");
    setMessages((prev) => [
      ...prev,
      { id: `u-${Date.now()}`, role: "user", content: text, timestamp: Date.now() },
    ]);
    setSending(true);
    const controller = new AbortController();
    abortRef.current = controller;
    const generation = ++streamGenerationRef.current;
    const batcher = createChatStreamBatcher((events) => {
      if (streamGenerationRef.current !== generation) return;
      setMessages((prev) => reduceChatStreamEvents(prev, events));
    });

    try {
      await sendChatStream(selectedAgent, sessionId, text, (event) => {
        if (streamGenerationRef.current !== generation) return;
        if (event.type === "content" && event.data?.content === "__NEW_SESSION__") {
          handleNewChat();
          loadSessions(selectedAgent);
          return;
        }
        batcher.enqueue(event);
      }, controller.signal);
      batcher.flush();
      loadSessions(selectedAgent);
    } catch (err) {
      batcher.flush();
      if (streamGenerationRef.current !== generation) return;
      const isAbort = err instanceof DOMException && err.name === "AbortError";
      setMessages((prev) => {
        const stopped = isAbort
          ? prev.map((message) => message.role === "tool-group"
              ? { ...message, toolCalls: message.toolCalls?.map((tool) => tool.result === undefined ? { ...tool, result: "(stopped)" } : tool) }
              : message)
          : prev;
        return [...stopped, {
          id: `e-${Date.now()}`,
          role: "agent",
          content: isAbort ? "(Stopped)" : err instanceof Error && err.message ? err.message : "Failed to get a response. Is the gateway running?",
          timestamp: Date.now(),
        }];
      });
    } finally {
      batcher.flush();
      if (abortRef.current === controller) {
        abortRef.current = null;
        setSending(false);
        textareaRef.current?.focus();
      }
    }
  }, [input, selectedAgent, sessionId, sending, loadSessions, handleNewChat]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleCopy = (msg: ChatMessage) => {
    navigator.clipboard.writeText(msg.content);
    setCopiedId(msg.id);
    setTimeout(() => setCopiedId(null), 1500);
  };

  const handleSelectSession = (sid: string) => {
    setSessionId(sid);
  };

  const formatTime = (ts: number) =>
    new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  const currentAgent = agents.find((a) => a.id === selectedAgent);
  const agentName = useAgentName(selectedAgent);

  return (
    <div className="flex h-[calc(100vh-3rem)] md:h-screen">
      {/* Sidebar: agents + sessions */}
      <div className="hidden w-56 flex-col border-r border-border bg-card/30 lg:flex">
        <div className="flex items-center justify-between border-b border-border p-3">
          <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
            Agents
          </p>
        </div>
        <div className="overflow-auto p-2 space-y-1">
          {agents.map((agent) => (
            <button
              key={agent.id}
              onClick={() => {
                setSelectedAgent(agent.id);
                handleNewChat();
              }}
              className={`flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition-colors ${
                selectedAgent === agent.id
                  ? "bg-primary/10 text-primary"
                  : "text-muted-foreground hover:bg-muted/50 hover:text-foreground"
              }`}
            >
              <Bot className="h-4 w-4 shrink-0" />
              <span className="truncate">{agent.id}</span>
            </button>
          ))}
        </div>

        {/* Session list */}
        {sessions.length > 0 && (
          <>
            <div className="flex items-center justify-between border-t border-b border-border p-3">
              <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                History
              </p>
            </div>
            <div className="flex-1 overflow-auto p-2 space-y-1">
              {sessions.map((s) => (
                <button
                  key={s.id}
                  onClick={() => handleSelectSession(s.id)}
                  className={`flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm transition-colors ${
                    sessionId === s.id
                      ? "bg-primary/10 text-primary"
                      : "text-muted-foreground hover:bg-muted/50 hover:text-foreground"
                  }`}
                >
                  <MessageSquare className="h-3.5 w-3.5 shrink-0" />
                  <span className="truncate text-xs">{s.preview}</span>
                </button>
              ))}
            </div>
          </>
        )}
      </div>

      {/* Chat area */}
      <div className="flex flex-1 flex-col">
        {/* Chat header */}
        <div className="flex h-12 items-center justify-between border-b border-border px-4 shrink-0">
          <div className="flex items-center gap-2.5">
            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-primary/10">
              <Bot className="h-4 w-4 text-primary" />
            </div>
            <span className="text-sm font-semibold">
              {selectedAgent || "Select an agent"}
            </span>
            {currentAgent && (
              <Badge variant="secondary" className="font-mono text-[10px]">
                {currentAgent.model}
              </Badge>
            )}
          </div>
          <div className="flex items-center gap-2">
            {agents.length > 1 && (
              <select
                value={selectedAgent}
                onChange={(e) => {
                  setSelectedAgent(e.target.value);
                  handleNewChat();
                }}
                className="rounded-md border border-border bg-card px-2 py-1 text-sm lg:hidden"
              >
                {agents.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.id}
                  </option>
                ))}
              </select>
            )}
            <button
              onClick={handleNewChat}
              className="flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground hover:text-foreground hover:bg-muted/50 transition-colors"
              title="New Chat"
            >
              <SquarePen className="h-4 w-4" />
            </button>
          </div>
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto min-h-0 px-4 py-4">
          <div className="mx-auto max-w-2xl space-y-3">
            {messages.length === 0 && (
              <div className="flex flex-col items-center justify-center py-24 text-center">
                <div className="flex h-16 w-16 items-center justify-center rounded-full bg-muted mb-4">
                  <Bot className="h-8 w-8 text-muted-foreground" />
                </div>
                <p className="text-lg font-medium mb-1">
                  Chat with {agentName || selectedAgent || "your agent"}
                </p>
                <p className="text-sm text-muted-foreground">
                  Send a message to start a conversation
                </p>
              </div>
            )}

            {messages.map((msg) =>
              msg.role === "tool-group" ? (
                <ToolCallGroup key={msg.id} msg={msg} />
              ) : (
                <div
                  key={msg.id}
                  className={`flex min-w-0 ${msg.role === "user" ? "justify-end" : "justify-start"}`}
                >
                  <div
                    className={`group relative min-w-0 ${msg.role === "user" ? "max-w-[80%]" : "w-full max-w-full"} ${
                      msg.role === "user" ? "order-1" : ""
                    }`}
                  >
                    <div
                      className={`overflow-hidden rounded-2xl px-4 py-2.5 ${
                        msg.role === "user"
                          ? "bg-primary text-primary-foreground rounded-br-md"
                          : "bg-muted rounded-bl-md"
                      }`}
                    >
                      <div className="chat-markdown min-w-0 text-[15px] leading-relaxed prose prose-sm dark:prose-invert max-w-none prose-p:my-1 prose-pre:my-2 prose-ul:my-1 prose-ol:my-1">
                        <ReactMarkdown
                          remarkPlugins={[remarkGfm]}
                          components={{
                            table: ({ children }) => (
                              <div className="my-3 max-w-full overflow-x-auto overscroll-x-contain rounded-lg border border-border/60">
                                <table className="w-max min-w-full border-collapse text-sm">
                                  {children}
                                </table>
                              </div>
                            ),
                            th: ({ children }) => (
                              <th className="whitespace-nowrap border-b border-border/70 bg-muted/40 px-3 py-2 text-left font-semibold">
                                {children}
                              </th>
                            ),
                            td: ({ children }) => (
                              <td className="max-w-72 whitespace-normal break-words border-b border-border/40 px-3 py-2 align-top">
                                {children}
                              </td>
                            ),
                            pre: ({ children }) => (
                              <pre className="max-w-full overflow-x-auto rounded-lg bg-background/80 p-3 text-sm">
                                {children}
                              </pre>
                            ),
                          }}
                        >
                          {msg.content}
                        </ReactMarkdown>
                      </div>
                    </div>
                    <div
                      className={`flex items-center gap-1.5 mt-1 ${
                        msg.role === "user" ? "justify-end" : "justify-start"
                      }`}
                    >
                      {msg.timestamp > 0 && (
                        <span className="text-[10px] text-muted-foreground/60">
                          {formatTime(msg.timestamp)}
                        </span>
                      )}
                      {msg.role === "agent" && (
                        <button
                          onClick={() => handleCopy(msg)}
                          className="opacity-0 group-hover:opacity-100 p-0.5 rounded hover:bg-muted text-muted-foreground/60 hover:text-muted-foreground transition-all"
                          title="Copy"
                        >
                          {copiedId === msg.id ? (
                            <Check className="h-3 w-3 text-emerald-500" />
                          ) : (
                            <Copy className="h-3 w-3" />
                          )}
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              )
            )}

            {sending && (
              <div className="flex justify-start">
                <div className="bg-muted rounded-2xl rounded-bl-md px-4 py-3">
                  <div className="flex items-center gap-1">
                    <span className="typing-dot inline-block h-2 w-2 rounded-full bg-muted-foreground/60" style={{ animationDelay: "0ms" }} />
                    <span className="typing-dot inline-block h-2 w-2 rounded-full bg-muted-foreground/60" style={{ animationDelay: "200ms" }} />
                    <span className="typing-dot inline-block h-2 w-2 rounded-full bg-muted-foreground/60" style={{ animationDelay: "400ms" }} />
                  </div>
                </div>
              </div>
            )}

            <div ref={messagesEndRef} />
          </div>
        </div>

        {/* Input */}
        <div className="shrink-0 px-4 pb-6 pt-2">
          <div className="mx-auto max-w-2xl">
            <div className="flex items-end gap-2 rounded-xl border border-border bg-card px-4 py-3 focus-within:ring-2 focus-within:ring-ring/20 transition-shadow">
              <textarea
                ref={textareaRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder={
                  selectedAgent
                    ? `Message ${agentName || selectedAgent}...`
                    : "Select an agent first"
                }
                disabled={!selectedAgent || sending}
                rows={1}
                className="flex-1 resize-none bg-transparent text-[15px] placeholder:text-muted-foreground/50 outline-none disabled:opacity-50"
                style={{ maxHeight: 200, minHeight: 24 }}
              />
              {sending ? (
                <Button
                  onClick={abortStream}
                  size="icon"
                  className="h-8 w-8 shrink-0 rounded-lg"
                  aria-label="Stop generating"
                >
                  <Square className="h-3.5 w-3.5 fill-current" />
                </Button>
              ) : (
                <Button
                  onClick={handleSend}
                  disabled={!input.trim() || !selectedAgent}
                  size="icon"
                  className="h-8 w-8 shrink-0 rounded-lg"
                  aria-label="Send message"
                >
                  <Send className="h-4 w-4" />
                </Button>
              )}
            </div>
            <p className="text-center text-[11px] text-muted-foreground/50 mt-2">
              Enter to send, Shift+Enter for new line
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}

/** Renders a group of tool calls as a collapsible summary. */
function ToolCallGroup({ msg }: { msg: ChatMessage }) {
  const [groupOpen, setGroupOpen] = useState(false);
  const [expandedTool, setExpandedTool] = useState<Record<string, boolean>>({});

  const tools = msg.toolCalls || [];
  const doneCount = tools.filter((tc) => tc.result != null).length;
  const allDone = doneCount === tools.length;

  const toggleTool = (id: string) =>
    setExpandedTool((prev) => ({ ...prev, [id]: !prev[id] }));

  return (
    <div className="flex justify-start">
      <div className="max-w-[85%] space-y-2">
        {/* Content before tools */}
        {msg.content && (
          <div className="bg-muted rounded-2xl rounded-bl-md px-4 py-2.5">
            <div className="text-[15px] leading-relaxed prose prose-sm dark:prose-invert max-w-none prose-p:my-1">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {msg.content}
              </ReactMarkdown>
            </div>
          </div>
        )}
        {/* Collapsed tool group summary */}
        <div className="rounded-lg border border-border bg-card/50 overflow-hidden">
          <button
            onClick={() => setGroupOpen(!groupOpen)}
            className="flex w-full items-center gap-2 px-3 py-2 text-xs hover:bg-muted/50 transition-colors"
          >
            {!allDone ? (
              <div className="h-3.5 w-3.5 shrink-0 rounded-full border-2 border-amber-500 border-t-transparent animate-spin" />
            ) : (
              <Wrench className="h-3.5 w-3.5 text-amber-500 shrink-0" />
            )}
            <span className="font-medium text-foreground">
              {allDone
                ? `Executed ${tools.length} tool${tools.length > 1 ? "s" : ""}`
                : `Running tools (${doneCount}/${tools.length})...`}
            </span>
            <span className="text-muted-foreground/60 text-[11px] flex-1 text-left truncate">
              {tools.map((tc) => tc.name).join(", ")}
            </span>
            {groupOpen ? (
              <ChevronDown className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
            )}
          </button>

          {groupOpen && (
            <div className="border-t border-border">
              {tools.map((tc) => (
                <div key={tc.id} className="border-b border-border last:border-b-0">
                  <button
                    onClick={() => toggleTool(tc.id)}
                    className="flex w-full items-center gap-2 px-3 py-1.5 text-xs hover:bg-muted/30 transition-colors"
                  >
                    {tc.result === undefined ? (
                      <div className="h-3 w-3 shrink-0 rounded-full border-2 border-amber-500/60 border-t-transparent animate-spin" />
                    ) : (
                      <Check className="h-3 w-3 text-emerald-500 shrink-0" />
                    )}
                    <span className="font-medium text-foreground">{tc.name}</span>
                    <span className="text-muted-foreground/50 font-mono truncate flex-1 text-left text-[11px]">
                      {(() => {
                        try {
                          const args = JSON.parse(tc.arguments);
                          return Object.values(args).join(", ");
                        } catch {
                          return tc.arguments;
                        }
                      })()}
                    </span>
                    {expandedTool[tc.id] ? (
                      <ChevronDown className="h-3 w-3 text-muted-foreground/50 shrink-0" />
                    ) : (
                      <ChevronRight className="h-3 w-3 text-muted-foreground/50 shrink-0" />
                    )}
                  </button>
                  {expandedTool[tc.id] && (
                    <div className="px-3 py-2 space-y-2 bg-muted/20">
                      <div>
                        <p className="text-[10px] font-medium text-muted-foreground uppercase mb-1">Input</p>
                        <pre className="text-xs font-mono bg-muted/50 rounded p-2 overflow-x-auto whitespace-pre-wrap break-all max-h-40">
                          {(() => {
                            try { return JSON.stringify(JSON.parse(tc.arguments), null, 2); }
                            catch { return tc.arguments; }
                          })()}
                        </pre>
                      </div>
                      {tc.result != null ? (
                        <div>
                          <p className="text-[10px] font-medium text-muted-foreground uppercase mb-1">Output</p>
                          <pre className="text-xs font-mono bg-muted/50 rounded p-2 overflow-x-auto whitespace-pre-wrap break-all max-h-60">
                            {tc.result.length > 2000 ? tc.result.slice(0, 2000) + "..." : tc.result}
                          </pre>
                        </div>
                      ) : (
                        <p className="text-xs text-muted-foreground/60 italic">Executing...</p>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
