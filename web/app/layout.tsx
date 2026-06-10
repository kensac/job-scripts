import type { Metadata } from "next";
import "@mantine/core/styles.css";
import "@mantine/charts/styles.css";
import "./globals.css";
import {
  ColorSchemeScript,
  MantineProvider,
  mantineHtmlProps,
} from "@mantine/core";
import { AppLayout } from "@/components/AppLayout";

export const metadata: Metadata = {
  title: "Job AI Audit",
  description: "Browse and analyze the AI job-filtering audit log",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" {...mantineHtmlProps}>
      <head>
        <ColorSchemeScript forceColorScheme="light" />
      </head>
      <body>
        <MantineProvider forceColorScheme="light">
          <AppLayout>{children}</AppLayout>
        </MantineProvider>
      </body>
    </html>
  );
}
