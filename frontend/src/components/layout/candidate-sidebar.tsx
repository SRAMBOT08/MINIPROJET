"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard,
  Briefcase,
  User,
  Users,
  Video,
  GraduationCap,
  LogOut,
} from "lucide-react";
import { cn, getInitials } from "@/lib/utils";
import { useAuth } from "@/context/AuthContext";
import { Logo } from "@/components/ui/logo";

// Mirrors the recruiter Sidebar (components/layout/sidebar.tsx) visually so the
// candidate portal reads as the same product, with candidate-specific destinations.
const NAV_ITEMS = [
  { label: "Dashboard", href: "/portal", icon: LayoutDashboard },
  { label: "Browse Jobs", href: "/jobs", icon: Briefcase },
  { label: "My Profile", href: "/profile", icon: User },
  { label: "Group Discussions", href: "/group-discussions", icon: Users },
  { label: "Role Play", href: "/role-play", icon: Video },
  { label: "Training", href: "/training", icon: GraduationCap },
];

export default function CandidateSidebar() {
  const pathname = usePathname();
  const { user, logout } = useAuth();

  return (
    <aside className="relative flex w-60 shrink-0 flex-col bg-surface border-r border-border">
      <div className="flex items-center gap-2.5 h-16 border-b border-border px-5">
        <Logo size="default" withText />
      </div>

      <nav className="flex-1 overflow-y-auto py-4 px-3">
        <ul className="flex flex-col gap-0.5">
          {NAV_ITEMS.map(({ label, href, icon: Icon }) => {
            const isActive =
              pathname === href ||
              (href !== "/portal" && (pathname?.startsWith(href + "/") ?? false));
            return (
              <li key={href}>
                <Link
                  href={href}
                  prefetch={true}
                  className={cn(
                    "flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors duration-100",
                    isActive
                      ? "bg-brand-light text-brand"
                      : "text-text-muted hover:bg-bg hover:text-text-primary"
                  )}
                >
                  <Icon className={cn("h-4 w-4 shrink-0", isActive ? "text-brand" : "text-text-muted")} />
                  <span>{label}</span>
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>

      <div className="border-t border-border p-3">
        <div className="flex items-center gap-3 rounded-lg px-2 py-2">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand text-white text-xs font-semibold">
            {getInitials(user?.name || "Candidate")}
          </div>
          <div className="flex-1 min-w-0">
            <p className="text-sm font-medium text-text-primary truncate">{user?.name || "Candidate"}</p>
            <p className="text-xs text-text-muted truncate">{user?.email || ""}</p>
          </div>
          <button
            onClick={() => logout("/login")}
            className="text-text-muted hover:text-error transition-colors duration-100 p-1 rounded-md hover:bg-bg cursor-pointer"
            aria-label="Sign out"
            title="Sign out"
          >
            <LogOut className="h-4 w-4" />
          </button>
        </div>
      </div>
    </aside>
  );
}
