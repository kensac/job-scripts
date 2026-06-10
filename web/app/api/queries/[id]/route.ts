import { NextResponse } from "next/server";
import { getJobTimeline, getQuery } from "@/lib/db";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  const query = getQuery(Number(id));
  if (!query) {
    return NextResponse.json({ error: "not found" }, { status: 404 });
  }
  const timeline = query.url ? getJobTimeline(query.url) : [];
  return NextResponse.json({ query, timeline });
}
