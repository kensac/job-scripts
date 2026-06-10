"use client";

import { useEffect, useState } from "react";
import { Badge, Drawer, Group, Loader, Text } from "@mantine/core";
import { ResponseDetail } from "@/components/ResponseDetail";
import type { QueryRow } from "@/lib/types";
import { checkColor, statusColor } from "@/lib/format";

interface DetailResponse {
  query: QueryRow;
  timeline: QueryRow[];
}

export function RowDetailDrawer({
  id,
  onClose,
}: {
  id: number | null;
  onClose: () => void;
}) {
  const [data, setData] = useState<DetailResponse | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (id === null) {
      setData(null);
      return;
    }
    setLoading(true);
    fetch(`/api/queries/${id}`)
      .then((r) => r.json())
      .then(setData)
      .finally(() => setLoading(false));
  }, [id]);

  const q = data?.query;

  return (
    <Drawer
      opened={id !== null}
      onClose={onClose}
      position="right"
      size="xl"
      title={
        q ? (
          <Group gap="xs">
            <Badge color={checkColor(q.check_type)}>{q.check_type}</Badge>
            <Badge color={statusColor(q.status)} variant="light">
              {q.status}
            </Badge>
            <Text fw={600}>#{q.id}</Text>
          </Group>
        ) : (
          "Query detail"
        )
      }
    >
      {loading && <Loader />}
      {q && <ResponseDetail row={q} timeline={data?.timeline} />}
    </Drawer>
  );
}
