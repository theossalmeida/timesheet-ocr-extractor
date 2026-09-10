"use client";

import { useState, type FormEvent } from "react";
import { api } from "@/lib/client";
import { Blueprint } from "./Blueprint";

export function AccountForm({ invitation, onSuccess }: { invitation: string; onSuccess: (registered: boolean) => Promise<void> }) {
  const [register, setRegister] = useState(Boolean(invitation));
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.currentTarget));
    setPending(true);
    setError("");
    try {
      await api(`/auth/${register ? "register" : "login"}`, "POST", { ...data, invitation_token: invitation });
      await onSuccess(register);
    } catch (error) { setError(error instanceof Error ? error.message : "Não foi possível entrar."); }
    finally { setPending(false); }
  }

  return <main className="login-main"><div className="login-content"><div><h6>Acesso restrito</h6><h2>{register ? "Criar conta no AUTUS" : "Entrar no AUTUS"}</h2><p className="text-muted">{invitation ? "Use o e-mail que recebeu o convite da equipe." : "Entre com seu e-mail e senha para acessar os documentos da equipe."}</p></div><Blueprint className="panel"><form onSubmit={submit} className="stack">
    {register && <div className="field"><label htmlFor="name">Nome</label><input id="name" name="name" className="input" autoComplete="name" maxLength={100} required /></div>}
    <div className="field"><label htmlFor="email">E-mail</label><input id="email" name="email" className="input" type="email" autoComplete="email" maxLength={254} required /></div>
    <div className="field"><label htmlFor="password">Senha{register ? " · pelo menos 8 caracteres" : ""}</label><input id="password" name="password" className="input" type="password" autoComplete={register ? "new-password" : "current-password"} minLength={register ? 8 : 1} maxLength={128} required /></div>
    {register && !invitation && <div className="field"><label htmlFor="bootstrap">Código privado de configuração inicial</label><input id="bootstrap" name="bootstrap_token" className="input" type="password" autoComplete="off" required /><p className="text-muted small">Disponível ao responsável pelo servidor. Novos membros entram por convite.</p></div>}
    {error && <p role="alert" className="error">{error}</p>}
    <button className="btn btn-primary btn-block" disabled={pending}>{pending ? "Aguarde…" : register ? "Criar conta" : "Entrar"}</button>
    <button type="button" className="btn btn-ghost" disabled={pending} onClick={() => { setRegister(!register); setError(""); }}>{register ? "Já tenho uma conta" : invitation ? "Criar conta com este convite" : "Configurar a primeira conta"}</button>
  </form></Blueprint><p className="text-muted small">Os PDFs enviados e os arquivos gerados são armazenados para consulta e download pelos membros da sua equipe.</p></div></main>;
}
