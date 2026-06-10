"use client";

import { useEffect, useState } from "react";
import {
  Accordion,
  Anchor,
  Badge,
  Drawer,
  Group,
  Loader,
  Stack,
  Text,
} from "@mantine/core";
import { ResponseDetail } from "@/components/ResponseDetail";
import type { QueryRow } from "@/lib/types";
import { checkColor, fmtDateTime, fmtInt, statusColor } from "@/lib/format";

export function JobDrawer({
  url,
  onClose,
}: {
  url: string | null;
  onClose: () => void;
}) {
  const [responses, setResponses] = useState<QueryRow[] | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!url) {
      setResponses(null);
      return;
    }
    setLoading(true);
    fetch(`/api/jobs/responses?url=${encodeURIComponent(url)}`)
      .then((r) => r.json())
      .then((d) => setResponses(d.responses))
      .finally(() => setLoading(false));
  }, [url]);

  const head = responses?.find((r) => r.company || r.job_title);
  const totalTokens =
    responses?.reduce((s, r) => s + (r.total_tokens ?? 0), 0) ?? 0;

  return (
    <Drawer
      opened={url !== null}
      onClose={onClose}
      position="right"
      size="xl"
      title={<Text fw={600}>Job detail</Text>}
    >
      {loading && <Loader />}
      {responses && (
        <Stack>
          <Stack gap={4}>
            <Text fw={600}>{head?.job_title || "Untitled role"}</Text>
            <Text size="sm" c="dimmed">
              {head?.company || "Unknown company"}
            </Text>
            {url && (
              <Anchor href={url} target="_blank" size="xs">
                {url}
              </Anchor>
            )}
            <Group gap="xs" mt={4}>
              <Badge variant="light">{responses.length} responses</Badge>
              <Badge variant="light" color="gray">
                {fmtInt(totalTokens)} tokens
              </Badge>
            </Group>
          </Stack>

          <Accordion variant="separated" defaultValue={`r-${responses[0]?.id}`}>
            {responses.map((r) => (
              <Accordion.Item key={r.id} value={`r-${r.id}`}>
                <Accordion.Control>
                  <Group gap="xs">
                    <Badge size="sm" color={checkColor(r.check_type)}>
                      {r.check_type}
                    </Badge>
                    <Badge size="sm" variant="light" color={statusColor(r.status)}>
                      {r.status}
                    </Badge>
                    <Text size="xs" c="dimmed">
                      {fmtDateTime(r.created_at)}
                    </Text>
                  </Group>
                </Accordion.Control>
                <Accordion.Panel>
                  <ResponseDetail row={r} />
                </Accordion.Panel>
              </Accordion.Item>
            ))}
          </Accordion>
        </Stack>
      )}
    </Drawer>
  );
}
