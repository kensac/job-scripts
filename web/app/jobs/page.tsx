"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Badge,
  Center,
  Group,
  Loader,
  Pagination,
  Paper,
  Select,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { IconSearch } from "@tabler/icons-react";
import { JobDrawer } from "@/components/JobDrawer";
import { RefreshControl } from "@/components/RefreshControl";
import { fmtDateTime, fmtInt, statusColor } from "@/lib/format";
import type { FilterOptions, JobListResult, JobSummary } from "@/lib/types";

const PAGE_SIZE = 50;

export default function JobsPage() {
  const [options, setOptions] = useState<FilterOptions | null>(null);
  const [verdict, setVerdict] = useState<string | null>(null);
  const [config, setConfig] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [debouncedSearch] = useDebouncedValue(search, 300);
  const [page, setPage] = useState(1);
  const [data, setData] = useState<JobListResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [selectedUrl, setSelectedUrl] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const refresh = useCallback(() => setReloadKey((k) => k + 1), []);

  useEffect(() => {
    fetch("/api/options")
      .then((r) => r.json())
      .then(setOptions);
  }, []);

  useEffect(() => {
    setPage(1);
  }, [verdict, config, debouncedSearch]);

  useEffect(() => {
    const params = new URLSearchParams({
      page: String(page),
      pageSize: String(PAGE_SIZE),
    });
    if (verdict) params.set("verdict", verdict);
    if (config) params.set("config", config);
    if (debouncedSearch) params.set("q", debouncedSearch);
    setLoading(true);
    fetch(`/api/jobs?${params.toString()}`)
      .then((r) => r.json())
      .then(setData)
      .finally(() => setLoading(false));
  }, [page, verdict, config, debouncedSearch, reloadKey]);

  const totalPages = useMemo(
    () => (data ? Math.max(1, Math.ceil(data.total / data.pageSize)) : 1),
    [data],
  );

  return (
    <Stack>
      <div>
        <Title order={3}>Jobs</Title>
        <Text c="dimmed" size="sm">
          One row per posting — drill in to see every AI check.
        </Text>
      </div>

      <Group justify="space-between" align="flex-end">
        <Group>
          <TextInput
            label="Search"
            placeholder="url, company, title…"
            leftSection={<IconSearch size={16} />}
            value={search}
            onChange={(e) => setSearch(e.currentTarget.value)}
            w={280}
          />
          <Select
            label="Verdict"
            placeholder="All"
            clearable
            data={[
              { value: "passed", label: "Passed" },
              { value: "rejected", label: "Rejected" },
            ]}
            value={verdict}
            onChange={setVerdict}
            w={150}
          />
          <Select
            label="Config"
            placeholder="All"
            clearable
            data={options?.configs ?? []}
            value={config}
            onChange={setConfig}
            w={170}
          />
        </Group>
        <Group gap="md">
          <Text size="sm" c="dimmed">
            {data ? `${fmtInt(data.total)} jobs` : ""}
          </Text>
          <RefreshControl onRefresh={refresh} loading={loading} />
        </Group>
      </Group>

      <Paper withBorder radius="md" pos="relative">
        <Table.ScrollContainer minWidth={820}>
          <Table highlightOnHover verticalSpacing="sm">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Verdict</Table.Th>
                <Table.Th>Company</Table.Th>
                <Table.Th>Title</Table.Th>
                <Table.Th>Checks</Table.Th>
                <Table.Th ta="right">Tokens</Table.Th>
                <Table.Th>Last activity</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {data?.rows.map((job: JobSummary) => (
                <Table.Tr
                  key={job.url}
                  onClick={() => setSelectedUrl(job.url)}
                  style={{ cursor: "pointer" }}
                >
                  <Table.Td>
                    <Badge color={statusColor(job.verdict)} variant="light">
                      {job.verdict}
                    </Badge>
                  </Table.Td>
                  <Table.Td>{job.company || "—"}</Table.Td>
                  <Table.Td>
                    <Text size="sm" lineClamp={1} maw={280}>
                      {job.job_title || job.url}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Group gap={6}>
                      <Badge size="sm" variant="default">
                        {job.checks}
                      </Badge>
                      {job.rejected > 0 && (
                        <Badge size="sm" color="red" variant="dot">
                          {job.rejected}
                        </Badge>
                      )}
                      {job.failed > 0 && (
                        <Badge size="sm" color="orange" variant="dot">
                          {job.failed}
                        </Badge>
                      )}
                    </Group>
                  </Table.Td>
                  <Table.Td ta="right">{fmtInt(job.total_tokens)}</Table.Td>
                  <Table.Td style={{ whiteSpace: "nowrap" }}>
                    <Text size="xs">{fmtDateTime(job.last_seen)}</Text>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
        {loading && (
          <Center p="xl">
            <Loader size="sm" />
          </Center>
        )}
        {!loading && data && data.rows.length === 0 && (
          <Center p="xl">
            <Text c="dimmed">No matching jobs</Text>
          </Center>
        )}
      </Paper>

      <Group justify="center">
        <Pagination value={page} onChange={setPage} total={totalPages} />
      </Group>

      <JobDrawer url={selectedUrl} onClose={() => setSelectedUrl(null)} />
    </Stack>
  );
}
