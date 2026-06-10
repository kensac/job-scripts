"use client";

import { useEffect, useState } from "react";
import { ActionIcon, Button, Group, Menu, Tooltip } from "@mantine/core";
import { IconChevronDown, IconRefresh } from "@tabler/icons-react";

const OPTIONS = [
  { label: "Off", value: 0 },
  { label: "Every 5s", value: 5000 },
  { label: "Every 15s", value: 15000 },
  { label: "Every 30s", value: 30000 },
];

export function RefreshControl({
  onRefresh,
  loading,
}: {
  onRefresh: () => void;
  loading?: boolean;
}) {
  const [interval, setIntervalMs] = useState(0);

  useEffect(() => {
    if (!interval) return;
    const id = setInterval(onRefresh, interval);
    return () => clearInterval(id);
  }, [interval, onRefresh]);

  const current = OPTIONS.find((o) => o.value === interval) ?? OPTIONS[0];

  return (
    <Group gap="xs">
      <Menu position="bottom-end" withinPortal>
        <Menu.Target>
          <Button
            variant={interval ? "light" : "default"}
            size="sm"
            rightSection={<IconChevronDown size={14} />}
          >
            Auto: {current.label}
          </Button>
        </Menu.Target>
        <Menu.Dropdown>
          {OPTIONS.map((o) => (
            <Menu.Item key={o.value} onClick={() => setIntervalMs(o.value)}>
              {o.label}
            </Menu.Item>
          ))}
        </Menu.Dropdown>
      </Menu>
      <Tooltip label="Refresh now">
        <ActionIcon
          variant="default"
          size="lg"
          onClick={onRefresh}
          loading={loading}
          aria-label="Refresh"
        >
          <IconRefresh size={18} />
        </ActionIcon>
      </Tooltip>
    </Group>
  );
}
