import { useState, useEffect, useCallback } from "react";

const API = import.meta.env.VITE_API_URL || "http://localhost:5173";

const api = {
  get: (path) => fetch(`${API}/api${path}`).then((r) => r.json()),
  post: async (path, body) => {
    const r = await fetch(`${API}/api${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    // FastAPI errors come back as { detail: "..." }, normalise to { error: "..." }
    if (!r.ok) {
      const msg = data?.detail
        ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))
        : `HTTP ${r.status}`;
      return { error: msg };
    }
    return data;
  },
  del: (path) => fetch(`${API}/api${path}`, { method: "DELETE" }).then((r) => r.json()),
  put: async (path, body) => {
    const r = await fetch(`${API}/api${path}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) {
      const msg = data?.detail
        ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))
        : `HTTP ${r.status}`;
      return { error: msg };
    }
    return data;
  },
  patch: async (path, body) => {
    const r = await fetch(`${API}/api${path}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) {
      const msg = data?.detail
        ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))
        : `HTTP ${r.status}`;
      return { error: msg };
    }
    return data;
  },
  extend: async (envName, ttlDays) => {
    const r = await fetch(`${API}/api/envs/${envName}/extend`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ttl_days: ttlDays }),
    });
    const data = await r.json();
    if (!r.ok) return { error: data?.detail || `HTTP ${r.status}` };
    return data;
  },
};

// ── Design tokens ─────────────────────────────────────────────────────────────
const C = {
  bg: "#0e0f11", bg2: "#15171a", bg3: "#1c1f24",
  border: "rgba(255,255,255,0.08)", border2: "rgba(255,255,255,0.14)",
  text: "#e8e6e0", muted: "#8a8880",
  blue: "#4f9cf9", teal: "#38d9a9", amber: "#f4a742",
  coral: "#e05c6e", purple: "#9b7cf4",
};

const card  = { background: C.bg2, border: `1px solid ${C.border}`, borderRadius: 12, padding: "24px" };
const pill  = (color) => ({ display: "inline-block", padding: "2px 10px", borderRadius: 20, fontSize: 11, fontFamily: "'IBM Plex Mono', monospace", fontWeight: 500, background: `${color}22`, color });
const btn   = (color = C.blue, ghost = false) => ({ display: "inline-flex", alignItems: "center", gap: 6, padding: "7px 16px", borderRadius: 8, fontSize: 13, fontWeight: 500, cursor: "pointer", border: `1px solid ${ghost ? color + "55" : color}`, background: ghost ? "transparent" : `${color}18`, color, transition: "all 0.15s" });

// ── Shared primitives ─────────────────────────────────────────────────────────

function Badge({ type }) {
  const map = { fixed: [C.blue,"fixed"], poc: [C.amber,"poc"], openshift: [C.teal,"openshift"], aws: [C.amber,"aws"], unknown: [C.muted,"unknown"], deploying: [C.amber,"deploying"], healthy: [C.teal,"healthy"], ok: [C.teal,"ok"] };
  const [color, label] = map[type] || [C.muted, type];
  return <span style={pill(color)}>{label}</span>;
}

function MonoLabel({ children, color = C.muted }) {
  return <span style={{ fontFamily: "'IBM Plex Mono', monospace", fontSize: 11, color }}>{children}</span>;
}

function SectionHeader({ label, title, action }) {
  return (
    <div style={{ marginBottom: 20 }}>
      <div style={{ fontFamily: "'IBM Plex Mono', monospace", fontSize: 10, color: C.blue, letterSpacing: "0.15em", textTransform: "uppercase", marginBottom: 6 }}>{label}</div>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <h2 style={{ fontFamily: "'Syne', sans-serif", fontSize: 22, fontWeight: 700, color: C.text }}>{title}</h2>
        {action}
      </div>
    </div>
  );
}

function Spinner() {
  return <span style={{ display: "inline-block", width: 14, height: 14, border: `2px solid ${C.border2}`, borderTopColor: C.blue, borderRadius: "50%", animation: "spin 0.7s linear infinite" }} />;
}

function ErrorBox({ msg }) {
  if (!msg) return null;
  return <div style={{ background: `${C.coral}18`, border: `1px solid ${C.coral}44`, borderRadius: 8, padding: "8px 12px", fontSize: 13, color: C.coral, marginBottom: 14 }}>{msg}</div>;
}

function WarningBox({ warnings }) {
  if (!warnings || warnings.length === 0) return null;
  return (
    <div style={{ background: `${C.amber}12`, border: `1px solid ${C.amber}44`, borderRadius: 8, padding: "12px 14px", marginBottom: 14 }}>
      <div style={{ fontSize: 12, fontWeight: 500, color: C.amber, marginBottom: 6, fontFamily: "'IBM Plex Mono', monospace" }}>
        Created with warnings — manual steps required
      </div>
      {warnings.map((w, i) => (
        <div key={i} style={{ fontSize: 12, color: C.amber, opacity: 0.85, marginTop: 4, lineHeight: 1.5 }}>
          · {w}
        </div>
      ))}
    </div>
  );
}

function Modal({ title, onClose, children }) {
  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", zIndex: 200, display: "flex", alignItems: "center", justifyContent: "center" }}>
      <div style={{ ...card, width: 500, maxHeight: "90vh", overflowY: "auto", position: "relative" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 20 }}>
          <h3 style={{ fontFamily: "'Syne', sans-serif", fontSize: 17, fontWeight: 600, color: C.text }}>{title}</h3>
          <button onClick={onClose} style={{ background: "none", border: "none", color: C.muted, cursor: "pointer", fontSize: 20, lineHeight: 1 }}>×</button>
        </div>
        {children}
      </div>
    </div>
  );
}

function Field({ label, hint, children }) {
  return (
    <div style={{ marginBottom: 14 }}>
      <label style={{ display: "block", fontSize: 12, color: C.muted, marginBottom: 5, fontFamily: "'IBM Plex Mono', monospace" }}>{label}</label>
      {children}
      {hint && <div style={{ fontSize: 11, color: C.muted, marginTop: 4 }}>{hint}</div>}
    </div>
  );
}

function Input({ value, onChange, placeholder, type = "text" }) {
  return <input type={type} value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} style={{ width: "100%", background: C.bg3, border: `1px solid ${C.border2}`, borderRadius: 8, padding: "8px 12px", fontSize: 13, color: C.text, outline: "none", fontFamily: "inherit" }} />;
}

function Select({ value, onChange, options }) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} style={{ width: "100%", background: C.bg3, border: `1px solid ${C.border2}`, borderRadius: 8, padding: "8px 12px", fontSize: 13, color: C.text, outline: "none" }}>
      {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  );
}

// ── Platform radio toggle ─────────────────────────────────────────────────────

function PlatformToggle({ value, onChange }) {
  const platforms = [
    { id: "openshift", label: "OpenShift", color: C.teal },
    { id: "aws",       label: "AWS / EKS", color: C.amber },
  ];
  return (
    <div style={{ display: "flex", gap: 8 }}>
      {platforms.map(({ id, label, color }) => (
        <button
          key={id}
          onClick={() => onChange(id)}
          style={{
            flex: 1, padding: "8px 12px", borderRadius: 8, fontSize: 13, fontWeight: 500,
            cursor: "pointer", transition: "all 0.15s",
            border: `1px solid ${value === id ? color : C.border2}`,
            background: value === id ? `${color}18` : "transparent",
            color: value === id ? color : C.muted,
          }}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

// ── Confirmation modal with identity disclaimer ───────────────────────────────

function ConfirmModal({ title, actions, onConfirm, onCancel }) {
  const [identity, setIdentity] = useState(null);
  const [loading, setLoading]   = useState(true);

  useEffect(() => {
    api.get("/identity")
      .then((data) => { setIdentity(data); setLoading(false); })
      .catch(() => { setIdentity({ display_name: "unknown", warnings: [] }); setLoading(false); });
  }, []);

  const platformColor = identity?.warnings?.length ? C.amber : C.teal;

  return (
    <Modal title={title} onClose={onCancel}>
      {loading ? (
        <div style={{ display: "flex", gap: 10, alignItems: "center", color: C.muted, padding: 8 }}>
          <Spinner /> Resolving identity…
        </div>
      ) : (
        <>
          {/* Actor box */}
          <div style={{ background: C.bg3, border: `1px solid ${C.border2}`, borderRadius: 10, padding: "14px 16px", marginBottom: 14 }}>
            <div style={{ fontSize: 11, color: C.muted, fontFamily: "'IBM Plex Mono', monospace", textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 8 }}>
              Changes performed on behalf of
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
              {identity.github_login && (
                <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                  <span style={{ ...pill(C.teal), fontSize: 10 }}>GitHub</span>
                  <span style={{ fontSize: 13, color: C.text, fontWeight: 500 }}>
                    {identity.github_name || identity.github_login}
                  </span>
                  <MonoLabel>@{identity.github_login}</MonoLabel>
                  {identity.github_email && <MonoLabel>{identity.github_email}</MonoLabel>}
                </div>
              )}
              {identity.jenkins_user && (
                <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                  <span style={{ ...pill(C.blue), fontSize: 10 }}>Jenkins</span>
                  <span style={{ fontSize: 13, color: C.text }}>{identity.jenkins_user}</span>
                  {identity.jenkins_url && <MonoLabel>{identity.jenkins_url}</MonoLabel>}
                </div>
              )}
              {identity.git_name && (
                <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                  <span style={{ ...pill(C.muted), fontSize: 10 }}>Git</span>
                  <span style={{ fontSize: 13, color: C.text }}>{identity.git_name}</span>
                  {identity.git_email && <MonoLabel>{identity.git_email}</MonoLabel>}
                </div>
              )}
              {!identity.github_login && !identity.jenkins_user && !identity.git_name && (
                <span style={{ fontSize: 13, color: C.muted }}>No identity resolved — tokens may be missing</span>
              )}
            </div>
          </div>

          {/* Actions list */}
          <div style={{ marginBottom: 14 }}>
            <div style={{ fontSize: 11, color: C.muted, fontFamily: "'IBM Plex Mono', monospace", textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 8 }}>
              Actions to perform
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {actions.map((a, i) => (
                <div key={i} style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 13, color: C.text }}>
                  <span style={{ color: C.teal, marginTop: 1 }}>·</span>
                  <span>{a}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Warnings from identity resolution */}
          {identity.warnings?.length > 0 && (
            <WarningBox warnings={identity.warnings} />
          )}

          {/* Buttons */}
          <div style={{ display: "flex", gap: 10, marginTop: 4 }}>
            <button style={btn(C.teal)} onClick={onConfirm}>Confirm</button>
            <button style={btn(C.muted, true)} onClick={onCancel}>Cancel</button>
          </div>
        </>
      )}
    </Modal>
  );
}

// ── Services ──────────────────────────────────────────────────────────────────

function CreateServiceModal({ envs, onClose, onCreated }) {
  // Steps: "mode" → "details" → "confirm" → "done"
  const [step,     setStep]     = useState("mode");
  // Mode selection
  const [mode,     setMode]     = useState("template");  // template | fork | external
  // Common fields
  const [name,     setName]     = useState("");
  const [owner,    setOwner]    = useState("");
  const [desc,     setDesc]     = useState("");
  // Template mode
  const [template,  setTemplate]  = useState("springboot");
  const [templates, setTemplates] = useState([]);
  // Fork mode
  const [forkFrom, setForkFrom] = useState("");
  const [hostedServices, setHostedServices] = useState([]);
  // External mode
  const [extUrl,        setExtUrl]        = useState("");
  const [githubRepos,   setGithubRepos]   = useState(null);   // null=not loaded, []+=loaded
  const [reposLoading,  setReposLoading]  = useState(false);
  const [reposErr,      setReposErr]      = useState("");
  const [repoFilter,    setRepoFilter]    = useState("");
  // Result state
  const [loading,  setLoading]  = useState(false);
  const [err,      setErr]      = useState("");
  const [warnings, setWarnings] = useState([]);
  const [result,   setResult]   = useState(null);
  const [confirming, setConfirming] = useState(false);

  useEffect(() => {
    api.get("/templates").then((list) => {
      if (Array.isArray(list) && list.length > 0) {
        setTemplates(list);
        setTemplate(list[0].id);
      }
    });
  }, []);

  useEffect(() => {
    if (mode === "fork") {
      api.get("/services/hosted").then((list) => setHostedServices(Array.isArray(list) ? list : []));
    }
    if (mode === "external" && githubRepos === null) {
      setReposLoading(true);
      setReposErr("");
      api.get("/github/repos").then((res) => {
        setReposLoading(false);
        if (res.error || !Array.isArray(res)) {
          setReposErr(res.error || "Could not load repositories (is GITHUB_TOKEN set?)");
          setGithubRepos([]);
        } else {
          setGithubRepos(res);
        }
      });
    }
  }, [mode]);

  const modeLabels = {
    template: { label: "AP3-hosted — from template", color: C.teal,
      desc: "Create a new GitHub repo in the AP3 org, scaffolded from a built-in template." },
    fork: { label: "AP3-hosted — fork service", color: C.purple,
      desc: "Create a new GitHub repo by forking an existing AP3-hosted service." },
    external: { label: "External repo", color: C.amber,
      desc: "Reference an existing GitHub/Git repo. AP3 registers it in Jenkins and dev env — no repo creation." },
  };

  const confirmActions = [
    mode === "template" ? `Scaffold '${name}' from template '${template}'`
    : mode === "fork"   ? `Fork '${forkFrom}' → new service '${name}'`
    :                     `Register external repo: ${extUrl}`,
    ...(mode !== "external" ? [`Create GitHub repo (AP3-hosted)`] : []),
    `Register Jenkins pipeline '${name}'`,
    `Register '${name}' in dev versions.yaml`,
  ];

  const handleConfirmed = async () => {
    setConfirming(false);
    setLoading(true);
    const body = {
      name, owner, description: desc,
      source_mode: mode,
      template,
      fork_from: mode === "fork" ? forkFrom : "",
      external_repo_url: mode === "external" ? extUrl : "",
      force: true,
    };
    const res = await api.post("/services", body);
    setLoading(false);
    if (res.error) { setErr(res.error); setStep("details"); return; }
    onCreated();
    if (res.warnings && res.warnings.length > 0) {
      setWarnings(res.warnings);
      setResult(res);
      setStep("done");
    } else {
      onClose();
    }
  };

  if (confirming) {
    return (
      <ConfirmModal
        title="Create service — confirm"
        actions={confirmActions}
        onConfirm={handleConfirmed}
        onCancel={() => setConfirming(false)}
      />
    );
  }

  // ── Step: mode selection ──────────────────────────────────────────────────
  if (step === "mode") {
    return (
      <Modal title="Create service — choose mode" onClose={onClose}>
        <div style={{ marginBottom: 8, fontSize: 12, color: C.muted }}>
          How should this service be hosted?
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 10, marginBottom: 20 }}>
          {Object.entries(modeLabels).map(([id, { label, color, desc }]) => (
            <button
              key={id}
              onClick={() => setMode(id)}
              style={{
                textAlign: "left", padding: "12px 16px", borderRadius: 10,
                cursor: "pointer", transition: "all 0.15s",
                border: `1px solid ${mode === id ? color : C.border2}`,
                background: mode === id ? `${color}14` : C.bg3,
              }}
            >
              <div style={{ fontWeight: 500, color: mode === id ? color : C.text, marginBottom: 4 }}>
                {label}
              </div>
              <div style={{ fontSize: 12, color: C.muted }}>{desc}</div>
            </button>
          ))}
        </div>
        <button style={btn(C.teal)} onClick={() => setStep("details")}>
          Next → fill in details
        </button>
      </Modal>
    );
  }

  // ── Step: done (created with warnings) ───────────────────────────────────
  if (step === "done") {
    return (
      <Modal title="Service created" onClose={onClose}>
        <div style={{ fontSize: 13, color: C.teal, marginBottom: 14 }}>
          ✓ Service <strong>{name}</strong> created ({mode} mode). Some steps were skipped:
        </div>
        <WarningBox warnings={warnings} />
        <button style={btn(C.muted, true)} onClick={onClose}>Close</button>
      </Modal>
    );
  }

  // ── Step: details ─────────────────────────────────────────────────────────
  const modeColor = modeLabels[mode]?.color || C.teal;
  return (
    <Modal title={`Create service — ${modeLabels[mode]?.label}`} onClose={onClose}>
      <ErrorBox msg={err} />

      {/* Mode badge */}
      <div style={{ marginBottom: 16, display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ ...pill(modeColor), fontSize: 11 }}>{modeLabels[mode]?.label}</span>
        <button style={{ ...btn(C.muted, true), fontSize: 11, padding: "2px 8px" }}
                onClick={() => { setErr(""); setStep("mode"); }}>
          ← change
        </button>
      </div>

      <Field label="Service name (kebab-case)">
        <Input value={name} onChange={setName} placeholder="my-service" />
      </Field>
      <Field label="Owner team">
        <Input value={owner} onChange={setOwner} placeholder="team-backend" />
      </Field>
      <Field label="Description (optional)">
        <Input value={desc} onChange={setDesc} placeholder="What does this service do?" />
      </Field>

      {mode === "template" && (
        <Field label="Template">
          <Select
            value={template}
            onChange={setTemplate}
            options={templates.length
              ? templates.map((t) => ({
                  value: t.id,
                  label: t.language ? `${t.id}  (${t.language})` : t.id,
                }))
              : [{ value: template, label: template }]
            }
          />
        </Field>
      )}

      {mode === "fork" && (
        <Field label="Fork from (existing AP3 service)"
               hint="The new service starts as a full copy of the source.">
          <Select
            value={forkFrom}
            onChange={setForkFrom}
            options={[
              { value: "", label: hostedServices.length
                ? "— select a service —"
                : "(loading services…)" },
              ...hostedServices.map((s) => ({ value: s, label: s })),
            ]}
          />
        </Field>
      )}

      {mode === "external" && (
        <>
          {reposLoading && (
            <div style={{ display: "flex", gap: 8, alignItems: "center", color: C.muted, fontSize: 12, marginBottom: 12 }}>
              <Spinner /> Loading available repositories…
            </div>
          )}
          {reposErr && (
            <div style={{ fontSize: 12, color: C.amber, background: `${C.amber}12`,
                          border: `1px solid ${C.amber}33`, borderRadius: 8,
                          padding: "8px 12px", marginBottom: 12 }}>
              {reposErr}
            </div>
          )}
          {!reposLoading && githubRepos && githubRepos.length > 0 && (
            <Field label="Pick from available repositories"
                   hint="Repos not yet registered in the platform. Click one to select it.">
              <Input
                value={repoFilter}
                onChange={setRepoFilter}
                placeholder="Filter repositories…"
              />
              <div style={{ marginTop: 6, maxHeight: 220, overflowY: "auto",
                            border: `1px solid ${C.border2}`, borderRadius: 8 }}>
                {githubRepos
                  .filter((r) => !repoFilter || r.name.toLowerCase().includes(repoFilter.toLowerCase()))
                  .map((repo) => {
                    const selected = extUrl === repo.clone_url;
                    return (
                      <button
                        key={repo.name}
                        onClick={() => {
                          setExtUrl(repo.clone_url);
                          if (!name) setName(repo.name.toLowerCase().replace(/[^a-z0-9-]/g, "-"));
                          if (!desc && repo.description) setDesc(repo.description);
                        }}
                        style={{
                          display: "block", width: "100%", textAlign: "left",
                          padding: "10px 14px", cursor: "pointer",
                          background: selected ? `${C.amber}18` : "transparent",
                          border: "none",
                          borderBottom: `1px solid ${C.border}`,
                        }}
                      >
                        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                          <span style={{ fontFamily: "'IBM Plex Mono', monospace", fontSize: 13,
                                         color: selected ? C.amber : C.text, fontWeight: selected ? 600 : 400 }}>
                            {repo.name}
                          </span>
                          {repo.private && <span style={pill(C.muted)}>private</span>}
                          {repo.language && <span style={pill(C.purple)}>{repo.language}</span>}
                          {repo.updated_at && (
                            <span style={{ fontSize: 11, color: C.muted, marginLeft: "auto" }}>
                              {repo.updated_at.slice(0, 10)}
                            </span>
                          )}
                        </div>
                        {repo.description && (
                          <div style={{ fontSize: 11, color: C.muted, marginTop: 2 }}>
                            {repo.description}
                          </div>
                        )}
                      </button>
                    );
                  })}
                {githubRepos.filter((r) => !repoFilter || r.name.toLowerCase().includes(repoFilter.toLowerCase())).length === 0 && (
                  <div style={{ padding: "12px 14px", fontSize: 12, color: C.muted }}>No repos match.</div>
                )}
              </div>
            </Field>
          )}
          {!reposLoading && githubRepos && githubRepos.length === 0 && !reposErr && (
            <div style={{ fontSize: 12, color: C.muted, marginBottom: 12 }}>
              All repositories in this account are already registered in the platform.
            </div>
          )}
          <Field label={githubRepos?.length ? "Or enter URL manually" : "Repository URL"}
                 hint="Full Git URL. AP3 will NOT create or modify this repo.">
            <Input
              value={extUrl}
              onChange={setExtUrl}
              placeholder="https://github.com/my-org/existing-service.git"
            />
          </Field>
        </>
      )}

      <div style={{ fontSize: 12, color: C.muted, marginBottom: 16,
                    padding: "8px 12px", background: C.bg3, borderRadius: 8,
                    lineHeight: 1.6 }}>
        {mode === "template" && (
          <>Scaffold from <strong style={{ color: C.text }}>{template}</strong> template,
          create GitHub repo in <strong style={{ color: C.text }}>{/* org */}</strong>
          the AP3 org, configure branch protection, register Jenkins pipeline.</>
        )}
        {mode === "fork" && (
          <>Clone <strong style={{ color: C.text }}>{forkFrom || "…"}</strong>,
          create new GitHub repo <strong style={{ color: C.text }}>{name || "…"}</strong>,
          register Jenkins pipeline.</>
        )}
        {mode === "external" && (
          <>Register the external repo in Jenkins and the dev environment.
          No repo creation or scaffolding.</>
        )}
        {" "}A confirmation step will show before any changes are made.
      </div>

      <button
        style={btn(modeColor)}
        onClick={() => {
          if (!name || !owner) { setErr("Name and owner are required."); return; }
          if (mode === "fork" && !forkFrom) { setErr("Please select a service to fork from."); return; }
          if (mode === "external" && !extUrl) { setErr("External repo URL is required."); return; }
          setErr(""); setConfirming(true);
        }}
        disabled={loading}
      >
        {loading ? <Spinner /> : null} Review &amp; create
      </button>
    </Modal>
  );
}


function DeployModal({ service, envNames, onClose, onDeployed }) {
  const [env, setEnv] = useState(envNames[0] || "dev");
  const [version, setVersion] = useState("");
  const [platformOverride, setPlatformOverride] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [confirming, setConfirming] = useState(false);

  const confirmActions = [
    `Deploy ${service.name}:${version || "<version>"}`,
    `Target environment: ${env}`,
    platformOverride ? `Platform override: ${platformOverride}` : `Platform: auto (from env cluster profile)`,
    `Update envs/${env}/versions.yaml`,
  ];

  const handleSubmit = () => {
    if (!version) { setErr("Version is required."); return; }
    setErr("");
    setConfirming(true);
  };

  const handleConfirmed = async () => {
    setConfirming(false);
    setLoading(true);
    const body = { env, service: service.name, version, force: true };
    if (platformOverride) body.platform = platformOverride;
    const res = await api.post("/deploy", body);
    setLoading(false);
    if (res.error) { setErr(res.error); return; }
    onDeployed(); onClose();
  };

  if (confirming) {
    return (
      <ConfirmModal
        title={`Deploy ${service.name} — confirm`}
        actions={confirmActions}
        onConfirm={handleConfirmed}
        onCancel={() => setConfirming(false)}
      />
    );
  }

  return (
    <Modal title={`Deploy ${service.name}`} onClose={onClose}>
      <ErrorBox msg={err} />
      <Field label="Target environment">
        <Select value={env} onChange={setEnv} options={envNames.map((e) => ({ value: e, label: e }))} />
      </Field>
      <Field label="Version (semver)"><Input value={version} onChange={setVersion} placeholder="1.9.0" /></Field>
      <Field
        label="Platform override (optional)"
        hint="Leave empty to use the environment's cluster profile."
      >
        <Select
          value={platformOverride}
          onChange={setPlatformOverride}
          options={[
            { value: "",          label: "— auto (from env cluster profile) —" },
            { value: "openshift", label: "OpenShift" },
            { value: "aws",       label: "AWS / EKS" },
          ]}
        />
      </Field>
      <button style={btn(C.amber)} onClick={handleSubmit} disabled={loading}>
        {loading ? <Spinner /> : null} Review &amp; deploy
      </button>
    </Modal>
  );
}

function RemoveServiceModal({ service, onClose, onRemoved }) {
  const [loading,  setLoading]  = useState(false);
  const [err,      setErr]      = useState("");
  const [result,   setResult]   = useState(null);

  const actions = [
    `Remove '${service.name}' from all environments`,
    "Delete Jenkins pipeline",
    "Remove AP3/Jenkins webhooks from GitHub (repo is kept)",
    "Git commit the env changes",
  ];

  const handleConfirm = async () => {
    setLoading(true);
    setErr("");
    const res = await fetch(`${API}/api/services/${encodeURIComponent(service.name)}`, { method: "DELETE" });
    const data = await res.json();
    setLoading(false);
    if (!res.ok) {
      setErr(data?.detail || `HTTP ${res.status}`);
      return;
    }
    onRemoved();
    setResult(data);
  };

  return (
    <Modal title={`Remove service — ${service.name}`} onClose={onClose}>
      {!result ? (
        <>
          <div style={{ fontSize: 13, color: C.muted, marginBottom: 16, lineHeight: 1.6 }}>
            This will permanently remove <span style={{ color: C.text, fontFamily: "'IBM Plex Mono', monospace" }}>{service.name}</span> from the platform.
            The GitHub repository will <strong style={{ color: C.text }}>not</strong> be deleted.
          </div>
          <div style={{ background: C.bg3, border: `1px solid ${C.border}`, borderRadius: 8, padding: "12px 14px", marginBottom: 16 }}>
            <div style={{ fontSize: 11, color: C.muted, fontFamily: "'IBM Plex Mono', monospace", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.1em" }}>Actions</div>
            {actions.map((a, i) => (
              <div key={i} style={{ fontSize: 12, color: C.text, marginTop: 5, display: "flex", gap: 8 }}>
                <span style={{ color: C.coral }}>→</span> {a}
              </div>
            ))}
          </div>
          <ErrorBox msg={err} />
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 8 }}>
            <button style={btn(C.muted, true)} onClick={onClose} disabled={loading}>Cancel</button>
            <button style={btn(C.coral)} onClick={handleConfirm} disabled={loading}>
              {loading ? <><Spinner /> Removing…</> : "Remove service"}
            </button>
          </div>
        </>
      ) : (
        <>
          <div style={{ fontSize: 13, color: C.teal, marginBottom: 16 }}>
            ✓ Service <span style={{ fontFamily: "'IBM Plex Mono', monospace" }}>{service.name}</span> removed.
          </div>
          {result.envs?.length > 0 && (
            <div style={{ fontSize: 12, color: C.muted, marginBottom: 12 }}>
              Removed from: {result.envs.join(", ")}
            </div>
          )}
          <WarningBox warnings={result.warnings} />
          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 8 }}>
            <button style={btn(C.blue)} onClick={onClose}>Close</button>
          </div>
        </>
      )}
    </Modal>
  );
}

function ServiceHistory({ name }) {
  const [data,    setData]    = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.get(`/services/${encodeURIComponent(name)}/history?limit=40`).then((res) => {
      setData(res);
      setLoading(false);
    });
  }, [name]);

  if (loading) return (
    <div style={{ display: "flex", gap: 8, alignItems: "center", color: C.muted, fontSize: 12, padding: "12px 0" }}>
      <Spinner /> Loading history…
    </div>
  );

  if (!data) return null;

  const { commits = [], releases = [], deployments = [], github_available, repo_url } = data;

  // Build a tag-name → release lookup for quick access in commit rows
  const releaseByTag = Object.fromEntries(releases.map((r) => [r.tag, r]));

  // Build a version → [deployments] lookup to show inline deploy events
  const deploysByVersion = {};
  for (const d of deployments) {
    (deploysByVersion[d.version] = deploysByVersion[d.version] || []).push(d);
  }

  return (
    <div style={{ marginTop: 20 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
        <span style={{ fontSize: 11, color: C.blue, fontFamily: "'IBM Plex Mono', monospace",
                       textTransform: "uppercase", letterSpacing: "0.12em" }}>
          Release history
        </span>
        {repo_url && (
          <a href={repo_url.replace(/\.git$/, "")} target="_blank" rel="noreferrer"
             style={{ fontSize: 11, color: C.muted, textDecoration: "none" }}>
            ↗ repository
          </a>
        )}
      </div>

      {!github_available && (
        <div style={{ fontSize: 12, color: C.amber, marginBottom: 10 }}>
          GitHub unavailable (token not set or repo not found) — showing platform deployments only.
        </div>
      )}

      {/* Deployment-only view when no commits */}
      {commits.length === 0 && deployments.length === 0 && (
        <div style={{ fontSize: 12, color: C.muted }}>No history found.</div>
      )}

      {/* Commits timeline */}
      {commits.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 0, fontSize: 12 }}>
          {commits.map((c, i) => {
            const isTagged = c.tags.length > 0;
            const isRelease = c.tags.some((t) => releaseByTag[t]);
            const deploys = c.tags.flatMap((t) => {
              // match tag like "v1.2.0" to version "1.2.0"
              const ver = t.replace(/^v/, "");
              return deploysByVersion[ver] || deploysByVersion[t] || [];
            });
            return (
              <div key={c.sha} style={{
                display: "flex", gap: 12, alignItems: "flex-start",
                padding: "8px 0",
                borderBottom: i < commits.length - 1 ? `1px solid ${C.border}` : "none",
                background: isRelease ? `${C.purple}08` : "transparent",
              }}>
                {/* Timeline dot */}
                <div style={{ paddingTop: 2, flexShrink: 0 }}>
                  <div style={{
                    width: 8, height: 8, borderRadius: "50%",
                    background: isRelease ? C.purple : isTagged ? C.teal : C.border2,
                    border: `2px solid ${isRelease ? C.purple : isTagged ? C.teal : C.border2}`,
                  }} />
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  {/* Tags / release badges */}
                  {c.tags.length > 0 && (
                    <div style={{ display: "flex", gap: 4, flexWrap: "wrap", marginBottom: 4 }}>
                      {c.tags.map((t) => (
                        <span key={t} style={pill(isRelease ? C.purple : C.teal)}>{t}</span>
                      ))}
                    </div>
                  )}
                  {/* Release notes (first 120 chars) */}
                  {isRelease && c.tags.some((t) => releaseByTag[t]?.notes) && (
                    <div style={{ fontSize: 11, color: C.muted, fontStyle: "italic",
                                  marginBottom: 4, lineHeight: 1.5 }}>
                      {c.tags.map((t) => releaseByTag[t]?.notes).filter(Boolean)[0]?.slice(0, 120)}
                      {c.tags.map((t) => releaseByTag[t]?.notes).filter(Boolean)[0]?.length > 120 ? "…" : ""}
                    </div>
                  )}
                  {/* Commit message */}
                  <div style={{ color: C.text, lineHeight: 1.4 }}>{c.message}</div>
                  {/* Meta */}
                  <div style={{ display: "flex", gap: 12, marginTop: 3 }}>
                    <span style={{ fontFamily: "'IBM Plex Mono', monospace", fontSize: 10,
                                   color: C.muted }}>{c.short_sha}</span>
                    <span style={{ fontSize: 11, color: C.muted }}>{c.author}</span>
                    {c.date && <span style={{ fontSize: 11, color: C.muted }}>{c.date.slice(0, 10)}</span>}
                  </div>
                  {/* Platform deploy badges for this version */}
                  {deploys.length > 0 && (
                    <div style={{ display: "flex", gap: 4, flexWrap: "wrap", marginTop: 5 }}>
                      {deploys.map((d, j) => (
                        <span key={j} style={{ ...pill(C.amber), fontSize: 10 }}>
                          deployed → {d.env}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Deployments with no matching commit (older / pre-catalog) */}
      {(() => {
        const shownVersions = new Set(
          commits.flatMap((c) => c.tags.flatMap((t) => [t, t.replace(/^v/, "")]))
        );
        const orphan = deployments.filter((d) => !shownVersions.has(d.version) && !shownVersions.has(`v${d.version}`));
        if (orphan.length === 0) return null;
        return (
          <div style={{ marginTop: 12 }}>
            <div style={{ fontSize: 11, color: C.muted, marginBottom: 6 }}>Earlier deployments</div>
            {orphan.map((d, i) => (
              <div key={i} style={{ display: "flex", gap: 8, alignItems: "center",
                                    fontSize: 11, color: C.muted, padding: "4px 0",
                                    borderTop: `1px solid ${C.border}` }}>
                <span style={pill(C.amber)}>{d.version}</span>
                <span>→ {d.env}</span>
                <span style={{ marginLeft: "auto" }}>{d.deployed_at.slice(0, 10)}</span>
                <span>{d.actor}</span>
              </div>
            ))}
          </div>
        );
      })()}
    </div>
  );
}

function JenkinsBanner({ detail, svcName, onFixed }) {
  const [fixing, setFixing] = useState(false);
  if (!detail) return null;
  // Only show when Jenkins is configured (non-null result)
  if (detail.jenkins_ok === null || detail.jenkins_ok === undefined) {
    if (!detail.jenkins_warning) return null;
    // Jenkins not configured — show muted info, no fix button
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 14px", marginBottom: 10,
                    background: `${C.muted}10`, border: `1px solid ${C.muted}33`, borderRadius: 8,
                    fontSize: 12, color: C.muted }}>
        Jenkins: {detail.jenkins_warning}
      </div>
    );
  }
  if (detail.jenkins_ok === true) {
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 14px", marginBottom: 10,
                    background: `${C.teal}15`, border: `1px solid ${C.teal}44`, borderRadius: 8,
                    fontSize: 12, color: C.teal }}>
        &#10003; Jenkins multibranch pipeline registered
      </div>
    );
  }
  // jenkins_ok === false — pipeline missing, show fix button
  const fixJenkins = async (e) => {
    e.stopPropagation();
    setFixing(true);
    try {
      await api.post(`/services/${encodeURIComponent(svcName)}/fix-jenkins`);
      const updated = await api.get(`/services/${encodeURIComponent(svcName)}`);
      onFixed(updated);
    } catch (_) { /* swallow */ }
    setFixing(false);
  };
  // For external repos, show a softer amber (informational) rather than red
  const isExternal = detail.ap3_hosted === false;
  const color = isExternal ? C.muted : C.amber;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 14px", marginBottom: 10,
                  background: `${color}15`, border: `1px solid ${color}55`, borderRadius: 8,
                  fontSize: 12, color }}>
      <span style={{ fontWeight: 700 }}>⚠</span>
      {detail.jenkins_warning || `No Jenkins pipeline for '${svcName}'`}
      {isExternal && <span style={{ color: C.muted, fontSize: 11 }}>&nbsp;(external repo)</span>}
      <button
        style={{ ...btn(color, true), marginLeft: "auto", fontSize: 11, padding: "3px 10px" }}
        onClick={fixJenkins}
        disabled={fixing}
      >
        {fixing ? "Creating…" : "Create pipeline"}
      </button>
    </div>
  );
}

function GitFlowBanner({ detail, svcName, onFixed }) {
  const [fixing, setFixing] = useState(false);
  if (!detail || detail.repo_exists !== true) return null;
  if (detail.gitflow_ok === true) {
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 14px", marginBottom: 14,
                    background: `${C.teal}15`, border: `1px solid ${C.teal}44`, borderRadius: 8,
                    fontSize: 12, color: C.teal }}>
        &#10003; GitFlow branches in place (main, develop)
      </div>
    );
  }
  if (detail.gitflow_ok === false) {
    const fixGitflow = async (e) => {
      e.stopPropagation();
      setFixing(true);
      try {
        await api.post(`/services/${encodeURIComponent(svcName)}/fix-gitflow`);
        const updated = await api.get(`/services/${encodeURIComponent(svcName)}`);
        onFixed(updated);
      } catch (_) { /* swallow — network error already visible in console */ }
      setFixing(false);
    };
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 14px", marginBottom: 14,
                    background: `${C.amber}15`, border: `1px solid ${C.amber}55`, borderRadius: 8,
                    fontSize: 12, color: C.amber }}>
        <span style={{ fontWeight: 700 }}>⚠</span>
        Missing GitFlow branches: <strong>{detail.gitflow_missing?.join(", ")}</strong>
        <button
          style={{ ...btn(C.amber, true), marginLeft: "auto", fontSize: 11, padding: "3px 10px" }}
          onClick={fixGitflow}
          disabled={fixing}
        >
          {fixing ? "Fixing…" : "Fix GitFlow"}
        </button>
      </div>
    );
  }
  return null;
}

function ServicesPanel({ envNames }) {
  const [services, setServices] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [deploying,        setDeploying]        = useState(null);
  const [removing,         setRemoving]         = useState(null);
  const [expanded,         setExpanded]         = useState(null);
  const [historyOpen,      setHistoryOpen]      = useState({});   // name → bool
  const [svcDetail,        setSvcDetail]        = useState({});   // name → detail obj

  const load = useCallback(async () => {
    setLoading(true);
    setServices(await api.get("/services"));
    setLoading(false);
  }, []);

  const expand = useCallback(async (name) => {
    const next = expanded === name ? null : name;
    setExpanded(next);
    if (next && !svcDetail[next]) {
      const detail = await api.get(`/services/${encodeURIComponent(next)}`);
      setSvcDetail((prev) => ({ ...prev, [next]: detail }));
    }
  }, [expanded, svcDetail]);

  useEffect(() => { load(); }, [load]);

  const fixedEnvs = ["dev", "staging", "prod"];

  return (
    <div>
      <SectionHeader label="01 — AP3 Services" title="Services"
        action={<button style={btn(C.teal)} onClick={() => setShowCreate(true)}>+ New service</button>} />

      {loading ? (
        <div style={{ display: "flex", gap: 10, alignItems: "center", color: C.muted, padding: 16 }}><Spinner /> Loading services…</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {services.map((svc) => (
            <div key={svc.name} style={{ ...card, padding: 0, overflow: "hidden" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 16, padding: "16px 20px", cursor: "pointer" }}
                   onClick={() => expand(svc.name)}>
                <div style={{ flex: 1 }}>
                  <span style={{ fontWeight: 500, color: C.text, fontFamily: "'IBM Plex Mono', monospace", fontSize: 14 }}>{svc.name}</span>
                </div>
                {fixedEnvs.map((env) => (
                  <div key={env} style={{ minWidth: 120, textAlign: "center" }}>
                    <div style={{ fontSize: 10, color: C.muted, marginBottom: 2, textTransform: "uppercase", letterSpacing: "0.1em" }}>{env}</div>
                    <MonoLabel color={C.text}>{svc.versions?.[env]?.version || "—"}</MonoLabel>
                  </div>
                ))}
                <button style={btn(C.amber, true)} onClick={(e) => { e.stopPropagation(); setDeploying(svc); }}>Deploy</button>
              </div>

              {expanded === svc.name && (
                <div style={{ borderTop: `1px solid ${C.border}`, padding: "16px 20px", background: C.bg3 }}>
                  {/* Repository warning */}
                  {(() => {
                    const detail = svcDetail[svc.name];
                    if (!detail) return null;
                    if (detail.repo_exists === false) {
                      return (
                        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 14px", marginBottom: 14,
                                      background: `${C.coral}15`, border: `1px solid ${C.coral}55`, borderRadius: 8,
                                      fontSize: 12, color: C.coral }}>
                          <span style={{ fontWeight: 700 }}>⚠</span>
                          {detail.repo_warning || "Repository not found"}
                          {detail.repo_url && (
                            <a href={detail.repo_url.replace(/\.git$/, "")} target="_blank" rel="noreferrer"
                               style={{ marginLeft: "auto", color: C.coral, fontSize: 11, opacity: 0.7 }}>
                              {detail.repo_url.replace(/\.git$/, "").replace(/^https?:\/\/[^/]+\//, "")}
                            </a>
                          )}
                        </div>
                      );
                    }
                    if (detail.repo_warning) {
                      return (
                        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 14px", marginBottom: 14,
                                      background: `${C.amber}15`, border: `1px solid ${C.amber}55`, borderRadius: 8,
                                      fontSize: 12, color: C.amber }}>
                          <span style={{ fontWeight: 700 }}>⚠</span>
                          {detail.repo_warning}
                        </div>
                      );
                    }
                    return null;
                  })()}
                  {/* Jenkins consistency banner */}
                  <JenkinsBanner
                    detail={svcDetail[svc.name]}
                    svcName={svc.name}
                    onFixed={(updated) => setSvcDetail((prev) => ({ ...prev, [svc.name]: updated }))}
                  />
                  {/* GitFlow compliance banner */}
                  <GitFlowBanner
                    detail={svcDetail[svc.name]}
                    svcName={svc.name}
                    onFixed={(updated) => setSvcDetail((prev) => ({ ...prev, [svc.name]: updated }))}
                  />
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))", gap: 12 }}>
                    {Object.entries(svc.versions || {}).map(([env, d]) => (
                      <div key={env} style={{ background: C.bg2, border: `1px solid ${C.border}`, borderRadius: 8, padding: "12px 14px" }}>
                        <div style={{ fontSize: 10, color: C.muted, textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 6 }}>{env}</div>
                        <div style={{ fontFamily: "'IBM Plex Mono', monospace", fontSize: 13, color: C.text, marginBottom: 4 }}>{d.version}</div>
                        {d.deployed_at && <div style={{ fontSize: 11, color: C.muted }}>{d.deployed_at.slice(0, 10)}</div>}
                        <div style={{ marginTop: 6 }}><Badge type={d.health || "unknown"} /></div>
                      </div>
                    ))}
                  </div>
                  <div style={{ marginTop: 12, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                    <span style={{ fontSize: 12, color: C.muted }}>Last deployed: {svc.last_deployed?.slice(0, 19) || "—"}</span>
                    <div style={{ display: "flex", gap: 8 }}>
                      <button
                        style={btn(C.purple, true)}
                        onClick={(e) => {
                          e.stopPropagation();
                          setHistoryOpen((prev) => ({ ...prev, [svc.name]: !prev[svc.name] }));
                        }}
                      >
                        {historyOpen[svc.name] ? "Hide history" : "History"}
                      </button>
                      <button style={btn(C.coral, true)} onClick={(e) => { e.stopPropagation(); setRemoving(svc); }}>Remove service</button>
                    </div>
                  </div>
                  {historyOpen[svc.name] && <ServiceHistory name={svc.name} />}
                </div>
              )}
            </div>
          ))}
          {services.length === 0 && <div style={{ color: C.muted, padding: 16 }}>No services found. Create your first one!</div>}
        </div>
      )}

      {showCreate && <CreateServiceModal envs={envNames} onClose={() => setShowCreate(false)} onCreated={load} />}
      {deploying  && <DeployModal service={deploying} envNames={envNames} onClose={() => setDeploying(null)} onDeployed={load} />}
      {removing   && <RemoveServiceModal service={removing} onClose={() => setRemoving(null)} onRemoved={() => { setRemoving(null); load(); }} />}
    </div>
  );
}

// ── Environments ──────────────────────────────────────────────────────────────

function CreateEnvModal({ envNames, clusters, onClose, onCreated }) {
  const [name,      setName]      = useState("");
  const [base,      setBase]      = useState("staging");
  const [platform,  setPlatform]  = useState("openshift");
  const [cluster,   setCluster]   = useState("");
  const [namespace, setNamespace] = useState("");
  const [owner,     setOwner]     = useState("");
  const [desc,      setDesc]      = useState("");
  const [ttl,       setTtl]       = useState("14");
  const [loading,   setLoading]   = useState(false);
  const [err,       setErr]       = useState("");
  const [warnings,  setWarnings]  = useState([]);
  const [created,   setCreated]   = useState(null);  // created env name

  const [confirming, setConfirming] = useState(false);

  const confirmActions = [
    `Fork '${base}' → POC environment`,
    `Platform: ${platform}  |  Cluster: ${cluster || "auto"}`,
    namespace ? `Namespace: ${namespace} (provided)` : `Namespace: auto-generated`,
    `Write versions.yaml to platform-config`,
    `Git commit`,
    `Expires: ${ttl} days from now`,
  ];

  const handleSubmit = () => {
    if (!name || !owner) { setErr("Name and owner are required."); return; }
    setErr("");
    setConfirming(true);
  };

  const handleConfirmed = async () => {
    setConfirming(false);
    setLoading(true);
    const body = { name, base, platform, owner, description: desc, ttl_days: parseInt(ttl) || 14, force: true };
    if (cluster)   body.cluster   = cluster;
    if (namespace) body.namespace = namespace;
    const res = await api.post("/envs", body);
    setLoading(false);
    if (res.error) { setErr(res.error); return; }
    onCreated();
    if (res.warnings && res.warnings.length > 0) {
      setCreated(res.name);
      setWarnings(res.warnings);
    } else {
      onClose();
    }
  };

  const clusterOptions = [
    { value: "", label: "— auto (from platform default) —" },
    ...clusters
      .filter((c) => c.platform === platform || !c.platform)
      .map((c) => ({ value: c.name, label: c.name })),
  ];

  if (confirming) {
    return (
      <ConfirmModal
        title="Create POC environment — confirm"
        actions={confirmActions}
        onConfirm={handleConfirmed}
        onCancel={() => setConfirming(false)}
      />
    );
  }

  // After creation with warnings — show summary before closing
  if (created) {
    return (
      <Modal title="POC environment created" onClose={onClose}>
        <div style={{ fontSize: 13, color: C.teal, marginBottom: 14 }}>
          ✓ Environment <strong style={{ fontFamily: "'IBM Plex Mono', monospace" }}>{created}</strong> created successfully.
        </div>
        <WarningBox warnings={warnings} />
        <button style={btn(C.muted, true)} onClick={onClose}>Close</button>
      </Modal>
    );
  }

  return (
    <Modal title="Create POC environment" onClose={onClose}>
      <ErrorBox msg={err} />

      <Field label="POC name (short slug)">
        <Input value={name} onChange={setName} placeholder="payment-experiment" />
      </Field>

      <Field label="Base environment (fork versions from)">
        <Select value={base} onChange={setBase} options={envNames.map((e) => ({ value: e, label: e }))} />
      </Field>

      <Field label="Target platform">
        <PlatformToggle value={platform} onChange={(p) => { setPlatform(p); setCluster(""); }} />
      </Field>

      <Field label="Cluster" hint="Optional — leave empty to use the platform default cluster.">
        <Select value={cluster} onChange={setCluster} options={clusterOptions} />
      </Field>

      <Field
        label="Namespace (optional)"
        hint="Provide a pre-existing namespace if you have no rights to create one. Leave empty for auto-generated."
      >
        <Input value={namespace} onChange={setNamespace} placeholder="my-team-existing-ns" />
      </Field>

      <Field label="Owner">
        <Input value={owner} onChange={setOwner} placeholder="john.doe" />
      </Field>

      <Field label="Description">
        <Input value={desc} onChange={setDesc} placeholder="Testing async payment flow with Kafka" />
      </Field>

      <Field label="TTL (days)" hint="1–365 days. Expiry is a soft deadline — no automatic destruction.">
        <Input value={ttl} onChange={setTtl} placeholder="14" type="number" />
      </Field>

      <div style={{ fontSize: 12, color: C.muted, marginBottom: 16, padding: "8px 12px", background: C.bg3, borderRadius: 8, lineHeight: 1.6 }}>
        <strong style={{ color: platform === "aws" ? C.amber : C.teal }}>
          {platform === "aws" ? "AWS / EKS" : "OpenShift"}
        </strong>
        {" "}— forks versions from <strong style={{ color: C.text }}>{base}</strong>.
        {namespace
          ? " Uses provided namespace (not created by the platform)."
          : " Auto-generates a namespace."}
        {" "}Soft deadline: {ttl} days (no automatic destruction).
      </div>

      <button style={btn(C.purple)} onClick={handleSubmit} disabled={loading}>
        {loading ? <Spinner /> : null} Review &amp; create
      </button>
    </Modal>
  );
}

function DiffModal({ envA, envB, onClose }) {
  const [diff, setDiff] = useState(null);
  useEffect(() => { api.get(`/envs/${envA}/diff/${envB}`).then(setDiff); }, [envA, envB]);

  return (
    <Modal title={`Diff: ${envA} → ${envB}`} onClose={onClose}>
      {!diff ? <Spinner /> : (
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr>{["Service", envA, envB, ""].map((h) => (
              <th key={h} style={{ textAlign: "left", padding: "6px 10px", fontSize: 11, color: C.muted, fontFamily: "'IBM Plex Mono', monospace", borderBottom: `1px solid ${C.border2}` }}>{h}</th>
            ))}</tr>
          </thead>
          <tbody>
            {diff.map((r) => (
              <tr key={r.service} style={{ background: r.changed ? `${C.amber}08` : "transparent" }}>
                <td style={{ padding: "8px 10px", fontFamily: "'IBM Plex Mono', monospace", fontSize: 12, color: C.text }}>{r.service}</td>
                <td style={{ padding: "8px 10px", fontSize: 12, color: C.muted }}>{r[envA]}</td>
                <td style={{ padding: "8px 10px", fontSize: 12, color: r.changed ? C.teal : C.muted }}>{r[envB]}</td>
                <td style={{ padding: "8px 10px" }}>{r.changed && <Badge type="ok" />}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Modal>
  );
}

function ExpiryBanner({ env, onExtended }) {
  const [extending, setExtending] = useState(false);
  const status = env.expiry_status;
  if (!status || status === "ok" || status === "permanent") return null;

  const isExpired = status === "expired";
  const color = isExpired ? C.coral : C.amber;
  const daysText = isExpired
    ? `Expired ${Math.abs(env.days_remaining)} day(s) ago`
    : `Expires in ${env.days_remaining} day(s) — ${env.expires_at?.slice(0, 10)}`;

  const handleExtend = async () => {
    setExtending(true);
    const res = await api.extend(env.name, 14);
    setExtending(false);
    if (!res.error) onExtended();
  };

  return (
    <div style={{
      background: `${color}14`, border: `1px solid ${color}44`,
      borderRadius: 8, padding: "8px 12px",
      display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8,
    }}>
      <span style={{ fontSize: 12, color }}>
        {isExpired ? "!! " : "! "}{daysText}
      </span>
      <button
        style={{ ...btn(color, true), fontSize: 11, padding: "3px 10px", whiteSpace: "nowrap" }}
        onClick={handleExtend} disabled={extending}
      >
        {extending ? <Spinner /> : null} +14 days
      </button>
    </div>
  );
}

const LIVE_STATUS_DOT = {
  ok:       { color: "#38d9a9", label: "Healthy" },
  degraded: { color: "#f4a742", label: "Degraded — some pods not ready" },
  drift:    { color: "#f4813f", label: "Version drift — wrong version running" },
  missing:  { color: "#e05c6e", label: "Not deployed — no pods found on cluster" },
  unknown:  { color: "#8a8880", label: "Unknown — cluster unreachable" },
};

function EnvCard({ env, onDestroy, onDiff, onExtended }) {
  const isPoc = env.type === "poc";
  const isExpired = env.expiry_status === "expired";
  const isWarning = env.expiry_status === "warning";
  const accentColor = isExpired ? C.coral
    : isWarning ? C.amber
    : isPoc ? C.purple
    : env.name === "prod" ? C.coral
    : env.name === "staging" ? C.blue
    : C.teal;
  const svcCount = Object.keys(env.services || {}).length;

  const [liveStatus, setLiveStatus] = useState(null);
  const [statusLoading, setStatusLoading] = useState(false);

  const checkStatus = async () => {
    setStatusLoading(true);
    const data = await api.get(`/status/${env.name}`);
    setLiveStatus(data?.error ? null : data);
    setStatusLoading(false);
  };

  const svcLive = {};
  for (const s of liveStatus?.services || []) svcLive[s.name] = s;
  const overallDot = liveStatus ? (LIVE_STATUS_DOT[liveStatus.overall] || LIVE_STATUS_DOT.unknown) : null;

  return (
    <div style={{ ...card, borderLeft: `3px solid ${accentColor}`, display: "flex", flexDirection: "column", gap: 12 }}>
      {/* Header row */}
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 8 }}>
        <div>
          <div style={{ fontFamily: "'IBM Plex Mono', monospace", fontSize: 13, fontWeight: 500, color: C.text, marginBottom: 6 }}>{env.name}</div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
            <Badge type={env.type} />
            <Badge type={env.platform || "openshift"} />
            {env.cluster && env.cluster !== "unknown" && <MonoLabel>{env.cluster}</MonoLabel>}
            {env.namespace && env.namespace !== "unknown" && (
              <span style={{ fontSize: 10, color: C.muted, fontFamily: "'IBM Plex Mono', monospace" }}>ns: {env.namespace}</span>
            )}
          </div>
        </div>
        <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
          <button
            style={{ ...btn(C.teal, true), fontSize: 11, padding: "4px 10px" }}
            onClick={checkStatus}
            disabled={statusLoading}
            title="Check live cluster status"
          >
            {statusLoading ? "…" : liveStatus ? "Refresh" : "Status"}
          </button>
          <button style={{ ...btn(C.blue, true), fontSize: 11, padding: "4px 10px" }} onClick={() => onDiff(env.name)}>Diff</button>
          {isPoc && (
            <button style={{ ...btn(C.coral, true), fontSize: 11, padding: "4px 10px" }} onClick={() => onDestroy(env.name)}>Destroy</button>
          )}
        </div>
      </div>

      {/* Expiry warning banner */}
      {isPoc && <ExpiryBanner env={env} onExtended={onExtended} />}

      {/* Description */}
      {env.description && <div style={{ fontSize: 12, color: C.muted, fontStyle: "italic" }}>{env.description}</div>}

      {/* Live status overall banner */}
      {liveStatus && (
        <div style={{
          display: "flex", alignItems: "center", gap: 8, padding: "5px 10px", borderRadius: 6,
          background: `${overallDot.color}15`, border: `1px solid ${overallDot.color}44`,
          fontSize: 11, color: overallDot.color, fontFamily: "'IBM Plex Mono', monospace",
        }}>
          <span style={{ width: 7, height: 7, borderRadius: "50%", background: overallDot.color, flexShrink: 0 }} />
          {liveStatus.reachable
            ? overallDot.label
            : `UNREACHABLE — ${liveStatus.error || ""}`}
          <span style={{ marginLeft: "auto", color: C.muted, fontSize: 10 }}>
            {liveStatus.checked_at?.slice(11, 19)} UTC
          </span>
        </div>
      )}

      {/* Services chips */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        {Object.entries(env.services || {}).slice(0, 6).map(([svc, d]) => {
          const live = svcLive[svc];
          const dot = live ? (LIVE_STATUS_DOT[live.status] || LIVE_STATUS_DOT.unknown) : null;
          const hasDrift = live?.running_version && live.running_version !== d.version;
          return (
            <div
              key={svc}
              style={{
                background: C.bg3,
                border: `1px solid ${dot ? dot.color + "55" : C.border}`,
                borderRadius: 6, padding: "4px 10px",
                display: "flex", gap: 8, alignItems: "center",
              }}
              title={live?.message || undefined}
            >
              {dot && <span style={{ width: 6, height: 6, borderRadius: "50%", background: dot.color, flexShrink: 0 }} />}
              <span style={{ fontSize: 12, color: C.text }}>{svc}</span>
              <MonoLabel color={accentColor}>{d.version}</MonoLabel>
              {hasDrift && <MonoLabel color={C.coral}>live:{live.running_version}</MonoLabel>}
            </div>
          );
        })}
        {svcCount > 6 && <span style={{ fontSize: 12, color: C.muted, padding: "4px 10px" }}>+{svcCount - 6} more</span>}
        {svcCount === 0 && <span style={{ fontSize: 12, color: C.muted }}>No services deployed</span>}
      </div>

      {/* Footer meta */}
      <div style={{ display: "flex", gap: 16, fontSize: 11, color: C.muted, flexWrap: "wrap" }}>
        {env.updated_at && <span>Updated: {env.updated_at.slice(0, 10)}</span>}
        {isPoc && env.expires_at && !isExpired && !isWarning && (
          <span>Expires: {env.expires_at.slice(0, 10)}</span>
        )}
      </div>
    </div>
  );
}

function EnvsPanel() {
  const [envs,       setEnvs]       = useState([]);
  const [clusters,   setClusters]   = useState([]);
  const [loading,    setLoading]    = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [diffTarget, setDiffTarget] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    const [envData, clusterData] = await Promise.all([
      api.get("/envs"),
      api.get("/clusters"),
    ]);
    setEnvs(envData);
    setClusters(clusterData);
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  const destroy = async (name) => {
    if (!confirm(`Destroy environment '${name}'?`)) return;
    await api.del(`/envs/${name}`);
    load();
  };

  const expiredCount = envs.filter((e) => e.expiry_status === "expired").length;
  const warningCount = envs.filter((e) => e.expiry_status === "warning").length;

  const envNames = envs.map((e) => e.name);
  const fixed    = envs.filter((e) => e.type === "fixed");
  const pocs     = envs.filter((e) => e.type === "poc");

  return (
    <div>
      <SectionHeader
        label="02 — Environments"
        title={
          <span>
            Environments
            {expiredCount > 0 && (
              <span style={{ ...pill(C.coral), fontSize: 11, marginLeft: 10 }}>
                {expiredCount} expired
              </span>
            )}
            {warningCount > 0 && expiredCount === 0 && (
              <span style={{ ...pill(C.amber), fontSize: 11, marginLeft: 10 }}>
                {warningCount} expiring soon
              </span>
            )}
          </span>
        }
        action={<button style={btn(C.purple)} onClick={() => setShowCreate(true)}>+ New POC</button>}
      />

      {loading ? (
        <div style={{ display: "flex", gap: 10, alignItems: "center", color: C.muted, padding: 16 }}><Spinner /> Loading environments…</div>
      ) : (
        <>
          <div style={{ fontSize: 12, color: C.muted, marginBottom: 10, fontFamily: "'IBM Plex Mono', monospace", textTransform: "uppercase", letterSpacing: "0.1em" }}>Fixed</div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(310px, 1fr))", gap: 12, marginBottom: 28 }}>
            {fixed.map((e) => <EnvCard key={e.name} env={e} onDestroy={destroy} onDiff={setDiffTarget} onExtended={load} />)}
          </div>

          {pocs.length > 0 && (
            <>
              <div style={{ fontSize: 12, color: C.muted, marginBottom: 10, fontFamily: "'IBM Plex Mono', monospace", textTransform: "uppercase", letterSpacing: "0.1em" }}>POC / Ephemeral</div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(310px, 1fr))", gap: 12 }}>
                {pocs.map((e) => <EnvCard key={e.name} env={e} onDestroy={destroy} onDiff={setDiffTarget} onExtended={load} />)}
            </div>
            </>
          )}
        </>
      )}

      {showCreate && (
        <CreateEnvModal
          envNames={envNames}
          clusters={clusters}
          onClose={() => setShowCreate(false)}
          onCreated={load}
        />
      )}
      {diffTarget && <DiffModal envA={diffTarget} envB="prod" onClose={() => setDiffTarget(null)} />}
    </div>
  );
}

// ── History panel ─────────────────────────────────────────────────────────────

const EVENT_COLORS = {
  env_create:  "#38d9a9",
  env_destroy: "#e05c6e",
  env_update:  "#4f9cf9",
  deploy:      "#9b7cf4",
  service_reg: "#f4a742",
  reset:       "#aaaaaa",
};

const EVENT_ICONS = {
  env_create:  "+",
  env_destroy: "×",
  env_update:  "~",
  deploy:      "▶",
  service_reg: "★",
  reset:       "↺",
};

function HistoryPanel() {
  const [events,      setEvents]      = useState([]);
  const [loading,     setLoading]     = useState(true);
  const [envFilter,   setEnvFilter]   = useState("");
  const [svcFilter,   setSvcFilter]   = useState("");
  const [typeFilter,  setTypeFilter]  = useState("");
  const [actorFilter, setActorFilter] = useState("");
  const [envNames,    setEnvNames]    = useState([]);
  const [fullHistory, setFullHistory] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    const params = new URLSearchParams();
    if (envFilter)   params.set("env",     envFilter);
    if (svcFilter)   params.set("service", svcFilter);
    if (typeFilter)  params.set("type",    typeFilter);
    if (actorFilter) params.set("actor",   actorFilter);
    if (fullHistory) params.set("full",    "true");
    params.set("limit", "200");
    const data = await api.get(`/history?${params}`);
    setEvents(Array.isArray(data) ? data : []);
    setLoading(false);
  }, [envFilter, svcFilter, typeFilter, actorFilter, fullHistory]);

  useEffect(() => {
    api.get("/envs").then((envs) => setEnvNames(envs.map((e) => e.name)));
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <div>
      <SectionHeader label="03 — Audit" title="Platform History" />

      {/* Filter bar */}
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 20 }}>
        <div style={{ flex: "1 1 140px" }}>
          <Select
            value={envFilter}
            onChange={setEnvFilter}
            options={[
              { value: "", label: "All environments" },
              ...envNames.map((e) => ({ value: e, label: e })),
            ]}
          />
        </div>
        <div style={{ flex: "1 1 140px" }}>
          <Select
            value={typeFilter}
            onChange={setTypeFilter}
            options={[
              { value: "",            label: "All event types" },
              { value: "deploy",      label: "Deployments" },
              { value: "env_create",  label: "Env created" },
              { value: "env_destroy", label: "Env destroyed" },
              { value: "env_update",  label: "Env updated" },
              { value: "reset",       label: "Platform reset" },
            ]}
          />
        </div>
        <div style={{ flex: "1 1 160px" }}>
          <Input value={svcFilter}   onChange={setSvcFilter}   placeholder="Filter by service…" />
        </div>
        <div style={{ flex: "1 1 160px" }}>
          <Input value={actorFilter} onChange={setActorFilter} placeholder="Filter by actor…" />
        </div>
        <button
          style={{ ...btn(fullHistory ? C.blue : C.bg3, fullHistory), whiteSpace: "nowrap",
                   border: `1px solid ${fullHistory ? C.blue : C.border}` }}
          onClick={() => setFullHistory(f => !f)}
          title="Show history across all resets"
        >
          {fullHistory ? "↺ Full history" : "Since last reset"}
        </button>
        <button style={{ ...btn(C.blue, true), whiteSpace: "nowrap" }} onClick={load}>
          Refresh
        </button>
      </div>

      {loading ? (
        <div style={{ display: "flex", gap: 10, alignItems: "center", color: C.muted, padding: 16 }}>
          <Spinner /> Loading history…
        </div>
      ) : events.length === 0 ? (
        <div style={{ color: C.muted, padding: 16 }}>No events found for these filters.</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          {events.map((e, i) => {
            const color  = EVENT_COLORS[e.event_type] || C.muted;
            const icon   = EVENT_ICONS[e.event_type]  || "·";
            const detail = e.version
              ? `${e.version}${e.cluster ? ` [${e.cluster}]` : ""}`
              : e.message || "";
            return (
              <div
                key={i}
                style={{
                  display: "grid",
                  gridTemplateColumns: "16px 150px 120px 1fr 1fr 1fr",
                  gap: "0 16px",
                  alignItems: "center",
                  padding: "9px 16px",
                  background: i % 2 === 0 ? C.bg2 : C.bg3,
                  borderRadius: 6,
                  borderLeft: `2px solid ${color}`,
                  fontSize: 12,
                }}
              >
                {/* Icon */}
                <span style={{ color, fontWeight: 700, fontSize: 13, textAlign: "center" }}>{icon}</span>
                {/* Timestamp */}
                <MonoLabel color={C.muted}>{e.timestamp.slice(0, 19).replace("T", " ")}</MonoLabel>
                {/* Type badge */}
                <span style={{ ...pill(color), fontSize: 10 }}>{e.label}</span>
                {/* Env */}
                <div style={{ display: "flex", gap: 6, alignItems: "center", minWidth: 0 }}>
                  <MonoLabel color={C.text}>{e.env}</MonoLabel>
                  {e.platform && (
                    <span style={{ ...pill(e.platform === "aws" ? C.amber : C.teal), fontSize: 9 }}>
                      {e.platform}
                    </span>
                  )}
                </div>
                {/* Actor */}
                <MonoLabel color={C.muted}>{(e.actor || "—").slice(0, 28)}</MonoLabel>
                {/* Detail */}
                <div style={{ minWidth: 0, overflow: "hidden" }}>
                  {e.service && (
                    <span style={{ fontFamily: "'IBM Plex Mono', monospace", color: C.text, marginRight: 6 }}>
                      {e.service}
                    </span>
                  )}
                  {detail && <span style={{ color: C.muted, fontSize: 11 }}>{detail}</span>}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {!loading && events.length > 0 && (
        <div style={{ marginTop: 12, fontSize: 11, color: C.muted, textAlign: "right" }}>
          {events.length} event{events.length !== 1 ? "s" : ""}
          {envFilter || svcFilter || typeFilter || actorFilter ? " (filtered)" : ""}
        </div>
      )}
    </div>
  );
}



// ── Integration settings ──────────────────────────────────────────────────────

function IntegrationSettings() {
  const [config,   setConfig]   = useState(null);
  const [editing,  setEditing]  = useState(false);
  const [loading,  setLoading]  = useState(true);
  const [saving,   setSaving]   = useState(false);
  const [err,      setErr]      = useState("");
  // Edit state
  const [githubUrl,    setGithubUrl]    = useState("");
  const [accountType,  setAccountType]  = useState("org");
  const [githubOrg,    setGithubOrg]    = useState("");
  const [jenkinsUrl,   setJenkinsUrl]   = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    const data = await api.get("/platform/config");
    setConfig(data);
    setGithubUrl(data.github_url   || "");
    setAccountType(data.github_account_type || "org");
    setGithubOrg(data.github_org   || "");
    setJenkinsUrl(data.jenkins_url || "");
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  const save = async () => {
    setSaving(true); setErr("");
    const res = await api.patch("/platform/config", {
      github_url: githubUrl, github_account_type: accountType,
      github_org: githubOrg, jenkins_url: jenkinsUrl,
    });
    setSaving(false);
    if (res.error) { setErr(res.error); return; }
    setConfig(res); setEditing(false);
  };

  const TokenBadge = ({ label, isSet }) => (
    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
      <span style={{ fontSize: 12, color: C.muted, fontFamily: "'IBM Plex Mono', monospace",
                     minWidth: 120 }}>{label}</span>
      <span style={{ ...pill(isSet ? C.teal : C.coral), fontSize: 10 }}>
        {isSet ? "set" : "not set"}
      </span>
      {!isSet && (
        <span style={{ fontSize: 11, color: C.muted }}>
          → set in .env or environment
        </span>
      )}
    </div>
  );

  if (loading) return (
    <div style={{ display: "flex", gap: 8, color: C.muted, padding: 12 }}>
      <Spinner /> Loading config…
    </div>
  );

  return (
    <div style={{ ...card, marginBottom: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center",
                    marginBottom: 16 }}>
        <div>
          <div style={{ fontFamily: "'Syne', sans-serif", fontSize: 15, fontWeight: 600,
                        color: C.text, marginBottom: 2 }}>
            Integration settings
          </div>
          <div style={{ fontSize: 12, color: C.muted }}>
            Stored in platform.yaml — editable here or directly in the file.
          </div>
        </div>
        {!editing && (
          <button style={{ ...btn(C.blue, true), fontSize: 12 }}
                  onClick={() => setEditing(true)}>Edit</button>
        )}
      </div>

      <ErrorBox msg={err} />

      {editing ? (
        <>
          <Field label="GitHub URL" hint="https://github.com or your GHE URL">
            <Input value={githubUrl} onChange={setGithubUrl}
                   placeholder="https://github.com" />
          </Field>
          <Field label="Account type">
            <Select value={accountType} onChange={setAccountType}
                    options={[
                      { value: "org",  label: "Organisation (recommended for teams)" },
                      { value: "user", label: "Personal user account" },
                    ]} />
          </Field>
          <Field label={accountType === "org" ? "Organisation name" : "GitHub username"}>
            <Input value={githubOrg} onChange={setGithubOrg}
                   placeholder={accountType === "org" ? "my-org" : "myusername"} />
          </Field>
          <Field label="Jenkins URL">
            <Input value={jenkinsUrl} onChange={setJenkinsUrl}
                   placeholder="https://jenkins.internal" />
          </Field>
          <div style={{ display: "flex", gap: 10, marginTop: 4 }}>
            <button style={btn(C.teal)} onClick={save} disabled={saving}>
              {saving ? <Spinner /> : null} Save to platform.yaml
            </button>
            <button style={btn(C.muted, true)}
                    onClick={() => { setEditing(false); setErr(""); load(); }}>
              Cancel
            </button>
          </div>
        </>
      ) : (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "160px 1fr",
                        gap: "6px 16px", fontSize: 13, marginBottom: 16 }}>
            {[
              ["GitHub URL",    config?.github_url],
              ["Account type",  config?.github_account_type],
              ["Org / user",    config?.github_org],
              ["Jenkins URL",   config?.jenkins_url],
            ].map(([label, value]) => (
              <>
                <span key={label + "l"} style={{ color: C.muted, fontSize: 12,
                       fontFamily: "'IBM Plex Mono', monospace" }}>{label}</span>
                <span key={label + "v"} style={{ color: C.text, fontFamily: "'IBM Plex Mono', monospace",
                       fontSize: 12 }}>{value || "—"}</span>
              </>
            ))}
          </div>

          <div style={{ borderTop: `1px solid ${C.border}`, paddingTop: 12 }}>
            <div style={{ fontSize: 11, color: C.muted, marginBottom: 8,
                          fontFamily: "'IBM Plex Mono', monospace",
                          textTransform: "uppercase", letterSpacing: "0.1em" }}>
              Tokens (env vars)
            </div>
            <TokenBadge label="GITHUB_TOKEN"  isSet={config?.github_token_set} />
            <TokenBadge label="JENKINS_USER"  isSet={config?.jenkins_user_set} />
            <TokenBadge label="JENKINS_TOKEN" isSet={config?.jenkins_token_set} />
          </div>
        </>
      )}
    </div>
  );
}

// ── Platform panel ────────────────────────────────────────────────────────────

function ClusterForm({ cluster, onSave, onCancel }) {
  // cluster is null for "add new", or an existing ClusterSchema for "edit"
  const editing = cluster != null;
  const [name,     setName]     = useState(cluster?.name     || "");
  const [platform, setPlatform] = useState(cluster?.platform || "openshift");
  // OpenShift fields
  const [apiUrl,   setApiUrl]   = useState(cluster?.api_url  || "");
  const [context,  setContext]  = useState(cluster?.context  || "");
  // AWS fields
  const [region,   setRegion]   = useState(cluster?.region   || "eu-west-1");
  const [eksName,  setEksName]  = useState(cluster?.cluster_name || "");
  // Shared
  const [registry, setRegistry] = useState(cluster?.registry || "");
  const [suffix,   setSuffix]   = useState(cluster?.helm_values_suffix || "");
  const [loading,  setLoading]  = useState(false);
  const [err,      setErr]      = useState("");

  const submit = async () => {
    if (!name) { setErr("Cluster name is required."); return; }
    setLoading(true);
    const body = { name, platform, registry, helm_values_suffix: suffix };
    if (platform === "openshift") { body.api_url = apiUrl; body.context = context; }
    else                          { body.region = region; body.cluster_name = eksName; }
    const res = editing
      ? await api.put(`/clusters/${name}`, body)
      : await api.post("/clusters", body);
    setLoading(false);
    if (res.error) { setErr(res.error); return; }
    onSave(res);
  };

  return (
    <div style={{ ...card, marginBottom: 16 }}>
      <div style={{ fontFamily: "'Syne', sans-serif", fontSize: 15, fontWeight: 600,
                    color: C.text, marginBottom: 16 }}>
        {editing ? `Edit cluster: ${cluster.name}` : "Add cluster"}
      </div>
      <ErrorBox msg={err} />

      {!editing && (
        <Field label="Cluster name" hint="e.g. openshift-dev, eks-prod">
          <Input value={name} onChange={setName} placeholder="openshift-dev" />
        </Field>
      )}

      <Field label="Platform">
        <PlatformToggle value={platform} onChange={(p) => { setPlatform(p); }} />
      </Field>

      {platform === "openshift" ? (
        <>
          <Field label="API server URL"
                 hint="https://api.your-cluster.example.com:6443">
            <Input value={apiUrl} onChange={setApiUrl}
                   placeholder="https://api.cluster.internal:6443" />
          </Field>
          <Field label="kubeconfig context name">
            <Input value={context} onChange={setContext}
                   placeholder={name || "openshift-dev"} />
          </Field>
        </>
      ) : (
        <>
          <Field label="AWS region">
            <Input value={region} onChange={setRegion} placeholder="eu-west-1" />
          </Field>
          <Field label="EKS cluster name"
                 hint="Used with 'aws eks update-kubeconfig'">
            <Input value={eksName} onChange={setEksName}
                   placeholder={name || "my-eks-cluster"} />
          </Field>
        </>
      )}

      <Field label="Container registry"
             hint={platform === "aws"
               ? "ECR URL: 123456789.dkr.ecr.eu-west-1.amazonaws.com"
               : "Internal registry: registry.internal"}>
        <Input value={registry} onChange={setRegistry}
               placeholder={platform === "aws"
                 ? "123456789.dkr.ecr.eu-west-1.amazonaws.com"
                 : "registry.internal"} />
      </Field>

      <Field label="Helm values suffix"
             hint="Resolves to helm/values-{suffix}.yaml in each service repo. Defaults to last segment of cluster name.">
        <Input value={suffix} onChange={setSuffix}
               placeholder={name.split("-").pop() || "dev"} />
      </Field>

      <div style={{ display: "flex", gap: 10, marginTop: 4 }}>
        <button style={btn(C.teal)} onClick={submit} disabled={loading}>
          {loading ? <Spinner /> : null} {editing ? "Save changes" : "Add cluster"}
        </button>
        <button style={btn(C.muted, true)} onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

function ClusterCard({ cluster, onEdit, onDeleted }) {
  const [deleting, setDeleting] = useState(false);
  const color = cluster.platform === "aws" ? C.amber : C.teal;

  const handleDelete = async () => {
    if (!confirm(`Remove cluster profile "${cluster.name}"?`)) return;
    if (cluster.in_use?.length > 0) {
      if (!confirm(
        `WARNING: this cluster is used by ${cluster.in_use.join(", ")}.\n` +
        `Remove anyway? The environments will keep their reference but the profile will be gone.`
      )) return;
    }
    setDeleting(true);
    const qs = cluster.in_use?.length > 0 ? "?force=true" : "";
    await fetch(`${API}/api/clusters/${cluster.name}${qs}`, { method: "DELETE" });
    setDeleting(false);
    onDeleted();
  };

  const endpoint = cluster.platform === "aws"
    ? `${cluster.region} / ${cluster.cluster_name || "—"}`
    : cluster.api_url || cluster.context || "—";

  return (
    <div style={{ ...card, borderLeft: `3px solid ${color}`, display: "flex",
                  flexDirection: "column", gap: 10 }}>
      <div style={{ display: "flex", alignItems: "flex-start",
                    justifyContent: "space-between" }}>
        <div>
          <div style={{ fontFamily: "'IBM Plex Mono', monospace", fontSize: 13,
                        fontWeight: 500, color: C.text, marginBottom: 5 }}>
            {cluster.name}
          </div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
            <Badge type={cluster.platform} />
            <MonoLabel>{cluster.registry}</MonoLabel>
            <span style={{ fontSize: 10, color: C.muted, fontFamily: "'IBM Plex Mono', monospace" }}>
              values-{cluster.helm_values_suffix}.yaml
            </span>
          </div>
        </div>
        <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
          <button style={{ ...btn(C.blue, true), fontSize: 11, padding: "4px 10px" }}
                  onClick={() => onEdit(cluster)}>Edit</button>
          <button style={{ ...btn(C.coral, true), fontSize: 11, padding: "4px 10px" }}
                  onClick={handleDelete} disabled={deleting}>
            {deleting ? <Spinner /> : "Remove"}
          </button>
        </div>
      </div>

      <div style={{ fontSize: 12, color: C.muted }}>
        {cluster.platform === "aws" ? "EKS" : "API"}: {endpoint}
      </div>

      {cluster.in_use?.length > 0 && (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          <span style={{ fontSize: 10, color: C.muted }}>Used by:</span>
          {cluster.in_use.map((e) => (
            <span key={e} style={{ ...pill(C.blue), fontSize: 10 }}>{e}</span>
          ))}
        </div>
      )}
    </div>
  );
}

function PlatformPanel() {
  const [clusters,  setClusters]  = useState([]);
  const [loading,   setLoading]   = useState(true);
  const [adding,    setAdding]    = useState(false);
  const [editing,   setEditing]   = useState(null);  // ClusterSchema | null

  const load = useCallback(async () => {
    setLoading(true);
    setClusters(await api.get("/clusters"));
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  const ocp = clusters.filter((c) => c.platform === "openshift");
  const aws = clusters.filter((c) => c.platform === "aws");

  const handleSaved = () => { setAdding(false); setEditing(null); load(); };

  return (
    <div>
      <SectionHeader
        label="04 — Infrastructure"
        title="Platform & Clusters"
        action={
          !adding && !editing
            ? <button style={btn(C.teal)} onClick={() => setAdding(true)}>+ Add cluster</button>
            : null
        }
      />

      <IntegrationSettings />

      {(adding || editing) && (
        <ClusterForm
          cluster={editing}
          onSave={handleSaved}
          onCancel={() => { setAdding(false); setEditing(null); }}
        />
      )}

      {loading ? (
        <div style={{ display: "flex", gap: 10, alignItems: "center",
                      color: C.muted, padding: 16 }}>
          <Spinner /> Loading clusters…
        </div>
      ) : clusters.length === 0 ? (
        <div style={{ ...card, color: C.muted, textAlign: "center", padding: 32 }}>
          No cluster profiles defined.{" "}
          <button style={{ ...btn(C.teal), display: "inline-flex" }}
                  onClick={() => setAdding(true)}>
            Add your first cluster
          </button>
        </div>
      ) : (
        <>
          {ocp.length > 0 && (
            <>
              <div style={{ fontSize: 12, color: C.teal, marginBottom: 10,
                            fontFamily: "'IBM Plex Mono', monospace",
                            textTransform: "uppercase", letterSpacing: "0.1em" }}>
                OpenShift — {ocp.length} cluster{ocp.length !== 1 ? "s" : ""}
              </div>
              <div style={{ display: "grid",
                            gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))",
                            gap: 12, marginBottom: 28 }}>
                {ocp.map((c) => (
                  <ClusterCard key={c.name} cluster={c}
                               onEdit={setEditing} onDeleted={load} />
                ))}
              </div>
            </>
          )}

          {aws.length > 0 && (
            <>
              <div style={{ fontSize: 12, color: C.amber, marginBottom: 10,
                            fontFamily: "'IBM Plex Mono', monospace",
                            textTransform: "uppercase", letterSpacing: "0.1em" }}>
                AWS / EKS — {aws.length} cluster{aws.length !== 1 ? "s" : ""}
              </div>
              <div style={{ display: "grid",
                            gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))",
                            gap: 12 }}>
                {aws.map((c) => (
                  <ClusterCard key={c.name} cluster={c}
                               onEdit={setEditing} onDeleted={load} />
                ))}
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}

// ── App shell ─────────────────────────────────────────────────────────────────

export default function App() {
  const [tab,      setTab]      = useState("services");
  const [envNames, setEnvNames] = useState([]);

  useEffect(() => {
    api.get("/envs").then((envs) => setEnvNames(envs.map((e) => e.name)));
  }, []);

  return (
    <>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Syne:wght@700&family=DM+Sans:wght@400;500&display=swap');
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: ${C.bg}; color: ${C.text}; font-family: 'DM Sans', sans-serif; font-size: 14px; line-height: 1.6; }
        button { font-family: inherit; }
        input, select, option { font-family: inherit; color: ${C.text}; background: transparent; }
        @keyframes spin { to { transform: rotate(360deg); } }
        ::-webkit-scrollbar { width: 6px; } ::-webkit-scrollbar-track { background: ${C.bg}; } ::-webkit-scrollbar-thumb { background: ${C.border2}; border-radius: 3px; }
      `}</style>

      <header style={{ borderBottom: `1px solid ${C.border}`, padding: "20px 40px", display: "flex", alignItems: "center", gap: 24 }}>
        <div>
          <div style={{ fontFamily: "'IBM Plex Mono', monospace", fontSize: 10, color: C.blue, letterSpacing: "0.15em", textTransform: "uppercase", marginBottom: 2 }}>Platform Dashboard</div>
          <div style={{ fontFamily: "'Syne', sans-serif", fontSize: 18, fontWeight: 700, color: C.text }}>AP3 Platform</div>
        </div>
        <nav style={{ display: "flex", gap: 4, marginLeft: "auto" }}>
          {[["services", "Services"], ["envs", "Environments"], ["history", "History"], ["platform", "Platform"]].map(([id, label]) => (
            <button key={id} onClick={() => setTab(id)} style={{ padding: "8px 18px", borderRadius: 8, fontSize: 13, fontWeight: 500, cursor: "pointer", border: `1px solid ${tab === id ? C.border2 : "transparent"}`, background: tab === id ? C.bg3 : "transparent", color: tab === id ? C.text : C.muted }}>
              {label}
            </button>
          ))}
        </nav>
        <a href="/docs/devops-guide.html" target="_blank" rel="noreferrer" style={{ ...btn(C.muted, true), fontSize: 12, textDecoration: "none" }}>Docs</a>
      </header>

      <main style={{ maxWidth: 1100, margin: "0 auto", padding: "36px 40px" }}>
        {tab === "services" && <ServicesPanel envNames={envNames} />}
        {tab === "envs"     && <EnvsPanel />}
        {tab === "history"  && <HistoryPanel />}
        {tab === "platform" && <PlatformPanel />}
      </main>
    </>
  );
}
