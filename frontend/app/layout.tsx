import "./globals.css";
import type { Metadata, Viewport } from "next";
import { AppFrame } from "./components/AppFrame";

export const metadata: Metadata = {
  title: "Bee",
  description: "私有知识库问答助手",
};

export const viewport: Viewport = { width: "device-width", initialScale: 1, viewportFit: "cover" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body>
        <AppFrame>{children}</AppFrame>
      </body>
    </html>
  );
}
