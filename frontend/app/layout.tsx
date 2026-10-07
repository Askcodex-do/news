import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "News AI — Global & Local",
  description:
    "An accuracy-first news platform. Independent AI synthesis from multiple verified sources.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
