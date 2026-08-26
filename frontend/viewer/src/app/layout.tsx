import type { Metadata } from "next";
import { AppHeader } from "@/components/app-header";
import { AppRail } from "@/components/app-rail";
import { SkinProvider, SKIN_BOOTSTRAP_SCRIPT } from "@/components/skin-provider";
import { ThemeProvider } from "@/components/theme-provider";
import { ViewerStatusProvider } from "@/components/viewer-status";
import { TooltipProvider } from "@/components/ui/tooltip";
import "./globals.css";

export const metadata: Metadata = {
  title: "Agent RCA Incident Viewer",
  description:
    "Read-only operator view of stored Incidents, Evidence, Frozen Context and RCA Reports.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="overflow-x-clip" suppressHydrationWarning>
      <head>
        {/*
         * Applies a stored skin before first paint so it does not flash.
         *
         * suppressHydrationWarning is required because browser extensions
         * commonly inject their own <script> into <head> before React
         * hydrates. React then compares its expected node against the
         * injected one and reports an attribute mismatch. The bootstrap has
         * already run by then, so the warning is noise — but only this
         * element's check is suppressed, not the tree's.
         */}
        <script
          suppressHydrationWarning
          dangerouslySetInnerHTML={{ __html: SKIN_BOOTSTRAP_SCRIPT }}
        />
      </head>
      <body className="min-h-dvh overflow-x-clip antialiased">
        <ThemeProvider
          attribute="class"
          defaultTheme="system"
          enableSystem
          disableTransitionOnChange
        >
          <SkinProvider>
            <TooltipProvider delayDuration={200}>
              <ViewerStatusProvider>
                <a
                  href="#main"
                  className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-card focus:px-3 focus:py-2 focus:text-sm"
                >
                  Skip to content
                </a>
                <div className="flex min-h-dvh">
                  <AppRail />
                  <div className="flex min-w-0 flex-1 flex-col">
                    <AppHeader />
                    <main id="main" className="min-w-0 flex-1 px-4 py-3">
                      {children}
                    </main>
                  </div>
                </div>
              </ViewerStatusProvider>
            </TooltipProvider>
          </SkinProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
