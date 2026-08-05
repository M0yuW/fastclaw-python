"use client";

import { useEffect, useState } from "react";
import { getAgents } from "@/lib/api";

// useAgentName resolves an agent id to its display name. While the agent
// list is loading, or if the id isn't in the list, it returns the id so
// page chrome doesn't flicker between empty and resolved states. Pass an
// empty string to skip the fetch entirely.
export function useAgentName(agentId: string): string {
  const [resolved, setResolved] = useState<{ agentId: string; name: string } | null>(null);

  useEffect(() => {
    if (!agentId) return;
    const controller = new AbortController();
    getAgents(controller.signal)
      .then((list) => {
        const me = list.find((agent) => agent.id === agentId);
        setResolved({ agentId, name: me?.name || agentId });
      })
      .catch(() => {
        // Keep rendering the id fallback when the request fails or is aborted.
      });
    return () => controller.abort();
  }, [agentId]);

  if (!agentId) return "";
  return resolved?.agentId === agentId ? resolved.name : agentId;
}
