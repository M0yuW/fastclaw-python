"use client";

import { useParams, usePathname } from "next/navigation";

// Static export only generates /agents/default/..., so useParams() can return
// "default" when the Go SPA fallback serves another agent. usePathname keeps
// the real browser path reactive without copying it into local state.
export function useAgentIdFromURL(): string {
  const params = useParams<{ id: string }>();
  const pathname = usePathname();
  const match = pathname?.match(/\/agents\/([^/]+)\//);
  return match?.[1] || params?.id || "default";
}
