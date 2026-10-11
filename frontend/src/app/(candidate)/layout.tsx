"use client";

import { useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import { Loader2, Menu } from "lucide-react";
import CandidateNav from "@/components/layout/candidate-nav";
import CandidateSidebar from "@/components/layout/candidate-sidebar";
import { useAuth } from "@/context/AuthContext";
import { cn } from "@/lib/utils";
import { OnboardingNotice } from "@/features/candidate-onboarding/components/onboarding-notice";

export default function CandidateLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const { isAuthenticated, isLoading, user, role } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);

  const isHr =
    role === "admin" ||
    role === "recruiter" ||
    user?.role === "admin" ||
    user?.role === "recruiter";

  // Interview invitation links are opaque, single-session tokens. The session
  // API intentionally exposes the lobby/start flow without an application
  // login, so an invited candidate must be able to enter `/interview/*` even
  // when their portal session has expired or they have never created an
  // account. Portal and schedule pages remain protected below.
  const isPublicInterview = pathname?.startsWith("/interview/") ?? false;

  useEffect(() => {
    if (!isLoading && !isAuthenticated && !isPublicInterview) {
      const redirectUrl = encodeURIComponent(pathname || "/portal");
      router.replace(`/login?from=${redirectUrl}`);
    } else if (!isLoading && isAuthenticated && isHr && pathname === "/portal") {
      router.replace("/admin/dashboard");
    }
  }, [isLoading, isAuthenticated, isHr, isPublicInterview, router, pathname]);

  if (
    isLoading ||
    (!isLoading && isAuthenticated && isHr && pathname === "/portal")
  ) {
    return (
      <div className="min-h-screen bg-bg flex items-center justify-center">
        <div className="flex flex-col items-center gap-2">
          <Loader2 className="h-8 w-8 text-brand animate-spin" />
          <p className="text-sm text-text-muted">
            {isHr && pathname === "/portal"
              ? "Redirecting to HR dashboard..."
              : "Verifying session..."}
          </p>
        </div>
      </div>
    );
  }

  // Active live interview room has its own fixed dedicated header without tab switching
  const isInterviewRoom =
    pathname?.startsWith("/interview/") &&
    !pathname?.endsWith("/report") &&
    !pathname?.endsWith("/done");

  if (isInterviewRoom) {
    return <>{children}</>;
  }

  // The signed-in candidate portal uses a vertical sidebar, matching the HR
  // side's navigation pattern. Public/unauthenticated pages (job browsing,
  // login) keep the horizontal nav — there is no candidate account context
  // yet for a sidebar to navigate between.
  if (!isHr && isAuthenticated) {
    return (
      <div className="flex h-screen overflow-hidden bg-bg">
        {mobileSidebarOpen && (
          <div
            className="fixed inset-0 z-30 bg-black/40 md:hidden"
            onClick={() => setMobileSidebarOpen(false)}
          />
        )}
        <div
          className={cn(
            "fixed inset-y-0 left-0 z-40 md:relative md:flex md:z-auto transition-transform duration-200",
            mobileSidebarOpen ? "translate-x-0" : "-translate-x-full md:translate-x-0"
          )}
        >
          <CandidateSidebar />
        </div>

        <div className="flex flex-1 flex-col overflow-hidden min-w-0">
          <button
            onClick={() => setMobileSidebarOpen(true)}
            className="md:hidden flex h-14 w-14 shrink-0 items-center justify-center text-text-muted hover:text-text-primary border-b border-border bg-surface"
            aria-label="Open sidebar"
          >
            <Menu className="h-5 w-5" />
          </button>
          <main className="flex-1 overflow-y-auto">
            <div className="mx-auto max-w-5xl px-4 sm:px-6 py-8">
              {pathname === "/portal" && <OnboardingNotice />}
              {children}
            </div>
          </main>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-bg flex flex-col">
      {/* Top bar */}
      <CandidateNav maxWidthClass="max-w-5xl" />

      {/* Page content */}
      <main className="flex-1">
        <div className="mx-auto max-w-5xl px-4 sm:px-6 py-8">
          {children}
        </div>
      </main>

      {/* Footer */}
      <footer className="border-t border-border py-5 mt-auto">
        <div className="mx-auto max-w-5xl px-4 sm:px-6 flex items-center justify-between">
          <p className="text-xs text-text-muted">
            &copy; {new Date().getFullYear()} Intra AI
          </p>
          <p className="text-xs text-text-muted">
            Adaptive Multi-Agent AI Voice Interviews
          </p>
        </div>
      </footer>
    </div>
  );
}
