"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, type Team } from "@/lib/client";
import { Blueprint } from "./Blueprint";

interface Members { members: { id: string; name: string; email: string; role: string; documents: number }[]; invitations: { id: string; email: string; expires_at: string }[] }

export function TeamPanel({ team, onTeamsChanged }: { team?: Team; onTeamsChanged: () => Promise<void> }) {
  const [members, setMembers] = useState<Members>({ members: [], invitations: [] });
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [inviteUrl, setInviteUrl] = useState("");
  const [message, setMessage] = useState("");
  const refresh = useCallback(async () => {
    if (team) setMembers(await api<Members>("/teams/members", "GET", undefined, team.id));
  }, [team]);
  useEffect(() => { setMembers({ members: [], invitations: [] }); setInviteUrl(""); refresh().catch(error => setError(error.message)); }, [refresh]);

  async function action(work: () => Promise<void>) {
    setPending(true); setError(""); setMessage("");
    try { await work(); }
    catch (error) { setError(error instanceof Error ? error.message : "Não foi possível concluir."); }
    finally { setPending(false); }
  }

  function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const name = new FormData(form).get("team_name");
    void action(async () => { await api("/teams", "POST", { name }); form.reset(); await onTeamsChanged(); setMessage("Equipe criada. Selecione-a no menu superior."); });
  }

  function invite(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const email = new FormData(form).get("invite_email");
    void action(async () => { const result = await api<{ url: string }>("/teams/invitations", "POST", { email }, team?.id); setInviteUrl(result.url); form.reset(); await refresh(); });
  }

  return <main className="team-main"><div><h6>Administração</h6><h2>{team?.name ?? "Crie sua equipe"}</h2><p className="text-muted">Os membros compartilham os documentos e o histórico da equipe. Administradores gerenciam os convites.</p></div>
    {error && <p role="alert" className="error">{error}</p>}{message && <p role="status">{message}</p>}
    {team?.role === "admin" && <Blueprint className="panel stack"><div className="row"><h5>Incluir membro</h5><span className="text-muted small">{members.members.length} ativos · {members.invitations.length} convites pendentes</span></div><form onSubmit={invite} className="row wrap"><div className="field grow"><label htmlFor="invite-email">E-mail</label><input id="invite-email" name="invite_email" className="input" type="email" placeholder="nome@escritorio.com.br" required /></div><button disabled={pending} className="btn btn-primary">Criar convite</button></form>{inviteUrl && <div className="stack"><p className="small">Compartilhe este link com a pessoa convidada. Válido por 7 dias.</p><input className="input" aria-label="Link do convite" readOnly value={inviteUrl} onFocus={event => event.currentTarget.select()} /><button className="btn btn-secondary" onClick={() => void action(async () => { await navigator.clipboard.writeText(inviteUrl); setMessage("Convite copiado."); })}>Copiar convite</button></div>}</Blueprint>}
    {team && <div className="table-scroll"><h5>Membros</h5><table className="table"><thead><tr><th>Membro</th><th>Situação</th><th>Documentos</th><th><span className="sr-only">Ações</span></th></tr></thead><tbody>{members.members.map(member => <tr key={member.id}><td>{member.name}<div className="text-muted small">{member.email}</div></td><td><span className="tag tag-accent">{member.role === "admin" ? "Administrador" : "Membro"}</span></td><td>{member.documents}</td><td>{team.role === "admin" && member.role !== "admin" && <button className="btn btn-ghost" disabled={pending} onClick={() => { if (window.confirm(`Remover ${member.name} da equipe? O histórico será mantido.`)) void action(async () => { await api(`/teams/members/${member.id}`, "DELETE", undefined, team.id); await refresh(); }); }}>Remover</button>}</td></tr>)}{members.invitations.map(invite => <tr key={invite.id}><td>{invite.email}<div className="small text-muted">Expira em {new Date(invite.expires_at).toLocaleDateString("pt-BR")}</div></td><td><span className="tag tag-outline">Convite pendente</span></td><td>—</td><td><button className="btn btn-ghost" disabled={pending} onClick={() => void action(async () => { await api(`/teams/invitations/${invite.id}`, "DELETE", undefined, team.id); setInviteUrl(""); await refresh(); })}>Cancelar</button></td></tr>)}</tbody></table><p className="small text-muted">Administradores não podem ser removidos. Remover um membro mantém seus documentos na equipe.</p></div>}
    <Blueprint className="panel"><form onSubmit={create} className="stack"><h5>Criar uma equipe</h5><div className="field"><label htmlFor="team-name">Nome da equipe</label><input id="team-name" name="team_name" className="input" maxLength={100} required /></div><button className="btn btn-secondary" disabled={pending}>Criar equipe</button></form></Blueprint>
    <details><summary>Alterar senha</summary><form className="stack panel" onSubmit={event => { event.preventDefault(); const form = event.currentTarget; const values = Object.fromEntries(new FormData(form)); void action(async () => { await api("/auth/password", "POST", values); form.reset(); setMessage("Senha alterada. As outras sessões foram encerradas."); }); }}><div className="field"><label htmlFor="current-password">Senha atual</label><input id="current-password" name="current_password" type="password" autoComplete="current-password" className="input" required /></div><div className="field"><label htmlFor="new-password">Nova senha · pelo menos 8 caracteres</label><input id="new-password" name="new_password" type="password" autoComplete="new-password" className="input" minLength={8} maxLength={128} required /></div><button className="btn btn-primary" disabled={pending}>Alterar senha</button></form></details>
  </main>;
}
