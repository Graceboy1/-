"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { APP_NAME, APP_SUBTITLE } from "./brand";
import { createStartupParticles, type StartupParticles } from "./startup-particles";
import "./startup-gate.css";

const STORAGE_KEY = "smart-trading-intro:v1";
const DURATION_MS = 5000;
const smooth = (from: number, to: number, value: number) => {
  const p = Math.max(0, Math.min(1, (value - from) / (to - from)));
  return p * p * (3 - 2 * p);
};

/** Only mount after authentication and successful API configuration.
 * The parent retains all auth/model decisions. This visual gate neither reads
 * credentials nor requests data, and children are not mounted behind the intro.
 */
export default function StartupGate({ children }: { children: ReactNode }) {
  const [finished, setFinished] = useState(false);
  const completeRef = useRef(false);
  const releaseRef = useRef<() => void>(() => {});
  const rootRef = useRef<HTMLElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const titleRef = useRef<HTMLHeadingElement>(null);

  const finish = useCallback(() => {
    if (completeRef.current) return;
    completeRef.current = true;
    releaseRef.current();
    try { window.sessionStorage.setItem(STORAGE_KEY, "1"); } catch { /* Private-mode storage cannot block entry. */ }
    setFinished(true);
  }, []);

  useEffect(() => {
    if (finished) {
      const focusFrame = window.requestAnimationFrame(() => {
        document.getElementById("workspace-settings")?.focus({ preventScroll: true });
      });
      return () => window.cancelAnimationFrame(focusFrame);
    }
    let frame = 0;
    let deadline = 0;
    let renderer: StartupParticles | null = null;
    let observer: ResizeObserver | null = null;
    let media: MediaQueryList | null = null;
    let released = false;
    let started = 0;
    let lastPaint = -Infinity;

    const release = () => {
      if (released) return;
      released = true;
      window.cancelAnimationFrame(frame);
      window.clearTimeout(deadline);
      observer?.disconnect();
      media?.removeEventListener?.("change", onReducedMotion);
      document.removeEventListener("visibilitychange", onVisibility);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", resize);
      try { renderer?.destroy(); } catch { /* A failed canvas must not trap the workspace. */ }
      renderer = null;
    };
    releaseRef.current = release;

    function resize() {
      if (released || !renderer) return;
      try {
        const root = rootRef.current;
        const heading = titleRef.current;
        if (!root || !heading) return finish();
        const rect = root.getBoundingClientRect();
        const titleRect = heading.getBoundingClientRect();
        const styles = getComputedStyle(heading);
        renderer.resize(rect.width, rect.height, {
          x: titleRect.left - rect.left + titleRect.width / 2,
          y: titleRect.top - rect.top + titleRect.height / 2,
          fontSize: parseFloat(styles.fontSize), fontFamily: styles.fontFamily,
          fontWeight: styles.fontWeight, tracking: parseFloat(styles.letterSpacing) || 0,
        });
        paint(performance.now());
      } catch { finish(); }
    }
    function paint(now: number) {
      if (released || !renderer || !rootRef.current) return;
      const seconds = Math.max(0, (now - started) / 1000);
      renderer.draw(seconds);
      rootRef.current.style.setProperty("--startup-title-opacity", String(smooth(2.4, 3.1, seconds)));
      rootRef.current.style.setProperty("--startup-subtitle-opacity", String(smooth(2.8, 3.55, seconds)));
      rootRef.current.style.setProperty("--startup-out-opacity", String(1 - smooth(4.5, 5, seconds)));
    }
    function tick(now: number) {
      frame = 0;
      if (released || document.hidden) return;
      if (now - started >= DURATION_MS) return finish();
      try {
        const interval = window.innerWidth < 720 ? 1000 / 30 : 1000 / 60;
        if (now - lastPaint >= interval - 1) { paint(now); lastPaint = now; }
        if (!released) frame = window.requestAnimationFrame(tick);
      } catch { finish(); }
    }
    function onVisibility() {
      window.cancelAnimationFrame(frame);
      frame = 0;
      if (!released && !document.hidden) {
        if (performance.now() - started >= DURATION_MS) finish();
        else frame = window.requestAnimationFrame(tick);
      }
    }
    function onKey(event: KeyboardEvent) {
      if (event.isComposing || event.altKey || event.ctrlKey || event.metaKey) return;
      if (event.code === "Space" || event.key === " " || event.key === "Spacebar" || event.key === "Escape") {
        event.preventDefault();
        finish();
      }
    }
    function onReducedMotion() { if (media?.matches) finish(); }

    try {
      // Fail open independently of RAF, including hidden or throttled tabs.
      deadline = window.setTimeout(finish, 6000);
      const replay = new URLSearchParams(window.location.search).get("intro") === "1";
      let seen = false;
      try { seen = window.sessionStorage.getItem(STORAGE_KEY) === "1"; } catch { /* Continue without storage. */ }
      media = window.matchMedia("(prefers-reduced-motion: reduce)");
      if ((!replay && seen) || media.matches) {
        finish();
        return release;
      }
      if (!canvasRef.current || !rootRef.current) {
        finish();
        return release;
      }
      started = performance.now();
      renderer = createStartupParticles(canvasRef.current, APP_NAME);
      resize();
      if (released) return release;
      document.addEventListener("visibilitychange", onVisibility);
      document.addEventListener("keydown", onKey);
      media.addEventListener?.("change", onReducedMotion);
      if (typeof ResizeObserver !== "undefined") {
        observer = new ResizeObserver(resize);
        observer.observe(rootRef.current);
      } else window.addEventListener("resize", resize);
      rootRef.current?.focus({ preventScroll: true });
      if (!document.hidden) frame = window.requestAnimationFrame(tick);
    } catch { finish(); }
    return release;
  }, [finished, finish]);

  if (finished) return <>{children}</>;
  return <section className="startup-screen" ref={rootRef} role="dialog" aria-modal="true" tabIndex={-1} aria-keyshortcuts="Space Escape" aria-labelledby="startup-title" aria-describedby="startup-subtitle startup-instruction">
    <canvas className="startup-canvas" ref={canvasRef} aria-hidden="true" />
    <div className="startup-copy">
      <h1 id="startup-title" className="startup-title" ref={titleRef}>{APP_NAME}</h1>
      <p id="startup-subtitle" className="startup-subtitle">{APP_SUBTITLE}</p>
    </div>
    <p id="startup-instruction" className="startup-instruction">品牌开场约五秒，完成后进入工作台。可按空格键或 Escape 直接进入。</p>
  </section>;
}
