import DecisionDesk from "./decision-desk";
import WorkspaceGate from "./workspace-gate";
import { APP_NAME, APP_SUBTITLE } from "./brand";

export default function Home() {
  return <>
    <header className="site-header">
      <div className="shell header-content">
        <div className="brand"><span className="brand-mark" aria-hidden="true">智</span><div><strong>{APP_NAME}</strong><span>{APP_SUBTITLE}</span></div></div>
      </div>
    </header>
    <main className="shell main-content">
      <WorkspaceGate><DecisionDesk /></WorkspaceGate>
      <footer className="site-footer">历史行情与合成案例分开展示 · 虚拟资金，仅供策略研究；不接实盘，不构成投资建议。</footer>
    </main>
  </>;
}
