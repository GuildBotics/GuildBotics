import { Alert, Text } from "@mantine/core";
import { CircleAlert } from "lucide-react";
import type { ReactNode } from "react";

// Form parts the setup screens and the providers' panels share.

export function RequiredLabel({ text }: { text: string }) {
  return (
    <>
      {text}
      <Text span c="danger" inherit aria-hidden="true">
        {" *"}
      </Text>
    </>
  );
}

export function InfoCallout({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Alert color="warning" icon={<CircleAlert size={18} />} title={title}>
      {children}
    </Alert>
  );
}
