"use client";

import {
  Anchor,
  Badge,
  Code,
  Group,
  ScrollArea,
  SimpleGrid,
  Stack,
  Tabs,
  Text,
  Timeline,
} from "@mantine/core";
import type { QueryRow } from "@/lib/types";
import {
  checkColor,
  fmtDateTime,
  fmtDuration,
  fmtInt,
  statusColor,
} from "@/lib/format";

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <Text size="xs" c="dimmed">
        {label}
      </Text>
      <Text size="sm">{value ?? "—"}</Text>
    </div>
  );
}

function Block({ title, text }: { title: string; text: string | null }) {
  return (
    <Stack gap={4}>
      <Text fw={600} size="sm">
        {title}
      </Text>
      {text ? (
        <Code
          block
          style={{ whiteSpace: "pre-wrap", maxHeight: 360, overflow: "auto" }}
        >
          {text}
        </Code>
      ) : (
        <Text size="sm" c="dimmed">
          (empty)
        </Text>
      )}
    </Stack>
  );
}

export function ResponseDetail({
  row,
  timeline,
}: {
  row: QueryRow;
  timeline?: QueryRow[];
}) {
  return (
    <Tabs defaultValue="overview">
      <Tabs.List mb="md">
        <Tabs.Tab value="overview">Overview</Tabs.Tab>
        <Tabs.Tab value="prompt">Prompt</Tabs.Tab>
        <Tabs.Tab value="input">Input</Tabs.Tab>
        <Tabs.Tab value="output">Output</Tabs.Tab>
        {timeline && (
          <Tabs.Tab value="timeline">Timeline ({timeline.length})</Tabs.Tab>
        )}
      </Tabs.List>

      <Tabs.Panel value="overview">
        <Stack>
          <SimpleGrid cols={2}>
            <Field label="Company" value={row.company} />
            <Field label="Job title" value={row.job_title} />
            <Field label="Model" value={row.model} />
            <Field label="Reasoning effort" value={row.reasoning_effort} />
            <Field label="Config" value={row.config_name} />
            <Field label="When" value={fmtDateTime(row.created_at)} />
            <Field label="Duration" value={fmtDuration(row.duration_ms)} />
            <Field label="Reason" value={row.reason} />
          </SimpleGrid>
          {row.url && (
            <div>
              <Text size="xs" c="dimmed">
                URL
              </Text>
              <Anchor href={row.url} target="_blank" size="sm">
                {row.url}
              </Anchor>
            </div>
          )}
          <SimpleGrid cols={3}>
            <Field label="Prompt tokens" value={fmtInt(row.prompt_tokens)} />
            <Field label="Cached" value={fmtInt(row.cached_tokens)} />
            <Field label="Completion" value={fmtInt(row.completion_tokens)} />
            <Field label="Reasoning" value={fmtInt(row.reasoning_tokens)} />
            <Field label="Total" value={fmtInt(row.total_tokens)} />
          </SimpleGrid>
          {row.error && <Block title="Error" text={row.error} />}
        </Stack>
      </Tabs.Panel>

      <Tabs.Panel value="prompt">
        <Block title="Instructions sent to model" text={row.instructions} />
      </Tabs.Panel>
      <Tabs.Panel value="input">
        <Block title="Input content sent to model" text={row.input_content} />
      </Tabs.Panel>
      <Tabs.Panel value="output">
        <Block title="Parsed structured output" text={row.parsed_json} />
      </Tabs.Panel>

      {timeline && (
        <Tabs.Panel value="timeline">
          <ScrollArea.Autosize mah={600}>
            <Timeline active={timeline.length - 1} bulletSize={18}>
              {timeline.map((t) => (
                <Timeline.Item
                  key={t.id}
                  title={
                    <Group gap="xs">
                      <Badge size="sm" color={checkColor(t.check_type)}>
                        {t.check_type}
                      </Badge>
                      <Badge size="sm" variant="light" color={statusColor(t.status)}>
                        {t.status}
                      </Badge>
                    </Group>
                  }
                >
                  <Text size="xs" c="dimmed">
                    {fmtDateTime(t.created_at)} · {fmtDuration(t.duration_ms)}
                  </Text>
                  {t.reason && <Text size="sm">{t.reason}</Text>}
                </Timeline.Item>
              ))}
            </Timeline>
          </ScrollArea.Autosize>
        </Tabs.Panel>
      )}
    </Tabs>
  );
}
