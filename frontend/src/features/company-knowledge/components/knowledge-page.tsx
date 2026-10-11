"use client";
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { companyKnowledgeApi as api, type CompanyDocument, type DocumentVersion } from "../api";

const input = "w-full rounded-lg border border-border bg-bg px-3 py-2 text-sm";

export default function KnowledgePage() {
  const { user } = useAuth(); const cache = useQueryClient();
  const [selected, setSelected] = useState<CompanyDocument | null>(null);
  const [history, setHistory] = useState<DocumentVersion[]>([]);
  const [showHistory, setShowHistory] = useState(false);
  const [title, setTitle] = useState(""); const [key, setKey] = useState(""); const [content, setContent] = useState("");
  const [audience, setAudience] = useState<"candidate" | "internal">("internal"); const [effective, setEffective] = useState("");
  const [query, setQuery] = useState(""); const [error, setError] = useState(""); const request = useRef("");
  const documents = useQuery({ queryKey: ["company-documents", user?.id], queryFn: api.list, retry: false });
  const save = useMutation({ mutationFn: () => {
    if (!request.current) request.current = crypto.randomUUID();
    return api.save({ title, policy_key: key, content, audience, effective_from: effective ? new Date(effective).toISOString() : new Date().toISOString(), expected_revision: selected?.revision || 0, request_id: request.current }, selected?.id);
  }, onSuccess: () => { request.current = ""; setSelected(null); setTitle(""); setKey(""); setContent(""); setHistory([]); void cache.invalidateQueries({ queryKey: ["company-documents"] }); } });
  const archive = useMutation({ mutationFn: api.archive, onSuccess: () => { setSelected(null); void cache.invalidateQueries({ queryKey: ["company-documents"] }); } });
  const retrieval = useMutation({ mutationFn: api.retrieve });
  async function edit(doc: CompanyDocument) {
    try { setError(""); const versions = await api.versions(doc.id); setSelected(doc); setHistory(versions); setShowHistory(false); setTitle(doc.title); setKey(doc.policy_key); setContent(versions[0]?.content || ""); setAudience(versions[0]?.audience || "internal"); setEffective(""); request.current = ""; save.reset(); }
    catch (err) { setError(err instanceof Error ? err.message : "Could not load document"); }
  }
  function changed() { request.current = ""; save.reset(); }
  return <div className="space-y-7 p-4 sm:p-6 lg:p-8">
    <div><h1 className="text-3xl font-semibold">Company knowledge</h1><p className="mt-2 max-w-3xl text-text-muted">Publish policies with effective dates. Candidate-visible excerpts can ground simulations; internal documents stay within your workspace.</p></div>
    <div className="grid gap-6 xl:grid-cols-[minmax(250px,1fr)_minmax(0,2fr)]">
      <Card><CardHeader className="flex-row items-center justify-between"><CardTitle>Documents</CardTitle><Button type="button" variant="secondary" size="sm" onClick={() => { setSelected(null); setHistory([]); setShowHistory(false); setTitle(""); setKey(""); setContent(""); changed(); }}>New</Button></CardHeader>
        <CardContent className="pt-4">
        {documents.isPending && <p role="status">Loading...</p>}{documents.error && <p role="alert" className="text-error">{documents.error.message}</p>}
        {!documents.data?.length && <p className="text-sm text-text-muted">No company documents yet.</p>}
        <ul className="space-y-3">{documents.data?.map(doc => <li key={doc.id} className="rounded-lg border border-border p-3">
          <Button type="button" variant="ghost" className="h-auto justify-start whitespace-normal rounded-md px-2 py-1 text-left font-medium" onClick={() => void edit(doc)}>{doc.title}</Button>
          <div className="mt-1 flex items-center gap-2 px-2 text-xs text-text-muted"><span>Revision {doc.revision}</span><Badge variant={doc.archived_at ? "muted" : "success"}>{doc.archived_at ? "Archived" : "Active"}</Badge></div>
          <Button type="button" variant="ghost" size="sm" disabled={archive.isPending} onClick={() => archive.mutate(doc)} className="mt-1 h-auto px-2 py-1 text-xs">{doc.archived_at ? "Restore" : "Archive"}</Button>
        </li>)}</ul>
        {(error || archive.error) && <p role="alert" className="mt-3 text-sm text-error">{error || archive.error?.message}</p>}
        </CardContent>
      </Card>
      <Card><CardHeader><CardTitle>{selected ? `Publish a new version of ${selected.title}` : "Add company policy"}</CardTitle></CardHeader>
        <CardContent className="pt-4 space-y-5">
        <form className="space-y-4" onSubmit={e => { e.preventDefault(); save.mutate(); }}>
          <label className="block text-sm">Title<input className={input} required maxLength={160} value={title} onChange={e => { setTitle(e.target.value); changed(); }} /></label>
          <label className="block text-sm">Policy key<input className={input} required pattern="[a-z0-9][a-z0-9_-]*" disabled={!!selected} placeholder="annual-leave" value={key} onChange={e => { setKey(e.target.value); changed(); }} /></label>
          <label className="block text-sm">Policy text<textarea className={input + " min-h-48"} required minLength={30} maxLength={100000} value={content} onChange={e => { setContent(e.target.value); changed(); }} /></label>
          <div className="grid gap-4 sm:grid-cols-2"><label className="block text-sm">Effective from (local time)<input className={input} type="datetime-local" value={effective} onChange={e => { setEffective(e.target.value); changed(); }} /><span className="text-xs text-text-muted">Leave blank for now. A new version supersedes the previous open version.</span></label>
            <label className="block text-sm">Audience<select className={input} value={audience} onChange={e => { setAudience(e.target.value as typeof audience); changed(); }}><option value="internal">Internal only</option><option value="candidate">Candidate-visible</option></select></label></div>
          <Button loading={save.isPending} disabled={!!selected?.archived_at}>Publish version</Button>
          {save.error && <p role="alert" className="text-sm text-error">{save.error.message}</p>}{save.isSuccess && <p role="status" className="text-sm text-success">Policy version published.</p>}
        </form>
        {history.length > 0 && <div className="border-t border-border pt-4">
          <Button type="button" variant="secondary" size="sm" onClick={() => setShowHistory(v => !v)} aria-expanded={showHistory}>{showHistory ? "Hide version history" : `Show version history (${history.length})`}</Button>
          {showHistory && <ul className="mt-3 space-y-3">{history.map(v => <li key={v.id} className="border-t border-border pt-3 text-sm"><p>Version {v.version}: {new Date(v.effective_from).toLocaleString()} to {v.effective_until ? new Date(v.effective_until).toLocaleString() : "open-ended"}</p><p className="mt-2 whitespace-pre-wrap text-text-muted">{v.content}</p></li>)}</ul>}
        </div>}
        </CardContent>
      </Card>
    </div>
    <Card><CardHeader><CardTitle>Check policy grounding</CardTitle><p className="mt-1 text-sm text-text-muted">Search relevant terms. Results are exact, dated source excerpts, including your internal documents.</p></CardHeader>
      <CardContent className="pt-4">
      <form className="flex flex-col gap-3 sm:flex-row" onSubmit={e => { e.preventDefault(); retrieval.mutate(query); }}><label className="flex-1"><span className="sr-only">Policy search</span><input className={input} minLength={2} maxLength={300} required placeholder="Annual leave" value={query} onChange={e => setQuery(e.target.value)} /></label><Button loading={retrieval.isPending}>Find sources</Button></form>
      {retrieval.error && <p role="alert" className="mt-3 text-error">{retrieval.error.message}</p>}
      {retrieval.data && <div className="mt-4 space-y-4"><p role="status" className="text-sm text-text-muted">{retrieval.data.message}</p>{retrieval.data.excerpts.map(excerpt => <blockquote key={excerpt.chunk_id} className="border-l-2 border-brand pl-4"><p className="whitespace-pre-wrap text-sm">{excerpt.text}</p><footer className="mt-2 text-xs text-text-muted">{excerpt.title}, version {excerpt.version}; effective {new Date(excerpt.effective_from).toLocaleDateString()}</footer></blockquote>)}</div>}
      </CardContent>
    </Card>
  </div>;
}
