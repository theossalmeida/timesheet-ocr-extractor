"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type Account, type Team } from "@/lib/client";
import { AccountForm } from "@/components/AccountForm";
import { Extractor } from "@/components/Extractor";
import { TeamPanel } from "@/components/TeamPanel";

export default function Home() {
  const [account, setAccount] = useState<Account | null>(null);
  const [teams, setTeams] = useState<Team[]>([]);
  const [teamId, setTeamId] = useState("");
  const [tab, setTab] = useState("extract");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [invitation, setInvitation] = useState("");
  const [error, setError] = useState("");
  const team = teams.find(value => value.id === teamId);

  const refresh = useCallback(async () => {
    const result = await api<{ user: Account; teams: Team[] }>("/auth/me");
    setAccount(result.user); setTeams(result.teams);
    setTeamId(current => result.teams.some(team => team.id === current) ? current : result.teams[0]?.id ?? "");
  }, []);

  useEffect(() => {
    const token = new URLSearchParams(window.location.search).get("invite") ?? sessionStorage.getItem("autus_invite") ?? "";
    setInvitation(token);
    if (token) { sessionStorage.setItem("autus_invite", token); window.history.replaceState({}, "", "/"); }
    const unauthorized = () => { setAccount(null); setTeams([]); setBusy(false); };
    window.addEventListener("autus:unauthorized", unauthorized);
    refresh().catch(() => undefined).finally(() => setLoading(false));
    return () => window.removeEventListener("autus:unauthorized", unauthorized);
  }, [refresh]);

  async function acceptInvite() {
    setError("");
    try { await api("/teams/invitations/accept", "POST", { token: invitation }); sessionStorage.removeItem("autus_invite"); setInvitation(""); await refresh(); }
    catch (error) { setError(error instanceof Error ? error.message : "Não foi possível aceitar o convite."); }
  }

  async function signedIn(registered: boolean) {
    await refresh();
    if (registered && invitation) {
      sessionStorage.removeItem("autus_invite"); setInvitation("");
    }
  }

  return <div className="app-shell"><header className="nav app-nav"><div className="brand-lockup"><img src="/logo.png" alt="AUTUS Extração de documentos trabalhistas" className="nav-logo" /></div>{account && <><button className="btn btn-ghost" aria-current={tab === "extract" ? "page" : undefined} disabled={busy} onClick={() => setTab("extract")}>Extrair</button><button className="btn btn-ghost" aria-current={tab === "team" ? "page" : undefined} disabled={busy} onClick={() => setTab("team")}>Equipe</button>{teams.length > 0 && <select aria-label="Equipe ativa" className="input team-select" disabled={busy} value={teamId} onChange={event => setTeamId(event.target.value)}>{teams.map(team => <option key={team.id} value={team.id}>{team.name}</option>)}</select>}<button className="btn btn-ghost small" disabled={busy} onClick={async () => { try { await api("/auth/logout", "POST"); setAccount(null); setTeams([]); } catch (error) { setError(error instanceof Error ? error.message : "Não foi possível sair."); } }}>Sair</button></>}</header>
    {error && <p role="alert" className="error global-error">{error}</p>}
    {loading ? <main className="login-main" role="status">Carregando AUTUS…</main> : !account ? <AccountForm invitation={invitation} onSuccess={signedIn} /> : <>{invitation && <div className="invite-banner"><span>Você recebeu um convite de equipe.</span><button className="btn btn-primary" onClick={() => void acceptInvite()}>Aceitar convite</button><button className="btn btn-ghost" onClick={() => { setInvitation(""); sessionStorage.removeItem("autus_invite"); }}>Dispensar</button></div>}{tab === "team" || !team ? <TeamPanel key={teamId} team={team} onTeamsChanged={refresh} /> : <Extractor key={team.id} team={team} onBusy={setBusy} />}</>}
    {!account && <footer className="app-footer"><span>AUTUS · Documentos trabalhistas</span><span>Acesso privado por equipe</span></footer>}
  </div>;
}
