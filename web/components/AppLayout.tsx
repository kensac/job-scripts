"use client";

import { AppShell, Group, NavLink, Text, ThemeIcon } from "@mantine/core";
import {
  IconChartBar,
  IconListDetails,
  IconBriefcase,
  IconStack2,
} from "@tabler/icons-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV = [
  { href: "/", label: "Responses", icon: IconListDetails },
  { href: "/jobs", label: "Jobs", icon: IconBriefcase },
  { href: "/dashboard", label: "Dashboard", icon: IconChartBar },
];

export function AppLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  return (
    <AppShell
      header={{ height: 56 }}
      navbar={{ width: 240, breakpoint: "sm" }}
      padding="lg"
    >
      <AppShell.Header>
        <Group h="100%" px="md" gap="sm">
          <ThemeIcon variant="light" radius="md" size="lg">
            <IconStack2 size={20} />
          </ThemeIcon>
          <div>
            <Text fw={700} size="sm" lh={1.1}>
              Job AI Audit
            </Text>
            <Text size="xs" c="dimmed" lh={1.1}>
              filtering analytics
            </Text>
          </div>
        </Group>
      </AppShell.Header>

      <AppShell.Navbar p="sm">
        <Text size="xs" c="dimmed" fw={600} tt="uppercase" px="sm" mb={6}>
          Explore
        </Text>
        {NAV.map(({ href, label, icon: Icon }) => (
          <NavLink
            key={href}
            component={Link}
            href={href}
            label={label}
            active={pathname === href}
            leftSection={<Icon size={18} />}
            variant="light"
            mb={2}
          />
        ))}
      </AppShell.Navbar>

      <AppShell.Main bg="gray.0">{children}</AppShell.Main>
    </AppShell>
  );
}
