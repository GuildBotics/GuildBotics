import { Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { type InferenceFailure, getInferenceFailures } from "../api/client";

// Each read scans this device's retained records, as the last CLI turns do.
const INFERENCE_FAILURES_REFRESH_MS = 15_000;

export function useInferenceFailures(enabled = true) {
  return useQuery({
    queryKey: ["inference-failures"],
    queryFn: getInferenceFailures,
    enabled,
    refetchInterval: INFERENCE_FAILURES_REFRESH_MS,
  });
}

// Why the provider refused the latest call with this key, until a call of it
// succeeds. A rate limit passes by itself, so it is a warning, not an error.
export function InferenceFailureNotice({ failure }: { failure: InferenceFailure | undefined }) {
  const { t, i18n } = useTranslation();
  if (!failure) return null;
  return (
    <Text size="xs" c={failure.category === "rate_limit" ? "warning" : "danger"}>
      {t("setup.intelligence.inferenceFailure.summary", {
        reason: t(`setup.intelligence.inferenceFailure.categories.${failure.category}`),
        time: new Date(failure.timestamp).toLocaleString(i18n.language),
      })}
    </Text>
  );
}
