"use client";

import { useEffect, useState } from "react";
import { Plus, UsersRound } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { archiveTeam, createTeam, deleteTeam, getTeamTemplates, getTeams, previewTeam, restoreTeam, type TeamInfo, type TeamTemplate } from "@/lib/api";

export default function TeamsPage() {
  const [teams, setTeams] = useState<TeamInfo[]>([]);
  const [templates, setTemplates] = useState<TeamTemplate[]>([]);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [templateKey, setTemplateKey] = useState("finance-market-research");
  const [specialists, setSpecialists] = useState("");
  const [requestId, setRequestId] = useState("");
  const [preview, setPreview] = useState<string[]>([]);
  const [previewReady, setPreviewReady] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const load = () => getTeams().then(setTeams).catch(() => setError("Unable to load teams"));
  useEffect(() => { load(); getTeamTemplates().then((items) => { setTemplates(items); if (items[0]) setTemplateKey(items[0].key); }).catch(() => {}); }, []);
  const selected = templates.find((item) => item.key === templateKey);
  async function runPreview(): Promise<boolean> {
    setError("");
    setPreviewing(true);
    try {
      const result = await previewTeam({ name: name || "Preview", templateKey, clientRequestId: requestId || crypto.randomUUID(), specialists: customRoles() });
      const checks = result.checks;
      const skillText = checks?.skills?.required?.length
        ? `Skills: ${checks.skills.prepared ? "ready" : "not prepared"} (${checks.skills.required.join(", ")})`
        : "Skills: none required";
      const toolText = checks?.tools?.available
        ? "Tools: available"
        : `Tools: missing ${checks?.tools?.missing?.join(", ") || "unknown"}`;
      const providerText = checks?.provider?.ok
        ? `Provider: ${checks.provider.name}/${checks.provider.model}`
        : `Provider: ${checks?.provider?.error || "not configured"}`;
      setPreview([providerText, skillText, toolText, `Roles: ${(result.roles || []).join(", ")}`]);
      setPreviewReady(result.ok);
      if (!result.ok) setError(result.detail || "Preview found prerequisites that need attention");
      return result.ok;
    } catch (cause) {
      setPreviewReady(false);
      setError(cause instanceof Error ? cause.message : "Preview failed");
      return false;
    } finally {
      setPreviewing(false);
    }
  }
  async function submit() {
    if (!name.trim()) return;
    if (!previewReady && !(await runPreview())) return;
    setSaving(true); setError("");
    const result = await createTeam({ name: name.trim(), description, templateKey, clientRequestId: requestId, specialists: customRoles() });
    setSaving(false);
    if (!result.ok) return setError(result.detail || result.error || "Team creation failed");
    setOpen(false); setName(""); setDescription(""); setSpecialists(""); setRequestId(""); setPreview([]); setPreviewReady(false); load();
  }
  function customRoles() { return specialists.split("\n").map((name) => name.trim()).filter(Boolean).map((name) => ({ key: name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, ""), name })); }
  async function archive(team: TeamInfo) { const result = await archiveTeam(team.id, team.revision); if (!result.ok) setError(result.detail || "Archive failed"); else load(); }
  async function restore(team: TeamInfo) { const result = await restoreTeam(team.id, team.revision); if (!result.ok) setError(result.detail || "Restore failed"); else load(); }
  async function remove(team: TeamInfo) { if (window.prompt(`Type ${team.id} to permanently delete this archived team and its specialist Agents.`) !== team.id) return; const result = await deleteTeam(team.id, team.revision); if (!result.ok) setError(result.detail || "Delete failed"); else load(); }
  return <div className="mx-auto max-w-5xl space-y-6 p-6">
    <div className="flex items-center justify-between"><div><h2 className="text-2xl font-semibold">Teams</h2><p className="mt-1 text-sm text-muted-foreground">Coordinator-led specialist teams.</p></div><Button onClick={() => { setRequestId(crypto.randomUUID()); setPreview([]); setPreviewReady(false); setOpen(true); }}><Plus /> Create team</Button></div>
    <div className="grid gap-4 md:grid-cols-2">{teams.map((team) => <Card key={team.id}><CardHeader><CardTitle className="flex items-center gap-2"><UsersRound size={18} />{team.name}</CardTitle><CardDescription>{team.description || team.templateKey}</CardDescription></CardHeader><CardContent className="space-y-3"><Badge variant={team.status === "active" ? "default" : "secondary"}>{team.status}</Badge><div className="text-sm text-muted-foreground">{team.members.length} members · revision {team.revision}</div><div className="flex flex-wrap gap-2">{team.members.map((member) => <Badge key={member.agentId} variant="outline">{member.roleKey}</Badge>)}</div>{team.status === "active" ? <Button variant="outline" size="sm" onClick={() => archive(team)}>Archive</Button> : <div className="flex gap-2"><Button variant="outline" size="sm" onClick={() => restore(team)}>Restore</Button><Button variant="destructive" size="sm" onClick={() => remove(team)}>Delete permanently</Button></div>}</CardContent></Card>)}</div>
    {!teams.length && !error && <Card><CardContent className="p-8 text-center text-muted-foreground">No teams yet. Create a template-based team to start.</CardContent></Card>}
    {error && <p className="text-sm text-destructive">{error}</p>}
    <Dialog open={open} onOpenChange={setOpen}><DialogContent><DialogHeader><DialogTitle>Create team</DialogTitle></DialogHeader><div className="space-y-4"><div><Label htmlFor="team-name">Name</Label><Input id="team-name" value={name} onChange={(event) => setName(event.target.value)} /></div><div><Label htmlFor="team-description">Description</Label><Textarea id="team-description" value={description} onChange={(event) => setDescription(event.target.value)} /></div><div><Label htmlFor="team-template">Template</Label><select id="team-template" className="mt-1 w-full rounded-md border bg-background p-2" value={templateKey} onChange={(event) => { setTemplateKey(event.target.value); setPreview([]); setPreviewReady(false); }}><option value="custom">Custom team</option>{templates.map((template) => <option key={template.key} value={template.key}>{template.name}</option>)}</select><p className="mt-2 text-xs text-muted-foreground">{selected?.roles.map((role) => role.name).join(" · ")}</p></div>{templateKey === "custom" && <div><Label htmlFor="team-specialists">Specialists</Label><Textarea id="team-specialists" placeholder="One specialist name per line" value={specialists} onChange={(event) => { setSpecialists(event.target.value); setPreview([]); setPreviewReady(false); }} /></div>}{preview.length > 0 && <div className="space-y-1 text-sm"><p className="font-medium">Preview</p>{preview.map((item) => <p key={item} className="text-muted-foreground">{item}</p>)}</div>}{error && <p className="text-sm text-destructive">{error}</p>}</div><DialogFooter><p className="mr-auto text-xs text-muted-foreground">Create runs the prerequisite check automatically.</p><Button variant="outline" onClick={() => void runPreview()} disabled={previewing || saving}>Preview</Button><Button onClick={() => void submit()} disabled={saving || previewing || !name.trim()}>{saving ? "Creating…" : previewing ? "Checking…" : "Create"}</Button></DialogFooter></DialogContent></Dialog>
  </div>;
}
