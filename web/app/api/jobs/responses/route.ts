import { NextRequest, NextResponse } from "next/server";
import { getJobResponses } from "@/lib/db";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const url = req.nextUrl.searchParams.get("url");
  if (!url) {
    return NextResponse.json({ error: "url required" }, { status: 400 });
  }
  return NextResponse.json({ responses: getJobResponses(url) });
}
