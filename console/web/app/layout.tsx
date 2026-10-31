import type { Metadata } from "next";
import "./globals.css";
import Shell from "@/components/Shell";

export const metadata: Metadata = {
  title: "Equity Console",
  description: "Local, read-only console for Indian-equity data, annual reports and concalls.",
};

// Sets the theme before first paint (stored choice, else the OS preference).
const themeScript = `try{var t=localStorage.getItem('ec-theme');if(!t){t=matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'}document.documentElement.dataset.theme=t}catch(e){document.documentElement.dataset.theme='light'}`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en-IN" data-theme="light" suppressHydrationWarning className="h-full antialiased">
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="min-h-full flex flex-col bg-bg text-ink">
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
