"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import { useAuth } from "@/lib/auth-context";
import { getDefaultRouteForRole } from "@/store/auth";

export default function HomePage() {
  const router = useRouter();
  const { status, session } = useAuth();

  useEffect(() => {
    if (status === "loading") {
      return;
    }
    router.replace(
      status === "authenticated" && session ? getDefaultRouteForRole(session.role) : "/login",
    );
  }, [status, session, router]);

  return null;
}
