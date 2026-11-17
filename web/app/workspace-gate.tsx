"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import "./workspace-gate.css";
import StartupGate from "./startup-gate";
import { APP_NAME } from "./brand";

// Authentication and model readiness come from the backend, never a URL flag.
// Cancelling API settings keeps this wrapper and the current desk alive.
function VerifiedApiEntry({ enabled, children }: { enabled: boolean; children: ReactNode }) {
  return enabled ? <StartupGate>{children}</StartupGate> : children;
}

type WorkspaceStatus = {
  account_exists: boolean;
  authenticated: boolean;
  ready: boolean;
  username: string | null;
  ai_available: boolean;
  model: string | null;
  server_model_available: boolean;
};
type AccountMode = "login" | "register";
type Action = AccountMode | "model" | "offline" | "logout";
type Pending = Action | "status" | null;

class WorkspaceError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

const PROVIDERS = [
  { name: "DeepSeek", address: "https://api.deepseek.com" },
  { name: "OpenAI", address: "https://api.openai.com/v1" },
  { name: "阿里云百炼", address: "https://dashscope.aliyuncs.com/compatible-mode/v1" },
];

async function workspaceJson(action: Action | "status", signal: AbortSignal, payload?: object): Promise<unknown> {
  // Never forward credentials to an environment-configured browser origin or through a redirect.
  const response = await fetch(`/contest-api/workspace/${action}`, {
    method: action === "status" ? "GET" : "POST",
    credentials: "same-origin", redirect: "error", cache: "no-store", signal,
    ...(payload ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) } : {}),
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const raw = typeof data?.detail === "string" && data.detail.length <= 240 ? data.detail : null;
    const fields = payload as { password?: string; confirm_password?: string; api_key?: string } | undefined;
    const detail = raw && ![fields?.password, fields?.confirm_password, fields?.api_key].some(secret => secret && raw.includes(secret)) ? raw : null;
    throw new WorkspaceError(detail ?? `工作区请求未完成（${response.status}），请重试。`, response.status);
  }
  return data;
}

function checkedWorkspaceStatus(data: unknown): WorkspaceStatus {
  const status = data as Partial<WorkspaceStatus> | null;
  if (!status || typeof status.account_exists !== "boolean" || typeof status.authenticated !== "boolean" || typeof status.ready !== "boolean" || typeof status.ai_available !== "boolean") {
    throw new WorkspaceError("本地服务返回了无效的工作区状态。请确认前后端均已更新。", 502);
  }
  return status as WorkspaceStatus;
}

async function workspaceRequest(action: Action | "status", signal: AbortSignal, payload?: object): Promise<WorkspaceStatus> {
  return checkedWorkspaceStatus(await workspaceJson(action, signal, payload));
}

function field(data: FormData, name: string) {
  const value = data.get(name);
  return typeof value === "string" ? value : "";
}

function clearInput(form: HTMLFormElement, name: string) {
  const input = form.elements.namedItem(name);
  if (input instanceof HTMLInputElement) input.value = "";
}

function clearAccountSecrets(form: HTMLFormElement | null) {
  if (!form) return;
  clearInput(form, "password");
  clearInput(form, "confirm_password");
}

function ModelForm({ status, busy, editing, onAction, onCancel }: {
  status: WorkspaceStatus; busy: Pending; editing: boolean;
  onAction: (action: Action, payload?: object) => void; onCancel: () => void;
}) {
  const [address, setAddress] = useState("");

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const data = new FormData(event.currentTarget);
    const payload = { base_url: field(data, "base_url").trim(), model: field(data, "model").trim(), api_key: field(data, "api_key").trim() };
    clearInput(event.currentTarget, "api_key");
    onAction("model", payload);
  }

  return <form className="workspace-form" onSubmit={submit} autoComplete="off">
    <fieldset disabled={busy !== null} className="workspace-fields">
      <label className="workspace-field" htmlFor="workspace-base-url"><span>API 基础地址</span>
        <input id="workspace-base-url" name="base_url" type="url" inputMode="url" value={address} onChange={event => setAddress(event.target.value)} maxLength={300} placeholder="https://…" autoCapitalize="none" spellCheck={false} aria-describedby="workspace-address-hint" required />
      </label>
      <div className="workspace-presets" role="group" aria-label="快速填入 API 地址">
        {PROVIDERS.map(({ name, address: preset }) => <button key={name} type="button" className="workspace-preset" aria-pressed={address === preset} onClick={() => setAddress(preset)}>{name}</button>)}
      </div>
      <p className="workspace-help workspace-field-hint" id="workspace-address-hint">网页支持以上服务的兼容地址；不要在地址末尾加 <code>/chat/completions</code>。</p>
      <label className="workspace-field" htmlFor="workspace-model-name"><span>模型名称</span>
        <input id="workspace-model-name" name="model" type="text" maxLength={120} placeholder="填写服务商提供的模型 ID" autoCapitalize="none" spellCheck={false} required />
      </label>
      <label className="workspace-field" htmlFor="workspace-api-key"><span>API Key</span>
        <input id="workspace-api-key" name="api_key" type="password" maxLength={1024} autoComplete="off" autoCapitalize="none" spellCheck={false} aria-describedby="workspace-api-notice" required />
      </label>
      <p className="workspace-notice" id="workspace-api-notice">验证会用 Key 向所选服务商发起最多两次模型请求（使用合成行情，可能计费）。账号密码不外发；Key 仅保留在本次服务端会话。</p>
      <button type="submit" className="primary" aria-describedby="workspace-api-notice">{busy === "model" ? "正在验证模型…" : editing ? "验证并更新 API" : "验证 API 并进入"}</button>
      {status.server_model_available && <button type="button" className="secondary" aria-describedby="workspace-api-notice" onClick={() => onAction("model", { use_server_config: true })}>验证服务器预设</button>}
      <div className="workspace-actions">
        <button type="button" className="workspace-text-button" onClick={() => onAction("offline")}>{editing ? "切换为离线模式" : "先用离线模式"}</button>
        {editing ? <button type="button" className="workspace-text-button" onClick={onCancel}>返回工作台</button>
          : <button type="button" className="workspace-text-button" onClick={() => onAction("logout")}>退出登录</button>}
      </div>
    </fieldset>
  </form>;
}

export default function WorkspaceGate({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<WorkspaceStatus | null>(null);
  const [busy, setBusy] = useState<Pending>("status");
  const [error, setError] = useState("");
  const [editing, setEditing] = useState(false);
  const [accountMode, setAccountMode] = useState<AccountMode | null>(null);
  const [revision, setRevision] = useState(0);
  const requestRef = useRef<AbortController | null>(null);
  const accountFormRef = useRef<HTMLFormElement | null>(null);
  const stage = !status ? "status" : !status.authenticated ? (accountMode ?? (status.account_exists ? "login" : "register")) : (!status.ready || editing ? "model" : "ready");

  const refresh = useCallback(async (notice = "") => {
    requestRef.current?.abort();
    clearAccountSecrets(accountFormRef.current);
    const controller = new AbortController();
    requestRef.current = controller;
    setStatus(null); setEditing(false); setAccountMode(null); setBusy("status"); setError("");
    try {
      const current = await workspaceRequest("status", controller.signal);
      if (!controller.signal.aborted) { setStatus(current); setError(notice); }
    } catch (failure) {
      if (!controller.signal.aborted) setError(failure instanceof WorkspaceError ? failure.message : "无法连接本地服务，请确认服务已启动。");
    } finally {
      if (requestRef.current === controller) { requestRef.current = null; setBusy(null); }
    }
  }, []);

  useEffect(() => {
    void refresh();
    const expired = () => { void refresh("登录已过期，请重新登录。"); };
    window.addEventListener("contest-session-expired", expired);
    return () => {
      requestRef.current?.abort();
      clearAccountSecrets(accountFormRef.current);
      window.removeEventListener("contest-session-expired", expired);
    };
  }, [refresh]);

  useEffect(() => {
    if (stage !== "status") document.getElementById(stage === "ready" ? "workspace-settings" : "workspace-gate-title")?.focus({ preventScroll: true });
  }, [stage]);

  function switchAccountMode(next: AccountMode) {
    if (requestRef.current || busy) return;
    clearAccountSecrets(accountFormRef.current);
    setAccountMode(next); setError("");
  }

  async function act(action: Action, payload?: object) {
    if (requestRef.current) return;
    const controller = new AbortController();
    requestRef.current = controller;
    setBusy(action); setError("");
    try {
      const next = await workspaceRequest(action, controller.signal, payload);
      if (controller.signal.aborted) return;
      setStatus(next); setEditing(false); setAccountMode(null);
      if (action === "model" || action === "offline") setRevision(previous => previous + 1);
    } catch (failure) {
      if (controller.signal.aborted) return;
      if (failure instanceof WorkspaceError) {
        if (action !== "login" && action !== "register" && failure.status === 401) {
          void refresh("登录已过期，请重新登录。"); return;
        }
        setError(failure.message);
      } else setError("无法连接本地服务，请重试。");
    } finally {
      if (requestRef.current === controller) { requestRef.current = null; setBusy(null); }
    }
  }

  function submitAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (requestRef.current || busy || (stage !== "register" && stage !== "login")) return;
    const data = new FormData(event.currentTarget);
    const username = field(data, "username").trim();
    const password = field(data, "password");
    const confirmation = field(data, "confirm_password");
    // Secrets stay in these request-local variables, never React/browser storage.
    clearAccountSecrets(event.currentTarget);
    if (!/^[\p{L}\p{N}_.-]{2,32}$/u.test(username)) {
      setError("账号须为 2–32 个文字、数字、下划线、点或连字符。"); return;
    }
    if (!password || Array.from(password).length > 128 || (stage === "register" && Array.from(password).length < 10)) {
      setError(stage === "register" ? "密码须为 10–128 位。" : "请填写有效密码。"); return;
    }
    if (stage === "register" && password !== confirmation) {
      setError("两次输入的密码不一致，请检查后重试。"); return;
    }
    void act(stage, { username, password, ...(stage === "register" ? { confirm_password: confirmation } : {}) });
  }

  const title = stage === "register" ? "注册账号" : stage === "login" ? `登录${APP_NAME}` : stage === "model" ? "连接模型 API" : "检查工作区";
  const description = stage === "register" ? "新账号独立保存本机研究记录，原账号可直接登录。"
    : stage === "model" ? "连接模型，或直接以离线模式进入。" : null;

  return <>
    {stage !== "ready" && <section className="workspace-gate" aria-labelledby="workspace-gate-title" aria-busy={busy !== null}>
      <div className="workspace-intro"><h1 id="workspace-gate-title" tabIndex={-1}>{title}</h1>
        {description && <p className="workspace-description">{description}</p>}
      </div>
      <div className="workspace-form-panel">
        {(stage === "login" || stage === "register") && <div className="workspace-login-method" role="group" aria-label="账号入口">
          <button type="button" aria-pressed={stage === "login"} disabled={busy !== null} onClick={() => switchAccountMode("login")}>账号登录</button>
          <button type="button" aria-pressed={stage === "register"} disabled={busy !== null} onClick={() => switchAccountMode("register")}>注册账号</button>
        </div>}
        {error && <div className="callout danger workspace-error" role="alert">{error}</div>}
        {stage === "status" && (error ? <button type="button" className="secondary" onClick={() => { void refresh(); }}>重新检查</button>
          : <p className="workspace-help" role="status">正在连接本地服务…</p>)}
        {(stage === "register" || stage === "login") && <form ref={accountFormRef} key={stage} className="workspace-form" onSubmit={submitAccount}>
          <fieldset disabled={busy !== null} className="workspace-fields">
            <label className="workspace-field" htmlFor="workspace-username"><span>账号</span>
              <input id="workspace-username" name="username" type="text" autoComplete="username" autoCapitalize="none" spellCheck={false} minLength={2} maxLength={32} required />
            </label>
            <label className="workspace-field" htmlFor="workspace-password"><span>密码</span>
              <input id="workspace-password" name="password" type="password" autoComplete={stage === "register" ? "new-password" : "current-password"} minLength={stage === "register" ? 10 : undefined} maxLength={128} required />
            </label>
            {stage === "register" && <>
              <label className="workspace-field" htmlFor="workspace-confirm-password"><span>确认密码</span>
                <input id="workspace-confirm-password" name="confirm_password" type="password" autoComplete="new-password" minLength={10} maxLength={128} required />
              </label>
              <p className="workspace-help">账号为 2–32 个字符，可用文字、数字、下划线、点和连字符。密码至少 10 位，请妥善保管。</p>
            </>}
            <button type="submit" className="primary">{busy === "register" || busy === "login" ? "正在处理…" : stage === "register" ? "注册并继续" : "登录"}</button>
          </fieldset>
        </form>}
        {stage === "login" && <p className="workspace-account-switch">没有账号？<button type="button" className="workspace-text-button" disabled={busy !== null} onClick={() => switchAccountMode("register")}>注册账号</button></p>}
        {stage === "model" && status && <>
          {editing && <p className="workspace-help workspace-edit-note">连接更新后会重新加载工作台。取消更改会保留当前页面。</p>}
          <ModelForm status={status} busy={busy} editing={editing} onAction={(action, payload) => { void act(action, payload); }} onCancel={() => { setEditing(false); setError(""); }} />
        </>}
      </div>
    </section>}

    {status?.authenticated && status.ready && <VerifiedApiEntry enabled={status.ai_available}><div className="workspace-content" hidden={stage !== "ready"}>
      <section className="workspace-bar" aria-label="账号及模型设置">
        <div className="workspace-identity"><strong>{status.username}</strong><span className={status.ai_available ? "pill good" : "pill"}>{status.ai_available ? `API 已验证${status.model ? ` · ${status.model}` : ""}` : "离线模式"}</span></div>
        <div className="workspace-actions">
          <button id="workspace-settings" type="button" className="workspace-text-button" disabled={busy !== null} onClick={() => { setEditing(true); setError(""); }}>API 设置</button>
          <button type="button" className="workspace-text-button" disabled={busy !== null} onClick={() => { void act("logout"); }}>{busy === "logout" ? "正在退出…" : "退出登录"}</button>
        </div>
      </section>
      {stage === "ready" && error && <div className="callout danger workspace-main-error" role="alert">{error}</div>}
      <div key={revision}>{children}</div>
    </div></VerifiedApiEntry>}
  </>;
}
