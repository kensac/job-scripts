import type { Operation } from "../runtime/operation";

export interface ProfileRow {
  school?: string;
  company?: string;
  title?: string;
  degree?: string;
  field?: string;
  gpa?: string;
  start?: string;
  end?: string;
  current?: boolean;
  location?: string;
  description?: string;
}

export type Profile = Record<string, string | number | boolean | ProfileRow[] | null>;

export interface FormField {
  key: string;
  label: string;
  kind: string;
  required: boolean;
  options: string[];
  fact?: string | null;
  _person?: boolean;
  _trace?: string[];
  _seen?: string[];
}

export interface Adapter {
  readonly host: string;
  ready(): boolean;
  read(): FormField[] | Promise<FormField[]>;
  fill(field: FormField, value: string | null, file: File | null): Promise<boolean>;
  current(field: FormField): string;
  submitButton(): Element | null | undefined;
  submitted(): boolean;
  applyButton?(): Element | null | undefined;
  nextButton?(): Element | null | undefined;
  errors?(): string[];
  proxySubmit?: boolean;
  useConfig?(value: unknown): boolean;
}

export interface AdapterContext {
  operation: Operation;
  profile: Profile;
  getPublicJson(url: string): Promise<{ ok: boolean; json?: unknown }>;
}
