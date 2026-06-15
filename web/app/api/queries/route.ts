import { NextRequest, NextResponse } from "next/server";
import { listQueries } from "@/lib/db";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const sp = req.nextUrl.searchParams;
  const page = Math.max(1, Number(sp.get("page") ?? "1"));
  const pageSize = Math.min(200, Math.max(1, Number(sp.get("pageSize") ?? "50")));
  const result = await listQueries({
    check_type: sp.get("check_type") ?? undefined,
    status: sp.get("status") ?? undefined,
    config: sp.get("config") ?? undefined,
    url: sp.get("url") ?? undefined,
    q: sp.get("q") ?? undefined,
    sort: sp.get("sort") ?? undefined,
    dir: (sp.get("dir") as "asc" | "desc") ?? undefined,
    page,
    pageSize,
  });
  return NextResponse.json(result);
}
