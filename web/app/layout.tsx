import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";
import { APP_NAME, APP_SUBTITLE } from "./brand";

export const metadata: Metadata = {
  title: `${APP_NAME}｜${APP_SUBTITLE}`,
  description: "从历史行情或独立合成案例生成条件研究方案，独立校验、冻结决策。历史数据明确标注时点，不接实盘。",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
