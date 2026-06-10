"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Center,
  Group,
  Loader,
  Paper,
  SimpleGrid,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { BarChart, DonutChart, LineChart } from "@mantine/charts";
import { RefreshControl } from "@/components/RefreshControl";
import { fmtCost, fmtInt, statusColor } from "@/lib/format";
import type { Stats } from "@/lib/types";

function StatCard({ label, value }: { label: string; value: string }) {
  return (
    <Paper withBorder p="md" radius="md">
      <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
        {label}
      </Text>
      <Text size="xl" fw={700}>
        {value}
      </Text>
    </Paper>
  );
}

export default function DashboardPage() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [loading, setLoading] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const refresh = useCallback(() => setReloadKey((k) => k + 1), []);

  useEffect(() => {
    setLoading(true);
    fetch("/api/stats")
      .then((r) => r.json())
      .then(setStats)
      .finally(() => setLoading(false));
  }, [reloadKey]);

  if (!stats) {
    return (
      <Center p="xl">
        <Loader />
      </Center>
    );
  }

  const { totals } = stats;
  const totalTokens = totals.prompt_tokens + totals.completion_tokens;
  const cachedPct = totals.prompt_tokens
    ? ((totals.cached_tokens / totals.prompt_tokens) * 100).toFixed(1)
    : "0";

  const donutData = stats.byStatus.map((s) => ({
    name: s.status,
    value: s.count,
    color: `${statusColor(s.status)}.6`,
  }));

  return (
    <Stack gap="lg">
      <Group justify="space-between" align="flex-end">
        <div>
          <Title order={3}>Dashboard</Title>
          <Text c="dimmed" size="sm">
            Token usage, cost, and outcomes across all AI checks.
          </Text>
        </div>
        <RefreshControl onRefresh={refresh} loading={loading} />
      </Group>

      <SimpleGrid cols={{ base: 2, sm: 3, md: 5 }}>
        <StatCard label="Queries" value={fmtInt(totals.queries)} />
        <StatCard label="Total tokens" value={fmtInt(totalTokens)} />
        <StatCard label="Reasoning tokens" value={fmtInt(totals.reasoning_tokens)} />
        <StatCard label="Cached input" value={`${cachedPct}%`} />
        <StatCard label="Est. cost" value={fmtCost(totals.cost)} />
      </SimpleGrid>

      <SimpleGrid cols={{ base: 1, md: 2 }}>
        <Paper withBorder p="md" radius="md">
          <Title order={6} mb="md">
            Tokens by check type
          </Title>
          <BarChart
            h={280}
            data={stats.byCheckType}
            dataKey="check_type"
            series={[
              { name: "prompt_tokens", label: "Prompt", color: "blue.6" },
              { name: "completion_tokens", label: "Completion", color: "orange.6" },
            ]}
            tickLine="y"
          />
        </Paper>

        <Paper withBorder p="md" radius="md">
          <Title order={6} mb="md">
            Outcomes
          </Title>
          <Group justify="center">
            <DonutChart
              h={280}
              data={donutData}
              withLabelsLine
              withLabels
              chartLabel="status"
            />
          </Group>
        </Paper>
      </SimpleGrid>

      <Paper withBorder p="md" radius="md">
        <Title order={6} mb="md">
          Tokens over time
        </Title>
        <LineChart
          h={300}
          data={stats.byDay}
          dataKey="day"
          series={[
            { name: "prompt_tokens", label: "Prompt", color: "blue.6" },
            { name: "completion_tokens", label: "Completion", color: "orange.6" },
            { name: "cached_tokens", label: "Cached", color: "teal.6" },
            { name: "reasoning_tokens", label: "Reasoning", color: "grape.6" },
          ]}
          curveType="monotone"
          withDots={false}
        />
      </Paper>
    </Stack>
  );
}
