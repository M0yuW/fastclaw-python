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
import { createTeam, getTeamTemplates, getTeams, previewTeam, type TeamInfo, type TeamTemplate } from "@/lib/api";

export default function TeamsPage() {
  const [teams, setTeams] = useState<TeamInfo[]>([]);
  const [templates, setTemplates] = useState<TeamTemplate[]>([]);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [templateKey, setTemplateKey] = useState("finance-market-research");
  const [preview, setPreview] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const load = () => getTeams().then(setTeams).catch(() => setError("Unable to load teams"));
  useEffect(() => { load(); getTeamTemplates().then((items) => { setTemplates(items); if (items[0]) setTemplateKey(items[0].key); }).catch(() => {}); }, []);
  const selected = templates.find((item) => item.key === templateKey);
  async function runPreview() {
    setError("");
    const result = await previewTeam({ name: name || "Preview", templateKey, clientRequestId: crypto.randomUUID() });
    if (!result.ok) return setError(result.detail || "Preview failed");
    setPreview(result.roles || []);
  }
  async function submit() {
    if (!name.trim()) return;
    setSaving(true); setError("");
    const result = await createTeam({ name: name.trim(), description, templateKey, clientRequestId: crypto.randomUUID() });
    setSaving(false);
    if (!result.ok) return setError(result.detail || result.error || "Team creation failed");
    setOpen(false); setName(""); setDescription(""); setPreview([]); load();
  }
  return <div className="mx-auto max-w-5xl space-y-6 p-6">
    <div className="flex items-center justify-between"><div><h2 className="text-2xl font-semibold">Teams</h2><p className="mt-1 text-sm text-muted-foreground">Coordinator-led specialist teams.</p></div><Button onClick={() => setOpen(true)}><Plus /> Create team</Button></div>
    <div className="grid gap-4 md:grid-cols-2">{teams.map((team) => <Card key={team.id}><CardHeader><CardTitle className="flex items-center gap-2"><UsersRound size={18} />{team.name}</CardTitle><CardDescription>{team.description || team.templateKey}</CardDescription></CardHeader><CardContent className="space-y-3"><Badge variant={team.status === "active" ? "default" : "secondary"}>{team.status}</Badge><div className="text-sm text-muted-foreground">{team.members.length} members · revision {team.revision}</div><div className="flex flex-wrap gap-2">{team.members.map((member) => <Badge key={member.agentId} variant="outline">{member.roleKey}</Badge>)}</div></CardContent></Card>)}</div>
    {!teams.length && !error && <Card><CardContent className="p-8 text-center text-muted-foreground">No teams yet. Create a template-based team to start.</CardContent></Card>}
    {error && <p className="text-sm text-destructive">{error}</p>}
    <Dialog open={open} onOpenChange={setOpen}><DialogContent><DialogHeader><DialogTitle>Create team</DialogTitle></DialogHeader><div className="space-y-4"><div><Label htmlFor="team-name">Name</Label><Input id="team-name" value={name} onChange={(event) => setName(event.target.value)} /></div><div><Label htmlFor="team-description">Description</Label><Textarea id="team-description" value={description} onChange={(event) => setDescription(event.target.value)} /></div><div><Label htmlFor="team-template">Template</Label><select id="team-template" className="mt-1 w-full rounded-md border bg-background p-2" value={templateKey} onChange={(event) => { setTemplateKey(event.target.value); setPreview([]); }}>{templates.map((template) => <option key={template.key} value={template.key}>{template.name}</option>)}</select><p className="mt-2 text-xs text-muted-foreground">{selected?.roles.map((role) => role.name).join(" · ")}</p></div>{preview.length > 0 && <div className="text-sm">Preview: {preview.join(", ")}</div>}</div><DialogFooter><Button variant="outline" onClick={runPreview}>Preview</Button><Button onClick={submit} disabled={saving || !name.trim()}>Create</Button></DialogFooter></DialogContent></Dialog>
  </div>;
}
