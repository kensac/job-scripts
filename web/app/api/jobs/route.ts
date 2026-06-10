import { NextRequest, NextResponse } from "next/server";
import { listJobs } from "@/lib/db";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const sp = req.nextUrl.searchParams;
  const page = Math.max(1, Number(sp.get("page") ?? "1"));
  const pageSize = Math.min(200, Math.max(1, Number(sp.get("pageSize") ?? "50")));
  return NextResponse.json(
    listJobs({
      q: sp.get("q") ?? undefined,
      config: sp.get("config") ?? undefined,
      verdict: sp.get("verdict") ?? undefined,
      page,
      pageSize,
    }),
  );
}
