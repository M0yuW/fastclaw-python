"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Sparkles,
  Trash2,
  Download,
  Loader2,
  Check,
  Settings,
} from "lucide-react";
import {
  getAgentSkills,
  getSkills,
  deleteAgentSkill,
  installSkill,
  getConfig,
  type SkillInfo,
} from "@/lib/api";
import { ConfigureSkillDialog, type SkillEntryView } from "@/components/configure-skill-dialog";
import { useAgentIdFromURL } from "@/hooks/use-agent-id";
import { useAgentName } from "@/hooks/use-agent-name";

export default function AgentSkillsPage() {
  const agentId = useAgentIdFromURL();
  const agentName = useAgentName(agentId);
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [installOpen, setInstallOpen] = useState(false);
  const [configureTarget, setConfigureTarget] = useState<SkillInfo | null>(null);
  // Skill env entries are GLOBAL (keyed by skill name), so the same
  // /api/config blob feeds both the global /skills page and this
  // agent-scoped one. Lets the user configure FAL_KEY etc. from
  // whichever entry point they're already on.
  const [skillEntries, setSkillEntries] = useState<Record<string, SkillEntryView>>({});

  const requestGenerationRef = useRef(0);
  const fetchSkills = useCallback((signal?: AbortSignal) => {
    const generation = ++requestGenerationRef.current;
    Promise.all([
      getAgentSkills(agentId, signal).catch(() => [] as SkillInfo[]),
      getConfig(signal).catch(() => null),
    ])
      .then(([list, cfg]) => {
        if (signal?.aborted || requestGenerationRef.current !== generation) return;
        setSkills(list || []);
        // Per-agent override map first (this page edits there); merge
        // global defaults underneath so the "configured" badge still
        // lights up when only the global value is set.
        const skillsCfg = cfg?.skills as
          | {
              entries?: Record<string, SkillEntryView>;
              agentEntries?: Record<string, Record<string, SkillEntryView>>;
            }
          | undefined;
        const globalEntries = skillsCfg?.entries || {};
        const agentMap = skillsCfg?.agentEntries?.[agentId] || {};
        const merged: Record<string, SkillEntryView> = { ...globalEntries };
        for (const [name, entry] of Object.entries(agentMap)) {
          merged[name] = entry;
        }
        setSkillEntries(merged);
      })
      .finally(() => {
        if (!signal?.aborted && requestGenerationRef.current === generation) {
          setLoading(false);
        }
      });
  }, [agentId]);

  useEffect(() => {
    const controller = new AbortController();
    fetchSkills(controller.signal);
    return () => controller.abort();
  }, [fetchSkills]);

  const handleDelete = async () => {
    if (!deleteTarget) return;
    await deleteAgentSkill(agentId, deleteTarget);
    setDeleteTarget(null);
    fetchSkills();
  };

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-semibold tracking-tight">Skills</h2>
          <p className="text-sm text-muted-foreground mt-1">
            Skills scoped to <strong>{agentName}</strong> — only this
            agent sees them
          </p>
        </div>
        <Button variant="outline" onClick={() => setInstallOpen(true)}>
          <Download className="h-4 w-4 mr-2" />
          Install Skill
        </Button>
      </div>

      {loading ? (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {[1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-40" />
          ))}
        </div>
      ) : skills.length === 0 ? (
        <div className="rounded-lg border border-border bg-card">
          <div className="flex flex-col items-center justify-center py-16">
            <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-primary/10 mb-4">
              <Sparkles className="h-7 w-7 text-primary" />
            </div>
            <p className="text-sm text-muted-foreground mb-1">
              No agent-scoped skills yet
            </p>
            <p className="text-xs text-muted-foreground/60 mb-4 max-w-sm text-center">
              Enable a skill from the local catalog. It is available only to
              this agent.
            </p>
            <Button variant="outline" size="sm" onClick={() => setInstallOpen(true)}>
              <Download className="h-4 w-4 mr-2" />
              Install Skill
            </Button>
          </div>
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {skills.map((skill) => (
            <div
              key={skill.name}
              className="group rounded-lg border border-border bg-card p-5 transition-colors hover:bg-muted/50"
            >
              <div className="flex items-start justify-between mb-3">
                <div className="flex items-center gap-2.5">
                  <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10">
                    <Sparkles className="h-4 w-4 text-primary" />
                  </div>
                  <div>
                    <p className="text-sm font-medium">{skill.name}</p>
                    <Badge variant="outline" className="mt-1 text-[10px]">
                      {skill.type || "skill"}
                    </Badge>
                  </div>
                </div>
                <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7 text-muted-foreground hover:text-foreground"
                    onClick={() => setConfigureTarget(skill)}
                    title="Configure env / API keys"
                  >
                    <Settings className="h-3.5 w-3.5" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7 text-muted-foreground hover:text-destructive"
                    onClick={() => setDeleteTarget(skill.name)}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              </div>
              <p className="text-sm text-muted-foreground line-clamp-2">
                {skill.description || "No description"}
              </p>
              {(skillEntries[skill.name]?.apiKey ||
                Object.keys(skillEntries[skill.name]?.env || {}).length > 0) && (
                <div className="mt-2 inline-flex items-center gap-1 text-[10px] text-emerald-500">
                  <Check className="h-3 w-3" />
                  configured
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      <AlertDialog open={!!deleteTarget} onOpenChange={() => setDeleteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Remove Skill</AlertDialogTitle>
            <AlertDialogDescription>
              Remove <strong>{deleteTarget}</strong> from{" "}
              <strong>{agentName}</strong>? Other agents are
              unaffected.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDelete}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              Remove
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <InstallSkillDialog
        key={installOpen ? "open" : "closed"}
        agentId={agentId}
        agentName={agentName}
        open={installOpen}
        onOpenChange={setInstallOpen}
        onInstalled={() => {
          setInstallOpen(false);
          fetchSkills();
        }}
        installedNames={new Set(skills.map((s) => s.name))}
      />

      <ConfigureSkillDialog
        key={configureTarget?.name || "closed"}
        skill={configureTarget}
        agentId={agentId}
        existing={configureTarget ? skillEntries[configureTarget.name] : undefined}
        onClose={() => setConfigureTarget(null)}
        onSaved={() => {
          setConfigureTarget(null);
          fetchSkills();
        }}
      />
    </div>
  );
}

function InstallSkillDialog({
  agentId,
  agentName,
  open,
  onOpenChange,
  onInstalled,
  installedNames,
}: {
  agentId: string;
  agentName: string;
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onInstalled: () => void;
  installedNames: Set<string>;
}) {
  const [query, setQuery] = useState("");
  const [catalog, setCatalog] = useState<SkillInfo[]>([]);
  const [loadingCatalog, setLoadingCatalog] = useState(false);
  const [installingId, setInstallingId] = useState<string | null>(null);
  const [installError, setInstallError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setLoadingCatalog(true);
    getSkills(controller.signal)
      .then((skills) => {
        if (!controller.signal.aborted) setCatalog(skills);
      })
      .catch(() => {
        if (!controller.signal.aborted) setCatalog([]);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingCatalog(false);
      });
    return () => controller.abort();
  }, [open]);

  const visible = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return catalog
      .filter((skill) => {
        return !normalized || `${skill.name} ${skill.description}`.toLowerCase().includes(normalized);
      })
      .slice(0, 20);
  }, [catalog, query]);

  const handleInstall = async (skill: SkillInfo) => {
    setInstallError(null);
    setInstallingId(skill.name);
    try {
      const resp = await installSkill({
        name: skill.name,
        agent: agentId,
      });
      if (!resp.ok) {
        setInstallError(resp.error || "install failed");
        return;
      }
      onInstalled();
    } catch (e) {
      setInstallError(e instanceof Error ? e.message : "install failed");
    } finally {
      setInstallingId(null);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Install Skill for {agentName}</DialogTitle>
          <DialogDescription>
            Choose a skill from the local catalog. Only this agent will be
            able to use it.
          </DialogDescription>
        </DialogHeader>

        <div className="relative">
          <Input
            autoFocus
            placeholder="Filter local skills…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>

        <div className="min-h-[240px] max-h-[420px] overflow-y-auto -mx-1 px-1">
          {loadingCatalog ? (
            <div className="space-y-2 py-2">
              {[1, 2, 3].map((i) => (
                <Skeleton key={i} className="h-14" />
              ))}
            </div>
          ) : visible.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-center">
              <Sparkles className="h-8 w-8 text-muted-foreground/40 mb-3" />
              <p className="text-sm text-muted-foreground">
                {query.trim() ? `No local skills found for ${query}` : "No local skills are available"}
              </p>
            </div>
          ) : (
            <>
              <p className="text-[10px] uppercase tracking-wider text-muted-foreground/70 mb-1.5 px-1">
                Local skill catalog
              </p>
              <div className="space-y-1.5 py-1">
                {visible.map((skill) => {
                  const already = installedNames.has(skill.name);
                  const busy = installingId === skill.name;
                  return (
                    <div
                      key={skill.name}
                      className="flex items-center gap-3 rounded-md border border-border bg-card p-3 hover:bg-muted/40 transition-colors"
                    >
                      <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 shrink-0">
                        <Sparkles className="h-4 w-4 text-primary" />
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <p className="text-sm font-medium truncate">{skill.name}</p>
                          <Badge variant="outline" className="text-[10px]">local</Badge>
                        </div>
                        <p className="text-xs text-muted-foreground line-clamp-2">
                          {skill.description || "No description"}
                        </p>
                      </div>
                      <Button
                        size="sm"
                        variant={already ? "outline" : "default"}
                        disabled={already || busy}
                        onClick={() => handleInstall(skill)}
                      >
                        {already ? (
                          <>
                            <Check className="h-3.5 w-3.5 mr-1.5" /> Installed
                          </>
                        ) : busy ? (
                          <>
                            <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" /> Installing…
                          </>
                        ) : (
                          "Install"
                        )}
                      </Button>
                    </div>
                  );
                })}
              </div>
            </>
          )}
        </div>

        {installError && (
          <p className="text-xs text-destructive break-all">{installError}</p>
        )}
      </DialogContent>
    </Dialog>
  );
}
