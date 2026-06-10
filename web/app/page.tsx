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
import { RefreshControl } from "@/components/RefreshControl";
import { RowDetailDrawer } from "@/components/RowDetailDrawer";
import {
  checkColor,
  fmtDateTime,
  fmtDuration,
  fmtInt,
  statusColor,
} from "@/lib/format";
import type { FilterOptions, ListResult, QueryRow } from "@/lib/types";

const PAGE_SIZE = 50;

export default function BrowserPage() {
  const [options, setOptions] = useState<FilterOptions | null>(null);
  const [checkType, setCheckType] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [config, setConfig] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [debouncedSearch] = useDebouncedValue(search, 300);
  const [page, setPage] = useState(1);
  const [data, setData] = useState<ListResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const refresh = useCallback(() => setReloadKey((k) => k + 1), []);

  useEffect(() => {
    fetch("/api/options")
      .then((r) => r.json())
      .then(setOptions);
  }, []);

  useEffect(() => {
    setPage(1);
  }, [checkType, status, config, debouncedSearch]);

  useEffect(() => {
    const params = new URLSearchParams({
      page: String(page),
      pageSize: String(PAGE_SIZE),
    });
    if (checkType) params.set("check_type", checkType);
    if (status) params.set("status", status);
    if (config) params.set("config", config);
    if (debouncedSearch) params.set("q", debouncedSearch);
    setLoading(true);
    fetch(`/api/queries?${params.toString()}`)
      .then((r) => r.json())
      .then(setData)
      .finally(() => setLoading(false));
  }, [page, checkType, status, config, debouncedSearch, reloadKey]);

  const totalPages = useMemo(
    () => (data ? Math.max(1, Math.ceil(data.total / data.pageSize)) : 1),
    [data],
  );

  return (
    <Stack>
      <div>
        <Title order={3}>Responses</Title>
        <Text c="dimmed" size="sm">
          Every AI query, newest first. Click a row for full detail.
        </Text>
      </div>

      <Group justify="space-between" align="flex-end">
        <Group>
          <TextInput
            label="Search"
            placeholder="reason, company, url, content…"
            leftSection={<IconSearch size={16} />}
            value={search}
            onChange={(e) => setSearch(e.currentTarget.value)}
            w={280}
          />
          <Select
            label="Check"
            placeholder="All"
            clearable
            data={options?.check_types ?? []}
            value={checkType}
            onChange={setCheckType}
            w={150}
          />
          <Select
            label="Status"
            placeholder="All"
            clearable
            data={options?.statuses ?? []}
            value={status}
            onChange={setStatus}
            w={140}
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
            {data ? `${fmtInt(data.total)} results` : ""}
          </Text>
          <RefreshControl onRefresh={refresh} loading={loading} />
        </Group>
      </Group>

      <Paper withBorder pos="relative">
        <Table.ScrollContainer minWidth={900}>
          <Table highlightOnHover stickyHeader>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>When</Table.Th>
                <Table.Th>Check</Table.Th>
                <Table.Th>Status</Table.Th>
                <Table.Th>Company</Table.Th>
                <Table.Th>Title</Table.Th>
                <Table.Th>Reason</Table.Th>
                <Table.Th ta="right">Tokens</Table.Th>
                <Table.Th ta="right">Time</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {data?.rows.map((row: QueryRow) => (
                <Table.Tr
                  key={row.id}
                  onClick={() => setSelectedId(row.id)}
                  style={{ cursor: "pointer" }}
                >
                  <Table.Td style={{ whiteSpace: "nowrap" }}>
                    <Text size="xs">{fmtDateTime(row.created_at)}</Text>
                  </Table.Td>
                  <Table.Td>
                    <Badge size="sm" color={checkColor(row.check_type)}>
                      {row.check_type}
                    </Badge>
                  </Table.Td>
                  <Table.Td>
                    <Badge
                      size="sm"
                      variant="light"
                      color={statusColor(row.status)}
                    >
                      {row.status}
                    </Badge>
                  </Table.Td>
                  <Table.Td>{row.company || "—"}</Table.Td>
                  <Table.Td>
                    <Text size="sm" lineClamp={1} maw={220}>
                      {row.job_title || "—"}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm" lineClamp={1} maw={320} c="dimmed">
                      {row.reason || "—"}
                    </Text>
                  </Table.Td>
                  <Table.Td ta="right">{fmtInt(row.total_tokens)}</Table.Td>
                  <Table.Td ta="right" style={{ whiteSpace: "nowrap" }}>
                    {fmtDuration(row.duration_ms)}
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
            <Text c="dimmed">No matching queries</Text>
          </Center>
        )}
      </Paper>

      <Group justify="center">
        <Pagination value={page} onChange={setPage} total={totalPages} />
      </Group>

      <RowDetailDrawer id={selectedId} onClose={() => setSelectedId(null)} />
    </Stack>
  );
}
